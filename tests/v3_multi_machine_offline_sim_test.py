"""Exercise the Git-only recovery and hand-off discussed in issue #882."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from v3_package_helpers import inherited_env


def write_checkpoint(vault, writer, contents):
    if (vault / 'writer.lock').read_text(encoding='utf-8').strip() != writer:
        raise PermissionError('writer does not hold the local lock')
    (vault / 'checkpoint.md').write_text(contents, encoding='utf-8')


@unittest.skipUnless(shutil.which('git'), 'Git is not installed')
class MultiMachineOfflineSimTest(unittest.TestCase):
    def test_offline_writes_handoff_and_revert(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            remote, a, b = (root / name for name in ('remote.git', 'vault-a', 'vault-b'))
            template = root / 'empty-template'
            template.mkdir()
            env = {key: value for key, value in inherited_env().items()
                   if not key.upper().startswith('GIT_')}
            env.update(GIT_ALLOW_PROTOCOL='file', GIT_CONFIG_NOSYSTEM='1',
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_TEMPLATE_DIR=str(template),
                       GIT_AUTHOR_NAME='Simulation', GIT_COMMITTER_NAME='Simulation',
                       GIT_AUTHOR_EMAIL='simulation.invalid',
                       GIT_COMMITTER_EMAIL='simulation.invalid', LC_ALL='C')

            def git(repo, *args, expected=0):
                result = subprocess.run(
                    ['git', *map(str, args)], cwd=repo, env=env, capture_output=True,
                    encoding='utf-8', errors='replace', timeout=30,
                )
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                return result

            def commit(repo, message):
                git(repo, 'add', '--all')
                git(repo, 'commit', '-m', message)
                return git(repo, 'rev-parse', 'HEAD').stdout.strip()

            git(root, 'init', '--bare', '--initial-branch=main', remote)
            git(root, 'clone', remote, a)
            (a / 'checkpoint.md').write_text('## Shared checkpoint\nBaseline\n', encoding='utf-8')
            commit(a, 'Create shared checkpoint')
            git(a, 'push', 'origin', 'main')
            git(root, 'clone', remote, b)

            # Each offline clone sees a free lock and edits the same card.
            contents_a = '## Shared checkpoint\nOffline edit A\n'
            contents_b = '## Shared checkpoint\nOffline edit B\n'
            for vault, writer, contents in ((a, 'A', contents_a), (b, 'B', contents_b)):
                lock = vault / 'writer.lock'
                self.assertFalse(lock.exists())
                lock.write_text(writer + '\n', encoding='utf-8')
                write_checkpoint(vault, writer, contents)
            tip_a = commit(a, 'Save offline edit A')
            tip_b = commit(b, 'Save offline edit B')

            git(a, 'push', 'origin', 'main')
            rejected = git(b, 'push', '--porcelain', 'origin', 'main', expected=1)
            self.assertIn('[rejected]', rejected.stdout)
            self.assertEqual(git(remote, 'rev-parse', 'main').stdout.strip(), tip_a)
            self.assertEqual(git(b, 'rev-parse', 'HEAD').stdout.strip(), tip_b)

            # Name the divergent tip before fetching so main can be recreated safely.
            stamp = git(b, 'log', '-1', '--format=%ct').stdout.strip()
            parked = f'conflict/B-{stamp}'
            git(b, 'branch', '-m', parked)
            git(b, 'fetch', 'origin', 'main')
            git(b, 'merge', '--no-commit', 'origin/main', expected=1)
            unmerged = git(b, 'diff', '--name-only', '--diff-filter=U').stdout.splitlines()
            self.assertEqual(unmerged, ['checkpoint.md', 'writer.lock'])
            git(b, 'merge', '--abort')
            git(b, 'switch', '--create', 'main', '--track', 'origin/main')

            def assert_offline_edits_preserved():
                for ref, tip, contents in (('main', tip_a, contents_a), (parked, tip_b, contents_b)):
                    history = git(b, 'log', '--format=%H', ref, '--', 'checkpoint.md')
                    self.assertIn(tip, history.stdout.splitlines())
                    self.assertEqual(git(b, 'show', f'{tip}:checkpoint.md').stdout, contents)

            assert_offline_edits_preserved()

            (a / 'writer.lock').unlink()
            handoff = 'A releases the writer lock for B.\n'
            (a / 'handoff.md').write_text(handoff, encoding='utf-8')
            commit(a, 'Hand off from A to B')
            git(a, 'push', 'origin', 'main')
            git(b, 'pull', '--ff-only')
            self.assertEqual((b / 'handoff.md').read_text(encoding='utf-8'), handoff)
            self.assertFalse((b / 'writer.lock').exists())
            (b / 'writer.lock').write_text('B\n', encoding='utf-8')
            commit(b, 'Acquire the writer lock for B')
            online_contents = '## Shared checkpoint\nOnline edit after hand-off\n'
            write_checkpoint(b, 'B', online_contents)
            last_write = commit(b, 'Save online edit B')
            git(b, 'push', 'origin', 'main')

            git(a, 'pull', '--ff-only')
            with self.assertRaises(PermissionError):
                write_checkpoint(a, 'A', 'Disallowed edit\n')
            self.assertEqual((a / 'checkpoint.md').read_text(encoding='utf-8'), online_contents)
            self.assertEqual(git(a, 'status', '--porcelain').stdout, '')

            git(b, 'revert', '--no-edit', last_write)
            git(b, 'push', 'origin', 'main')
            git(a, 'pull', '--ff-only')
            self.assertEqual((a / 'checkpoint.md').read_text(encoding='utf-8'), contents_a)
            self.assertEqual((a / 'writer.lock').read_text(encoding='utf-8'), 'B\n')
            self.assertEqual((a / 'handoff.md').read_text(encoding='utf-8'), handoff)
            assert_offline_edits_preserved()
