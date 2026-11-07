"""Path-loaded modules must work after their loader restores sys.path."""
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

from v3_package_helpers import inherited_env

ROOT = Path(__file__).resolve().parents[1]


class AdjacentImportTest(unittest.TestCase):
    def run_isolated(self, body):
        with tempfile.TemporaryDirectory(prefix='beyin-adjacent-') as temp:
            script = '''
import importlib.util
from pathlib import Path
import sys

scripts = Path(sys.argv[1]) / 'template/.claude/scripts'
root = Path(sys.argv[2])
vault = root / 'vault'
vault.mkdir()
original_path = sys.path[:]

def load(name):
    spec = importlib.util.spec_from_file_location('isolated_' + name, scripts / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.path.insert(0, str(scripts))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = original_path
    return module

def restore_path():
    sys.path[:] = original_path
    assert str(scripts) not in sys.path
'''
            result = subprocess.run(
                [sys.executable, '-c', textwrap.dedent(script) + textwrap.dedent(body),
                 str(ROOT), temp], cwd=temp, capture_output=True, text=True,
                encoding='utf-8', timeout=30,
                env=inherited_env(HOME=temp, USERPROFILE=temp, PYTHONDONTWRITEBYTECODE='1'))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_sync_and_note_operations_after_loader_restores_path(self):
        self.run_isolated('''
sync = load('beyin_v3_sync')
engine = sync.SyncEngine(vault, root / 'state')
restore_path()
assert engine.sync()['status'] == 'succeeded'
restore_path()
engine.note_create('notes/first.md', 'Zircon calibration.', {'id': 'first'})
restore_path()
snapshot = engine.note_read('notes/first.md')
assert 'Zircon calibration.' in snapshot['content']
restore_path()
result = engine.note_edit({'source': 'notes/first.md',
    'expected_sha256': snapshot['sha256'],
    'edits': [{'old': 'Zircon calibration.', 'new': 'Zircon calibrated.'}]})
assert result['status'] == 'written', result
assert 'Zircon calibrated.' in (vault / 'notes/first.md').read_text(encoding='utf-8')
''')

    def test_passage_context_after_loader_restores_path(self):
        self.run_isolated(r'''
core = load('beyin_v3')
store = core.MemoryStore(root / 'state', vault)
text = '# Zircon\n\nZircon calibration schedule.'
(vault / 'note.md').write_text(text, encoding='utf-8')
store.ingest({'id': 'first', 'kind': 'note', 'source': 'note.md',
    'text': text, 'facts': {}, 'visibility': 'internal'})
restore_path()
assert 'beyin_v3_passage' not in sys.modules
context = store.context_for('claude', 'Zircon calibration', strict=True)
assert context['records'], context
assert (root / 'state/passages.json').is_file(), context
''')

    def test_strict_note_fallback_loads_exclusion_policy(self):
        self.run_isolated('''
core = load('beyin_v3')
store = core.MemoryStore(root / 'state', vault)
for name in ('notes', 'archive'):
    (vault / name).mkdir()
    source = name + '/note.md'
    (vault / source).write_text('Zircon calibration.', encoding='utf-8')
    store.ingest({'id': name, 'kind': 'note', 'source': source,
        'text': 'Zircon calibration.', 'facts': {}, 'visibility': 'internal'})
(root / 'state/retrieval.json').write_text('{"strict_exclude": ["archive/"]}', encoding='utf-8')
store.STRICT_PASSAGES = False
restore_path()
context = store.context_for('claude', 'Zircon calibration', strict=True)
assert [record['source'] for record in context['records']] == ['notes/note.md'], context
''')


if __name__ == '__main__':
    unittest.main()
