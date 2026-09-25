"""Context must refresh source state without lifecycle hooks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ContextRefreshTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name)/'vault'
        (self.vault/'notes').mkdir(parents=True)
        self.state = Path(self.temp.name)/'state'
        self.source = self.vault/'notes/decision.md'
        self.write('Initial synthetic calibration value.')
        result = self.run_cli('sync')
        self.assertEqual(result.returncode, 0, result.stderr)

    def write(self, body):
        self.source.write_text('---\n{"id":"calibration","kind":"fact","project":"demo","visibility":"internal"}\n---\n'+body+'\n',encoding='utf-8')

    def run_cli(self, *args):
        return subprocess.run([sys.executable,str(ROOT/'scripts/beyin_v3.py'),'--vault',str(self.vault),'--state',str(self.state),*args],capture_output=True,text=True,encoding='utf-8')

    def test_context_refreshes_external_edit_without_hook(self):
        self.write('Current synthetic calibration value CHANGED.')
        result = self.run_cli('context','synthetic calibration','--project','demo')
        self.assertEqual(result.returncode,0,result.stderr)
        output=json.loads(result.stdout)
        self.assertIn('CHANGED',output['records'][0]['text'])
        self.assertNotIn('Initial',result.stdout)

    def test_no_sync_source_cli_does_not_create_bytecode(self):
        from v3_package_helpers import snapshot
        checkout = Path(self.temp.name) / 'read-only checkout'
        for name in ('scripts/beyin_v3.py', 'template/.claude/scripts/beyin_v3.py'):
            target = checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        before = snapshot(checkout), snapshot(self.vault), snapshot(self.state)
        env = dict(os.environ)
        env.pop('PYTHONDONTWRITEBYTECODE', None)
        env.pop('PYTHONPYCACHEPREFIX', None)
        result = subprocess.run(
            [sys.executable, str(checkout / 'scripts/beyin_v3.py'),
             '--vault', str(self.vault), '--state', str(self.state),
             'context', 'synthetic calibration', '--no-sync'],
            env=env, capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r['id'] for r in json.loads(result.stdout)['records']], ['calibration'])
        self.assertEqual((snapshot(checkout), snapshot(self.vault), snapshot(self.state)), before)

    def test_no_sync_missing_runtime_and_jev_rejection_do_not_write(self):
        from v3_package_helpers import snapshot
        missing = Path(self.temp.name) / 'missing runtime'
        for extra in ([], ['--jev']):
            with self.subTest(extra=extra):
                before = snapshot(self.vault)
                result = subprocess.run(
                    [sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                     '--vault', str(self.vault), '--state', str(missing),
                     'context', 'synthetic calibration', '--no-sync', *extra],
                    capture_output=True, text=True, encoding='utf-8')
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')
                self.assertIn('cannot be combined' if extra else 'not initialized', result.stderr)
                self.assertFalse(missing.exists())
                self.assertEqual(snapshot(self.vault), before)

    def test_context_serves_fresh_healthy_subset_with_explicit_degraded_warning(self):
        healthy = self.vault/'notes/healthy.md'
        healthy.write_text('Current healthy nebula calibration note.\n', encoding='utf-8')
        self.source.write_text('---\nunsupported:\n  nested: metadata\n---\nChanged source',encoding='utf-8')
        result=self.run_cli('context','nebula calibration')
        self.assertEqual(result.returncode,0,result.stderr)
        output=json.loads(result.stdout)
        self.assertTrue(output['partial'])
        self.assertEqual(output['source_sync']['status'],'degraded')
        self.assertEqual(output['source_sync']['warning_count'],1)
        self.assertEqual(
            Path(output['source_sync']['warnings'][0]['source']).parts,
            ('notes', 'decision.md'),
        )
        self.assertIn('healthy.md',{record['source'].split('/')[-1] for record in output['records']})
        self.assertNotIn('Initial',result.stdout)

    def test_context_refuses_conflict_without_stale_result(self):
        (self.vault/'notes/duplicate.md').write_bytes(self.source.read_bytes())
        result=self.run_cli('context','synthetic calibration')
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(result.stdout,'')
        self.assertIn('conflict',result.stderr.lower())

    def test_rejected_inference_frontmatter_is_history_only(self):
        from v3_package_helpers import snapshot
        source = self.vault / 'notes/inference.md'
        source.write_text(
            '---\n{"id":"amber-preference","kind":"inference","validity":"current"}\n'
            '---\nSynthetic user prefers amber diagrams.\n', encoding='utf-8')
        self.assertEqual(self.run_cli('sync').returncode, 0)
        current = self.run_cli('context', 'amber diagrams', '--no-sync')
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertIn('amber-preference', [row['id'] for row in json.loads(current.stdout)['records']])
        source.write_text(
            '---\n{"id":"amber-preference","kind":"inference","validity":"rejected",'
            '"rejected_reason":"User corrected this inference.","rejected_at":"2026-09-24"}\n'
            '---\nSynthetic user prefers amber diagrams.\n', encoding='utf-8')
        self.assertEqual(self.run_cli('sync').returncode, 0)
        before = snapshot(self.vault), snapshot(self.state)
        for extra in ([], ['--status', 'rejected']):
            result = self.run_cli('context', 'amber diagrams', '--no-sync', *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('amber-preference', [row['id'] for row in json.loads(result.stdout)['records']])
        self.assertEqual((snapshot(self.vault), snapshot(self.state)), before)
        history = self.run_cli('history', 'amber-preference')
        self.assertEqual(history.returncode, 0, history.stderr)
        events = json.loads(history.stdout)
        self.assertEqual([event['record']['validity'] for event in events], ['current', 'rejected'])
        self.assertEqual(events[-1]['record']['rejected_reason'],
                         'User corrected this inference.')

    def test_rejected_at_timestamp_in_yaml_frontmatter_keeps_sync_healthy(self):
        # Agents copy the updated_at shape; a timestamp must not degrade sync for the whole vault.
        (self.vault / 'notes/inference.md').write_text(
            '---\nid: amber-preference\nkind: inference\nvalidity: rejected\n'
            'rejected_reason: User corrected this inference.\nrejected_at: 2026-09-24T10:00:00Z\n'
            '---\nSynthetic user prefers amber diagrams.\n', encoding='utf-8')
        synced = self.run_cli('sync')
        self.assertEqual(synced.returncode, 0, synced.stderr)
        self.assertEqual(json.loads(synced.stdout)['status'], 'succeeded')
        context = self.run_cli('context', 'amber diagrams', '--no-sync')
        self.assertNotIn('amber-preference', [row['id'] for row in json.loads(context.stdout)['records']])
        history = self.run_cli('history', 'amber-preference')
        self.assertEqual(history.returncode, 0, history.stderr)
        self.assertEqual(json.loads(history.stdout)[-1]['record']['rejected_at'], '2026-09-24T10:00:00Z')

    def test_doctor_reports_rejection_on_a_note_without_inference_kind(self):
        rejection = {'validity': 'rejected', 'rejected_reason': 'User corrected this.', 'rejected_at': '2026-09-24'}
        for name, kind in (('plain', None), ('fact', 'fact'), ('inference', 'inference')):
            metadata = dict(rejection, id=name, **({'kind': kind} if kind else {}))
            (self.vault / 'notes' / (name + '.md')).write_text(
                '---\n' + json.dumps(metadata) + '\n---\nSynthetic user prefers ' + name + ' diagrams.\n', encoding='utf-8')
        synced = self.run_cli('sync')
        self.assertEqual(json.loads(synced.stdout)['status'], 'succeeded')
        doctor = self.run_cli('doctor')
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        report = json.loads(doctor.stdout)
        self.assertEqual(report['status'], 'needs_attention')
        self.assertEqual(report['validity']['ignored_rejection_count'], 2)
        self.assertEqual([(row['id'], row['kind']) for row in report['validity']['ignored_rejections']],
                         [('fact', 'fact'), ('plain', 'note')])

    def test_history_syncs_edits_and_keeps_deleted_audit_trail(self):
        def write(name, **metadata):
            (self.vault / 'notes' / (name + '.md')).write_text(
                '---\n' + json.dumps(dict({'id': name, 'kind': 'inference'}, **metadata)) +
                '\n---\nSynthetic user prefers ' + name + ' diagrams.\n', encoding='utf-8')
        write('amber', validity='current')
        write('teal', validity='current')
        self.assertEqual(self.run_cli('sync').returncode, 0)
        # SKILL.md flow: mark the inference rejected, then run history with no explicit sync.
        write('amber', validity='rejected', rejected_reason='User corrected this inference.', rejected_at='2026-09-24')
        edited = self.run_cli('history', 'amber')
        self.assertEqual(edited.returncode, 0, edited.stderr)
        events = json.loads(edited.stdout)
        self.assertEqual([e['event_type'] for e in events], ['ingest', 'update'])
        self.assertEqual([e['record']['validity'] for e in events], ['current', 'rejected'])
        (self.vault / 'notes/amber.md').unlink()
        deleted = self.run_cli('history', 'amber')
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual([e['event_type'] for e in json.loads(deleted.stdout)], ['ingest', 'update', 'delete'])
        # A rejection without its reason fails validation: the record leaves context,
        # and history shows the drop instead of an empty list.
        write('teal', validity='rejected', rejected_at='2026-09-24')
        dropped = self.run_cli('history', 'teal')
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        events = json.loads(dropped.stdout)
        self.assertEqual([e['event_type'] for e in events], ['ingest', 'delete'])
        self.assertEqual(events[-1]['record']['validity'], 'current')
        context = self.run_cli('context', 'teal diagrams', '--no-sync')
        self.assertNotIn('teal', [row['id'] for row in json.loads(context.stdout)['records']])
        # A deleted private record stays hidden from the default internal audience.
        private = self.vault / 'notes/private.md'
        private.write_text('---\n{"id":"hidden","kind":"fact","visibility":"private"}\n---\nSynthetic private canary.\n', encoding='utf-8')
        self.assertEqual(self.run_cli('sync').returncode, 0)
        private.unlink()
        hidden = self.run_cli('history', 'hidden')
        self.assertEqual(hidden.returncode, 0, hidden.stderr)
        self.assertEqual(json.loads(hidden.stdout), [])

    def test_history_refuses_conflict_without_stale_result(self):
        (self.vault / 'notes/duplicate.md').write_bytes(self.source.read_bytes())
        result = self.run_cli('history', 'calibration')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('conflict', result.stderr.lower())

if __name__=='__main__':unittest.main()
