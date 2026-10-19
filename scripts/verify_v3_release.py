#!/usr/bin/env python3
"""Verify the SAME candidate ZIP on each platform, including a real released baseline."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
from v3_package_helpers import isolated_env, run_python, snapshot
from beyin_v3_update import validate_package


def verified(package):
    package = Path(package).resolve()
    checksum = package.with_name(package.name + '.sha256').read_text(encoding='ascii').split()
    if len(checksum) != 2 or checksum[1] != package.name or checksum[0] != hashlib.sha256(package.read_bytes()).hexdigest():
        raise ValueError('candidate/baseline outer checksum mismatch')
    return validate_package(package)[0]


def verify(candidate, baseline):
    candidate, baseline = Path(candidate).resolve(), Path(baseline).resolve()
    new, old = verified(candidate), verified(baseline)
    with tempfile.TemporaryDirectory(prefix='beyin-release-proof-') as tmp:
        root = Path(tmp); env = isolated_env(root / 'home')
        vault = root / 'Synthetic Vault Ölçüm'; vault.mkdir(); state = root / 'state'
        def cli(*args):
            result = run_python(vault / 'beyin.py', args, vault, env)
            if result.returncode: raise AssertionError(result.stderr.decode('utf-8', errors='replace'))
            return json.loads(result.stdout)
        for name, package in (('old', baseline), ('new', candidate)):
            with zipfile.ZipFile(package) as archive: archive.extractall(root / name)
        result = run_python(root / 'old/scripts/install_v3.py', ['--vault', vault, '--state', state], root, env)
        assert result.returncode == 0, result.stderr
        assert (vault / '.beyin-version').read_text(encoding='ascii').strip() == old['version']
        note = vault / 'user-note.md'; note.write_text('Synthetic release proof quartz note.\n', encoding='utf-8')
        original = note.read_bytes()
        before = snapshot(vault), snapshot(state)
        assert cli('update', '--check', '--package', candidate)['status'] == 'available'
        assert before == (snapshot(vault), snapshot(state)), 'check mutated installation'
        assert cli('update', '--package', candidate)['status'] == 'updated'
        assert (vault / '.beyin-version').read_text(encoding='ascii').strip() == new['version']
        assert cli('update', '--package', candidate)['status'] == 'noop'
        assert 'updates' in cli('doctor')
        cli('preferences', '--update-notifications', 'off')
        assert cli('doctor')['updates']['status'] == 'disabled'
        cli('sync')
        before = snapshot(vault), snapshot(state)
        assert cli('context', 'release proof quartz', '--no-sync')['records']
        assert before == (snapshot(vault), snapshot(state)), 'no-sync mutated installation'
        after = vault / 'after-update.md'; after.write_text('Synthetic user work after update.\n', encoding='utf-8')
        assert cli('rollback')['status'] == 'rolled_back'
        assert (vault / '.beyin-version').read_text(encoding='ascii').strip() == old['version']
        cli('preferences')  # Old parser must still accept its untouched settings.
        cli('doctor')
        assert note.read_bytes() == original and after.is_file()
        # Interrupt an actual package update, then recover using the installed entry.
        code = '''import sys
from pathlib import Path
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(sys.argv[1]) / '.claude/scripts'))
import beyin_v3_update as u
def stop(phase, index=None):
    if phase == 'after_replace' and index == 0: raise OSError('synthetic interruption')
u.transaction_hook = stop
try: u.update(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
except OSError: pass
else: raise AssertionError('fault did not fire')
'''
        fault = subprocess.run([sys.executable, '-B', '-c', code, str(vault), str(state), str(candidate)],
                               env=env, capture_output=True, timeout=60)
        assert fault.returncode == 0, fault.stderr
        assert (state / 'update-journal.json').is_file()
        assert cli('recover')['status'] == 'recovered'
        cli('recover')
        assert (vault / '.beyin-version').read_text(encoding='ascii').strip() == new['version']
        assert note.read_bytes() == original and after.is_file()
        # Clean install from the identical candidate bytes, not the source checkout.
        clean = root / 'Clean Vault'; clean.mkdir()
        installed = run_python(root / 'new/scripts/install_v3.py', ['--vault', clean, '--state', root / 'clean-state'], root, env)
        assert installed.returncode == 0, installed.stderr
        doctor = run_python(clean / 'beyin.py', ['doctor'], clean, env)
        assert doctor.returncode == 0, doctor.stderr
        assert json.loads(doctor.stdout)['updates']['status'] == 'unknown'
        return {'status': 'passed', 'baseline': old['version'], 'version': new['version'],
                'platform': sys.platform, 'python': sys.version.split()[0],
                'sha256': hashlib.sha256(candidate.read_bytes()).hexdigest(),
                'checks': ['clean_install', 'released_baseline_update', 'read_only_check', 'same_version_noop',
                           'doctor', 'preferences', 'no_sync', 'rollback_old_preferences', 'interrupted_recover', 'user_notes_preserved']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.package, args.baseline), indent=2))
