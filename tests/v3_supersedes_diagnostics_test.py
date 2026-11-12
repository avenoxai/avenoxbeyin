"""#604: explicit human diagnostics, without pinning a target or changing retirement rules."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

from v3_package_helpers import install, isolated_env, run_python, snapshot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3 as runtime  # noqa: E402

SPEC = importlib.util.spec_from_file_location('supersedes_entry', ROOT / 'scripts/beyin_entry.py')
entry = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(entry)
QUERY = 'synthetic calibration amber'
NOTICE = 'referans su anda hicbir notu emekli etmiyor'


class HumanSupersedesTest(unittest.TestCase):
    def report(self, issues, count=None):
        count = len(issues) if count is None else count
        return {'status': 'succeeded', 'supersedes_issues': issues, 'supersedes_issue_count': count,
                'supersedes': {'dead': issues, 'dead_count': count, 'truncated': count > len(issues)}}

    def test_both_presenters_show_bounded_sanitized_issue_details_and_full_count(self):
        issues = [{'source': 'notes/plan\n\x1b[31m\u202e.md', 'value': 'plan\r\n\x07\u202d.md',
                   'reason': 'ambiguous supersedes reference'},
                  {'source': 'notes/missing.md', 'value': 'missing', 'reason': 'unresolved supersedes reference'},
                  {'source': 'notes/self.md', 'value': 'self', 'reason': 'supersedes reference points to the note itself'},
                  {'source': 'HIDDEN_FOURTH_SOURCE', 'value': 'FOURTH_VALUE', 'reason': 'unresolved supersedes reference'}]
        result = self.report(issues, count=25)
        before = json.dumps(result)
        for command in ('sync', 'doctor'):
            with self.subTest(command=command):
                text = entry.human_result(result, command)
                self.assertIn('25 ' + NOTICE, text)
                self.assertIn('notes/plan / ?[31m?.md -> plan / ??.md', text)
                self.assertIn('birden fazla notla eslesiyor', text)
                self.assertIn('hedef bulunamadi', text)
                self.assertIn('notun kendisine isaret ediyor', text)
                self.assertIn('+22 referans daha', text)
                self.assertIn('acik id veya tam vault yolu', text)
                self.assertIn('--json', text)
                self.assertNotIn('HIDDEN_FOURTH_SOURCE', text)
                self.assertNotIn('FOURTH_VALUE', text)
                for control in ('\x1b', '\x07', '\r', '\u202d', '\u202e'):
                    self.assertNotIn(control, text)
        self.assertEqual(json.dumps(result), before)

    def test_large_fields_and_future_reason_are_bounded_without_inventing_a_target(self):
        result = self.report([{'source': 's' * 10000, 'value': 'v' * 10000, 'reason': 'r' * 10000}])
        for command in ('sync', 'doctor'):
            text = entry.human_result(result, command)
            self.assertIn(NOTICE, text)
            self.assertIn('...', text)
            self.assertLess(len(text), 1600)
            self.assertNotIn('s' * 161, text)
            self.assertNotIn('v' * 161, text)
            self.assertNotIn('r' * 161, text)

    def test_cycle_reason_is_presentable_without_changing_the_resolver(self):
        result = self.report([{'source': 'notes/a.md', 'value': 'b',
                              'reason': 'supersedes reference is part of a cycle'}])
        for command in ('sync', 'doctor'):
            self.assertIn('dongunun parcasi', entry.human_result(result, command))

    def test_healthy_reports_and_other_commands_stay_quiet(self):
        for result in ({'status': 'succeeded'}, self.report([])):
            for command in ('sync', 'doctor'):
                self.assertNotIn('Supersedes uyarisi', entry.human_result(result, command))
        result = self.report([{'source': 'notes/new.md', 'value': 'old',
                              'reason': 'unresolved supersedes reference'}])
        result.update(records=[], abstained=True)
        self.assertNotIn('Supersedes uyarisi', entry.human_result(result, 'context'))


class StatelessResolutionLimitsTest(unittest.TestCase):
    """The limits MARKDOWN.md documents for a bare file-name reference: each sync recomputes the
    match from the current index, so a later same-named note changes it; an id or full path does not."""
    OLD = {'id': 'n-old', 'source': 'notes/plan.md', 'text': 'old'}
    OTHER = {'id': 'n-other', 'source': 'archive/plan.md', 'text': 'unrelated'}
    ROOT_NOTE = {'id': 'n-root', 'source': 'plan.md', 'text': 'unrelated root'}

    def replacement(self, reference):
        return {'id': 'n-new', 'source': 'notes/plan-v2.md', 'text': 'new', 'supersedes': [reference]}

    def test_bare_name_follows_the_current_index_and_explicit_targets_do_not(self):
        bare = self.replacement('plan.md')
        self.assertEqual(runtime.resolve_supersedes([self.OLD, bare]), ({'n-old'}, []))
        # Another folder: ambiguous, reported by sync/doctor, and nothing is retired.
        retired, dead = runtime.resolve_supersedes([self.OLD, bare, self.OTHER])
        self.assertEqual(retired, set())
        self.assertEqual([issue['reason'] for issue in dead], ['ambiguous supersedes reference'])
        # Vault root: the bare value is the new note's exact path, so it is retired instead and
        # there is nothing to report. The documentation names this limit.
        self.assertEqual(runtime.resolve_supersedes([self.OLD, bare, self.ROOT_NOTE]), ({'n-root'}, []))
        for reference in ('n-old', 'notes/plan.md', 'notes/plan', '[[notes/plan]]'):
            with self.subTest(reference=reference):
                records = [self.OLD, self.replacement(reference), self.OTHER, self.ROOT_NOTE]
                self.assertEqual(runtime.resolve_supersedes(records), ({'n-old'}, []))


class InstalledSupersedesDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v3-supersedes-diagnostic-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.vault, self.state = root / 'vault', root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(root / 'home')
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def note(self, source, identity, **meta):
        path = self.vault / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\n' + json.dumps(dict(id=identity, **meta)) + '\n---\n' +
                        QUERY + ': ' + identity + '\n', encoding='utf-8')

    def run_cli(self, *args):
        result = run_python(self.vault / 'beyin.py', args, self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        return result.stdout.decode('utf-8')

    def cli(self, *args):
        return json.loads(self.run_cli(*args, '--json'))

    def active(self):
        return {r['id'] for r in self.cli('context', QUERY, '--no-sync')['records']}

    def seed(self, reference='plan.md'):
        self.note('notes/plan.md', 'n-old')
        self.note('notes/plan-v2.md', 'n-new', supersedes=[reference])
        self.assertEqual(self.cli('sync')['status'], 'succeeded')
        self.assertEqual(self.active(), {'n-new'})

    def test_added_basename_reports_actual_current_ambiguity_in_sync_and_read_only_doctor(self):
        self.seed()
        self.note('other/plan.md', 'n-other')
        source_before = snapshot(self.vault)
        result = self.cli('sync')
        issue = {'id': 'n-new', 'source': 'notes/plan-v2.md', 'value': 'plan.md',
                 'reason': 'ambiguous supersedes reference'}
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['conflicts'], [])
        self.assertEqual(result['supersedes_issue_count'], 1)
        self.assertEqual(result['supersedes_issues'], [issue])
        self.assertEqual(self.active(), {'n-old', 'n-new', 'n-other'})
        self.assertEqual(snapshot(self.vault), source_before)
        state_before = snapshot(self.state)
        doctor = self.cli('doctor')
        self.assertEqual(doctor['status'], 'never_seen')
        self.assertEqual(doctor['supersedes'], {'dead_count': 1, 'dead': [issue], 'truncated': False})
        text = self.run_cli('doctor', '--human')
        self.assertEqual(snapshot(self.state), state_before, 'doctor stays read-only')
        sync_text = self.run_cli('sync', '--human')
        self.assertEqual(snapshot(self.vault), source_before)
        for text in (text, sync_text):
            self.assertIn('1 ' + NOTICE, text)
            self.assertIn('notes/plan-v2.md -> plan.md', text)
            self.assertIn('birden fazla notla eslesiyor', text)
            self.assertIn('yeniden baglama girebilir', text)
            self.assertIn('acik id veya tam vault yolu', text)
        # Explicit reads and per-turn hooks do not acquire a repeating warning.
        self.assertNotIn('Supersedes uyarisi', self.run_cli('context', QUERY, '--human'))
        payload = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'supersedes-diagnostic',
                   'prompt': QUERY, 'cwd': str(self.vault)}
        hook = run_python(self.vault / '.claude/scripts/beyin_v3_hook.py',
                          ['--vault', self.vault, '--state', self.state, '--harness', 'claude'],
                          self.vault, self.env, payload)
        self.assertEqual(hook.returncode, 0, hook.stderr)
        self.assertNotIn('Supersedes uyarisi', hook.stdout.decode())
        self.assertNotIn('needs attention', hook.stdout.decode())

    def test_explicit_id_and_path_restore_retirement_without_removing_the_duplicate(self):
        self.seed()
        self.note('other/plan.md', 'n-other')
        self.cli('sync')
        self.assertEqual(self.active(), {'n-old', 'n-new', 'n-other'})
        for reference in ('notes/plan.md', 'n-old'):
            with self.subTest(reference=reference):
                self.note('notes/plan-v2.md', 'n-new', supersedes=[reference])
                sources = snapshot(self.vault)
                sync = self.cli('sync')
                self.assertEqual(sync['status'], 'succeeded')
                self.assertNotIn('supersedes_issues', sync)
                self.assertEqual(self.active(), {'n-new', 'n-other'})
                self.assertEqual(self.cli('doctor')['supersedes']['dead_count'], 0)
                for command in ('sync', 'doctor'):
                    self.assertNotIn('Supersedes uyarisi', self.run_cli(command, '--human'))
                self.assertEqual(snapshot(self.vault), sources)

    def test_unresolved_and_self_references_remain_informational(self):
        self.note('notes/self.md', 'n-self', supersedes=['[[self]]'])
        self.note('notes/missing.md', 'n-missing', supersedes=['missing-target'])
        result = self.cli('sync')
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['supersedes_issue_count'], 2)
        self.assertEqual(self.active(), {'n-self', 'n-missing'})
        for command in ('sync', 'doctor'):
            text = self.run_cli(command, '--human')
            self.assertIn('2 ' + NOTICE, text)
            self.assertIn('hedef bulunamadi', text)
            self.assertIn('notun kendisine isaret ediyor', text)


if __name__ == '__main__':
    unittest.main()
