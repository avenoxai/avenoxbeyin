"""devices: opt-in, staged multi-device layout for one vault. Off by default.

Beyin stays local-first: nothing here runs unless the user asks for it, and the default
installation is unchanged. This first step covers the two levels that need no git and no
background job:

- `solo`   one computer. The default; nothing is configured.
- `phone`  one computer plus phones/tablets through a file sync service the user already has:
           iCloud Drive (free, Apple devices), Obsidian Sync (paid, any platform) or another
           service. Beyin runs on the computer; the phone reads and edits notes in Obsidian.

Git-based levels (several computers, an always-on hub) are discussed in #205 and are not part
of this module yet.

Rules this module keeps:

- `--detect` and `--plan` write nothing and make no network call. `--apply` writes only the
  settings file in the machine-local runtime state; it never writes into the vault.
- Settings live in the runtime state, never in the vault, so they are not synchronised to a
  device they do not describe and a rollback to an older engine ignores them.
- Standard library only.
"""
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys

SETTINGS_FILE = 'devices.json'
LEVELS = ('solo', 'phone')
PHONE_SYNCS = ('icloud', 'obsidian-sync', 'other')
GIT_TIMEOUT = 20
DOC = 'docs/v3/DEVICES.md'
_SERVICES = (
    ('Library/Mobile Documents/iCloud~md~obsidian/', 'icloud', True),
    ('Library/Mobile Documents/', 'icloud', False),
    ('Library/CloudStorage/Dropbox', 'dropbox', False),
    ('Library/CloudStorage/OneDrive', 'onedrive', False),
    ('Library/CloudStorage/GoogleDrive', 'google-drive', False),
    ('/iCloudDrive/', 'icloud', False),
    ('/Dropbox/', 'dropbox', False),
    ('/OneDrive/', 'onedrive', False),
    ('/OneDrive - ', 'onedrive', False),
    ('/Google Drive/', 'google-drive', False),
)


class DevicesError(ValueError):
    pass


def _atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(4) + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


# ---------------------------------------------------------------- settings

def default_settings():
    return {'schema': 1, 'level': 'solo', 'phone_sync': None}


def _valid(data):
    if not isinstance(data, dict) or data.get('schema') != 1:
        return False
    if data.get('level') not in LEVELS or data.get('phone_sync') not in (None, *PHONE_SYNCS):
        return False
    return (data['level'] == 'phone') == (data['phone_sync'] is not None)


def read_settings(state):
    """Return (settings, valid). A missing file is the valid default; a broken file reads as
    the default with valid False and is never overwritten silently."""
    path = Path(state) / SETTINGS_FILE
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default_settings(), True
    except (OSError, ValueError):  # ValueError covers UnicodeDecodeError and broken JSON
        return default_settings(), False
    merged = {**default_settings(), **data} if isinstance(data, dict) else data
    if not _valid(merged):
        return default_settings(), False
    return merged, True


def status(vault, state):
    settings, valid = read_settings(state)
    return {'valid': valid, 'enabled': settings['level'] != 'solo', **settings}


def doctor(state):
    """Read-only line for doctor: only the switch while the feature is off."""
    settings, valid = read_settings(state)
    if valid and settings['level'] == 'solo':
        return {'enabled': False}
    return {'enabled': settings['level'] != 'solo', 'valid': valid, 'level': settings['level'],
            'phone_sync': settings['phone_sync']}


# ---------------------------------------------------------------- read-only git facts

def _git(vault, *args):
    exe = shutil.which('git')
    if not exe:
        return subprocess.CompletedProcess(args, 127, '', 'git not found')
    env = dict(os.environ)
    env.update({'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C', 'LANGUAGE': 'C'})
    try:
        return subprocess.run([exe, '-C', str(vault), '-c', 'core.quotePath=false', *args], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=GIT_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, '', 'timeout after ' + str(GIT_TIMEOUT) + 's')


def _git_facts(vault):
    """Whether the vault is a git work tree and where its git directory lives. Local commands
    only; remotes are not read."""
    vault = Path(vault).resolve()
    top = _git(vault, 'rev-parse', '--show-toplevel')
    try:
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != vault:
            return {'repository': False}
    except OSError:
        return {'repository': False}
    done = _git(vault, 'rev-parse', '--absolute-git-dir')
    git_dir = Path(done.stdout.strip()).resolve() if done.returncode == 0 and done.stdout.strip() else None
    return {'repository': True,
            'git_dir_inside_vault': bool(git_dir and (git_dir == vault / '.git' or vault in git_dir.parents)),
            # `git init --separate-git-dir` leaves a .git file holding this machine's absolute path.
            'git_file_in_vault': (vault / '.git').is_file()}


# ---------------------------------------------------------------- detection

def sync_location(path):
    """Which file sync service, if any, owns this path. Pure path inspection: no file is read."""
    text = Path(path).as_posix() + '/'
    for marker, service, phone in _SERVICES:
        if marker in text:
            return {'service': service, 'obsidian_ios_folder': phone}
    return {'service': None, 'obsidian_ios_folder': False}


def _obsidian(vault):
    folder = Path(vault) / '.obsidian'
    result = {'vault': folder.is_dir(), 'sync_plugin': False, 'syncthing': (Path(vault) / '.stfolder').exists()}
    try:
        plugins = json.loads((folder / 'core-plugins.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return result
    # Older Obsidian wrote a list of enabled ids, newer versions a {id: bool} object.
    result['sync_plugin'] = ('sync' in plugins if isinstance(plugins, list)
                             else bool(plugins.get('sync')) if isinstance(plugins, dict) else False)
    return result


def detect(vault, state):
    """Describe this machine for level selection. Writes nothing; makes no network call."""
    vault, state = Path(vault), Path(state)
    location = sync_location(vault)
    result = {
        'platform': 'macos' if sys.platform == 'darwin' else 'windows' if sys.platform.startswith('win') else 'linux',
        'python': '.'.join(map(str, sys.version_info[:3])),
        'vault_sync_service': location['service'],
        'vault_in_obsidian_ios_folder': location['obsidian_ios_folder'],
        'state_sync_service': sync_location(state)['service'],
        'obsidian': _obsidian(vault),
        'git': _git_facts(vault),
    }
    result['levels'] = _eligibility(result)
    result['current_fit'] = _current_fit(result)
    return result


def _eligibility(found):
    """What each phone option still needs on this machine. `other` depends on a service Beyin
    cannot inspect, so it lists nothing."""
    phone = {'icloud': [], 'obsidian-sync': [], 'other': []}
    if found['platform'] != 'macos':
        phone['icloud'].append('a Mac that keeps the vault in the Obsidian iCloud folder')
    elif not found['vault_in_obsidian_ios_folder']:
        phone['icloud'].append('move the vault under iCloud Drive/Obsidian (see ' + DOC + ')')
    if not found['obsidian']['sync_plugin']:
        phone['obsidian-sync'].append('an Obsidian Sync subscription and the Sync core plugin turned on in this vault')
    return {'solo': {'needs': []}, 'phone': {'needs_per_option': phone}}


def _current_fit(found):
    """What the environment already looks like, as a starting suggestion and nothing more."""
    phone = ('icloud' if found['vault_in_obsidian_ios_folder'] else
             'obsidian-sync' if found['obsidian']['sync_plugin'] else
             'other' if found['vault_sync_service'] or found['obsidian']['syncthing'] else None)
    return {'level': 'phone' if phone else 'solo', 'phone_sync': phone}


# ---------------------------------------------------------------- options, plan, apply

def normalize(options, state):
    """Validate CLI options against the stored settings; raise DevicesError with a plain reason."""
    stored, valid = read_settings(state)
    if not valid:
        # An explicit --level solo is the way back, also from a file this version cannot read.
        if options.get('level') != 'solo':
            raise DevicesError(SETTINGS_FILE + ' in the runtime state is invalid or from a newer version; '
                               'fix or remove it, or reset with --apply --level solo')
        stored = default_settings()
    level = options.get('level') or stored['level']
    if level not in LEVELS:
        raise DevicesError('--level must be one of: ' + ', '.join(LEVELS))
    phone_sync = options.get('phone_sync') if options.get('phone_sync') is not None else stored['phone_sync']
    if phone_sync not in (None, *PHONE_SYNCS):
        raise DevicesError('--phone-sync must be one of: ' + ', '.join(PHONE_SYNCS))
    if level == 'solo':
        if options.get('phone_sync'):
            raise DevicesError('--phone-sync needs --level phone')
        phone_sync = None
    elif not phone_sync:
        raise DevicesError('--level phone needs --phone-sync ' + '|'.join(PHONE_SYNCS))
    return {**stored, 'schema': 1, 'level': level, 'phone_sync': phone_sync}


def plan(vault, state, options):
    """What `--apply` would store and what is left for the user. Writes nothing."""
    vault, state = Path(vault), Path(state)
    settings = normalize(options, state)
    stored, valid = read_settings(state)
    manual, warnings = [], []
    location = sync_location(vault)
    if sync_location(state)['service']:
        warnings.append('The runtime state is inside a file sync folder. Keep it outside: a synced SQLite index gets corrupted.')

    choice = settings['phone_sync']
    if choice == 'icloud':
        if sys.platform != 'darwin':
            warnings.append('The iCloud bridge is built for a Mac. On other systems iCloud Drive may be missing or keep '
                            'notes as online-only placeholders that Beyin cannot index.')
        if not location['obsidian_ios_folder']:
            manual.append('Move the vault into iCloud Drive/Obsidian so the iPhone or iPad Obsidian app can open it, '
                          'then run the installer with --rebind-state (docs/v3/MULTI-MACHINE.md, moving a vault).')
        manual.append('On the phone: install Obsidian, choose "Open folder as vault" under iCloud Drive/Obsidian. '
                      'Run Beyin on one computer only at this level; the phone reads and edits notes.')
        warnings.append('iCloud also uploads hidden folders. .beyin-note-write/ holds note backups; they stay in your own '
                        'iCloud account. Do not keep the vault in iCloud and Obsidian Sync at the same time.')
    elif choice == 'obsidian-sync':
        if location['service'] == 'icloud':
            warnings.append('This vault is in iCloud. Obsidian advises against combining iCloud with Obsidian Sync; '
                            'pick one of them for this vault.')
        manual.append('In Obsidian on every device: Settings > Core plugins > Sync, sign in and connect the same remote '
                      'vault. Obsidian Sync skips hidden folders other than .obsidian, so each computer keeps its own '
                      'Beyin installation files.')
    elif choice == 'other':
        manual.append('Exclude .beyin-note-write/ and any .git folder from your sync service if it allows exclusions. '
                      'Conflict copies the service creates are reported by `beyin.py sync`, never merged or deleted.')
    # Obsidian Sync skips hidden folders; iCloud, Syncthing, Dropbox and the like carry them.
    synced = choice in ('icloud', 'other') or location['service'] or (Path(vault) / '.stfolder').exists()
    facts = _git_facts(vault) if choice and synced else {}
    if facts.get('git_dir_inside_vault'):
        warnings.append('This vault is a git repository whose .git folder is inside the synced folder. File sync '
                        'services corrupt git data and carry it to other devices; keep the git directory outside the '
                        'synced folder or stop using git in this vault (see ' + DOC + ').')
    elif facts.get('git_file_in_vault'):
        warnings.append('The vault holds a .git file that points to this machine\'s git directory by absolute path. '
                        'A file sync service carries it to other devices, where that path is wrong (see ' + DOC + ').')
    return {'level': settings['level'], 'phone_sync': choice, 'manual_steps': manual, 'warnings': warnings,
            'settings': settings, 'changes_settings': not valid or settings != stored}


def public_plan(result):
    """The plan as shown to a person."""
    return {key: value for key, value in result.items() if key != 'settings'}


def apply(vault, state, options):
    """Store the chosen level in the runtime state. Nothing in the vault is written."""
    planned = plan(vault, state, options)
    if planned['changes_settings']:
        _atomic(Path(state) / SETTINGS_FILE, json.dumps(planned['settings'], ensure_ascii=False, indent=2) + '\n')
    return {**public_plan(planned), 'status': 'applied' if planned['changes_settings'] else 'unchanged'}
