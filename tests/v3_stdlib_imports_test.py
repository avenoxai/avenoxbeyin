"""Packaged runtime code imports only the standard library and its own modules.

The installer promises Python 3.11+ and nothing else. An optional backend (for example a local
model server) must stay an external process: its Python packages are never imported here.
"""
import ast
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
# Exactly the Python files the release builder packages (scripts/build_v3_release.py).
PACKAGED = sorted([ROOT / 'scripts' / name for name in ('install_v3.py', 'beyin_v3.py', 'beyin_entry.py')]
                  + list((ROOT / 'template/.claude/scripts').glob('beyin_v3*.py')))


def imported(path):
    """Every absolute import in the file, including lazy imports inside functions."""
    tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split('.')[0]
        elif isinstance(node, ast.ImportFrom) and not node.level:
            yield node.lineno, node.module.split('.')[0]


# A local import must name a module the release actually ships. Helpers such as
# _portalock.py live next to the V3 modules in the source tree but are not packaged,
# so importing one would pass every source-tree test and fail in an installed vault (#151).
LOCAL = {path.stem for path in PACKAGED}


def allowed(name):
    return name in sys.stdlib_module_names or name in LOCAL


class StdlibImportTest(unittest.TestCase):
    def test_packaged_runtime_has_no_third_party_import(self):
        self.assertGreater(len(PACKAGED), 20)
        offenders = [f'{path.relative_to(ROOT).as_posix()}:{line} {name}'
                     for path in PACKAGED for line, name in imported(path) if not allowed(name)]
        self.assertEqual(offenders, [])

    def test_dynamic_third_party_import_is_confined_to_isolated_pdf_reader(self):
        # M03 permits one pinned wheel in the reader subprocess. A dynamic import
        # must not quietly move third-party code into the stdlib core.
        offenders = []
        for path in PACKAGED:
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                dynamic = (isinstance(function, ast.Name) and function.id == '__import__' or
                           isinstance(function, ast.Attribute) and function.attr == 'import_module')
                if not dynamic:
                    continue
                name = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
                if isinstance(name, str) and (allowed(name.split('.')[0]) or
                        path.name == 'beyin_v3_extract_reader.py' and name == 'pypdf'):
                    continue
                offenders.append(f'{path.name}:{node.lineno} {name!r}')
        self.assertEqual(offenders, [])

    def test_the_guard_rejects_model_runtimes(self):
        for name in ('torch', 'laya', 'transformers', 'numpy', 'requests'):
            self.assertFalse(allowed(name), name)

    def test_the_guard_rejects_unpackaged_local_helpers(self):
        for name in ('_portalock', 'flush', 'compile', 'antigravity_hooks', 'beyin_helper'):
            self.assertFalse(allowed(name), name)

    def test_packaged_list_matches_the_builder_and_the_updater_allowlist(self):
        spec = importlib.util.spec_from_file_location('beyin_release_builder_under_test', ROOT / 'scripts/build_v3_release.py')
        builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
        spec = importlib.util.spec_from_file_location('beyin_updater_under_test', ROOT / 'template/.claude/scripts/beyin_v3_update.py')
        updater = importlib.util.module_from_spec(spec); spec.loader.exec_module(updater)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'release.zip'
            builder.build(archive, '3.0.0')
            with zipfile.ZipFile(archive) as release:
                names = [name for name in release.namelist() if name != 'manifest.json']
        shipped = sorted(ROOT / name for name in names if name.endswith('.py'))
        self.assertEqual(shipped, PACKAGED)
        self.assertEqual([name for name in names if not updater.allowed(name)], [])


if __name__ == '__main__':
    unittest.main()
