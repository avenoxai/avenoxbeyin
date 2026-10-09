"""Offline product fixture helpers. Never downloads a release or reads user state."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'scripts/install_v3.py'
BUILDER = ROOT / 'scripts/build_v3_release.py'


def windows_runtime_env():
    """Variables Windows needs even in a cleared test environment. Without SYSTEMROOT,
    ssl.SSLContext() fails on Python 3.14 (OpenSSL 3.5): '[SSL] unknown error (0xa080024)'."""
    return {key: os.environ[key] for key in ('SYSTEMROOT', 'WINDIR') if key in os.environ}


# Every BEYIN_* variable is a user setting or a hook flag (BEYIN_V3_SKIP, BEYIN_JEV_DISABLE,
# BEYIN_V3_FILTER_HARNESS_TURNS, BEYIN_V3_NO_RECEIPT_REMINDER, BEYIN_UPDATES_OFF, ...). A suite run
# from a configured shell or an agent session must not inherit them. Only the values the suite
# itself inherited are dropped: one a test sets in os.environ (BEYIN_PYTHON for a missing
# interpreter) still reaches its child, as does one passed explicitly.
USER_ENV_PREFIX = 'BEYIN_'
_INHERITED = {key: value for key, value in os.environ.items() if key.upper().startswith(USER_ENV_PREFIX)}


def inherited_env(**extra):
    """The current environment without the user Beyin settings the suite inherited, plus extra."""
    env = {key: value for key, value in os.environ.items() if _INHERITED.get(key) != value}
    env.update(extra)
    return env


def clean_environ(**extra):
    """In-process counterpart of inherited_env: a patch.dict for os.environ (start/stop or with)."""
    return patch.dict(os.environ, inherited_env(**extra), clear=True)


def isolated_path(*first):
    """PATH for a cleared test environment: the given folders, then os.defpath. Windows adds
    its own folders because cmd.exe resolves AutoRun commands (doskey) through PATH alone;
    never the PowerShell folder, which installed commands must name by absolute path."""
    entries = [str(folder) for folder in first]
    if sys.platform == 'win32':
        win = os.environ.get('SYSTEMROOT') or os.environ.get('WINDIR') or r'C:\Windows'
        entries.extend([os.path.join(win, 'System32'), win])
    return os.pathsep.join(entries + [os.defpath])


def isolated_env(home):
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    env = {'HOME': str(home), 'USERPROFILE': str(home), 'APPDATA': str(home / 'appdata'),
           'LOCALAPPDATA': str(home / 'localappdata'), 'TEMP': str(home), 'TMP': str(home),
           'PATH': isolated_path(Path(sys.executable).parent),
           'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONIOENCODING': 'utf-8', 'BEYIN_V3_NO_SPAWN': '1'}
    env.update(windows_runtime_env())
    return env


def run_python(script, args, cwd, env, payload=None):
    return subprocess.run([sys.executable, str(script), *map(str, args)], cwd=cwd, env=env,
                          input=json.dumps(payload).encode('utf-8') if payload is not None else None,
                          capture_output=True, timeout=60)


def install(vault, state, env):
    return run_python(INSTALLER, ['--vault', vault, '--state', state], ROOT, env)


def build_package(path, version, env):
    if not BUILDER.is_file():
        raise AssertionError('Versioned release builder not implemented')
    result = run_python(BUILDER, ['--output', path, '--version', version], ROOT, env)
    if result.returncode:
        raise AssertionError('Offline package build failed: ' + result.stderr.decode('utf-8', errors='replace'))
    return Path(path)


def snapshot(root):
    root = Path(root)
    return {p.relative_to(root).as_posix(): ('symlink:' + os.readlink(p) if p.is_symlink()
            else hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob('*')) if p.is_file() or p.is_symlink()}


def rewrite_zip(source, target, mutate):
    with zipfile.ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    mutate(files)
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return Path(target)
