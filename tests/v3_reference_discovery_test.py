"""Discovery failures must not masquerade as a complete instruction-link report."""
import errno
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_package_helpers import ROOT, install, isolated_env, run_python, snapshot

SCRIPTS = ROOT / 'template/.claude/scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import beyin_v3_companion as companion
import beyin_v3_references as references


class ReferenceDiscoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-reference-discovery-')
        self.addCleanup(tmp.cleanup)
        self.vault = Path(tmp.name).resolve() / 'vault'
        self.vault.mkdir()

    def write(self, name, text='# instructions\n'):
        path = self.vault / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def symlink(self, target, name, directory=False):
        link = self.vault / name
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            link.symlink_to(target, target_is_directory=directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest('symlinks unavailable: ' + type(exc).__name__)
        return link

    def test_healthy_discovery_preserves_order_case_and_mirrors(self):
        self.write('AGENTS.md')
        self.write('CLAUDE.md', '# different instructions\n')
        self.write(companion.DEFAULT_DIRECTORY + '/Kurallar.md')
        for root in ('.agents', '.claude', '.kiro'):
            self.write(root + '/skills/demo/SKILL.md', '[[Missing]]\n')
        self.write('.agents/skills/demo/.hidden/reference.md')
        self.write('.agents/skills/demo/UPPER.MD')
        self.write('.agents/skills/demo/no.txt')
        self.write('.kiro/skills/demo/other.md')
        (self.vault / '.agents/skills/directory.md').mkdir()
        expected = ['AGENTS.md', 'CLAUDE.md', companion.DEFAULT_DIRECTORY + '/Kurallar.md',
                    '.agents/skills/demo/.hidden/reference.md', '.agents/skills/demo/SKILL.md']
        # pathlib's default glob case follows the platform, not Obsidian link matching.
        if Path('UPPER.MD').match('*.md'):
            expected.append('.agents/skills/demo/UPPER.MD')
        expected.append('.kiro/skills/demo/other.md')
        self.assertEqual([p.relative_to(self.vault).as_posix()
                          for p in references.instruction_files(self.vault)], expected)
        report = references.check(self.vault)
        self.assertEqual(report, {'checked_files': len(expected), 'dead_count': 1,
                                 'dead': [{'file': '.agents/skills/demo/SKILL.md',
                                           'line': 1, 'target': 'Missing'}], 'truncated': False})

    def test_root_and_file_symlinks_follow_but_nested_directory_symlinks_do_not(self):
        source = self.write('AGENTS.md')
        self.symlink(source, 'CLAUDE.md')
        target = self.vault.parent / 'skill-source'
        target.mkdir()
        (target / 'SKILL.md').write_text('# skill\n', encoding='utf-8')
        self.symlink(target, '.agents/skills', directory=True)
        self.symlink(source, '.claude/skills/file.md')
        self.symlink(target, '.claude/skills/nested', directory=True)
        self.symlink(self.vault / 'missing', '.kiro/skills/dangling.md')
        alias = self.symlink(self.vault, '../vault-alias', directory=True)
        self.assertEqual([p.relative_to(self.vault).as_posix()
                          for p in references.instruction_files(self.vault)],
                         ['AGENTS.md', '.agents/skills/SKILL.md'])
        self.assertEqual(references.check(alias), references.check(self.vault))
        index = references._note_index(self.vault)
        self.assertNotIn('skill.md', index)

    def test_note_index_keeps_hidden_files_prunes_hidden_folders_and_directory_symlinks(self):
        self.write('visible/.hidden.md')
        self.write('visible/.hidden-folder/Excluded.md')
        self.write('visible/Case Note.MD')
        outside = self.vault.parent / 'external'
        outside.mkdir()
        (outside / 'ExcludedAlso.md').write_text('# outside\n', encoding='utf-8')
        self.symlink(outside, 'linked-directory', directory=True)
        index = references._note_index(self.vault)
        for key in ('.hidden.md', '.hidden', 'case note', 'visible/case note.md', 'linked-directory'):
            self.assertIn(key, index)
        for key in ('excluded', 'excludedalso', '.hidden-folder'):
            self.assertNotIn(key, index)

    def test_index_is_lazy_and_built_only_once(self):
        self.write('AGENTS.md')
        with patch.object(references, '_note_index', side_effect=AssertionError('eager index')):
            self.assertEqual(references.check(self.vault)['dead_count'], 0)
        self.write('AGENTS.md', '[[One]] [[Two]]\n')
        with patch.object(references, '_note_index', wraps=references._note_index) as index:
            self.assertEqual(references.check(self.vault)['dead_count'], 2)
        index.assert_called_once_with(self.vault)

    def test_companion_selection_preserves_named_custom_ambiguous_and_ignored_folders(self):
        self.write('custom/Soul.md')
        self.write('custom/Kurallar.md')
        self.write('archive/Core.md')
        self.write('.hidden/Core.md')
        for expected in ('custom', 'My Echo', None):
            with self.subTest(expected=expected):
                if expected == 'My Echo':
                    self.write('My Echo/Kurallar.md')
                elif expected is None:
                    self.write('Other Companion/Kurallar.md')
                selected = companion.directory(self.vault)
                self.assertEqual(selected, self.vault / expected if expected else None)
                self.assertEqual(companion.directory(self.vault, strict=True), selected)
                files = references.instruction_files(self.vault)
                self.assertEqual(files, [selected / 'Kurallar.md'] if selected else [])

    def test_companion_directory_symlinks_are_ignored(self):
        self.write('custom/Core.md')
        self.symlink(self.vault / 'custom', 'Linked Companion', directory=True)
        self.assertEqual(companion.directory(self.vault), self.vault / 'custom')
        self.assertEqual(companion.directory(self.vault, strict=True), self.vault / 'custom')

    def test_missing_optional_files_and_non_directory_skill_parents_are_normal(self):
        self.write('.kiro', '# a file, not a client folder\n')
        self.assertEqual(references.check(self.vault), {
            'checked_files': 0, 'dead_count': 0, 'dead': [], 'truncated': False})
        self.assertEqual(companion.directory(self.vault), self.vault / companion.DEFAULT_DIRECTORY)
        self.assertEqual(companion.directory(self.vault, strict=True),
                         self.vault / companion.DEFAULT_DIRECTORY)

    def test_default_companion_predicate_behavior_is_unchanged(self):
        self.write('custom/Core.md')
        original = Path.is_file

        def fail(path):
            if path == self.vault / 'custom/Core.md':
                raise PermissionError('private-default-canary')
            return original(path)

        with patch.object(Path, 'is_file', fail):
            with self.assertRaises(PermissionError):
                companion.directory(self.vault)
        with patch.object(Path, 'is_file', return_value=False):
            self.assertEqual(companion.directory(self.vault), self.vault / companion.DEFAULT_DIRECTORY)
            self.assertEqual(companion.directory(self.vault, strict=True), self.vault / 'custom')

    def test_stat_errors_are_not_missing_files_even_with_suppressing_pathlib_predicates(self):
        for name in ('AGENTS.md', '.agents/skills', 'custom/Core.md'):
            with self.subTest(path=name):
                if name == '.agents/skills':
                    (self.vault / name).mkdir(parents=True, exist_ok=True)
                else:
                    self.write(name)
                target = self.vault / name
                original_stat, original_file, original_dir = Path.stat, Path.is_file, Path.is_dir

                def stat(path, *args, **kwargs):
                    if path == target:
                        raise PermissionError(errno.EACCES, 'private-stat-canary', str(path))
                    return original_stat(path, *args, **kwargs)

                def suppress(predicate):
                    def call(path, *args, **kwargs):
                        try:
                            return predicate(path, *args, **kwargs)
                        except OSError:
                            return False
                    return call

                # Python 3.14's predicates suppress all OSError. Exercise that boundary
                # on older interpreters too, while stat continues to surface the error.
                with patch.object(Path, 'stat', stat), \
                        patch.object(Path, 'is_file', suppress(original_file)), \
                        patch.object(Path, 'is_dir', suppress(original_dir)):
                    with self.assertRaises(PermissionError):
                        references.check(self.vault)

    def test_skill_and_note_scandir_failures_propagate(self):
        for name, operation in (('.agents/skills', references.instruction_files),
                                ('.agents/skills/nested', references.instruction_files),
                                ('notes', references._note_index)):
            with self.subTest(path=name):
                folder = self.vault / name
                folder.mkdir(parents=True, exist_ok=True)
                original = os.scandir

                def scan(path):
                    if Path(path) == folder:
                        raise OSError(errno.EIO, 'private-scan-canary', str(path))
                    return original(path)

                with patch.object(os, 'scandir', scan):
                    with self.assertRaises(OSError):
                        operation(self.vault)

    def test_entry_classification_errors_propagate_instead_of_becoming_files(self):
        folder = self.vault / 'notes'
        folder.mkdir()
        original = os.scandir

        class Entry:
            def __init__(self, entry):
                self.entry = entry

            def __getattr__(self, key):
                return getattr(self.entry, key)

            def is_dir(self, *args, **kwargs):
                if Path(self.entry.path) == folder:
                    raise OSError(errno.EIO, 'private-entry-canary')
                return self.entry.is_dir(*args, **kwargs)

        class Entries:
            def __init__(self, path):
                self.iterator = original(path)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.iterator.close()

            def __iter__(self):
                return self

            def __next__(self):
                return Entry(next(self.iterator))

        with patch.object(os, 'scandir', Entries):
            with self.assertRaises(OSError):
                references._note_index(self.vault)

    def test_companion_failure_is_not_silently_ignored(self):
        with patch.object(companion, 'directory', side_effect=OSError('private-companion-canary')):
            with self.assertRaises(OSError):
                references.check(self.vault)

    def test_mirror_comparison_failure_is_not_a_successful_partial_check(self):
        twin = self.write('.agents/skills/demo/SKILL.md')
        self.write('.claude/skills/demo/SKILL.md')
        original = Path.read_bytes

        def read(path):
            if path == twin:
                raise PermissionError('private-mirror-canary')
            return original(path)

        with patch.object(Path, 'read_bytes', read):
            with self.assertRaises(PermissionError):
                references.check(self.vault)


@unittest.skipIf(os.name == 'nt', 'POSIX directory permissions required')
class InstalledReferenceDiscoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='v3-reference-discovery-installed-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.vault, self.state = self.root / 'vault', self.root / 'state'
        self.vault.mkdir()
        self.env = isolated_env(self.root / 'home')
        self.env.update(XDG_STATE_HOME=str(self.root / 'xdg'), XDG_CONFIG_HOME=str(self.root / 'config'))
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def doctor(self, *flags):
        result = run_python(self.vault / 'beyin.py', ['doctor', *flags], self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b'')
        return result.stdout.decode('utf-8')

    def assert_directory_failure(self, folder):
        before = snapshot(self.vault)
        mode = folder.stat().st_mode
        folder.chmod(0)
        try:
            try:
                list(folder.iterdir())
            except PermissionError:
                pass
            else:
                self.skipTest('current user bypasses directory permissions')
            for flags in ((), ('--json',)):
                with self.subTest(flags=flags):
                    report = json.loads(self.doctor(*flags))
                    self.assertEqual(report['instruction_references'],
                                     {'status': 'unavailable', 'error': 'PermissionError'})
                    self.assertNotEqual(report['status'], 'needs_attention')
                    for key in ('state_location', 'preferences', 'jev'):
                        self.assertIn(key, report)
            human = self.doctor('--human')
            self.assertIn('Talimat ve skill baglanti denetimi tamamlanamadi (bilgi): PermissionError.', human)
            self.assertNotIn('kirik baglanti (bilgi)', human)
            self.assertNotIn('discovery-canary', human)
        finally:
            folder.chmod(mode)
        self.assertEqual(snapshot(self.vault), before)

    def test_unreadable_skill_folder_does_not_reduce_checked_files(self):
        folder = self.vault / '.agents/skills/discovery-canary'
        folder.mkdir()
        (folder / 'SKILL.md').write_text('[[Missing]]\n', encoding='utf-8')
        healthy = json.loads(self.doctor())['instruction_references']
        self.assertEqual(healthy['dead_count'], 1)
        self.assert_directory_failure(folder)
        self.assertEqual(json.loads(self.doctor())['instruction_references'], healthy)

    def test_unreadable_skill_root_does_not_reduce_checked_files(self):
        folder = self.vault / '.agents/skills'
        (folder / 'discovery-canary.md').write_text('[[Missing]]\n', encoding='utf-8')
        healthy = json.loads(self.doctor())['instruction_references']
        self.assertEqual(healthy['dead_count'], 1)
        self.assert_directory_failure(folder)
        self.assertEqual(json.loads(self.doctor())['instruction_references'], healthy)

    def test_unreadable_hidden_folder_remains_pruned(self):
        folder = self.vault / '.discovery-canary'
        folder.mkdir()
        (folder / 'Excluded.md').write_text('# hidden\n', encoding='utf-8')
        (self.vault / 'AGENTS.md').write_text('[[Excluded]]\n', encoding='utf-8')
        healthy = json.loads(self.doctor())['instruction_references']
        self.assertEqual(healthy['dead_count'], 1)
        before, mode = snapshot(self.vault), folder.stat().st_mode
        folder.chmod(0)
        try:
            try:
                list(folder.iterdir())
            except PermissionError:
                pass
            else:
                self.skipTest('current user bypasses directory permissions')
            self.assertEqual(json.loads(self.doctor())['instruction_references'], healthy)
            self.assertNotIn('denetimi tamamlanamadi', self.doctor('--human'))
        finally:
            folder.chmod(mode)
        self.assertEqual(snapshot(self.vault), before)

    def test_unreadable_note_folder_does_not_turn_a_live_bare_link_dead(self):
        folder = self.vault / 'notes/discovery-canary'
        folder.mkdir(parents=True)
        (folder / 'Existing.md').write_text('# note\n', encoding='utf-8')
        (self.vault / 'AGENTS.md').write_text('[[Existing]]\n', encoding='utf-8')
        healthy = json.loads(self.doctor())['instruction_references']
        self.assertEqual(healthy['dead_count'], 0)
        self.assert_directory_failure(folder)
        self.assertEqual(json.loads(self.doctor())['instruction_references'], healthy)

    def test_unreadable_companion_candidate_does_not_omit_existing_rules(self):
        folder = self.vault / 'discovery-canary'
        folder.mkdir()
        rules = self.vault / companion.DEFAULT_DIRECTORY / 'Kurallar.md'
        rules.write_text('[[Missing]]\n', encoding='utf-8')
        healthy = json.loads(self.doctor())['instruction_references']
        self.assertEqual(healthy['dead_count'], 1)
        self.assert_directory_failure(folder)
        self.assertEqual(json.loads(self.doctor())['instruction_references'], healthy)


if __name__ == '__main__':
    unittest.main()
