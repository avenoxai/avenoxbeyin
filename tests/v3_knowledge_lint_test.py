"""Real filesystem regressions for the selected #448 navigation capability."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_lint as lint
from beyin_v3_sync import SyncEngine
from beyin_v3_notes import NoteConflict
SPEC = importlib.util.spec_from_file_location('knowledge_cli', ROOT / 'scripts/beyin_v3.py')
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class KnowledgeLintTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name); self.vault = self.base / 'vault'; self.vault.mkdir()
        self.state = self.base / 'state'

    def note(self, name, text):
        path = self.vault / name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8'); return path

    def test_relative_qualified_ambiguous_and_missing_heading_embed(self):
        self.note('a/note.md', '# Alpha')
        self.note('b/note.md', '# Present\nblock ^yes')
        self.note('b/sub/source.md', '[[../note#Present]] [[missing/note]] [[#Missing]] ![[missing]] [[../note#^yes]] [[../note#Absent]]')
        index = lint.build_vault_index(self.vault)
        self.assertEqual(lint.resolve_wikilink(self.vault, '../note', 'b/sub/source.md', index)['resolved_path'], 'b/note.md')
        self.assertFalse(lint.resolve_wikilink(self.vault, 'missing/note', vault_index=index)['exists'])
        self.assertTrue(lint.resolve_wikilink(self.vault, 'note', vault_index=index)['ambiguous'])
        report = lint.lint_vault(self.vault)
        self.assertEqual(report['broken_links_count'], 4)
        self.assertEqual(sum(b['type'] == 'heading' for b in report['broken_links']), 2)

    def test_casefold_path_collision_fails_closed(self):
        first = self.note('knowledge/Note.md', '# One')
        second = self.note('knowledge/note.md', '# Two')
        if first.samefile(second):
            self.skipTest('case-insensitive filesystem')
        index = lint.build_vault_index(self.vault)
        for target in ('NOTE', 'knowledge/NOTE'):
            result = lint.resolve_wikilink(self.vault, target, vault_index=index)
            self.assertFalse(result['exists']); self.assertTrue(result['ambiguous'])

    def test_folder_report_uses_full_graph_and_media_relative_to_source(self):
        self.note('outside.md', '[[knowledge/a]]')
        self.note('knowledge/a.md', '![[images/photo.png]]')
        media = self.vault / 'knowledge/images/photo.png'; media.parent.mkdir(); media.write_bytes(b'image')
        result = lint.lint_vault(self.vault, 'knowledge')
        self.assertEqual(result['orphans'], []); self.assertEqual(result['broken_links'], [])

    def test_private_untrusted_sensitive_archive_generated_and_user_exclusions(self):
        for folder in ('secrets', 'Arşiv', 'node_modules', '.codex', 'vendor'):
            self.note(folder + '/canary.md', '# CANARY')
        for name, header in [('private', 'visibility: private'), ('untrusted', 'trust: untrusted'), ('generated', 'generated_by: receipt'), ('rejected', 'kind: inference\nvalidity: rejected')]:
            self.note('knowledge/' + name + '.md', '---\n' + header + '\n---\n# CANARY')
        self.note('knowledge/public.md', '# Visible')
        self.state.mkdir(); (self.state / 'index.json').write_text(json.dumps({'excluded_dirs': ['vendor']}))
        report = lint.plan_moc(self.vault, state=self.state)
        self.assertNotIn('CANARY', json.dumps(report)); self.assertEqual(report['indexed_notes_count'], 1)
        self.assertEqual(lint.lint_vault(self.vault, state=self.state)['scanned_files_count'], 1)

    def test_all_trust_forms_private_destination_and_sensitive_filename_are_hidden(self):
        for field, value in [('trusted', 'false'), ('status', 'untrusted'), ('kind', 'untrusted'), ('visibility', 'invalid')]:
            self.note('knowledge/' + field + '.md', '---\n' + field + ': ' + value + '\n---\n# CANARY')
        self.note('secret.md', '# CANARY')
        self.note('passwords.txt', 'CANARY')
        self.assertEqual(lint.build_vault_index(self.vault)['by_rel'], {})
        self.assertEqual(lint.build_vault_index(self.vault)['media'], {})
        for header in ('visibility: private', 'trust: untrusted', 'trusted: false', 'status: untrusted'):
            self.note('knowledge/MOC.md', '---\n' + header + '\n---\n# CANARY')
            with self.assertRaisesRegex(ValueError, 'excluded MOC destination'):
                lint.plan_moc(self.vault)
        self.note('secrets/n.md', '# CANARY')
        with self.assertRaisesRegex(ValueError, 'excluded target folder'):
            lint.lint_vault(self.vault, 'secrets')

    def test_moc_traversal_and_symlinks_do_not_touch_outside(self):
        self.note('knowledge/a.md', '# Public')
        victim = self.base / 'outside.md'; victim.write_text('KEEP')
        for filename in ('../../outside.md', '/outside.md', '..\\outside.md'):
            with self.assertRaises(ValueError): lint.plan_moc(self.vault, moc_filename=filename)
        try: (self.vault / 'knowledge/MOC.md').symlink_to(victim)
        except (OSError, NotImplementedError): self.skipTest('symlinks unavailable')
        with self.assertRaises(ValueError): lint.plan_moc(self.vault)
        self.assertEqual(victim.read_text(), 'KEEP')
        (self.vault / 'knowledge/MOC.md').unlink()
        (self.vault / 'knowledge/outside').symlink_to(self.base, target_is_directory=True)
        self.assertEqual(lint.plan_moc(self.vault)['indexed_notes_count'], 1)

    def test_moc_shared_writer_preserves_bytes_and_detects_interleaving(self):
        self.note('knowledge/a.md', '# C:\\Users\\Hakan')
        moc = self.note('knowledge/MOC.md', 'unused')
        original = b'\xef\xbb\xbf# Manual\r\n' + lint.MOC_START_MARKER.encode() + b'\r\nold\r\n' + lint.MOC_END_MARKER.encode() + b'\r\nFooter\r\n'
        moc.write_bytes(original)
        engine = SyncEngine(self.vault, self.state)
        plan = lint.plan_moc(self.vault, state=self.state)
        moc.write_bytes(original + b'Concurrent edit\r\n')
        with self.assertRaises(NoteConflict): engine.note_edit(plan['request'])
        result = lint.generate_moc(engine)
        self.assertEqual(result['status'], 'written')
        after = moc.read_bytes()
        self.assertTrue(after.startswith(b'\xef\xbb\xbf# Manual\r\n'))
        self.assertTrue(after.endswith(b'Footer\r\nConcurrent edit\r\n'))
        self.assertIn(b'C:\\Users\\Hakan', after)
        self.assertEqual(lint.generate_moc(engine)['status'], 'unchanged')

    def test_cli_read_only_does_not_create_state_or_call_writer(self):
        self.note('knowledge/a.md', '# Public')
        before = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        for args in (['lint'], ['moc', '--dry-run']):
            with patch.object(Path, 'mkdir', side_effect=AssertionError('write attempted')):
                result, code = cli.main(['--vault', str(self.vault), '--state', str(self.state)] + args, return_result=True)
            self.assertEqual(code, 0, result)
        after = {p.relative_to(self.base): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}
        self.assertEqual(before, after); self.assertFalse(self.state.exists())

    def test_cli_active_workspace_is_typed_denial_before_vault_scan(self):
        self.note('knowledge/a.md', '# CANARY')
        self.state.mkdir(); (self.state / 'workspace-runtime.json').write_text('{"enabled":true}')
        for args in (['lint'], ['moc', '--dry-run'], ['moc']):
            with patch.object(lint, 'build_vault_index', side_effect=AssertionError('scope bypass')):
                result, code = cli.main(['--vault', str(self.vault), '--state', str(self.state)] + args, return_result=True)
            self.assertEqual(code, 4, result)
            self.assertEqual(result['data']['operation_status'], 'denied')
            self.assertNotIn('CANARY', json.dumps(result))

    def test_source_cli_read_only_import_does_not_emit_bytecode(self):
        self.note('knowledge/a.md', '# Public')
        program = "import sys,__future__;sys.pycache_prefix=sys.argv[1];sys.argv=sys.argv[2:];exec(compile(open(sys.argv[0],encoding='utf-8').read(),sys.argv[0],'exec'),{'__name__':'__main__','__file__':sys.argv[0]})"
        cache = self.base / 'cache'
        process = subprocess.run([sys.executable, '-c', program, str(cache), str(ROOT / 'scripts/beyin_v3.py'), '--vault', str(self.vault), '--state', str(self.state), 'lint'], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(list(cache.rglob('*.pyc')), []); self.assertFalse(self.state.exists())


if __name__ == '__main__': unittest.main()
