#!/usr/bin/env python3
"""Vendored and generated trees must stay out of the index, synthetic local fixtures only."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'template/.claude/scripts/beyin_v3_sync.py'


def load_module():
    if not MODULE.is_file():
        raise AssertionError('SyncEngine not implemented')
    spec = importlib.util.spec_from_file_location('beyin_v3_exclude_subject', MODULE)
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


class VendoredTreeTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix='beyin-exclude-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.vault = self.root / 'Synthetic Beyin Çalışma'
        self.vault.mkdir()
        self.state = self.root / 'runtime'
        self.state.mkdir()
        self.engine = self.module.SyncEngine(self.vault, self.state)
        self.addCleanup(lambda: getattr(self.engine.store, 'close', lambda: None)())

    def note(self, relative, body):
        metadata = {'id': 'synthetic-' + relative.replace('/', '-').replace('.', '-'),
                    'kind': 'note', 'project': 'nebula', 'revision': 1,
                    'visibility': 'internal', 'facts': {'owner': 'Synthetic Reviewer'}}
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('---\n' + json.dumps(metadata, ensure_ascii=False) + '\n---\n' + body, encoding='utf-8')
        return path

    def sources(self):
        with self.engine.store._connect() as db:
            return sorted(row[0] for row in db.execute('SELECT source FROM markdown_sources'))

    def test_vendored_dir_is_not_indexed(self):
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/README.md', 'Nebula calibration vendored readme.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        sources = self.sources()
        self.assertIn('notlar/gercek.md', sources)
        self.assertNotIn('proje/3rdparty/SDL2/README.md', sources)

    def test_vendored_dir_is_pruned_at_any_depth_and_case(self):
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/ThirdParty/lib/SECURITY.md', 'Nebula calibration vendored security.\n')
        self.note('proje/a/b/c/vendor/CONTRIBUTING.md', 'Nebula calibration vendored deep.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        sources = self.sources()
        self.assertEqual(sources, ['notlar/gercek.md'])

    def test_existing_excluded_dirs_still_apply(self):
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/node_modules/pkg/README.md', 'Nebula calibration node module.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])

    def test_user_excluded_dirs_add_to_defaults(self):
        (self.state / 'index.json').write_text(json.dumps({'excluded_dirs': ['_Build', 'target']}), encoding='utf-8')
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/_Build/linux/_deps/zstd-src/CONTRIBUTING.md', 'Nebula calibration build tree.\n')
        self.note('rust/target/debug/notes.md', 'Nebula calibration cargo tree.\n')
        self.note('proje/3rdparty/lib/README.md', 'Nebula calibration vendored readme.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])

    def test_malformed_config_falls_back_to_defaults(self):
        (self.state / 'index.json').write_text('{not json', encoding='utf-8')
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.note('proje/3rdparty/SDL2/README.md', 'Nebula calibration vendored readme.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])

    def test_config_rejects_path_traversal_entries(self):
        (self.state / 'index.json').write_text(
            json.dumps({'excluded_dirs': ['../gizli', 'a/b', '', '.', '..', 7]}), encoding='utf-8')
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])

    def test_config_entry_count_is_capped(self):
        (self.state / 'index.json').write_text(
            json.dumps({'excluded_dirs': ['d%d' % i for i in range(200)]}), encoding='utf-8')
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])

    def test_config_is_not_a_symlink(self):
        outside = self.root / 'outside.json'
        outside.write_text(json.dumps({'excluded_dirs': ['notlar']}), encoding='utf-8')
        (self.state / 'index.json').symlink_to(outside)
        self.note('notlar/gercek.md', 'Nebula calibration real note.\n')
        self.assertEqual(self.engine.sync()['status'], 'succeeded')
        self.assertEqual(self.sources(), ['notlar/gercek.md'])


if __name__ == '__main__':
    unittest.main()
