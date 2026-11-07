"""Tests for Obsidian-Native Knowledge Organization & Lint (RFC #368 P0-E)."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "template/.claude/scripts"))

import beyin_v3 as runtime
import beyin_v3_lint as linter
from v3_package_helpers import inherited_env

CLI_SPEC = importlib.util.spec_from_file_location("beyin_cli", ROOT / "scripts/beyin_v3.py")
cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(cli)


class KnowledgeLintTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.state = Path(self.tmp.name) / "state"
        self.state.mkdir()

    def _owned_moc(self, extra=''):
        (self.vault / 'knowledge').mkdir(exist_ok=True)
        (self.vault / 'knowledge/a.md').write_text('# A', encoding='utf-8')
        target = self.vault / 'knowledge/MOC.md'
        target.write_text('Handwritten text\n' + linter.MOC_START_MARKER + '\nold\n' + linter.MOC_END_MARKER + '\n' + extra,
                          encoding='utf-8')
        return target

    def test_moc_refuses_concurrent_edit_after_snapshot(self):
        # Obsidian does not take the vault lock: it saves after the snapshot, while the
        # replacement is being prepared. The last check before the rename must see it.
        target = self._owned_moc()
        original_mkstemp = tempfile.mkstemp
        def editor_saves(*args, **kwargs):
            with open(target, 'a', encoding='utf-8') as handle:
                handle.write('Concurrent user text\n')
            return original_mkstemp(*args, **kwargs)
        with patch.object(tempfile, 'mkstemp', editor_saves):
            with self.assertRaisesRegex(ValueError, 'changed'):
                linter.generate_moc(self.vault)
        text = target.read_text(encoding='utf-8')
        self.assertIn('Concurrent user text', text)
        self.assertIn('\nold\n', text)
        self.assertEqual([p.name for p in target.parent.iterdir() if p.name.startswith('.beyin')], [])

    @unittest.skipIf(os.name == 'nt', 'Windows cannot rename over an open file; only the pre-rename check applies')
    def test_moc_restores_in_place_save_inside_replace_window(self):
        target = self._owned_moc()
        original_replace = os.replace
        def editor_saves_then_rename(source, destination, *args, **kwargs):
            if Path(destination).resolve() == target.resolve():
                with open(target, 'a', encoding='utf-8') as handle:
                    handle.write('Window edit\n')
            return original_replace(source, destination, *args, **kwargs)
        with patch.object(os, 'replace', editor_saves_then_rename):
            with self.assertRaisesRegex(ValueError, 'restored'):
                linter.generate_moc(self.vault)
        self.assertIn('Window edit', target.read_text(encoding='utf-8'))
        self.assertEqual([p.name for p in target.parent.iterdir() if p.name.startswith('.beyin')], [])

    def test_moc_never_rewrites_a_file_without_one_marker_pair(self):
        (self.vault / 'knowledge').mkdir()
        (self.vault / 'knowledge/a.md').write_text('# A', encoding='utf-8')
        target = self.vault / 'knowledge/MOC.md'
        start, end = linter.MOC_START_MARKER, linter.MOC_END_MARKER
        for text in ('# Elle yazılmış harita\n- [[a]]\n',
                     f'{start}\nx\n{end}\n{start}\ny\n{end}\n',
                     f'{end}\nters\n{start}\n'):
            with self.subTest(text=text):
                target.write_text(text, encoding='utf-8')
                before = target.read_bytes()
                with self.assertRaisesRegex(ValueError, 'pair'):
                    linter.generate_moc(self.vault)
                self.assertEqual(target.read_bytes(), before)

    def test_moc_without_changes_does_not_touch_the_file(self):
        target = self._owned_moc()
        self.assertEqual(linter.generate_moc(self.vault)['status'], 'updated')
        before = os.stat(target)
        self.assertEqual(linter.generate_moc(self.vault)['status'], 'unchanged')
        after = os.stat(target)
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_moc_keeps_bom_and_crlf_outside_the_block(self):
        target = self._owned_moc()
        head = '\ufeff# Başlık\r\nKullanıcı metni\r\n'.encode('utf-8')
        tail = '\r\nSon satır\r\n'.encode('utf-8')
        block = (linter.MOC_START_MARKER + '\r\nold\r\n' + linter.MOC_END_MARKER).encode('utf-8')
        target.write_bytes(head + block + tail)
        linter.generate_moc(self.vault)
        data = target.read_bytes()
        self.assertTrue(data.startswith(head))
        self.assertTrue(data.endswith(tail))
        self.assertIn(b'knowledge/a.md', data)
        self.assertNotIn(b'\n', data.replace(b'\r\n', b''))

    def test_moc_file_is_a_plain_markdown_name_in_a_regular_folder(self):
        (self.vault / 'knowledge').mkdir()
        (self.vault / 'projects/app').mkdir(parents=True)
        rules = self.vault / 'projects/app/Kurallar.md'
        rules.write_text('# Kurallar\n', encoding='utf-8')
        for name in ('../projects/app/Kurallar.md', 'sub/MOC.md', 'MOC.txt', '.MOC.md', ' MOC.md'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    linter.generate_moc(self.vault, 'knowledge', name)
        (self.vault / '.claude').mkdir()
        with self.assertRaises(ValueError):
            linter.generate_moc(self.vault, '.claude')
        self.assertEqual(rules.read_text(encoding='utf-8'), '# Kurallar\n')
        self.assertFalse((self.vault / '.claude/MOC.md').exists())

    def test_moc_link_titles_cannot_break_wikilinks(self):
        (self.vault / 'knowledge').mkdir()
        (self.vault / 'knowledge/plan.md').write_text('# [Taslak] Plan | v2 ]]\n', encoding='utf-8')
        linter.generate_moc(self.vault)
        links = linter.extract_wikilinks((self.vault / 'knowledge/MOC.md').read_text(encoding='utf-8'))
        self.assertEqual([(link['file_target'], link['display']) for link in links],
                         [('knowledge/plan.md', 'Taslak Plan v2')])

    def test_heading_link_needs_the_heading_on_one_line(self):
        (self.vault / 'b.md').write_text('#\nMissing\n## Var Olan\n', encoding='utf-8')
        (self.vault / 'a.md').write_text('[[b#Missing]] [[b#Var Olan]]', encoding='utf-8')
        result = linter.lint_vault(self.vault)
        self.assertEqual([link['raw'] for link in result['broken_links']], ['[[b#Missing]]'])

    def test_turkish_uppercase_archive_folder_is_skipped(self):
        for source in ('knowledge/ARŞİV/eski.md', 'knowledge/Arşiv/eski2.md', 'knowledge/güncel.md'):
            path = self.vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# ' + Path(source).stem, encoding='utf-8')
        linter.generate_moc(self.vault)
        text = (self.vault / 'knowledge/MOC.md').read_text(encoding='utf-8')
        self.assertIn('güncel', text)
        self.assertNotIn('eski', text)

    def test_wikilink_scan_stays_linear_on_adversarial_lines(self):
        import time
        for line in ('[[' + 'a' * 200000, '[[a' * 70000, '[[' + 'a|' * 100000, '![[' * 70000):
            started = time.monotonic()
            linter.extract_wikilinks(line + '\n' + line)
            self.assertLess(time.monotonic() - started, 5)

    def test_moc_omits_sensitive_archived_and_private_notes(self):
        for source, text in [('knowledge/public.md', '# Public'),
                             ('knowledge/secrets/secret.md', '# SecretCanary'),
                             ('knowledge/Arşiv/old.md', '# ArchiveCanary'),
                             ('knowledge/private-note.md', '---\nvisibility: private\n---\n# PrivateCanary')]:
            path = self.vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        linter.generate_moc(self.vault)
        result = (self.vault / 'knowledge/MOC.md').read_text()
        self.assertIn('Public', result)
        self.assertNotIn('Canary', result)

    def _write_notes(self, vault, notes):
        for source, text in notes.items():
            path = vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')

    def test_moc_never_copies_another_projects_titles_into_a_scoped_context(self):
        # Nested scopes: a MOC over knowledge/ must not carry knowledge/acme titles into a file
        # that another project's explicit context query receives (with and without shared_unscoped).
        notes = {'knowledge/acme/teklif.md': '---\ntitle: Acme birleşme teklifi 42 milyon\n---\nAcme birleşme teklifi gizli.\n',
                 'knowledge/beta/plan.md': '# Beta plan\nBeta birleşme teklifi planı.\n',
                 'knowledge/genel/g.md': '# Genel birleşme teklifi rehberi\nGenel birleşme teklifi notu.\n'}
        cases = (({'knowledge/acme': 'acme', 'knowledge/beta': 'beta'}, True, 'beta', 2,
                  ['knowledge/genel/g.md']),
                 ({'knowledge': 'general', 'knowledge/acme': 'acme'}, False, 'general', 1,
                  ['knowledge/beta/plan.md', 'knowledge/genel/g.md']))
        for index, (folders, shared, reader, other, listed) in enumerate(cases):
            with self.subTest(shared=shared):
                vault, state = Path(self.tmp.name) / f'scoped{index}', Path(self.tmp.name) / f'state{index}'
                vault.mkdir()
                state.mkdir()
                self._write_notes(vault, notes)
                (vault / runtime.PROJECT_SCOPES_FILE).write_text(
                    json.dumps({'folders': folders, 'shared_unscoped': shared}), encoding='utf-8')
                result = linter.generate_moc(vault)
                text = (vault / 'knowledge/MOC.md').read_text(encoding='utf-8')
                self.assertNotIn('Acme', text)
                self.assertEqual(sorted(link['file_target'] for link in linter.extract_wikilinks(text)), listed)
                self.assertEqual((result['status'], result['skipped']['other_scope']), ('created', other))
                # The acme MOC lists acme's own note and belongs to acme's scope only.
                linter.generate_moc(vault, 'knowledge/acme')
                self.assertIn('Acme birleşme teklifi 42 milyon',
                              (vault / 'knowledge/acme/MOC.md').read_text(encoding='utf-8'))
                answer, code = cli.main(['--vault', str(vault), '--state', str(state), 'context',
                                         '--project', reader, 'Acme birleşme teklifi'], return_result=True)
                self.assertEqual(code, 0, answer)
                self.assertTrue(answer['records'])
                self.assertNotIn('Acme birleşme', json.dumps(answer, ensure_ascii=False))

    def test_moc_follows_project_fields_visibility_and_trust(self):
        long_title = 'Uzun başlık ' * 40
        self._write_notes(self.vault, {
            'knowledge/a.md': '---\nproject: acme\n---\n# AcmeCanary\n',
            'knowledge/b.md': '# Ortak not\n',
            'knowledge/c.md': '---\nvisibility: public\n---\n# Açık not\n',
            'knowledge/d.md': '---\ntrust: untrusted\n---\n# UntrustedCanary\n',
            'knowledge/e.md': '---\nkind: preference\nstatus: rejected\n---\n# RejectedCanary\n',
            'knowledge/f.md': f'# {long_title}\n'})
        # No scope file: a project field alone decides, exactly as retrieval does.
        result = linter.generate_moc(self.vault)
        text = (self.vault / 'knowledge/MOC.md').read_text(encoding='utf-8')
        self.assertIn('Ortak not', text)
        self.assertIn('Açık not', text)
        self.assertNotIn('Canary', text)
        self.assertEqual(result['skipped'], {'other_scope': 1, 'narrower_visibility': 0, 'untrusted': 2})
        self.assertTrue(all(len(link['display']) <= linter.MAX_MOC_TITLE_CHARS + 1
                            for link in linter.extract_wikilinks(text)))
        start, end = linter.MOC_START_MARKER, linter.MOC_END_MARKER
        # A public MOC lists only public notes; an acme MOC lists only acme's notes.
        (self.vault / 'knowledge/Herkese.md').write_text(f'---\nvisibility: public\n---\n{start}\n{end}\n', encoding='utf-8')
        linter.generate_moc(self.vault, 'knowledge', 'Herkese.md')
        text = (self.vault / 'knowledge/Herkese.md').read_text(encoding='utf-8')
        self.assertIn('Açık not', text)
        self.assertNotIn('Ortak not', text)
        (self.vault / 'knowledge/Acme.md').write_text(f'---\nproject: acme\n---\n{start}\n{end}\n', encoding='utf-8')
        result = linter.generate_moc(self.vault, 'knowledge', 'Acme.md')
        text = (self.vault / 'knowledge/Acme.md').read_text(encoding='utf-8')
        self.assertEqual(result['moc_scope'], 'acme')
        self.assertIn('AcmeCanary', text)
        self.assertNotIn('Ortak not', text)

    def test_relative_links_and_missing_qualified_paths(self):
        for source in ['a/note.md', 'b/note.md', 'b/sub/source.md']:
            path = self.vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# Present', encoding='utf-8')
        result = linter.resolve_wikilink(self.vault, '../note', source_file='b/sub/source.md')
        self.assertEqual(result['resolved_path'], 'b/note.md')
        self.assertFalse(linter.resolve_wikilink(self.vault, 'missing/note')['exists'])

    def test_root_relative_and_partial_path_links_resolve_like_obsidian(self):
        for source, text in [('note.md', '# N'), ('a.md', '[[./note]]'),
                             ('knowledge/kavramlar/temel.md', '# T'), ('p/x.md', '[[kavramlar/temel]]'),
                             ('b/sub/source.md', '[[./note]] [[missing/temel]]')]:
            path = self.vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        result = linter.lint_vault(self.vault)
        self.assertEqual(sorted((link['source'], link['raw']) for link in result['broken_links']),
                         [('b/sub/source.md', '[[./note]]'), ('b/sub/source.md', '[[missing/temel]]')])
        self.assertNotIn('a.md', result['orphans'])
        self.assertNotIn('knowledge/kavramlar/temel.md', result['orphans'])

    def test_attachments_resolve_by_name_and_dotted_note_names_stay_notes(self):
        for source, text in [('notlar/rapor.md', '![[diyagram.png]] [[belge.pdf]] ![[yok.png]] [[Sürüm v1.2]] '
                                                  '![[../ekler/diyagram.png]] [[diagram.png]]'),
                             ('notlar/Sürüm v1.2.md', '# Sürüm'), ('notlar/Sürüm v1.md', '# Eski'),
                             ('diagram.md', '# Not, resim değil')]:
            path = self.vault / source
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        for attachment in ('ekler/diyagram.png', 'dosyalar/belge.pdf', 'gizli/diagram.png'):
            path = self.vault / attachment
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'\x89PNG')
        result = linter.lint_vault(self.vault)
        self.assertEqual(sorted(link['raw'] for link in result['broken_links']),
                         ['![[yok.png]]', '[[diagram.png]]'])
        self.assertNotIn('notlar/Sürüm v1.2.md', result['orphans'])
        self.assertIn('notlar/Sürüm v1.md', result['orphans'])
        self.assertIn('diagram.md', result['orphans'])

    def test_lint_folder_must_exist_inside_the_vault(self):
        (self.vault / 'knowledge').mkdir()
        for folder in ('knowlege', '..', '../elsewhere'):
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(ValueError, 'not found'):
                    linter.lint_vault(self.vault, folder)
        self.assertEqual(linter.lint_vault(self.vault, 'knowledge/')['scanned_files_count'], 0)
        (self.vault / 'knowledge/a.md').write_text('# A', encoding='utf-8')
        self.assertEqual(linter.lint_vault(self.vault, 'knowledge/../knowledge')['scanned_files_count'], 1)

    def test_links_and_headings_inside_code_are_ignored(self):
        (self.vault / 'script.md').write_text('# Kurulum\n```bash\n# Install\nif [[ -f x ]]; then echo; fi\n```\n',
                                              encoding='utf-8')
        (self.vault / 'a.md').write_text('Kod `[[ -n "$x" ]]` ve [[script#Kurulum]] ama [[script#Install]]\n'
                                         '~~~\n[[yok]]\n~~~\n', encoding='utf-8')
        result = linter.lint_vault(self.vault)
        self.assertEqual([link['raw'] for link in result['broken_links']], ['[[script#Install]]'])
        self.assertEqual(result['total_links_count'], 2)

    def test_folder_lint_counts_external_backlinks_and_missing_embeds(self):
        (self.vault / 'knowledge').mkdir()
        (self.vault / 'knowledge/a.md').write_text('# Present\n![[missing]]\n[[#Absent]]', encoding='utf-8')
        (self.vault / 'outside.md').write_text('[[knowledge/a]]', encoding='utf-8')
        result = linter.lint_vault(self.vault, 'knowledge')
        self.assertEqual(result['broken_links_count'], 2)
        self.assertNotIn('knowledge/a.md', result['orphans'])

    def test_moc_rejects_escape_and_preserves_external_file(self):
        (self.vault / 'knowledge').mkdir()
        outside = self.vault.parent / 'outside.md'
        outside.write_text('User text', encoding='utf-8')
        with self.assertRaises(ValueError):
            linter.generate_moc(self.vault, 'knowledge', '../../outside.md')
        self.assertEqual(outside.read_text(), 'User text')

    def test_moc_update_preserves_backslashes_in_titles(self):
        (self.vault / 'knowledge').mkdir()
        (self.vault / 'knowledge/note.md').write_text('# C:\\Users\\Synthetic\n', encoding='utf-8')
        linter.generate_moc(self.vault)
        before = (self.vault / 'knowledge/MOC.md').read_text()
        linter.generate_moc(self.vault)
        self.assertEqual((self.vault / 'knowledge/MOC.md').read_text(), before)

    def test_extract_wikilinks(self):
        sample_text = """
# System Overview
This note links to [[Architecture]] and also [[Architecture|System Architecture]].
Here is an anchor link [[Architecture#Database]] and [[Architecture#Database|DB Schema]].
Here is an internal heading link: [[#Summary]].
Here is an embedded image: ![[diagram.png]].
And another link on line 8: [[Components/Auth]].
"""
        links = linter.extract_wikilinks(sample_text)
        self.assertEqual(len(links), 7)

        # 1. [[Architecture]]
        self.assertEqual(links[0]["file_target"], "Architecture")
        self.assertIsNone(links[0]["display"])
        self.assertIsNone(links[0]["heading"])
        self.assertFalse(links[0]["is_embed"])
        self.assertEqual(links[0]["line"], 3)

        # 2. [[Architecture|System Architecture]]
        self.assertEqual(links[1]["file_target"], "Architecture")
        self.assertEqual(links[1]["display"], "System Architecture")

        # 3. [[Architecture#Database]]
        self.assertEqual(links[2]["file_target"], "Architecture")
        self.assertEqual(links[2]["heading"], "Database")

        # 4. [[Architecture#Database|DB Schema]]
        self.assertEqual(links[3]["file_target"], "Architecture")
        self.assertEqual(links[3]["heading"], "Database")
        self.assertEqual(links[3]["display"], "DB Schema")

        # 5. [[#Summary]]
        self.assertEqual(links[4]["file_target"], "")
        self.assertEqual(links[4]["heading"], "Summary")

        # 6. ![[diagram.png]]
        self.assertEqual(links[5]["file_target"], "diagram.png")
        self.assertTrue(links[5]["is_embed"])

        # 7. [[Components/Auth]]
        self.assertEqual(links[6]["file_target"], "Components/Auth")
        self.assertEqual(links[6]["line"], 7)

    def test_resolve_wikilink(self):
        # Setup notes
        (self.vault / "knowledge/concepts").mkdir(parents=True)
        (self.vault / "knowledge/concepts/clean-code.md").write_text("# Clean Code", encoding="utf-8")
        (self.vault / "projects/app").mkdir(parents=True)
        (self.vault / "projects/app/main.md").write_text("# Main App", encoding="utf-8")
        (self.vault / "projects/app/local-note.md").write_text("# Local", encoding="utf-8")

        index = linter.build_vault_index(self.vault)

        # 1. Exact path
        res_exact = linter.resolve_wikilink(self.vault, "knowledge/concepts/clean-code.md", vault_index=index)
        self.assertTrue(res_exact["exists"])
        self.assertEqual(res_exact["resolved_path"], "knowledge/concepts/clean-code.md")

        # 2. Exact path without extension
        res_no_ext = linter.resolve_wikilink(self.vault, "knowledge/concepts/clean-code", vault_index=index)
        self.assertTrue(res_no_ext["exists"])
        self.assertEqual(res_no_ext["resolved_path"], "knowledge/concepts/clean-code.md")

        # 3. Parent-relative path
        res_rel = linter.resolve_wikilink(self.vault, "local-note", source_file="projects/app/main.md", vault_index=index)
        self.assertTrue(res_rel["exists"])
        self.assertEqual(res_rel["resolved_path"], "projects/app/local-note.md")

        # 4. Basename shortest path lookup
        res_basename = linter.resolve_wikilink(self.vault, "clean-code", vault_index=index)
        self.assertTrue(res_basename["exists"])
        self.assertEqual(res_basename["resolved_path"], "knowledge/concepts/clean-code.md")

        # 5. Nonexistent
        res_missing = linter.resolve_wikilink(self.vault, "nonexistent-doc", vault_index=index)
        self.assertFalse(res_missing["exists"])
        self.assertIsNone(res_missing["resolved_path"])

    def test_lint_vault_broken_links_and_orphans(self):
        # Setup notes
        (self.vault / "knowledge").mkdir(parents=True)
        (self.vault / "knowledge/note-a.md").write_text("# Note A\nLinks to [[note-b]].", encoding="utf-8")
        (self.vault / "knowledge/note-b.md").write_text("# Note B\nLinks to [[broken-target]].", encoding="utf-8")
        (self.vault / "knowledge/orphan.md").write_text("# Orphan Note\nNo links at all.", encoding="utf-8")
        (self.vault / "knowledge/unlinked.md").write_text("# Unlinked\nLinks to [[note-a]], but nobody links to it.", encoding="utf-8")

        report = linter.lint_vault(self.vault)
        self.assertEqual(report["status"], "issues_found")
        self.assertEqual(report["broken_links_count"], 1)
        self.assertEqual(report["broken_links"][0]["target"], "broken-target")
        self.assertEqual(report["broken_links"][0]["source"], "knowledge/note-b.md")
        self.assertEqual(report["broken_links"][0]["line"], 2)

        self.assertIn("knowledge/orphan.md", report["orphans"])
        self.assertIn("knowledge/unlinked.md", report["unlinked"])

    def test_lint_vault_clean_pass(self):
        (self.vault / "knowledge").mkdir(parents=True)
        (self.vault / "knowledge/alpha.md").write_text("# Alpha\nLinks to [[beta]].", encoding="utf-8")
        (self.vault / "knowledge/beta.md").write_text("# Beta\nLinks to [[alpha]].", encoding="utf-8")

        report = linter.lint_vault(self.vault)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["broken_links_count"], 0)
        self.assertEqual(report["orphan_count"], 0)

    def test_generate_moc_fresh_creation(self):
        (self.vault / "knowledge/concepts").mkdir(parents=True)
        (self.vault / "knowledge/concepts/solid.md").write_text("# SOLID Principles", encoding="utf-8")
        (self.vault / "knowledge/patterns").mkdir(parents=True)
        (self.vault / "knowledge/patterns/factory.md").write_text("---\ntitle: Factory Pattern\n---\n# Pattern Note", encoding="utf-8")

        res = linter.generate_moc(self.vault, "knowledge", "MOC.md")
        self.assertEqual(res["status"], "created")
        self.assertEqual(res["indexed_notes_count"], 2)

        moc_file = self.vault / "knowledge/MOC.md"
        self.assertTrue(moc_file.is_file())
        text = moc_file.read_text(encoding="utf-8")

        self.assertIn("# Knowledge — Map of Content (MOC)", text)
        self.assertIn("### Concepts", text)
        self.assertIn("[[knowledge/concepts/solid.md|SOLID Principles]]", text)
        self.assertIn("### Patterns", text)
        self.assertIn("[[knowledge/patterns/factory.md|Factory Pattern]]", text)
        self.assertIn(linter.MOC_START_MARKER, text)
        self.assertIn(linter.MOC_END_MARKER, text)

    def test_generate_moc_preserves_user_content(self):
        (self.vault / "projects/my-ws").mkdir(parents=True)
        (self.vault / "projects/my-ws/note1.md").write_text("# Module One", encoding="utf-8")

        moc_path = self.vault / "projects/my-ws/MOC.md"
        user_header = "# Handcrafted Project Dashboard\n\nCustom user guidance here.\n\n"
        user_footer = "\n\n## User Checklist\n- [x] Setup repository\n- [ ] Deploy"
        existing_moc = user_header + linter.MOC_START_MARKER + "\nold stale index\n" + linter.MOC_END_MARKER + user_footer
        moc_path.write_text(existing_moc, encoding="utf-8")

        # Run MOC update
        res = linter.generate_moc(self.vault, "projects/my-ws", "MOC.md")
        self.assertEqual(res["status"], "updated")

        new_text = moc_path.read_text(encoding="utf-8")
        self.assertTrue(new_text.startswith(user_header))
        self.assertTrue(new_text.endswith(user_footer))
        self.assertIn("[[projects/my-ws/note1.md|Module One]]", new_text)
        self.assertNotIn("old stale index", new_text)

    def test_cli_lint_and_moc_subcommands(self):
        (self.vault / "knowledge").mkdir(parents=True)
        (self.vault / "knowledge/doc1.md").write_text("# Doc 1\n[[doc2]]", encoding="utf-8")
        (self.vault / "knowledge/doc2.md").write_text("# Doc 2\n[[doc1]]", encoding="utf-8")

        # 1. Test CLI lint via cli.main
        res_lint, code_lint = cli.main(["--vault", str(self.vault), "--state", str(self.state), "lint"], return_result=True)
        self.assertEqual(code_lint, 0)
        self.assertEqual(res_lint["status"], "ok")
        self.assertEqual(res_lint["broken_links_count"], 0)

        # 2. Test CLI moc via subprocess
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/beyin_v3.py"),
                "--vault", str(self.vault),
                "--state", str(self.state),
                "moc",
                "--folder", "knowledge",
            ],
            capture_output=True,
            text=True,
            check=True,
            env=inherited_env(),
        )
        parsed = json.loads(proc.stdout)
        self.assertEqual(parsed["status"], "created")
        self.assertEqual(parsed["indexed_notes_count"], 2)
        self.assertTrue((self.vault / "knowledge/MOC.md").is_file())


if __name__ == "__main__":
    unittest.main()
