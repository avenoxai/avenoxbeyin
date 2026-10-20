"""Inventory Python text boundaries; gate newly scoped implicit locale decoding.

No third-party dependency, filesystem mutation, import of product modules or fix mode.
This is deliberately syntactic: inventory decisions need ownership review, and
binary/JSON/stdio calls are listed rather than treated as missing encodings.
"""
import argparse
import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TREES = ('scripts', 'template/.claude/scripts', 'template/.agents', 'tests')
BOUNDARIES = {'open', 'read_text', 'write_text', 'TextIOWrapper', 'load', 'dump',
              'run', 'Popen', 'check_output', 'check_call', 'decode', 'encode',
              'read_bytes', 'write_bytes', 'FileHandler', 'basicConfig'}


def literal(node, default=None):
    return node.value if isinstance(node, ast.Constant) else default


def inspect_source(source, filename):
    """Return stable locations and categories, never infer ownership from content."""
    rows = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
        if name not in BOUNDARIES:
            continue
        kw = {item.arg: item.value for item in node.keywords}
        category, implicit = 'protocol/manual', False
        if name in {'read_text', 'write_text', 'TextIOWrapper'}:
            # Positional encoding is legal for these APIs.
            position = 0 if name == 'read_text' else 1
            encoding = kw.get('encoding', node.args[position] if len(node.args) > position else None)
            implicit = encoding is None or literal(encoding, 'dynamic') is None
            category = 'implicit-text' if implicit else 'explicit-text'
        elif name == 'open':
            # os.open yields a descriptor; archive/network .open yield bytes.
            receiver = ast.unparse(node.func.value) if isinstance(node.func, ast.Attribute) else ''
            if receiver == 'os' or receiver in {'archive', 'package'} or 'opener' in receiver:
                category = 'binary'
            else:
                mode_node = kw.get('mode', node.args[1] if len(node.args) > 1 else None)
                if isinstance(node.func, ast.Attribute):
                    mode_node = kw.get('mode', node.args[0] if node.args else None)
                mode = literal(mode_node, 'r')
                if 'b' in str(mode):
                    category = 'binary'
                elif isinstance(node.func, ast.Name) or mode_node is None or isinstance(mode_node, ast.Constant):
                    encoding = kw.get('encoding', node.args[3] if isinstance(node.func, ast.Name) and len(node.args) > 3 else None)
                    implicit = encoding is None or literal(encoding, 'dynamic') is None
                    category = 'implicit-text' if implicit else 'explicit-text'
        elif name in {'run', 'Popen', 'check_output', 'check_call'}:
            text = any(literal(kw.get(key)) is True for key in ('text', 'universal_newlines'))
            implicit = text and ('encoding' not in kw or literal(kw['encoding'], 'dynamic') is None)
            category = 'implicit-subprocess' if implicit else 'subprocess'
        elif name in {'read_bytes', 'write_bytes'}:
            category = 'binary'
        elif name in {'encode', 'decode'}:
            category = 'codec'  # str.encode/bytes.decode default is UTF-8, never locale.
        elif name in {'FileHandler', 'basicConfig'}:
            category = 'logging/manual'
        rows.append(dict(path=filename, line=node.lineno, call=name, category=category, implicit=implicit))
    return sorted(rows, key=lambda row: (row['line'], row['call']))


def inventory(root=ROOT):
    paths = set(root.glob('*.py'))
    for tree in TREES:
        paths.update((root / tree).rglob('*.py'))
    return [row for path in sorted(paths) if not any(p in {'_vendor', 'vendor', '__pycache__'} for p in path.parts)
            for row in inspect_source(path.read_text(encoding='utf-8'), path.relative_to(root).as_posix())]


def gated(path):
    # Incremental gate: critical product boundaries and changed regressions.
    return (path.startswith('scripts/') or path.startswith('template/.claude/scripts/') or
            path.startswith('template/.agents/') or path in {
                'tests/v3_workspace_onboarding_test.py', 'tests/v3_encoding_contract_test.py'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', action='store_true', help='emit every boundary as TSV')
    args = parser.parse_args()
    rows = inventory()
    if args.inventory:
        print('path\tline\tcall\tcategory')
        for row in rows:
            print('\t'.join(str(row[key]) for key in ('path', 'line', 'call', 'category')))
        return 0
    failures = [row for row in rows if row['implicit'] and gated(row['path'])]
    for row in failures:
        print(f"{row['path']}:{row['line']}: {row['category']} {row['call']}")
    print(f"Encoding gate: {len(failures)} unresolved scoped boundaries; {len(rows)} inventoried calls")
    return bool(failures)


if __name__ == '__main__':
    sys.exit(main())
