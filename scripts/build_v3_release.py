#!/usr/bin/env python3
"""Build a minimal checksum-listed portable release from this checkout."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def installer():
    spec = importlib.util.spec_from_file_location('beyin_release_installer', ROOT / 'scripts/install_v3.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def build(output, version='3.0.0'):
    if not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', version):
        raise ValueError('stable version required')
    paths = [ROOT / 'scripts' / name for name in ('install_v3.py', 'beyin_v3.py', 'beyin_entry.py')]
    paths += sorted((ROOT / 'template/.claude/scripts').glob('beyin_v3*.py'))
    paths += [ROOT / 'template/.agents/skills' / name / 'SKILL.md' for name in ('beyin', 'beyin-doktor', 'beyin-guncelle')]
    files = {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in paths}
    manifest = {'schema': 1, 'version': version, 'min_python': '3.11', 'runtime_schema': 1, 'migrations': [], 'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    steps_file = ROOT / 'after-update.json'
    if steps_file.is_file():
        # The manifest field also works with old updaters that reject new archive paths.
        steps = json.loads(steps_file.read_text(encoding='utf-8'))
        manifest['after_update_steps'] = steps['steps'] if isinstance(steps, dict) else steps
    legacy = [ROOT/'template/.claude/scripts'/name for name in ('flush.py','compile.py')] + [ROOT/'template/.claude/hooks'/(name+suffix) for name in ('session-start','session-end','pre-compact','prompt-counter') for suffix in ('.sh','.ps1')]
    manifest['legacy_hashes'] = {p.relative_to(ROOT/'template').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in legacy if p.exists()}
    # cad7953-era updaters reject every other key before they can load the new
    # installer. Keep this schema-1 wire field compatible; the checksum-listed
    # installer also knows the released starter hashes for all six skill paths.
    doctor = '.claude/skills/beyin-doktor/SKILL.md'
    manifest['legacy_skill_hashes'] = {doctor: installer().default_skill_hashes()[doctor]}
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', json.dumps(manifest, sort_keys=True, indent=2) + '\n')
        for name, data in sorted(files.items()): archive.writestr(name, data)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    checksum = output.with_name(output.name + '.sha256')
    checksum.write_text(f'{digest}  {output.name}\n', encoding='utf-8')
    return {
        'status': 'built',
        'version': version,
        'files': len(files),
        'sha256': digest,
        'checksum': str(checksum),
    }


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--version', default='3.0.0')
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.version)))
