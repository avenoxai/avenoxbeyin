"""Privacy frontmatter casing and alias handling tests (#963)."""
from contextlib import closing
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))

import beyin_v3  # noqa: E402
import beyin_v3_audit  # noqa: E402
import beyin_v3_bridge  # noqa: E402
import beyin_v3_jev  # noqa: E402
import beyin_v3_lint  # noqa: E402
import beyin_v3_projections  # noqa: E402
import beyin_v3_sync  # noqa: E402
import beyin_v3_triage  # noqa: E402


class PrivacyFrontmatterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-privacy-frontmatter-')
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.vault = root / 'vault'
        self.state = root / 'state'
        self.vault.mkdir()
        self.engine = beyin_v3_sync.SyncEngine(self.vault, self.state)
        self.addCleanup(self.engine.store.close)

    def write_note(self, relative, content):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8')
        return path

    def records(self, query='Zirkon', audience='internal'):
        return self.engine.store.retrieve(query, audience=audience, limit=20)['records']

    def snapshot_ids(self, audience='internal'):
        return [r['source'] for r in self.engine.store.snapshot_context(audience=audience, limit=20)['records']]

    def test_visibility_key_and_value_casing_normalized_without_leaking_or_dropping(self):
        cases = [
            ('notes/cap-key.md', '---\nVisibility: private\n---\nZirkon CAP_KEY_CANARY.\n', 'private'),
            ('notes/upper-key.md', '---\nVISIBILITY: private\n---\nZirkon UPPER_KEY_CANARY.\n', 'private'),
            ('notes/cap-val.md', '---\nvisibility: Private\n---\nZirkon CAP_VAL_CANARY.\n', 'private'),
            ('notes/upper-val.md', '---\nvisibility: PRIVATE\n---\nZirkon UPPER_VAL_CANARY.\n', 'private'),
            ('notes/trimmed-val.md', '---\nvisibility: "  Private  "\n---\nZirkon TRIMMED_CANARY.\n', 'private'),
            ('notes/turkish-case.md', '---\nVİSİBİLİTY: PRİVATE\n---\nZirkon TURKISH_CANARY.\n', 'private'),
            ('notes/json-case.md', '---\n{"Visibility": "Private"}\n---\nZirkon JSON_CASE_CANARY.\n', 'private'),
            ('notes/internal-case.md', '---\nVisibility: Internal\n---\nZirkon INTERNAL_CASE_CONTROL.\n', 'internal'),
            ('notes/public-case.md', '---\nvisibility: PUBLIC\n---\nZirkon PUBLIC_CASE_CONTROL.\n', 'public'),
        ]
        for rel, content, _ in cases:
            self.write_note(rel, content)

        report = self.engine.sync()
        self.assertEqual(report['status'], 'succeeded', report)
        self.assertEqual(report['warnings'], [])
        self.assertEqual(report['indexed'], len(cases))

        internal_sources = {r['source'] for r in self.records(audience='internal')}
        self.assertEqual(internal_sources, {'notes/internal-case.md', 'notes/public-case.md'})
        self.assertEqual(set(self.snapshot_ids(audience='internal')), {'notes/internal-case.md', 'notes/public-case.md'})

        public_sources = {r['source'] for r in self.records(audience='public')}
        self.assertEqual(public_sources, {'notes/public-case.md'})

        private_records = {r['source']: r for r in self.records(audience='private')}
        self.assertEqual(set(private_records), {rel for rel, _, _ in cases})
        for rel, _, expected_vis in cases:
            self.assertEqual(private_records[rel]['visibility'], expected_vis, rel)
            self.assertNotIn('Visibility', private_records[rel])
            self.assertNotIn('VISIBILITY', private_records[rel])

    def test_private_and_gizli_boolean_aliases_fail_safe_with_warning(self):
        cases = [
            ('notes/priv-bool.md', '---\nprivate: true\n---\nZirkon PRIV_BOOL_CANARY.\n'),
            ('notes/priv-cap-key.md', '---\nPrivate: true\n---\nZirkon PRIV_CAP_KEY_CANARY.\n'),
            ('notes/priv-str-true.md', '---\nprivate: "true"\n---\nZirkon PRIV_STR_CANARY.\n'),
            ('notes/priv-yes.md', '---\nprivate: Yes\n---\nZirkon PRIV_YES_CANARY.\n'),
            ('notes/priv-evet.md', '---\nprivate: evet\n---\nZirkon PRIV_EVET_CANARY.\n'),
            ('notes/gizli-bool.md', '---\ngizli: true\n---\nZirkon GIZLI_BOOL_CANARY.\n'),
            ('notes/gizli-turkish.md', '---\nGİZLİ: EVET\n---\nZirkon GIZLI_TR_CANARY.\n'),
            ('notes/priv-overrides-internal.md', '---\nvisibility: internal\nprivate: true\n---\nZirkon OVERRIDE_CANARY.\n'),
            ('notes/json-dup-private.md', '---\n{"private": true, "private": false}\n---\nZirkon JSON_DUP_CANARY.\n'),
        ]
        controls = [
            ('notes/priv-false.md', '---\nprivate: false\n---\nZirkon PRIV_FALSE_CONTROL.\n'),
            ('notes/gizli-no.md', '---\ngizli: "no"\n---\nZirkon GIZLI_NO_CONTROL.\n'),
            ('notes/already-private.md', '---\nvisibility: private\nprivate: true\n---\nZirkon ALREADY_PRIV.\n'),
        ]
        for rel, content in cases + controls:
            self.write_note(rel, content)

        first = self.engine.sync()
        self.assertEqual(first['status'], 'succeeded', first)
        self.assertEqual(first['indexed'], len(cases) + len(controls))

        # Every alias note without explicit visibility: private gets a clear warning.
        warned = {w['source']: w for w in first['warnings']}
        self.assertEqual(set(warned), {rel for rel, _ in cases})
        for rel, w in warned.items():
            self.assertEqual(w.get('kind'), 'privacy_frontmatter', w)
            self.assertIn('visibility: private', w['reason'], w)

        # None of the alias notes leak into internal or public context.
        internal_sources = {r['source'] for r in self.records(audience='internal')}
        self.assertEqual(internal_sources, {'notes/priv-false.md', 'notes/gizli-no.md'})

        # Warm sync keeps emitting the warnings until the frontmatter is fixed.
        warm = self.engine.sync()
        self.assertEqual({w['source'] for w in warm['warnings']}, {rel for rel, _ in cases})

        # Adding explicit visibility: private clears the warning.
        self.write_note('notes/priv-bool.md', '---\nvisibility: private\nprivate: true\n---\nZirkon PRIV_BOOL_CANARY.\n')
        after_fix = self.engine.sync()
        self.assertNotIn('notes/priv-bool.md', {w['source'] for w in after_fix['warnings']})

    def test_privacy_tags_fail_safe_with_warning_without_false_positives(self):
        private_tag_cases = [
            ('notes/tag-flow.md', '---\ntags: [private]\n---\nZirkon TAG_FLOW_CANARY.\n'),
            ('notes/tag-mixed.md', '---\ntags: [work, Private]\n---\nZirkon TAG_MIXED_CANARY.\n'),
            ('notes/tag-gizli.md', '---\ntags: [GİZLİ]\n---\nZirkon TAG_GIZLI_CANARY.\n'),
            ('notes/tag-hash.md', '---\ntags: ["#private"]\n---\nZirkon TAG_HASH_CANARY.\n'),
            ('notes/tag-nested.md', '---\ntags: [private/banka]\n---\nZirkon TAG_NESTED_CANARY.\n'),
            ('notes/tag-scalar.md', '---\ntags: private\n---\nZirkon TAG_SCALAR_CANARY.\n'),
            ('notes/tag-cap-comma.md', '---\nTags: work, gizli\n---\nZirkon TAG_CAP_CANARY.\n'),
            ('notes/tag-block.md', '---\ntags:\n  - work\n  - private\n---\nZirkon TAG_BLOCK_CANARY.\n'),
            ('notes/tag-overrides-internal.md', '---\nvisibility: internal\ntags: [private]\n---\nZirkon TAG_OVR_CANARY.\n'),
        ]
        controls = [
            ('notes/tag-equity.md', '---\ntags: [private-equity, privacy]\n---\nZirkon TAG_EQUITY_CONTROL.\n'),
            ('notes/tag-explicit-priv.md', '---\nvisibility: private\ntags: [private]\n---\nZirkon TAG_EXPLICIT_PRIV.\n'),
        ]
        for rel, content in private_tag_cases + controls:
            self.write_note(rel, content)

        report = self.engine.sync()
        self.assertEqual(report['status'], 'succeeded', report)
        warned = {w['source']: w for w in report['warnings']}
        self.assertEqual(set(warned), {rel for rel, _ in private_tag_cases})
        for w in warned.values():
            self.assertEqual(w.get('kind'), 'privacy_frontmatter', w)
            self.assertIn('visibility: private', w['reason'], w)

        internal_sources = {r['source'] for r in self.records(audience='internal')}
        self.assertEqual(internal_sources, {'notes/tag-equity.md'})

        # Warm sync retains the warnings too.
        self.assertEqual({w['source'] for w in self.engine.sync()['warnings']}, {rel for rel, _ in private_tag_cases})

    def test_conflicting_visibility_keys_fail_safe_to_private_with_warning(self):
        cases = [
            ('notes/conflict-yaml.md', '---\nvisibility: internal\nVisibility: private\n---\nZirkon CONFLICT_YAML_CANARY.\n'),
            ('notes/conflict-pub-int.md', '---\nvisibility: public\nVISIBILITY: internal\n---\nZirkon CONFLICT_PI_CANARY.\n'),
            ('notes/conflict-invalid.md', '---\nvisibility: internal\nVisibility: secret\n---\nZirkon CONFLICT_INV_CANARY.\n'),
            ('notes/conflict-json-case.md', '---\n{"visibility": "internal", "Visibility": "private"}\n---\nZirkon CONFLICT_JC_CANARY.\n'),
            ('notes/conflict-json-dup.md', '---\n{"visibility": "private", "visibility": "internal"}\n---\nZirkon CONFLICT_JD_CANARY.\n'),
        ]
        for rel, content in cases:
            self.write_note(rel, content)

        report = self.engine.sync()
        self.assertEqual(report['status'], 'succeeded', report)
        self.assertEqual(report['indexed'], len(cases))
        self.assertEqual({w['source'] for w in report['warnings']}, {rel for rel, _ in cases})
        for w in report['warnings']:
            self.assertIn('conflicting visibility', w['reason'])
            self.assertIn('private', w['reason'])

        self.assertEqual(self.records(audience='internal'), [])
        self.assertEqual(self.records(audience='public'), [])
        priv = {r['source']: r for r in self.records(audience='private')}
        self.assertEqual(set(priv), {rel for rel, _ in cases})
        for rec in priv.values():
            self.assertEqual(rec['visibility'], 'private')

    def test_unknown_visibility_values_never_leak_and_warn(self):
        cases = [
            ('notes/secret.md', '---\nvisibility: secret\n---\nZirkon SECRET_VAL_CANARY.\n'),
            ('notes/cap-secret.md', '---\nVisibility: secret\n---\nZirkon CAP_SECRET_CANARY.\n'),
            ('notes/gizli-val.md', '---\nvisibility: gizli\n---\nZirkon GIZLI_VAL_CANARY.\n'),
            ('notes/bogus.md', '---\nvisibility: bogus\n---\nZirkon BOGUS_VAL_CANARY.\n'),
        ]
        for rel, content in cases:
            self.write_note(rel, content)

        report = self.engine.sync()
        self.assertEqual(report['status'], 'degraded', report)
        self.assertEqual(report['indexed'], 0)
        self.assertEqual({w['source'] for w in report['warnings']}, {rel for rel, _ in cases})
        for w in report['warnings']:
            self.assertIn('invalid visibility', w['reason'])
        self.assertEqual(self.records(audience='internal'), [])
        self.assertEqual(self.records(audience='public'), [])
        self.assertEqual(self.records(audience='private'), [])

    def test_nested_case_variant_and_alias_gate_keys_stay_excluded(self):
        for header in ('metadata:\n  Visibility: private', 'metadata:\n  private: true', 'metadata:\n  gizli: evet'):
            with self.subTest(header=header):
                self.write_note('notes/nested.md', f'---\n{header}\n---\nZirkon NESTED_CANARY.\n')
                report = self.engine.sync()
                self.assertEqual(report['status'], 'degraded', report)
                self.assertEqual(report['indexed'], 0)
                self.assertEqual(self.records(audience='internal'), [])

    def test_note_create_normalizes_valid_casing_and_rejects_invalid_or_conflicting(self):
        res1 = self.engine.note_create('notes/created-cap.md', 'Zirkon CREATED_CAP_CANARY.', {'Visibility': 'Private'})
        self.assertEqual(res1['status'], 'succeeded')
        res2 = self.engine.note_create('notes/created-alias.md', 'Zirkon CREATED_ALIAS_CANARY.', {'private': True})
        self.assertEqual(res2['status'], 'succeeded')
        res3 = self.engine.note_create('notes/created-tag.md', 'Zirkon CREATED_TAG_CANARY.', {'tags': ['private']})
        self.assertEqual(res3['status'], 'succeeded')

        self.assertEqual(self.records(audience='internal'), [])
        priv = {r['source']: r for r in self.records(audience='private')}
        self.assertEqual(set(priv), {'notes/created-cap.md', 'notes/created-alias.md', 'notes/created-tag.md'})
        for rec in priv.values():
            self.assertEqual(rec['visibility'], 'private')

        for bad_meta in (
            {'visibility': 'secret'},
            {'Visibility': 'secret'},
            {'visibility': None},
            {'visibility': ['private']},
            {'visibility': 'internal', 'Visibility': 'private'},
        ):
            with self.subTest(bad_meta=bad_meta):
                with self.assertRaisesRegex(ValueError, 'invalid visibility'):
                    self.engine.note_create('notes/rejected.md', 'Body.', bad_meta)
                self.assertFalse((self.vault / 'notes/rejected.md').exists())

    def test_note_edit_on_alias_private_note_succeeds_and_stays_private(self):
        self.write_note('notes/personal.md', '---\nprivate: true\n---\nZirkon original body.\n')
        self.engine.sync()
        sha = self.engine.note_read('notes/personal.md')['sha256']
        res = self.engine.note_edit({
            'source': 'notes/personal.md',
            'expected_sha256': sha,
            'edits': [{'old': 'original body', 'new': 'updated body'}],
        })
        self.assertEqual(res['status'], 'written')
        self.assertEqual(self.records(audience='internal'), [])
        priv = self.records(audience='private')
        self.assertEqual(len(priv), 1)
        self.assertIn('updated body', priv[0]['text'])

    def test_projections_bridge_jev_and_readers_stay_consistent_including_stale_rows(self):
        self.write_note('notes/open.md', 'Zirkon open control.\n')
        self.write_note('notes/cap.md', '---\nVisibility: private\n---\nZirkon CAP_CROSS_CANARY.\n')
        self.write_note('notes/alias.md', '---\nprivate: true\n---\nZirkon ALIAS_CROSS_CANARY.\n')
        self.write_note('notes/tag.md', '---\ntags: [gizli]\n---\nZirkon TAG_CROSS_CANARY.\n')
        self.write_note('notes/bad.md', '---\nVisibility: secret\n---\nZirkon BAD_CROSS_CANARY.\n')
        self.engine.sync()

        with self.engine.store._connect() as db:
            hidden = beyin_v3_projections._hidden_ref_sources(
                db, ['notes/open.md', 'notes/cap.md', 'notes/alias.md', 'notes/tag.md', 'notes/bad.md']
            )
        self.assertEqual(hidden, {'notes/cap.md', 'notes/alias.md', 'notes/tag.md', 'notes/bad.md'})

        # Direct markdown readers (triage, audit, lint) also withhold all variants.
        for rel in ('notes/cap.md', 'notes/alias.md', 'notes/tag.md', 'notes/bad.md'):
            path = self.vault / rel
            with self.subTest(reader='triage', rel=rel):
                self.assertFalse(beyin_v3_triage._scan_allowed(path, self.vault, frozenset()))
            with self.subTest(reader='audit', rel=rel):
                self.assertIsNone(beyin_v3_audit._auditable(path, rel))
            with self.subTest(reader='lint', rel=rel):
                self.assertIsNone(beyin_v3_lint._note_metadata(self.vault, path))

        # Stale pre-upgrade SQLite rows (indexed before fix with visibility='internal' + Visibility='private'/alias)
        # must still be blocked by bridge, jev, projections, and store._eligible before sync runs!
        stale_rows = [
            {'id': 'stale-cap', 'source': 'notes/open.md', 'text': 'Zirkon STALE_CAP', 'Visibility': 'private', 'visibility': 'internal'},
            {'id': 'stale-alias', 'source': 'notes/open.md', 'text': 'Zirkon STALE_ALIAS', 'private': True, 'visibility': 'internal'},
            {'id': 'stale-tag', 'source': 'notes/open.md', 'text': 'Zirkon STALE_TAG', 'tags': ['private'], 'visibility': 'internal'},
        ]
        for row in stale_rows:
            with self.subTest(stale_id=row['id']):
                self.assertFalse(beyin_v3_bridge._visible(row))
                self.assertFalse(beyin_v3_jev.remote_allowed(row))

        # Inject a stale pre-upgrade row directly into SQLite and verify _hidden_ref_sources + retrieve block it.
        open_sha = beyin_v3_sync._hash((self.vault / 'notes/open.md').read_bytes())
        stale_payload = json.dumps({
            'id': 'stale-db-row', 'kind': 'note', 'source': 'notes/open.md',
            'source_sha256': open_sha, 'revision': 1, 'text': 'Zirkon STALE_DB_CANARY',
            'Visibility': 'private', 'visibility': 'internal', 'facts': {}, 'supersedes': [],
        })
        with closing(sqlite3.connect(self.state / 'memory.sqlite3')) as db:
            db.execute("INSERT OR REPLACE INTO records VALUES ('stale-db-row', ?)", (stale_payload,))
            db.execute("INSERT OR REPLACE INTO markdown_sources VALUES ('stale-db-row', 'notes/stale-virtual.md')")
            # Test _hidden_ref_sources on the stale row too.
            db.execute("INSERT OR REPLACE INTO records VALUES ('stale-ref', ?)", (stale_payload,))
            db.execute("INSERT OR REPLACE INTO markdown_sources VALUES ('stale-ref', 'notes/stale-ref.md')")
            db.commit()
            hidden_stale = beyin_v3_projections._hidden_ref_sources(db, ['notes/stale-ref.md'])
        self.assertIn('notes/stale-ref.md', hidden_stale)
        retrieved_ids = {r['id'] for r in self.records(audience='internal')}
        self.assertNotIn('stale-db-row', retrieved_ids)

    def test_human_sync_output_mentions_privacy_frontmatter_warnings(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('beyin_entry_privacy_test', ROOT / 'scripts/beyin_entry.py')
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        self.write_note('notes/alias.md', '---\nprivate: true\n---\nZirkon human sync test.\n')
        text = entry.human_result(self.engine.sync(), 'sync')
        self.assertIn('notes/alias.md', text)
        self.assertIn('visibility: private', text)

    def test_cli_sync_and_context_repro_from_issue_963(self):
        self.write_note('notes/a.md', '---\nVisibility: private\n---\nZirkon ISSUE963_A_CANARY.\n')
        self.write_note('notes/b.md', '---\nvisibility: Private\n---\nZirkon ISSUE963_B_CANARY.\n')
        self.write_note('notes/c.md', '---\nprivate: true\n---\nZirkon ISSUE963_C_CANARY.\n')
        self.write_note('notes/d.md', '---\ngizli: true\n---\nZirkon ISSUE963_D_CANARY.\n')
        self.write_note('notes/e.md', '---\ntags: [private]\n---\nZirkon ISSUE963_E_CANARY.\n')
        self.write_note('notes/open.md', 'Zirkon ISSUE963_OPEN_CONTROL.\n')

        cli = str(ROOT / 'scripts/beyin_v3.py')
        sync_proc = subprocess.run(
            [sys.executable, cli, '--vault', str(self.vault), '--state', str(self.state), 'sync'],
            capture_output=True, text=True, encoding='utf-8', timeout=60,
        )
        self.assertEqual(sync_proc.returncode, 0, sync_proc.stderr)
        sync_out = json.loads(sync_proc.stdout)
        self.assertEqual(sync_out['status'], 'succeeded', sync_out)
        self.assertEqual(sync_out['indexed'], 6)
        self.assertEqual({w['source'] for w in sync_out['warnings']}, {'notes/c.md', 'notes/d.md', 'notes/e.md'})

        ctx_proc = subprocess.run(
            [sys.executable, cli, '--vault', str(self.vault), '--state', str(self.state), 'context', 'Zirkon'],
            capture_output=True, text=True, encoding='utf-8', timeout=60,
        )
        self.assertEqual(ctx_proc.returncode, 0, ctx_proc.stderr)
        ctx_out = json.loads(ctx_proc.stdout)
        self.assertEqual([r['source'] for r in ctx_out['records']], ['notes/open.md'])
        for canary in ('ISSUE963_A_CANARY', 'ISSUE963_B_CANARY', 'ISSUE963_C_CANARY', 'ISSUE963_D_CANARY', 'ISSUE963_E_CANARY'):
            self.assertNotIn(canary, ctx_proc.stdout)


if __name__ == '__main__':
    unittest.main()
