"""List verified backup hashes, or recover one exact UTF-8 version to a new file.

py -3 scripts/restore_v3_compact_backup.py --vault /path/to/vault --name Threads.md
Add --sha256 HASH --output /path/outside/vault/recovered-Threads.md to recover a version.
This utility never changes archives, live notes, or state.
"""
import argparse
import json
from pathlib import Path
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--vault', type=Path, required=True)
    p.add_argument('--name', choices=['Threads.md', 'Last-Session.md'], required=True)
    p.add_argument('--sha256')
    p.add_argument('--output', type=Path)
    args = p.parse_args()
    vault = args.vault.resolve()
    sys.path.insert(0, str(vault / '.claude/scripts'))
    from beyin_v3_compact import _archive_history
    from beyin_v3_companion import directory
    _, versions = _archive_history(directory(vault), args.name, vault)
    if args.sha256:
        if args.output is None or args.sha256 not in versions:
            p.error('provide --output and a verified hash from the list')
        if args.output.resolve().is_relative_to(vault):
            p.error('recovery output must be outside the vault')
        with args.output.open('xb') as output:
            output.write(versions[args.sha256].encode('utf-8'))
    else:
        print(json.dumps([dict(sha256=k, chars=len(v), bytes=len(v.encode('utf-8')))
                          for k, v in versions.items()], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
