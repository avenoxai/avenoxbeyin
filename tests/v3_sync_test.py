#!/usr/bin/env python3
"""Source-authoritative synchronization contract, synthetic local fixtures only."""
import importlib.util
import json
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'template/.claude/scripts/beyin_v3_sync.py'


def load_module():
    if not MODULE.is_file():
        raise AssertionError('SyncEngine not implemented')
    spec = importlib.util.spec_from_file_location('beyin_v3_sync_test_subject', MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    scripts = str(MODULE.parent)
    sys.path.insert(0, scripts)
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = old
        sys.path.remove(scripts)
    return module


class SourceSyncTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-sync-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Synthetic Beyin Çalışma'
        self.vault.mkdir()
        self.state = self.root / 'runtime'
        self.engine = self.module.SyncEngine(self.vault, self.state)
        self.addCleanup(lambda: getattr(self.engine.store, 'close', lambda: None)())

    def write(self, name='notes/task.md', body='Nebula calibration awaits owner Synthetic Reviewer.\n', **changes):
        metadata = {'id': 'nebula-task', 'kind': 'task', 'project': 'nebula', 'revision': 1,
                    'status': 'active', 'visibility': 'internal', 'facts': {'owner': 'Synthetic Reviewer'}}
        metadata.update(changes)
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\n' + json.dumps(metadata, ensure_ascii=False) + '\n---\n' + body, encoding='utf-8')
        return path

    def records(self, query='Nebula calibration', **kwargs):
        return self.engine.store.retrieve(query, project='nebula', **kwargs)['records']

    def test_add_edit_delete_follow_authoritative_source(self):
        path = self.write()
        result = self.engine.sync()
        self.assertEqual(result['status'], 'succeeded')
        self.assertGreaterEqual(result['indexed'], 1)
        self.assertEqual(self.records()[0]['facts']['owner'], 'Synthetic Reviewer')
        self.write(body='Nebula calibration approved by New Reviewer.\n', revision=2, facts={'owner': 'New Reviewer'})
        self.engine.sync()
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['revision'], 2)
        self.assertEqual(records[0]['facts']['owner'], 'New Reviewer')
        self.assertNotIn('Synthetic Reviewer', records[0]['text'])
        path.unlink()
        deleted = self.engine.sync()
        self.assertGreaterEqual(deleted['deleted'], 1)
        self.assertEqual(self.records(), [])

    def test_unresolved_git_conflict_markers_are_reported_and_not_indexed(self):
        # #205: a pull --rebase that stops on a conflict leaves markers the agent would read as content.
        body = ('<<<<<<< HEAD\nNebula calibration awaits owner Synthetic Reviewer.\n=======\n'
                'Nebula calibration moved to owner Synthetic Auditor.\n>>>>>>> 3f9a1c2 (other machine)\n')
        path = self.write(body=body)
        report = self.engine.sync()
        self.assertEqual(report['status'], 'degraded')
        self.assertEqual([w['source'] for w in report['warnings']], ['notes/task.md'])
        self.assertIn('conflict markers', report['warnings'][0]['reason'])
        self.assertEqual(self.records(), [])
        self.write()
        report = self.engine.sync()
        self.assertEqual(report['warnings'], [])
        self.assertEqual([r['id'] for r in self.records()], ['nebula-task'])

    def test_setext_heading_is_not_a_conflict_marker(self):
        body = 'Nebula calibration awaits owner Synthetic Reviewer.\n\nSetext heading\n=======\n\nMore text.\n'
        self.write(body=body)
        report = self.engine.sync()
        self.assertEqual(report['warnings'], [])
        self.assertEqual([r['id'] for r in self.records()], ['nebula-task'])

    def test_conflict_inside_code_fence_and_unlabeled_markers_are_reported(self):
        for body in ('Nebula calibration awaits owner Synthetic Reviewer.\n```bash\n<<<<<<< HEAD\necho a\n'
                     '=======\necho b\n>>>>>>> 3f9a1c2\n```\n',
                     '<<<<<<<\nNebula calibration awaits owner Synthetic Reviewer.\n=======\nother\n>>>>>>>\n'):
            with self.subTest(body=body):
                self.write(body=body)
                report = self.engine.sync()
                self.assertEqual([w['source'] for w in report['warnings']], ['notes/task.md'])
                self.assertEqual(self.records(), [])

    def test_crlf_and_diff3_conflicts_are_reported(self):
        # core.autocrlf=true checks markers out with CRLF; merge.conflictStyle=diff3 adds a ||||||| base section.
        for eol, body in (('\r\n', '<<<<<<< HEAD\nNebula calibration awaits owner Synthetic Reviewer.\n=======\nother\n'
                                   '>>>>>>> 3f9a1c2\n'),
                          ('\n', '<<<<<<< HEAD\nNebula calibration awaits owner Synthetic Reviewer.\n'
                                 '||||||| merged common ancestors\nbase\n=======\nother\n>>>>>>> 3f9a1c2\n')):
            with self.subTest(body=body):
                path = self.write(body=body)
                # Bytes, not write_text: the platform newline would hide which line ending is under test.
                path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n').replace(b'\n', eol.encode()))
                report = self.engine.sync()
                self.assertEqual([w['source'] for w in report['warnings']], ['notes/task.md'])
                self.assertEqual(self.records(), [])

    def test_receipt_with_conflict_markers_is_not_indexed(self):
        # A device that first sees a receipt after an unresolved add/add merge must not index the markers.
        self.write()
        receipt = self.engine.receipt('shared-topic', 'Calibration done here.', ['notes/task.md'], 'codex')
        raw = (self.vault / receipt['source']).read_text(encoding='utf-8')
        fresh = self.root / 'fresh'
        (fresh / 'receipts').mkdir(parents=True)
        (fresh / 'notes').mkdir()
        (fresh / 'notes/task.md').write_text((self.vault / 'notes/task.md').read_text(encoding='utf-8'), encoding='utf-8')
        (fresh / receipt['source']).write_text(raw.replace(
            'Calibration done here.\n',
            '<<<<<<< HEAD\nCalibration done here.\n=======\nCalibration done there.\n>>>>>>> 3f9a1c2\n'), encoding='utf-8')
        engine = self.module.SyncEngine(fresh, self.root / 'fresh-state')
        report = engine.sync()
        self.assertEqual(report['status'], 'degraded')
        self.assertIn(receipt['source'], [w['source'] for w in report['warnings']])
        outcomes = fresh / 'knowledge/v3/outcomes.md'
        self.assertFalse(outcomes.exists() and '<<<<<<<' in outcomes.read_text(encoding='utf-8'))

    def test_history_keeps_deleted_record_audit_trail_behind_last_snapshot(self):
        path = self.write()
        self.engine.sync()
        self.write(body='Nebula calibration approved by New Reviewer.\n', revision=2)
        self.engine.sync()
        path.unlink()
        self.engine.sync()
        history = self.engine.store.history('nebula-task')
        self.assertEqual([e['event_type'] for e in history], ['ingest', 'update', 'delete'])
        self.assertEqual(history[-1]['record'], history[-2]['record'])
        self.write()
        self.engine.sync()
        self.assertEqual([e['event_type'] for e in self.engine.store.history('nebula-task')],
                         ['ingest', 'update', 'delete', 'ingest'])
        private = self.write('notes/private.md', id='hidden', visibility='private', body='SYNTHETIC_PRIVATE_CANARY\n')
        untrusted = self.write('notes/untrusted.md', id='untrusted', kind='untrusted')
        self.engine.sync()
        private.unlink()
        untrusted.unlink()
        self.engine.sync()
        self.assertEqual(self.engine.store.history('hidden'), [])
        self.assertEqual(self.engine.store.history('hidden', audience='public'), [])
        self.assertEqual([e['event_type'] for e in self.engine.store.history('hidden', audience='private')],
                         ['ingest', 'delete'])
        self.assertEqual(self.engine.store.history('untrusted', audience='private'), [])

    def test_duplicate_source_id_quarantined_without_winner(self):
        self.write()
        self.engine.sync()
        self.write('notes/conflicting.md', body='Nebula calibration has different owner.\n')
        report = self.engine.sync()
        self.assertTrue(report['conflicts'])
        self.assertEqual(self.records(), [])
        self.assertTrue((self.vault / 'notes/task.md').exists())
        self.assertTrue((self.vault / 'notes/conflicting.md').exists())

    def test_source_rename_updates_citation_without_duplicate(self):
        path = self.write()
        self.engine.sync()
        destination = path.with_name('renamed.md')
        path.rename(destination)
        self.engine.sync()
        records = self.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['id'], 'nebula-task')
        self.assertEqual(records[0]['source'], 'notes/renamed.md')

    def test_plain_markdown_full_thread_body_preserved(self):
        path = self.vault / 'Threads.md'
        body = '# Threads\n## Active\n### Thread: Spectroscope\n**Status:** waiting\nSpectroscope decision belongs to Synthetic Reviewer.\nNext action: inspect violet calibration sample.\n'
        path.write_text(body, encoding='utf-8')
        self.engine.sync()
        response = self.engine.store.retrieve('Spectroscope decision reviewer')
        self.assertEqual(len(response['records']), 1)
        self.assertIn('Next action: inspect violet calibration sample.', response['records'][0]['text'])
        self.assertIn('decision belongs to Synthetic Reviewer.', response['records'][0]['text'])
        self.assertEqual(response['records'][0]['source'], 'Threads.md')

    def test_companion_source_snapshot_pins_all_sources_and_keeps_latest_journal_tail(self):
        companion = self.vault / '🔮 850-Companion'
        companion.mkdir()
        values = {
            'Last-Session.md': 'Previous verified outcome.\n' + ('L' * 900),
            'Threads.md': 'Active owner Synthetic Reviewer.\n' + ('T' * 900),
            'Kurallar.md': ('K' * 900) + '\nLATEST_RULE_SENTINEL',
            'Journal.md': ('J' * 900) + '\nLATEST_JOURNAL_SENTINEL',
        }
        for name, body in values.items():
            (companion / name).write_text(body, encoding='utf-8')
        self.engine.sync()
        snapshot = self.engine.store.source_snapshot(list(values), budget_chars=2200)
        self.assertEqual([Path(record['source']).name for record in snapshot['records']], list(values))
        self.assertEqual(snapshot['missing_sources'], [])
        rendered = json.dumps(snapshot, ensure_ascii=False)
        self.assertIn('LATEST_RULE_SENTINEL', rendered)
        self.assertIn('LATEST_JOURNAL_SENTINEL', rendered)
        self.assertLessEqual(len(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(',', ':'))), 2200)

    def test_public_boundary_excludes_private_and_untrusted_source(self):
        self.write('notes/public.md', id='visible', visibility='public')
        self.write('notes/private.md', id='hidden', visibility='private', body='Nebula calibration SYNTHETIC_PRIVATE_CANARY\n')
        self.write('notes/untrusted.md', id='untrusted', kind='untrusted')
        self.engine.sync()
        response = self.engine.store.retrieve('Nebula calibration', project='nebula', audience='public')
        self.assertEqual([r['id'] for r in response['records']], ['visible'])
        self.assertNotIn('SYNTHETIC_PRIVATE_CANARY', json.dumps(response))

    def test_receipt_markdown_idempotent_and_collision_rejected(self):
        self.write()
        self.engine.sync()
        first = self.engine.receipt('synthetic-event', 'Nebula reviewed.', ['notes/task.md'], 'codex')
        second = self.engine.receipt('synthetic-event', 'Nebula reviewed.', ['notes/task.md'], 'claude')
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'succeeded')
        source = self.vault / first['source']
        self.assertTrue(source.is_file())
        self.assertIn('Nebula reviewed.', source.read_text(encoding='utf-8'))
        original = source.read_bytes()
        with self.assertRaises(ValueError):
            self.engine.receipt('synthetic-event', 'Different summary.', ['notes/task.md'], 'codex')
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(len(list((self.vault / 'receipts').glob('*.md'))), 1)
        self.engine.sync()
        self.assertFalse(any(r.get('kind') == 'receipt' for r in self.records('Nebula reviewed')))

    def test_receipt_manually_mutated_source_never_overwritten(self):
        self.write()
        self.engine.sync()
        receipt = self.engine.receipt('edited-event', 'Nebula reviewed.', ['notes/task.md'], 'codex')
        source = self.vault / receipt['source']
        source.write_text('User synthetic edit must survive.\n', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.engine.receipt('edited-event', 'Nebula reviewed.', ['notes/task.md'], 'codex')
        self.assertEqual(source.read_text(encoding='utf-8'), 'User synthetic edit must survive.\n')

    def test_task_update_preserves_body_and_rejects_stale_revision(self):
        body = 'Nebula calibration.\n\n- Keep this exact task prose.\n  Unicode: ölçüm 🔭\n'
        path = self.write(body=body)
        self.engine.sync()
        updated = self.engine.update_task('nebula-task', 1, {'status': 'waiting'})
        self.assertEqual(updated['revision'], 2)
        self.assertEqual(updated['status'], 'waiting')
        self.assertEqual(path.read_text(encoding='utf-8').split('\n---\n', 1)[1], body)
        saved = path.read_bytes()
        with self.assertRaises(ValueError):
            self.engine.update_task('nebula-task', 1, {'status': 'done'})
        self.assertEqual(path.read_bytes(), saved)
        self.engine.sync()
        self.assertEqual(self.records()[0]['status'], 'waiting')

    def test_manual_edit_without_revision_bump_invalidates_old_expected_revision(self):
        self.write()
        self.engine.sync()
        self.assertEqual(self.records()[0]['revision'], 1)
        # Human editor changes authoritative content but leaves frontmatter revision 1.
        self.write(body='Nebula calibration manually changed by source author.\n', status='waiting')
        self.engine.sync()
        effective = self.records()[0]['revision']
        self.assertEqual(effective, 2)
        self.engine.sync()
        self.assertEqual(self.records()[0]['revision'], effective)
        with self.assertRaises(ValueError):
            self.engine.update_task('nebula-task', 1, {'status': 'done'})
        updated = self.engine.update_task('nebula-task', effective, {'status': 'done'})
        self.assertEqual(updated['revision'], 3)
        self.engine.sync()
        self.assertEqual(self.records()[0]['revision'], 3)

    def test_unsupported_metadata_reports_degraded_not_succeeded(self):
        source = self.vault / 'unsupported.md'
        source.write_text('---\nid: unsupported\nfacts:\n  owner:\n    name: Synthetic Reviewer\n---\nNebula calibration metadata fixture.\n', encoding='utf-8')
        report = self.engine.sync()
        self.assertTrue(report['warnings'])
        self.assertEqual(report['status'], 'degraded')
        self.assertEqual(self.records(), [])

    def test_yaml_scalar_lists_and_empty_values_round_trip(self):
        cases = [
            ('tags: [ikinci-beyin, obsidian]', {'tags': ['ikinci-beyin', 'obsidian']}),
            ('tags: []', {'tags': []}),
            ('tags: [  ]', {'tags': []}),
            ('aliases: ["Foo, Bar", \'baz\', \'it\'\'s fine\']',
             {'aliases': ['Foo, Bar', 'baz', "it's fine"]}),
            (r'aliases: ["quote: \"x\"", "back\\slash", "line\nfeed", "\u00e7"]',
             {'aliases': ['quote: "x"', 'back\\slash', 'line\nfeed', 'ç']}),
            ('tags: [1, true, null, word]', {'tags': ['1', 'true', 'null', 'word']}),
            ('tags: [1, true, null]', {'tags': [1, True, None]}),
            ('tags:\n  - a\n  - "b c"', {'tags': ['a', 'b c']}),
            ('aliases:\n    - "Foo, Bar"\n    - \'it\'\'s fine\'\n    - plain, comma',
             {'aliases': ['Foo, Bar', "it's fine", 'plain, comma']}),
            (r'aliases:' + '\n  - "line\\nfeed"\n  - "\\u00e7"',
             {'aliases': ['line\nfeed', 'ç']}),
            ('tags:\n  - 1\n  - true\n  - null', {'tags': ['1', 'true', 'null']}),
            ('tags:\n  - a\naliases:\n    - b\ntitle:',
             {'tags': ['a'], 'aliases': ['b'], 'title': None}),
            ('title:\naliases: []', {'title': None, 'aliases': []}),
            ('title:   ', {'title': None}),
            ('tags:\naliases:', {'tags': None, 'aliases': None}),
            ('aliases: ["", \'\', " # literal", "&literal", "[literal]"]',
             {'aliases': ['', '', ' # literal', '&literal', '[literal]']}),
            ('tags:\n  - ""\n  - \' # literal\'\n  - "!literal"',
             {'tags': ['', ' # literal', '!literal']}),
            ('title: "Example"\nscore: 1\nenabled: true\nempty: null',
             {'title': 'Example', 'score': 1, 'enabled': True, 'empty': None}),
        ]
        source = self.vault / 'lists.md'
        body = 'Nebula calibration YAML fixture.\n'
        for header, expected in cases:
            with self.subTest(header=header):
                metadata = dict(expected, id='yaml-note', project='nebula')
                source.write_text('---\nid: yaml-note\nproject: nebula\n' + header + '\n---\n' + body, encoding='utf-8')
                self.assertEqual(self.module.parse(source.read_text(encoding='utf-8')), (metadata, body))
                report = self.engine.sync()
                self.assertEqual(report['status'], 'succeeded', report)
                record = self.records()[0]
                for key, value in expected.items():
                    self.assertEqual(record[key], value)
                self.assertEqual(self.module.parse(self.module.render(metadata, body)), (metadata, body))

    def test_yaml_single_level_mapping_round_trip(self):
        """#179: Single-level nested mapping (e.g. Claude Code metadata: with type/modified) indexes cleanly."""
        cases = [
            ('metadata:\n  type: project\n  modified: 2026-09-26T12:00:00.000Z',
             {'metadata': {'type': 'project', 'modified': '2026-09-26T12:00:00.000Z'}}),
            ('facts:\n  owner: Synthetic Reviewer\n  priority: 1\n  verified: true\n  notes: null',
             {'facts': {'owner': 'Synthetic Reviewer', 'priority': 1, 'verified': True, 'notes': None}}),
            ('custom:\n  quoted: "value with spaces"\n  single: \'another value\'\n  tags: [a, b]',
             {'custom': {'quoted': 'value with spaces', 'single': 'another value', 'tags': ['a', 'b']}}),
            ('metadata:\n  empty:\n  active: true',
             {'metadata': {'empty': None, 'active': True}}),
            ('info:\n    indented_4_spaces: value\n    another: 123',
             {'info': {'indented_4_spaces': 'value', 'another': 123}}),
        ]
        source = self.vault / 'mapping.md'
        body = 'Nebula calibration nested mapping fixture.\n'
        for header, expected in cases:
            with self.subTest(header=header):
                metadata = dict(expected, id='mapping-note', project='nebula')
                source.write_text('---\nid: mapping-note\nproject: nebula\n' + header + '\n---\n' + body, encoding='utf-8')
                self.assertEqual(self.module.parse(source.read_text(encoding='utf-8')), (metadata, body))
                report = self.engine.sync()
                self.assertEqual(report['status'], 'succeeded', report)
                self.assertEqual(report['warnings'], [])
                record = self.records()[0]
                for key, value in expected.items():
                    self.assertEqual(record[key], value)
                self.assertEqual(self.module.parse(self.module.render(metadata, body)), (metadata, body))

    def test_nested_gate_keys_keep_source_excluded(self):
        """#179: a privacy or trust key nested under a mapping is never applied, so the
        source stays out of the index (as before nested mappings were read) instead of
        being served with the default internal visibility."""
        cases = [
            ('metadata:\n  visibility: private', 'visibility'),
            ('metadata:\n  type: project\n  visibility: public', 'visibility'),
            ('metadata:\n  trust: untrusted', 'trust'),
            ('metadata:\n  trusted: false', 'trusted'),
            ('metadata:\n  remote_allowed: false', 'remote_allowed'),
            ('metadata:\n  status: untrusted', 'status'),
            ('metadata:\n  kind: untrusted', 'kind'),
        ]
        source = self.vault / 'nested-gate.md'
        for header, key in cases:
            with self.subTest(header=header):
                source.write_text('---\nid: nested-gate\nproject: nebula\n' + header +
                                  '\n---\nNebula calibration nested gate fixture.\n', encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'nested ' + key + ' is not applied'):
                    self.module.parse(source.read_text(encoding='utf-8'))
                report = self.engine.sync()
                self.assertEqual(report['status'], 'degraded', report)
                self.assertEqual([w['source'] for w in report['warnings']], ['nested-gate.md'])
                self.assertEqual(self.records(), [])
        # Ordinary values under the same names stay readable.
        source.write_text('---\nid: nested-gate\nproject: nebula\nmetadata:\n  status: draft\n  kind: memory\n'
                          '---\nNebula calibration nested gate fixture.\n', encoding='utf-8')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.records()[0]['metadata'], {'status': 'draft', 'kind': 'memory'})

    def test_nested_mapping_crlf_and_turkish_keys(self):
        source = self.vault / 'crlf.md'
        source.write_bytes('---\r\nid: crlf-map\r\nproject: nebula\r\nmetadata:\r\n  öncelik: yüksek\r\n'
                           '  Şifre_notu: "yok"\r\n  boş:\r\n---\r\nNebula calibration Arşiv fixture.\r\n'.encode('utf-8'))
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.records()[0]['metadata'], {'öncelik': 'yüksek', 'Şifre_notu': 'yok', 'boş': None})

    def test_inline_json_and_plain_values_keep_existing_behaviour(self):
        body = 'Nebula calibration inline JSON fixture.\n'
        header = ('id: inline-json\nfacts: {"nested": [1, true]}\ntags: [["a"], {"b": "c"}]\n'
                  "title: Karar: V3\nnote: 'it'quote'")
        expected = {'id': 'inline-json', 'facts': {'nested': [1, True]}, 'tags': [['a'], {'b': 'c'}],
                    'title': 'Karar: V3', 'note': "it'quote"}
        self.assertEqual(self.module.parse('---\n' + header + '\n---\n' + body), (expected, body))

    def test_inline_comments_and_template_placeholders_are_indexed(self):
        cases = [
            ('kaynak: olcum   # not', {'kaynak': 'olcum'}),
            ('kaynak: olcum\t# not', {'kaynak': 'olcum'}),
            ('kaynak: Karar: V3 # not', {'kaynak': 'Karar: V3'}),
            ('created: {{TODAY}}', {'created': '{{TODAY}}'}),
            ('created: {{date:YYYY-MM-DD}}', {'created': '{{date:YYYY-MM-DD}}'}),
            ('created: {{}}', {'created': '{{}}'}),
            ('kaynak: "olcum" # not', {'kaynak': 'olcum'}),
            ("kaynak: 'olcum' # not", {'kaynak': 'olcum'}),
            ('kaynak: "a # b" # not', {'kaynak': 'a # b'}),
            ('kaynak: c#sharp', {'kaynak': 'c#sharp'}),
            ('kaynak: "a # b"', {'kaynak': 'a # b'}),
            ("note: 'it'quote'", {'note': "it'quote"}),
            ('aliases: ["", \'\', " # literal"]', {'aliases': ['', '', ' # literal']}),
            # A trailing comment never changes the type the bare value would have had.
            ('seviye: 2 # yuksek', {'seviye': 2}),
            ('acik: true # evet', {'acik': True}),
            ('bos: null # yok', {'bos': None}),
            ('adres: https://example.com/#bolum', {'adres': 'https://example.com/#bolum'}),
        ]
        source = self.vault / 'comments.md'
        body = 'Nebula calibration comment fixture.\n'
        for header, expected in cases:
            with self.subTest(header=header):
                metadata = dict(expected, id='comment-note', project='nebula')
                source.write_text('---\nid: comment-note\nproject: nebula\n' + header + '\n---\n' + body, encoding='utf-8')
                self.assertEqual(self.module.parse(source.read_text(encoding='utf-8')), (metadata, body))
                report = self.engine.sync()
                self.assertEqual(report['status'], 'succeeded', report)
                self.assertEqual(report['warnings'], [])
                record = self.records()[0]
                for key, value in expected.items():
                    self.assertEqual(record[key], value)

    def test_unicode_property_keys_may_start_with_localized_letters(self):
        header = ('özet: karar\nşirket: Avenox\nüberblick: bereit\n'
                  'étiquette: memoire\nключ: значение\n_internal: true')
        metadata, body = self.module.parse('---\n' + header + '\n---\nLocalized keys.\n')
        self.assertEqual(metadata['özet'], 'karar')
        self.assertEqual(metadata['şirket'], 'Avenox')
        self.assertEqual(metadata['überblick'], 'bereit')
        self.assertEqual(metadata['étiquette'], 'memoire')
        self.assertEqual(metadata['ключ'], 'значение')
        self.assertTrue(metadata['_internal'])
        self.assertEqual(body, 'Localized keys.\n')
        for key in ('1özet', '-özet', '.özet'):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.module.parse(f'---\n{key}: test\n---\nBody\n')

    def test_canonical_json_still_preserves_complex_metadata(self):
        metadata = {'id': 'json-note', 'project': 'nebula', 'title': None,
                    'facts': {'nested': {'values': [1, True, None]}}, 'tags': [['a'], {'b': 'c'}]}
        body = 'Nebula calibration canonical JSON fixture.\n'
        source = self.vault / 'canonical.md'
        source.write_text(self.module.render(metadata, body), encoding='utf-8')
        self.assertEqual(self.module.parse(source.read_text(encoding='utf-8')), (metadata, body))
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        record = self.records()[0]
        for key, value in metadata.items():
            self.assertEqual(record[key], value)

    def test_unsupported_yaml_forms_warn_and_exclude_source(self):
        headers = [
            'field:\n  nested:\n    deeper: value', 'field:\n  nested: value\n  - item',
            'field:\n  nested: value\n    extra: value', 'field:\n\tnested: value',
            'field:\n  nested: value\n  nested: duplicate',
            'field: {nested: value}', 'field: [a, [b]]',
            'field:\n  - [nested]', 'field:\n  - {nested: value}',
            'field:\n  - - nested', 'field:\n  - nested: value',
            'field:\n  - a\n    - b', 'field:\n    - a\n  - b',
            'field: |\n  multiline', 'field: >\n  folded', 'field: [a,\n  b]',
            'field:\n  - |\n    multiline', 'field:\n  - >\n    folded',
            'field: &anchor value', 'field: *alias', 'field: !tag value',
            'field: [a, &anchor b]', 'field: [*alias]', 'field: [!tag value]',
            'field:\n  - &anchor a', 'field:\n  - *alias', 'field:\n  - !tag a',
            'field: [a # comment, b]', 'field:\n  - a # comment',
            'field: [# comment]', 'field: [a\t# comment]',
            'field: [a]\nfield: [b]', 'field:\nfield: value',
            'field:\n\t- a', 'field:\n \t- a', 'field:\n  - a\n\t- b',
            '  field: value', '\tfield: value', 'field:\n- a',
            'field:\n  - ', 'field:\n  - a\n  continuation',
            'field: [a,,b]', 'field: [a,]', 'field: [a', 'field: [a] trailing',
            'field: ["a" trailing]', "field: ['unclosed]", 'field: ["unclosed]',
            r'field: ["bad\q"]', "field: ['bad'quote']",
            'field: [a: b]', 'field: [a:]', 'field: [? a]', 'field: [- a]',
            'field: # comment', 'field: {{a}} {{b}}', 'field: {{a}}}',
            'field: {{nested: {value}}}', 'field: "unclosed # comment',
            "field: 'unclosed # comment", 'field: "a" trailing # comment',
            'field: [a, b] # comment',
        ]
        source = self.vault / 'unsupported.md'
        for header in headers:
            with self.subTest(header=header):
                source.write_text('---\nid: unsupported\nproject: nebula\n' + header +
                                  '\n---\nNebula calibration rejected fixture.\n', encoding='utf-8')
                report = self.engine.sync()
                self.assertEqual(report['status'], 'degraded', report)
                self.assertEqual(report['indexed'], 0)
                self.assertEqual([w['source'] for w in report['warnings']], ['unsupported.md'])
                self.assertEqual(self.records(), [])

    def test_yaml_null_and_list_values_respect_record_validation(self):
        source = self.vault / 'invalid.md'
        for field in ('id', 'kind', 'status', 'project', 'updated_at', 'revision', 'visibility', 'facts', 'supersedes'):
            for value in ('', '[public, internal]', '\n  - public\n  - internal'):
                # Supersedes explicitly accepts a sequence of record IDs.
                if field == 'supersedes' and value:
                    continue
                with self.subTest(field=field, value=value):
                    source.write_text('---\n' + field + ': ' + value +
                                      '\n---\nNebula calibration invalid metadata.\n', encoding='utf-8')
                    report = self.engine.sync()
                    self.assertEqual(report['status'], 'degraded', report)
                    self.assertEqual(report['indexed'], 0)
                    self.assertEqual(len(report['warnings']), 1)
                    self.assertEqual(self.engine.store.snapshot_context()['records'], [])

    def test_block_list_vault_sync_and_context_cli_succeed(self):
        (self.vault / 'plain.md').write_text('Nebula calibration plain note.\n', encoding='utf-8')
        (self.vault / 'flow.md').write_text('---\ntags: [nebula, calibration]\n---\nNebula calibration flow note.\n', encoding='utf-8')
        (self.vault / 'block.md').write_text('---\ntags:\n  - nebula\n  - "calibration"\ntitle:\n---\nNebula calibration block note.\n', encoding='utf-8')
        for command in (('sync',), ('context', 'Nebula calibration')):
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/beyin_v3.py'),
                                     '--vault', str(self.vault), '--state', str(self.state), *command],
                                    capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            if command[0] == 'sync':
                self.assertEqual(output['status'], 'succeeded')
                self.assertEqual(output['indexed'], 3)
                self.assertEqual(output['warnings'], [])
            else:
                self.assertEqual({record['source'] for record in output['records']}, {'plain.md', 'flow.md', 'block.md'})
                self.assertFalse(output['abstained'])

    def test_block_list_task_update_writes_json_and_preserves_body(self):
        for label, newline in (('LF', '\n'), ('CRLF', '\r\n')):
            with self.subTest(newline=label):
                self._assert_block_list_task_update(newline)

    def _assert_block_list_task_update(self, newline):
        body = '# Nebula calibration\n\n- Keep exact prose.\n  Unicode: ölçüm 🔭\n'.replace('\n', newline)
        header = ('id: yaml-task\nkind: task\nrevision: 1\nproject: nebula\nstatus: active\n'
                  'tags:\n  - a\n  - "b c"\naliases: ["Foo, Bar", \'baz\']\ntitle:')
        source = self.vault / 'task.md'
        # Write exact bytes so the newline style is the one under test on every platform.
        source.write_bytes(('---\n' + header + '\n---\n').replace('\n', newline).encode('utf-8') + body.encode('utf-8'))
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        original, parsed_body = self.module.parse(source.read_bytes().decode('utf-8'))
        self.assertEqual(parsed_body, body)
        before = source.read_bytes()
        for field in ('status', 'visibility'):
            for value in (None, ['waiting']):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        self.engine.update_task('yaml-task', 1, {field: value})
                    self.assertEqual(source.read_bytes(), before)
        updated = self.engine.update_task('yaml-task', 1, {'status': 'waiting'})
        expected = dict(original, status='waiting', revision=2)
        rendered = source.read_bytes().decode('utf-8')
        self.assertEqual(json.loads(rendered.split('---', 2)[1]), expected)
        self.assertEqual(source.read_bytes(), self.module.render(expected, body).encode('utf-8'))
        self.assertEqual(self.module.parse(rendered), (expected, body))
        for key, value in expected.items():
            self.assertEqual(updated[key], value)
        source.unlink()
        self.engine.sync()

    def test_receipt_has_immutable_iso_created_at(self):
        self.write()
        self.engine.sync()
        receipt = self.engine.receipt('dated-receipt', 'Synthetic dated evidence.', ['notes/task.md'], 'codex')
        path = self.vault / receipt['source']
        original = path.read_bytes()
        frontmatter = path.read_text(encoding='utf-8').split('---', 2)[1]
        metadata = json.loads(frontmatter)
        self.assertIn('created_at', metadata)
        timestamp = datetime.fromisoformat(metadata['created_at'].replace('Z', '+00:00'))
        self.assertIsNotNone(timestamp.tzinfo)
        self.assertEqual(receipt, self.engine.receipt('dated-receipt', 'Synthetic dated evidence.', ['notes/task.md'], 'claude'))
        self.assertEqual(path.read_bytes(), original)

    def test_no_query_snapshot_preserves_visibility_status_and_budget(self):
        self.write('notes/active.md', id='active', status='active', visibility='public')
        self.write('notes/waiting.md', id='waiting', status='waiting')
        self.write('notes/done.md', id='done', status='done')
        self.write('notes/private.md', id='private', visibility='private')
        self.write('notes/untrusted.md', id='untrusted', kind='untrusted')
        self.engine.sync()
        response = self.engine.store.snapshot_context(audience='internal', budget_chars=8000, limit=5)
        self.assertEqual({r['id'] for r in response['records']}, {'active', 'waiting'})
        self.assertLessEqual(response['used_chars'], 8000)
        public = self.engine.store.snapshot_context(audience='public', budget_chars=8000, limit=5)
        self.assertEqual([r['id'] for r in public['records']], ['active'])

    def test_snapshot_includes_plain_notes_and_statusless_facts(self):
        plain = self.vault / 'observatory.md'
        plain.write_text('# Synthetic observatory note\nNebula observatory uses violet calibration.\n', encoding='utf-8')
        fact = self.vault / 'fact.md'
        fact.write_text('---\n' + json.dumps({'id': 'statusless-fact', 'kind': 'fact', 'project': 'nebula', 'visibility': 'internal'}) +
                        '\n---\nNebula spectroscope owner is Synthetic Reviewer.\n', encoding='utf-8')
        self.write('notes/done.md', id='done-task', status='done')
        self.engine.sync()
        snapshot = self.engine.store.snapshot_context(audience='internal', budget_chars=8000, limit=5)
        self.assertEqual({r['source'] for r in snapshot['records']}, {'observatory.md', 'fact.md'})
        self.assertNotIn('done-task', [r['id'] for r in snapshot['records']])
        self.assertIn('violet calibration', json.dumps(snapshot))
        self.assertIn('Synthetic Reviewer', json.dumps(snapshot))

    def test_source_replace_failure_recovers_consistently(self):
        path = self.write()
        self.engine.sync()
        original = path.read_bytes()
        real_replace = os.replace
        def fail_source(src, dst, *args, **kwargs):
            if Path(dst).resolve() == path.resolve():
                raise OSError('Synthetic source replace failure')
            return real_replace(src, dst, *args, **kwargs)
        with patch.object(self.module.os, 'replace', side_effect=fail_source):
            with self.assertRaises(OSError):
                self.engine.update_task('nebula-task', 1, {'status': 'waiting'})
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.records()[0]['revision'], 1)
        getattr(self.engine.store, 'close', lambda: None)()
        self.engine = self.module.SyncEngine(self.vault, self.state)
        self.engine.sync()
        recovered = self.records()[0]
        self.assertEqual(recovered['revision'], 2)
        self.assertEqual(recovered['status'], 'waiting')
        self.engine.sync()
        self.assertEqual(self.records()[0]['revision'], 2)

    def test_recovery_does_not_overwrite_intervening_manual_change(self):
        path = self.write()
        self.engine.sync()
        real_replace = os.replace
        def fail_source(src, dst, *args, **kwargs):
            if Path(dst).resolve() == path.resolve():
                raise OSError('Synthetic source replace failure')
            return real_replace(src, dst, *args, **kwargs)
        with patch.object(self.module.os, 'replace', side_effect=fail_source):
            with self.assertRaises(OSError):
                self.engine.update_task('nebula-task', 1, {'status': 'waiting'})
        self.write(body='Nebula calibration manual source revision wins.\n', revision=2, status='done')
        manual = path.read_bytes()
        report = self.engine.sync()
        self.assertTrue(report['conflicts'])
        self.assertEqual(path.read_bytes(), manual)
        self.assertNotEqual(self.records()[0]['status'], 'waiting')


class ReceiptStateResetTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-receipt-reset-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        (self.vault / 'notes').mkdir()
        (self.vault / 'notes/task.md').write_text('# Task\nInitial work item.\n', encoding='utf-8')
        from beyin_v3 import ReceiptConflict
        self.ReceiptConflict = ReceiptConflict
        self._hash = self.module._hash

    def test_receipt_submission_after_state_reset_does_not_deadlock(self):
        """State reset or multi-device vault sync must not cause receipt projection deadlock."""
        state1 = self.root / 'state1'
        engine1 = self.module.SyncEngine(self.vault, state1)
        engine1.sync()
        engine1.receipt('event-1', 'Initial calibration completed.', ['notes/task.md'], 'codex', session='s1')

        outcomes1 = (self.vault / 'knowledge/v3/outcomes.md').read_text(encoding='utf-8')
        self.assertIn('Initial calibration completed.', outcomes1)

        # Simulate state reset (wiped cache, new machine, Obsidian Sync)
        state2 = self.root / 'state2'
        engine2 = self.module.SyncEngine(self.vault, state2)
        engine2.sync()

        res = engine2.receipt('event-2', 'Second phase verified.', ['notes/task.md'], 'codex', session='s2')
        self.assertEqual(res['status'], 'succeeded')

        outcomes2 = (self.vault / 'knowledge/v3/outcomes.md').read_text(encoding='utf-8')
        self.assertIn('Initial calibration completed.', outcomes2)
        self.assertIn('Second phase verified.', outcomes2)

    def test_event_id_reused_on_another_device_is_reported(self):
        # #205: two devices write the same event_id; the merge keeps the other device's file.
        other = self.root / 'other'
        (other / 'notes').mkdir(parents=True)
        (other / 'notes/task.md').write_text('# Task\nInitial work item.\n', encoding='utf-8')
        here = self.module.SyncEngine(self.vault, self.root / 'state-here')
        there = self.module.SyncEngine(other, self.root / 'state-there')
        mine = here.receipt('shared-topic', 'Calibration done here.', ['notes/task.md'], 'codex')
        there.receipt('shared-topic', 'Calibration done there.', ['notes/task.md'], 'claude')
        source = self.vault / mine['source']
        own = source.read_bytes()
        source.unlink()
        source.write_bytes((other / mine['source']).read_bytes())
        report = here.sync()
        self.assertEqual(report['status'], 'conflict')
        self.assertIn(mine['source'], [c.get('source') for c in report['conflicts']])
        source.unlink()
        source.write_bytes(own)
        report = here.sync()
        self.assertNotIn(mine['source'], [c.get('source') for c in report['conflicts']])

    def test_receipt_divergence_does_not_fail_unrelated_writes(self):
        # #210 review: another receipt's divergence must not turn a completed, unrelated write into an exception.
        other = self.root / 'other'
        (other / 'notes').mkdir(parents=True)
        (other / 'notes/task.md').write_text('# Task\nInitial work item.\n', encoding='utf-8')
        (self.vault / 'notes/work.md').write_text('---\n' + json.dumps(
            {'id': 'work-task', 'kind': 'task', 'project': 'nebula', 'revision': 1, 'status': 'active',
             'visibility': 'internal'}) + '\n---\nNebula work.\n', encoding='utf-8')
        here = self.module.SyncEngine(self.vault, self.root / 'state-here')
        there = self.module.SyncEngine(other, self.root / 'state-there')
        mine = here.receipt('shared-topic', 'Calibration done here.', ['notes/task.md'], 'codex')
        there.receipt('shared-topic', 'Calibration done there.', ['notes/task.md'], 'claude')
        source = self.vault / mine['source']
        source.unlink()
        source.write_bytes((other / mine['source']).read_bytes())
        self.assertEqual(here.receipt('independent-event', 'Unrelated work.', ['notes/task.md'], 'codex')['status'],
                         'succeeded')
        self.assertEqual(here.update_task('work-task', 1, {'status': 'waiting'})['revision'], 2)
        report = here.sync()
        self.assertEqual(report['status'], 'conflict')
        self.assertIn(mine['source'], [c.get('source') for c in report['conflicts']])

    def test_receipt_with_whitespace_and_newlines_recovers_cleanly(self):
        """Review Point 1 & 2: Summaries with trailing newlines or spaces must not cause false event id collision."""
        summaries = ['Done.\n', '  Done.', 'Line1\nLine2  ']
        for i, s in enumerate(summaries):
            state1 = self.root / f'ws_state1_{i}'
            engine1 = self.module.SyncEngine(self.vault, state1)
            engine1.sync()
            eid = f'event-ws-{i}'
            engine1.receipt(eid, s, ['notes/task.md'], 'codex', session=f's{i}')

            # Reset state and re-sync
            state2 = self.root / f'ws_state2_{i}'
            engine2 = self.module.SyncEngine(self.vault, state2)
            report = engine2.sync()
            self.assertEqual(report['conflicts'], [])

            # Reasserting same receipt on reset state must succeed without conflict
            res = engine2.receipt(eid, s, ['notes/task.md'], 'codex', session=f's{i}')
            self.assertEqual(res['status'], 'succeeded')

            # Following with another receipt must succeed without projection conflict
            res_next = engine2.receipt(f'event-next-{i}', 'Next step.', ['notes/task.md'], 'codex', session=f's{i}')
            self.assertEqual(res_next['status'], 'succeeded')

    def test_receipt_validation_reports_warnings(self):
        """Review Point 4: At-rest receipt files with bad filename hash, harness or refs report warnings."""
        state = self.root / 'state_val'
        engine = self.module.SyncEngine(self.vault, state)
        engine.sync()

        receipts_dir = self.vault / 'receipts'
        receipts_dir.mkdir(exist_ok=True)

        # 1. Filename does not match hash of event_id
        bad_hash_file = receipts_dir / f'{self._hash("another-event")}.md'
        bad_hash_file.write_text('---\n{"kind": "receipt", "event_id": "ev-1", "refs": ["notes/task.md"], "harness": "codex"}\n---\nSummary\n', encoding='utf-8')

        # 2. Invalid harness
        harness_eid = 'ev-bad-harness'
        bad_harness_file = receipts_dir / f'{self._hash(harness_eid)}.md'
        bad_harness_file.write_text(f'---\n{{"kind": "receipt", "event_id": "{harness_eid}", "refs": ["notes/task.md"], "harness": "unsupported"}}\n---\nSummary\n', encoding='utf-8')

        # 3. Missing refs
        refs_eid = 'ev-bad-refs'
        bad_refs_file = receipts_dir / f'{self._hash(refs_eid)}.md'
        bad_refs_file.write_text(f'---\n{{"kind": "receipt", "event_id": "{refs_eid}", "refs": [], "harness": "codex"}}\n---\nSummary\n', encoding='utf-8')

        report = engine.sync()
        warn_sources = {w['source'] for w in report['warnings']}
        self.assertIn(f'receipts/{self._hash("another-event")}.md', warn_sources)
        self.assertIn(f'receipts/{self._hash(harness_eid)}.md', warn_sources)
        self.assertIn(f'receipts/{self._hash(refs_eid)}.md', warn_sources)

    def test_existing_receipt_reassertion_after_state_reset(self):
        """Calling receipt() on an existing receipt after state reset must not trigger false source change conflict."""
        state1 = self.root / 'state_reassert1'
        engine1 = self.module.SyncEngine(self.vault, state1)
        engine1.sync()
        engine1.receipt('event-1', 'Initial calibration completed.', ['notes/task.md'], 'codex', session='s1')

        state2 = self.root / 'state_reassert2'
        engine2 = self.module.SyncEngine(self.vault, state2)
        engine2.sync()

        res = engine2.receipt('event-1', 'Initial calibration completed.', ['notes/task.md'], 'codex', session='s1')
        self.assertEqual(res['status'], 'succeeded')

    def test_direct_receipt_call_without_prior_sync_recovers_receipt(self):
        """Calling receipt() before sync() on fresh state must recover on-disk receipt without conflict."""
        state1 = self.root / 'state_direct1'
        engine1 = self.module.SyncEngine(self.vault, state1)
        engine1.sync()
        engine1.receipt('event-1', 'Initial calibration completed.', ['notes/task.md'], 'codex', session='s1')

        state3 = self.root / 'state_direct3'
        engine3 = self.module.SyncEngine(self.vault, state3)
        res = engine3.receipt('event-1', 'Initial calibration completed.', ['notes/task.md'], 'codex', session='s1')
        self.assertEqual(res['status'], 'succeeded')

    def test_manual_receipt_tampering_still_fails(self):
        """Genuine manual tampering with receipt content must still raise ReceiptConflict."""
        state1 = self.root / 'state_tamper1'
        engine1 = self.module.SyncEngine(self.vault, state1)
        engine1.sync()
        res1 = engine1.receipt('event-1', 'Original text.', ['notes/task.md'], 'codex', session='s1')

        receipt_file = self.vault / res1['source']
        receipt_file.write_text(receipt_file.read_text(encoding='utf-8').replace('Original text.', 'Tampered text.'), encoding='utf-8')

        state2 = self.root / 'state_tamper2'
        engine2 = self.module.SyncEngine(self.vault, state2)
        with self.assertRaises(self.ReceiptConflict):
            engine2.receipt('event-1', 'Original text.', ['notes/task.md'], 'codex', session='s1')

    def test_receipts_symlink_rejected(self):
        """Review Point 3: A symlinked receipts/ directory or file inside must be rejected and not projected."""
        outside = self.root / 'outside'
        outside.mkdir()
        secret_receipt = outside / f'{self._hash("ev-symlink")}.md'
        secret_receipt.write_text(f'---\n{{"kind": "receipt", "event_id": "ev-symlink", "refs": ["notes/task.md"], "harness": "codex"}}\n---\nSecret Outside Content\n', encoding='utf-8')
        engine = self.module.SyncEngine(self.vault, self.root / 'state_sym')
        outcomes = self.vault / 'knowledge/v3/outcomes.md'
        (self.vault / 'receipts').mkdir()
        file_link = self.vault / 'receipts' / secret_receipt.name
        try:
            file_link.symlink_to(secret_receipt)
        except OSError:
            self.skipTest('Symlink creation not permitted')
        report = engine.sync()
        self.assertIn(f'receipts/{secret_receipt.name}', {w['source'] for w in report['warnings']})
        self.assertFalse(outcomes.exists())
        file_link.unlink()
        (self.vault / 'receipts').rmdir()
        (self.vault / 'receipts').symlink_to(outside, target_is_directory=True)
        report = engine.sync()
        self.assertIn('receipts', {w['source'] for w in report['warnings']})
        self.assertFalse(outcomes.exists())

    def test_carriage_return_summary_does_not_block_later_receipts(self):
        """Windows CRLF summaries are written, read back and recovered byte-exact, never as a journal collision."""
        engine = self.module.SyncEngine(self.vault, self.root / 'state_cr1')
        engine.sync()
        summaries = {'event-crlf': 'Line1\r\nLine2', 'event-cr': 'Done.\r'}
        for event_id, summary in summaries.items():
            self.assertEqual(engine.receipt(event_id, summary, ['notes/task.md'], 'codex')['status'], 'succeeded')
        self.assertEqual(engine.receipt('event-after-cr', 'Next.', ['notes/task.md'], 'codex')['status'], 'succeeded')
        self.assertEqual(engine.sync()['conflicts'], [])
        reset = self.module.SyncEngine(self.vault, self.root / 'state_cr2')
        self.assertEqual(reset.sync()['conflicts'], [])
        for event_id, summary in summaries.items():
            self.assertEqual(reset.receipt(event_id, summary, ['notes/task.md'], 'codex')['status'], 'succeeded')
        self.assertEqual(reset.receipt('event-after-reset', 'Later.', ['notes/task.md'], 'codex')['status'], 'succeeded')

    def test_renamed_ref_does_not_degrade_sync_or_block_recovery(self):
        """Refs were valid at write time; a later rename must not warn on every sync or break recovery."""
        (self.vault / 'notes/old.md').write_text('# Old\n', encoding='utf-8')
        engine = self.module.SyncEngine(self.vault, self.root / 'state_ref1')
        engine.sync()
        engine.receipt('event-old', 'Worked on old note.', ['notes/old.md'], 'codex')
        (self.vault / 'notes/old.md').rename(self.vault / 'notes/renamed.md')
        self.assertEqual(engine.sync()['status'], 'succeeded')
        reset = self.module.SyncEngine(self.vault, self.root / 'state_ref2')
        self.assertEqual(reset.sync()['conflicts'], [])
        self.assertEqual(reset.receipt('event-new', 'After reset.', ['notes/task.md'], 'codex')['status'], 'succeeded')

    def test_malformed_receipt_metadata_is_reported_not_fatal(self):
        """Disk metadata is untrusted: bad created_at or session is a warning, never a crash or a new path."""
        engine = self.module.SyncEngine(self.vault, self.root / 'state_meta')
        engine.sync()
        receipts_dir = self.vault / 'receipts'
        receipts_dir.mkdir()
        bad = {'ev-int-date': {'created_at': 5}, 'ev-slash-date': {'created_at': '2026/09/24T10:00:00'},
               'ev-dots-date': {'created_at': '../../evil'}, 'ev-bad-session': {'session': '../x'}}
        for event_id, extra in bad.items():
            metadata = dict({'kind': 'receipt', 'event_id': event_id, 'harness': 'codex', 'refs': ['notes/task.md']}, **extra)
            (receipts_dir / f'{self._hash(event_id)}.md').write_text(self.module.render(metadata, 'Crafted.\n'), encoding='utf-8')
        report = engine.sync()
        self.assertEqual({w['source'] for w in report['warnings']}, {f'receipts/{self._hash(e)}.md' for e in bad})
        self.assertEqual(engine.receipt('event-ok', 'Still writable.', ['notes/task.md'], 'codex')['status'], 'succeeded')
        self.assertFalse((self.vault / 'daily/v3/2026').exists())

    def test_non_receipt_files_in_receipts_dir_are_ignored(self):
        """Human notes and V2 historical files under receipts/ were never indexed and must not degrade sync."""
        (self.vault / 'receipts').mkdir()
        (self.vault / 'receipts/README.md').write_text('Human note about receipts\n', encoding='utf-8')
        engine = self.module.SyncEngine(self.vault, self.root / 'state_foreign')
        engine.receipt('event-1', 'Done.', ['notes/task.md'], 'codex')
        self.assertEqual(engine.sync()['status'], 'succeeded')

    def test_warm_sync_skips_receipt_scan_until_directory_changes(self):
        """A warm sync stops at one stat of receipts/; any new entry changes its signature and is scanned."""
        engine = self.module.SyncEngine(self.vault, self.root / 'state_warm')
        engine.receipt('event-1', 'First device.', ['notes/task.md'], 'codex')
        receipts_dir = self.vault / 'receipts'
        past = (int(datetime.now().timestamp()) - 60) * 10**9
        os.utime(receipts_dir, ns=(past, past))  # Older than the same-tick window, so the signature is stored.
        self.assertEqual(engine.sync()['status'], 'succeeded')
        crafted = receipts_dir / f'{self._hash("ev-crafted")}.md'
        crafted.write_text('Not a receipt.\n', encoding='utf-8')
        os.utime(receipts_dir, ns=(past, past))  # Same signature: nothing is listed or parsed.
        self.assertEqual(engine.sync()['status'], 'succeeded')
        os.utime(receipts_dir)
        self.assertIn(f'receipts/{crafted.name}', {w['source'] for w in engine.sync()['warnings']})
        crafted.unlink()
        other = self.module.SyncEngine(self.vault, self.root / 'state_warm_other')
        self.assertEqual(other.sync()['conflicts'], [])
        self.assertEqual(other.receipt('event-2', 'Second device.', ['notes/task.md'], 'codex')['status'], 'succeeded')
        self.assertEqual(engine.sync()['conflicts'], [])
        self.assertIn('Second device.', (self.vault / 'knowledge/v3/outcomes.md').read_text(encoding='utf-8'))

    def test_receipt_hidden_by_unchanged_directory_mtime_heals_on_next_sync(self):
        """Where a filesystem keeps the directory mtime, a missed receipt costs one conflicted sync, not a deadlock."""
        engine = self.module.SyncEngine(self.vault, self.root / 'state_heal')
        engine.receipt('event-1', 'First device.', ['notes/task.md'], 'codex')
        receipts_dir = self.vault / 'receipts'
        past = (int(datetime.now().timestamp()) - 60) * 10**9
        os.utime(receipts_dir, ns=(past, past))
        self.assertEqual(engine.sync()['status'], 'succeeded')
        other = self.module.SyncEngine(self.vault, self.root / 'state_heal_other')
        self.assertEqual(other.sync()['conflicts'], [])
        self.assertEqual(other.receipt('event-2', 'Second device.', ['notes/task.md'], 'codex')['status'], 'succeeded')
        os.utime(receipts_dir, ns=(past, past))
        self.assertTrue(engine.sync()['conflicts'])
        self.assertEqual(engine.sync()['conflicts'], [])
        self.assertEqual(engine.receipt('event-3', 'Back in step.', ['notes/task.md'], 'codex')['status'], 'succeeded')


if __name__ == '__main__':
    unittest.main()
