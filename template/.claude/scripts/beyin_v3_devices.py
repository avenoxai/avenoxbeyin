"""devices: opt-in, staged multi-device layout for one vault. Off by default.

Beyin stays local-first: nothing here runs unless the user asks for it, and the default
installation is unchanged. A user picks the smallest level that fits their devices and budget:

- `solo`       one computer. The default; nothing is configured.
- `phone`      one computer plus phones/tablets through a file sync service the user already
               has: iCloud Drive (free, Apple devices), Obsidian Sync (paid, any platform) or
               another service. Beyin runs on the computer; the phone reads and edits notes
               in Obsidian. No git, no server.
- `computers`  two or more computers that each run Beyin, synchronised through a git remote
               the user owns (a private hosted repository, a NAS, another machine over SSH).
               A phone sync from the previous level can be kept on one of the computers.
- `hub`        an always-on machine (VPS, home server, single-board computer, a spare laptop)
               holds the bare repository and can run the agent and the capture server around
               the clock. Other devices are clients. A private mesh VPN such as Tailscale is
               the recommended transport; this module never installs or logs in to one.

Rules this module keeps:

- `--detect` and `--plan` write nothing and make no network call. `--apply` performs only the
  listed actions. `--sync-once` talks to the configured git remote: that is its whole purpose.
- Settings and the last sync record live in the machine-local runtime state, never in the
  vault, so they are not synchronised to a device they do not describe and a rollback to an
  older engine ignores them.
- It never untracks, deletes or rewrites user notes. A rebase that stops on a conflict is
  aborted so the working tree (which a phone may be reading through a file sync service)
  never carries conflict markers; the local commits stay and the conflict is reported.
- No secret is printed or stored in the vault. The capture token is generated into the state
  directory with owner-only permissions and referenced by path.
- Standard library only.
"""
import calendar
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
import time

SETTINGS_FILE = 'devices.json'
RECORD_FILE = 'devices-sync.json'
LOCK_FILE = 'devices-sync.lock'
LOG_FILE = 'devices-sync.log'
TOKEN_FILE = 'ingest-token.env'
LEVELS = ('solo', 'phone', 'computers', 'hub')
PHONE_SYNCS = ('icloud', 'obsidian-sync', 'other')
ROLES = ('client', 'hub')
SCHEDULERS = ('launchd', 'systemd-user', 'schtasks', 'cron', 'none')
GIT_TIMEOUT = 180
MIN_INTERVAL = 5
MAX_INTERVAL = 1440
DEFAULT_PORT = 8484
OFF_MESSAGE = 'devices sync is off; choose a level with: devices --apply --level computers|hub'
BLOCK_START = '# >>> beyin devices (managed block)'
BLOCK_END = '# <<< beyin devices'
DOC = 'docs/v3/DEVICES.md'

# docs/v3/MULTI-MACHINE.md: every machine installs and updates Beyin itself.
GITIGNORE_LINES = (
    '.beyin-runtime.json', '.beyin-version', 'beyin.py', 'Beyni Guncelle.cmd',
    'Beyni Güncelle.command', 'Beyni Güncelle.sh', 'Beyni Güncelle.desktop', '.pi/extensions/beyin-v3.ts',
    '.claude/settings.local.json', '.claude/scripts/', '.claude/hermes-plugin/',
    '.claude/skills/beyin/', '.claude/skills/beyin-doktor/', '.claude/skills/beyin-guncelle/',
    '.claude/skills/beyin-kontrol/', '.agents/hooks.json', '.agents/skills/beyin/',
    '.agents/skills/beyin-doktor/', '.agents/skills/beyin-guncelle/', '.agents/skills/beyin-kontrol/',
    '.codex/config.toml', '.codex/hooks.json', '.kiro/hooks/beyin-v3.json', '.kiro/skills/beyin/',
    '.kiro/skills/beyin-doktor/', '.kiro/skills/beyin-guncelle/', '.kiro/skills/beyin-kontrol/',
    '.opencode/plugins/beyin-v3.js', '.omp/hooks/pre/beyin-v3.ts', '**/.beyin-compact.lock',
    '.beyin-note-write/', 'daily/v3/', 'knowledge/v3/', '.DS_Store',
    '.obsidian/workspace*.json',
)
# When the user keeps their own files in .claude/scripts the whole folder must not be ignored.
SCRIPTS_DIR_LINE = '.claude/scripts/'
SCRIPTS_FILE_LINES = ('.claude/scripts/beyin_v3*.py', '.claude/scripts/compile.py',
                      '.claude/scripts/flush.py', '.claude/scripts/__pycache__/')
GITATTRIBUTES_LINES = ('receipts/** -text', '**/Last-Session.md merge=union',
                       '**/Journal.md merge=union', '**/Arşiv/*.md merge=union')
_LABEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,31}')
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')
_HOST = re.compile(r'[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?')
_SERVICES = (
    ('Library/Mobile Documents/iCloud~md~obsidian/', 'icloud', True),
    ('Library/Mobile Documents/', 'icloud', False),
    ('Library/CloudStorage/Dropbox', 'dropbox', False),
    ('Library/CloudStorage/OneDrive', 'onedrive', False),
    ('Library/CloudStorage/GoogleDrive', 'google-drive', False),
    ('/iCloudDrive/', 'icloud', False),
    ('/Dropbox/', 'dropbox', False),
    ('/OneDrive', 'onedrive', False),
    ('/Google Drive/', 'google-drive', False),
)


class DevicesError(ValueError):
    pass


def _utc(seconds=None):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


def _atomic(path, text, private=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(4) + '.tmp')
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, 0o600 if private else 0o644)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _vault_key(vault):
    return hashlib.sha256(str(Path(vault)).encode('utf-8')).hexdigest()[:8]


# ---------------------------------------------------------------- settings

def default_settings():
    return {'schema': 1, 'level': 'solo', 'phone_sync': None, 'role': None, 'remote_name': 'origin',
            'branch': 'main', 'interval': 0, 'device_name': None, 'scheduler': None, 'capture': None}


def _valid(data):
    if not isinstance(data, dict) or data.get('schema') != 1:
        return False
    if data.get('level') not in LEVELS:
        return False
    if data.get('phone_sync') not in (None, *PHONE_SYNCS) or data.get('role') not in (None, *ROLES):
        return False
    interval = data.get('interval', 0)
    if isinstance(interval, bool) or not isinstance(interval, int) or (interval and not MIN_INTERVAL <= interval <= MAX_INTERVAL):
        return False
    for key in ('remote_name', 'branch'):
        if not isinstance(data.get(key), str) or not _NAME.fullmatch(data[key]):
            return False
    name = data.get('device_name')
    return name is None or (isinstance(name, str) and bool(_LABEL.fullmatch(name)))


def read_settings(state):
    """Return (settings, valid). A missing file is the valid default; a broken file reads as
    the default with valid False and is never overwritten silently."""
    path = Path(state) / SETTINGS_FILE
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return default_settings(), True
    except OSError:
        return default_settings(), False
    try:
        data = json.loads(raw)
    except ValueError:
        return default_settings(), False
    if not _valid(data):
        return default_settings(), False
    return {**default_settings(), **data}, True


def _read_record(state):
    try:
        data = json.loads((Path(state) / RECORD_FILE).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status(vault, state):
    settings, valid = read_settings(state)
    result = {'valid': valid, 'enabled': settings['level'] != 'solo', **settings}
    record = _read_record(state)
    if record:
        result['last_sync'] = record
    return result


def doctor(state):
    """Read-only line for doctor: only the switch while the feature is off."""
    settings, valid = read_settings(state)
    record = _read_record(state)
    if valid and settings['level'] == 'solo' and not record:
        return {'enabled': False}
    result = {'enabled': settings['level'] != 'solo', 'valid': valid, 'level': settings['level'],
              'phone_sync': settings['phone_sync'], 'role': settings['role'], 'interval': settings['interval']}
    if record:
        result['last_sync'] = {key: record.get(key) for key in ('status', 'at', 'reason') if record.get(key) is not None}
        try:
            at = calendar.timegm(time.strptime(record.get('at', ''), '%Y-%m-%dT%H:%M:%SZ'))
            result['last_sync']['age_minutes'] = max(0, int((time.time() - at) // 60))
        except (TypeError, ValueError, OverflowError):
            pass
    return result


# ---------------------------------------------------------------- git helpers

def _git_exe():
    return shutil.which('git')


def _git(vault, *args, timeout=GIT_TIMEOUT):
    exe = _git_exe()
    if not exe:
        raise DevicesError('git is not installed or not on PATH')
    env = dict(os.environ)
    # Never wait for a password or passphrase in a scheduled run; fixed messages for parsing.
    env.update({'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C', 'LANGUAGE': 'C'})
    env.setdefault('GIT_SSH_COMMAND', 'ssh -o BatchMode=yes -o ConnectTimeout=20')
    try:
        return subprocess.run([exe, '-C', str(vault), *args], capture_output=True, text=True,
                              encoding='utf-8', errors='replace', timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, '', 'timeout after ' + str(timeout) + 's')


def _tail(text, limit=300):
    text = _redact((text or '').strip())
    return text[-limit:]


def _redact(url):
    """Drop credentials embedded in a remote URL before it is printed or stored."""
    url = re.sub(r'(https?://)[^/@\s]+@', r'\1', url or '')
    return re.sub(r'(?<=://)([^/@:\s]+):[^/@\s]+@', r'\1@', url)


def _is_repo(vault):
    if not _git_exe():
        return False
    done = _git(vault, 'rev-parse', '--show-toplevel', timeout=20)
    if done.returncode != 0:
        return False
    try:
        return Path(done.stdout.strip()).resolve() == Path(vault).resolve()
    except OSError:
        return False


def _git_dir(vault):
    done = _git(vault, 'rev-parse', '--absolute-git-dir', timeout=20)
    return Path(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def _remotes(vault):
    done = _git(vault, 'remote', '-v', timeout=20)
    remotes = {}
    if done.returncode == 0:
        for line in done.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                remotes.setdefault(parts[0], parts[1])
    return remotes


def _transport(url):
    if re.match(r'^(https?)://', url):
        return 'https'
    if url.startswith('ssh://') or re.match(r'^[^/\s]+@[^/\s:]+:', url):
        return 'ssh'
    if url.startswith(('git://', 'file://')):
        return url.split(':', 1)[0]
    return 'local'


def _in_progress(git_dir):
    for name, label in (('rebase-merge', 'rebase'), ('rebase-apply', 'rebase'), ('MERGE_HEAD', 'merge'),
                        ('CHERRY_PICK_HEAD', 'cherry-pick'), ('BISECT_LOG', 'bisect')):
        if (git_dir / name).exists():
            return label
    return None


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


def scheduler_kind(platform=None, environ=None):
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    if platform == 'darwin':
        return 'launchd'
    if platform.startswith('win'):
        return 'schtasks'
    if shutil.which('systemctl') and environ.get('XDG_RUNTIME_DIR') and \
            Path(environ['XDG_RUNTIME_DIR'], 'systemd').is_dir():
        return 'systemd-user'
    return 'cron' if shutil.which('crontab') else 'none'


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
        'tools': {name: bool(shutil.which(name)) for name in ('git', 'ssh', 'tailscale')},
        'scheduler': scheduler_kind(),
        'git': {'repository': False},
    }
    if result['tools']['git'] and _is_repo(vault):
        git_dir = _git_dir(vault)
        remotes = _remotes(vault)
        branch = _git(vault, 'symbolic-ref', '--short', '-q', 'HEAD', timeout=20).stdout.strip() or None
        result['git'] = {
            'repository': True, 'branch': branch,
            'remotes': {name: {'transport': _transport(url), 'url': _redact(url)} for name, url in remotes.items()},
            'git_dir_inside_vault': bool(git_dir and (git_dir == vault / '.git' or vault in git_dir.parents)),
            'in_progress': _in_progress(git_dir) if git_dir else None,
        }
    result['levels'] = _eligibility(result)
    result['current_fit'] = _current_fit(result)
    return result


def _eligibility(found):
    tools, levels = found['tools'], {'solo': {'ready': True, 'needs': []}}
    phone = {'icloud': [], 'obsidian-sync': [], 'other': []}
    if found['platform'] != 'macos':
        phone['icloud'].append('a Mac that keeps the vault in the Obsidian iCloud folder')
    elif not found['vault_in_obsidian_ios_folder']:
        phone['icloud'].append('move the vault under iCloud Drive/Obsidian (see ' + DOC + ')')
    if not found['obsidian']['sync_plugin']:
        phone['obsidian-sync'].append('an Obsidian Sync subscription and the Sync core plugin turned on in this vault')
    levels['phone'] = {'ready': any(not needs for needs in phone.values()), 'options': phone, 'needs': []}
    computers = [] if tools['git'] else ['install git']
    levels['computers'] = {'ready': not computers, 'needs': computers}
    hub = list(computers) + ([] if tools['ssh'] else ['install an SSH client'])
    levels['hub'] = {'ready': not hub, 'needs': hub,
                     'recommended': [] if tools['tailscale'] else ['Tailscale (or another private network) between the devices']}
    return levels


def _current_fit(found):
    """What the environment already looks like, as a starting suggestion and nothing more."""
    git = found['git']
    phone = ('icloud' if found['vault_in_obsidian_ios_folder'] else
             'obsidian-sync' if found['obsidian']['sync_plugin'] else
             'other' if found['vault_sync_service'] or found['obsidian']['syncthing'] else None)
    if git.get('repository') and git.get('remotes'):
        level = 'hub' if any(item['transport'] in ('ssh', 'local') for item in git['remotes'].values()) else 'computers'
    else:
        level = 'phone' if phone else 'solo'
    return {'level': level, 'phone_sync': phone}


# ---------------------------------------------------------------- options and plan

def normalize(options, vault, state):
    """Validate CLI options against the stored settings; raise DevicesError with a plain reason."""
    stored, valid = read_settings(state)
    if not valid:
        raise DevicesError(SETTINGS_FILE + ' in the runtime state is invalid; fix or remove it first')
    level = options.get('level') or stored['level']
    if level not in LEVELS:
        raise DevicesError('--level must be one of: ' + ', '.join(LEVELS))
    chosen = {**stored, 'level': level}
    for key in ('phone_sync', 'role', 'remote_name', 'branch', 'device_name'):
        if options.get(key) is not None:
            chosen[key] = options[key]
    if options.get('interval') is not None:
        chosen['interval'] = options['interval']
    if chosen['phone_sync'] not in (None, *PHONE_SYNCS):
        raise DevicesError('--phone-sync must be one of: ' + ', '.join(PHONE_SYNCS))
    if level == 'solo':
        chosen.update({'phone_sync': None, 'role': None, 'interval': 0})
    if level == 'phone':
        if not chosen['phone_sync']:
            raise DevicesError('--level phone needs --phone-sync ' + '|'.join(PHONE_SYNCS))
        chosen.update({'role': None, 'interval': 0})
    if level == 'computers':
        chosen['role'] = None
    if level == 'hub' and chosen['role'] not in ROLES:
        raise DevicesError('--level hub needs --role hub (on the always-on machine) or --role client (elsewhere)')
    interval = chosen['interval']
    if isinstance(interval, bool) or not isinstance(interval, int) or (interval and not MIN_INTERVAL <= interval <= MAX_INTERVAL):
        raise DevicesError('--interval is minutes between sync rounds: 0 (off) or ' + str(MIN_INTERVAL) + '..' + str(MAX_INTERVAL))
    for key in ('remote_name', 'branch'):
        if not _NAME.fullmatch(chosen[key] or ''):
            raise DevicesError('--' + key.replace('_', '-') + ' has unsupported characters')
    if chosen['device_name'] is not None and not _LABEL.fullmatch(chosen['device_name']):
        raise DevicesError('--device-name: letters, digits, space, dot, dash, underscore; at most 32 characters')
    extra = {key: options.get(key) for key in ('remote', 'bare', 'hub_host', 'hub_user', 'hub_path', 'git_dir',
                                               'capture_host')}
    extra['init_git'] = bool(options.get('init_git'))
    extra['capture'] = bool(options.get('capture'))
    extra['capture_port'] = options.get('capture_port') or DEFAULT_PORT
    if not 1024 <= extra['capture_port'] <= 65535:
        raise DevicesError('--capture-port must be between 1024 and 65535')
    if level in ('solo', 'phone'):
        for key in ('remote', 'bare', 'hub_host', 'hub_path', 'git_dir'):
            if extra[key]:
                raise DevicesError('--' + key.replace('_', '-') + ' belongs to --level computers or hub')
    if extra['capture'] and level == 'solo':
        raise DevicesError('--capture needs a level above solo')
    for key in ('bare', 'git_dir'):
        if extra[key]:
            target = Path(extra[key]).expanduser().resolve()
            if target == Path(vault) or Path(vault) in target.parents:
                raise DevicesError('--' + key.replace('_', '-') + ' must be outside the vault')
            if sync_location(target)['service']:
                raise DevicesError('--' + key.replace('_', '-') + ' must not be inside a file sync folder: a synced git directory gets corrupted')
            extra[key] = target
    if extra['bare'] and not (level == 'hub' and chosen['role'] == 'hub'):
        raise DevicesError('--bare creates the central repository; it belongs to --level hub --role hub')
    if extra['hub_host'] and not (level == 'hub' and chosen['role'] == 'client'):
        raise DevicesError('--hub-host belongs to --level hub --role client')
    if extra['hub_host']:
        if not _HOST.fullmatch(extra['hub_host']):
            raise DevicesError('--hub-host must be a host name or address, without user or path')
        if not extra['hub_path'] or not str(extra['hub_path']).startswith('/'):
            raise DevicesError('--hub-host needs --hub-path with the absolute path of the bare repository on the hub')
        if extra['remote']:
            raise DevicesError('use either --remote or --hub-host with --hub-path')
        user = extra['hub_user']
        if user and not _NAME.fullmatch(user):
            raise DevicesError('--hub-user has unsupported characters')
        extra['remote'] = 'ssh://' + (user + '@' if user else '') + extra['hub_host'] + extra['hub_path']
    if extra['bare'] and extra['remote']:
        raise DevicesError('use either --bare (this machine holds the repository) or --remote')
    if extra['bare']:
        extra['remote'] = str(extra['bare'])
    return chosen, extra


def _action(kind, summary, **detail):
    return {'kind': kind, 'summary': summary, **detail}


def _merged_block(existing, lines):
    """Return the file text with the managed block holding every wanted line that is not
    already present outside the block. The rest of the file is left byte for byte."""
    newline = '\r\n' if '\r\n' in existing else '\n'
    body, inside, kept_outside = [], False, set()
    for row in existing.splitlines():
        if row.strip() == BLOCK_START:
            inside = True
            continue
        if row.strip() == BLOCK_END:
            inside = False
            continue
        if not inside:
            body.append(row)
            kept_outside.add(row.strip())
    while body and not body[-1].strip():
        body.pop()
    wanted = [line for line in lines if line not in kept_outside]
    if not wanted:
        merged = body
    else:
        merged = body + ([''] if body else []) + [BLOCK_START, *wanted, BLOCK_END]
    text = newline.join(merged)
    return text + newline if text else ''


def _ignore_lines(vault):
    scripts = Path(vault) / '.claude/scripts'
    foreign = []
    if scripts.is_dir():
        foreign = sorted(item.name for item in scripts.iterdir()
                         if item.name != '__pycache__' and not item.name.startswith('.')
                         and not re.fullmatch(r'beyin_v3(_[a-z]+)*\.py', item.name)
                         and item.name not in ('compile.py', 'flush.py'))
    if not foreign:
        return list(GITIGNORE_LINES), []
    lines = []
    for line in GITIGNORE_LINES:
        lines.extend(SCRIPTS_FILE_LINES if line == SCRIPTS_DIR_LINE else (line,))
    return lines, foreign


def _tracked_machine_files(vault):
    paths = [line.rstrip('/') for line in GITIGNORE_LINES if '*' not in line and line != '.DS_Store']
    done = _git(vault, 'ls-files', '--', *paths, timeout=30)
    return sorted(done.stdout.splitlines())[:40] if done.returncode == 0 else []


def _sync_command(vault, state):
    entry = Path(vault) / 'beyin.py'
    if entry.is_file():
        return [sys.executable, str(entry), 'devices', '--sync-once']
    cli = Path(__file__).with_name('beyin_v3_cli.py')
    if not cli.is_file():
        cli = Path(__file__).resolve().parents[3] / 'scripts/beyin_v3.py'
    return [sys.executable, str(cli), '--vault', str(vault), '--state', str(state), 'devices', '--sync-once']


def _systemd_quote(argument):
    return '"' + argument.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def scheduler_spec(vault, state, interval, kind=None, home=None, command=None):
    """Files and commands that run one sync round every `interval` minutes on this platform.
    Pure: returns the plan, runs nothing."""
    kind = kind or scheduler_kind()
    home = Path(home) if home else Path.home()
    command = command or _sync_command(vault, state)
    key = _vault_key(vault)
    log = str(Path(state) / LOG_FILE)
    if kind == 'launchd':
        label = 'com.avenox.beyin-devices.' + key
        path = home / 'Library/LaunchAgents' / (label + '.plist')
        content = plistlib.dumps({
            'Label': label, 'ProgramArguments': command, 'StartInterval': interval * 60, 'RunAtLoad': True,
            'ProcessType': 'Background', 'StandardOutPath': log, 'StandardErrorPath': log,
        }).decode('utf-8')
        domain = 'gui/' + str(os.getuid() if hasattr(os, 'getuid') else 0)
        return {'kind': kind, 'label': label, 'files': {str(path): content},
                'unload': [['launchctl', 'bootout', domain + '/' + label]],
                'load': [['launchctl', 'bootstrap', domain, str(path)]]}
    if kind == 'systemd-user':
        unit = 'beyin-devices-' + key
        folder = home / '.config/systemd/user'
        service = ('[Unit]\nDescription=Beyin devices sync round\n\n[Service]\nType=oneshot\n'
                   'ExecStart=' + ' '.join(_systemd_quote(item) for item in command) + '\n')
        timer = ('[Unit]\nDescription=Beyin devices sync timer\n\n[Timer]\nOnBootSec=2min\n'
                 'OnUnitActiveSec=' + str(interval) + 'min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n')
        return {'kind': kind, 'label': unit,
                'files': {str(folder / (unit + '.service')): service, str(folder / (unit + '.timer')): timer},
                'unload': [['systemctl', '--user', 'disable', '--now', unit + '.timer']],
                'load': [['systemctl', '--user', 'daemon-reload'],
                         ['systemctl', '--user', 'enable', '--now', unit + '.timer']]}
    if kind == 'schtasks':
        name = 'BeyinDevices-' + key
        windowless = Path(command[0]).with_name('pythonw.exe')
        argv = [str(windowless) if windowless.is_file() else command[0], *command[1:]]
        line = subprocess.list2cmdline(argv)
        return {'kind': kind, 'label': name, 'files': {},
                'unload': [['schtasks', '/Delete', '/F', '/TN', name]],
                'load': [['schtasks', '/Create', '/F', '/SC', 'MINUTE', '/MO', str(interval), '/TN', name, '/TR', line]]}
    quoted = ' '.join("'" + item.replace("'", "'\\''") + "'" for item in command)
    if kind == 'cron':
        manual = ('Add this line with `crontab -e`: */' + str(interval) + ' * * * * ' + quoted + ' >> ' +
                  "'" + log.replace("'", "'\\''") + "' 2>&1") if interval < 60 else (
            'Add this line with `crontab -e`: 0 */' + str(max(1, interval // 60)) + ' * * * ' + quoted)
    else:
        manual = ('No scheduler was found (for example inside a container). Run this from your own startup script: '
                  'while true; do ' + quoted + '; sleep ' + str(interval * 60) + '; done')
    return {'kind': kind, 'label': None, 'files': {}, 'unload': [], 'load': [], 'manual': manual}


def _tailscale_ip():
    exe = shutil.which('tailscale')
    if not exe:
        return None
    try:
        done = subprocess.run([exe, 'ip', '-4'], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    address = done.stdout.strip().splitlines()[0] if done.returncode == 0 and done.stdout.strip() else ''
    return address if re.fullmatch(r'\d{1,3}(\.\d{1,3}){3}', address) else None


def _capture_plan(vault, state, extra, home=None):
    host = extra.get('capture_host') or _tailscale_ip()
    warnings = []
    if not host:
        host = '127.0.0.1'
        warnings.append('No Tailscale address was found, so the capture server would listen on this machine only '
                        '(127.0.0.1). Join the devices to one private network and pass --capture-host <that address>.')
    elif host == '0.0.0.0':
        warnings.append('--capture-host 0.0.0.0 listens on every network this machine is on. Prefer the private '
                        'network address; never forward this port from a router.')
    token_file = Path(state) / TOKEN_FILE
    entry = Path(vault) / 'beyin.py'
    command = [sys.executable, str(entry), 'ingest-server', '--host', host, '--port', str(extra['capture_port'])]
    actions = [_action('token', 'Create a random capture token readable only by this user', path=str(token_file))]
    manual = []
    kind = scheduler_kind()
    if kind == 'systemd-user':
        unit = 'beyin-capture-' + _vault_key(vault)
        path = (Path(home) if home else Path.home()) / '.config/systemd/user' / (unit + '.service')
        content = ('[Unit]\nDescription=Beyin capture server (append-only inbox)\n\n[Service]\n'
                   'EnvironmentFile=' + str(token_file) + '\nExecStart=' +
                   ' '.join(_systemd_quote(item) for item in command) + '\nRestart=on-failure\nRestartSec=15\n\n'
                   '[Install]\nWantedBy=default.target\n')
        actions.append(_action('service', 'Install and start the capture server as a user service',
                               files={str(path): content},
                               load=[['systemctl', '--user', 'daemon-reload'],
                                     ['systemctl', '--user', 'enable', '--now', unit + '.service']]))
        manual.append('Keep the service running after logout on a server: sudo loginctl enable-linger "$USER"')
    else:
        manual.append('Start the capture server from your own service manager or startup script; it reads the token '
                      'from the environment: set -a; . ' + "'" + str(token_file) + "'; set +a; " +
                      ' '.join("'" + item + "'" for item in command))
    manual.append('On the phone, create a share-sheet shortcut that POSTs to http://' + host + ':' +
                  str(extra['capture_port']) + '/ingest with the header "Authorization: Bearer <token>". The token is '
                  'the value after BEYIN_INGEST_TOKEN= in ' + str(token_file) + ' (docs/v3/INGEST-SERVER.md).')
    return {'host': host, 'port': extra['capture_port'], 'actions': actions, 'manual': manual, 'warnings': warnings}


def plan(vault, state, options, home=None):
    """Everything `--apply` would do for these options. Writes nothing."""
    vault, state = Path(vault), Path(state)
    chosen, extra = normalize(options, vault, state)
    stored, _ = read_settings(state)
    level, actions, manual, warnings = chosen['level'], [], [], []
    location = sync_location(vault)
    if sync_location(state)['service']:
        warnings.append('The runtime state is inside a file sync folder. Keep it outside: a synced SQLite index gets corrupted.')

    if chosen['phone_sync'] == 'icloud':
        if sys.platform != 'darwin':
            warnings.append('The iCloud bridge is built for a Mac. On Windows, iCloud Drive may keep notes as '
                            'online-only placeholders that Beyin cannot index.')
        if not location['obsidian_ios_folder']:
            manual.append('Move the vault into iCloud Drive/Obsidian so the iPhone or iPad Obsidian app can open it, '
                          'then run the installer with --rebind-state (docs/v3/MULTI-MACHINE.md, moving a vault).')
        manual.append('On the phone: install Obsidian, choose "Open folder as vault" under iCloud Drive/Obsidian. '
                      'Run Beyin on one computer only at this level; the phone reads and edits notes.')
        warnings.append('iCloud also uploads hidden folders. .beyin-note-write/ holds note backups; they stay in your own '
                        'iCloud account. Do not keep the vault in iCloud and Obsidian Sync at the same time.')
    elif chosen['phone_sync'] == 'obsidian-sync':
        if location['service'] == 'icloud':
            warnings.append('This vault is in iCloud. Obsidian advises against combining iCloud with Obsidian Sync; '
                            'pick one of them for this vault.')
        manual.append('In Obsidian on every device: Settings > Core plugins > Sync, sign in and connect the same remote '
                      'vault. Obsidian Sync skips hidden folders other than .obsidian, so each computer keeps its own '
                      'Beyin installation files.')
    elif chosen['phone_sync'] == 'other':
        manual.append('Exclude .beyin-note-write/ and any .git folder from your sync service if it allows exclusions. '
                      'Conflict copies the service creates are reported by `beyin.py sync`, never merged or deleted.')
    if chosen['phone_sync'] and level == 'phone' and _git_exe() and _is_repo(vault) and location['service']:
        git_dir = _git_dir(vault)
        if git_dir and (git_dir == vault / '.git' or vault in git_dir.parents):
            warnings.append('This vault is a git repository whose .git folder is inside the synced folder. File sync '
                            'services corrupt git data; move it out with --level computers --git-dir <path outside>.')

    scheduler = None
    if level in ('computers', 'hub'):
        if not _git_exe():
            raise DevicesError('git is not installed or not on PATH; install it or choose --level phone')
        repository = _is_repo(vault)
        if not repository:
            if not extra['init_git']:
                raise DevicesError('the vault is not a git repository; pass --init-git to let Beyin create one, '
                                   'or clone your existing repository first')
            actions.append(_action('git_init', 'Create a git repository in the vault', branch=chosen['branch']))
        git_dir = _git_dir(vault) if repository else vault / '.git'
        if extra['git_dir']:
            actions.append(_action('git_dir', 'Move the git directory outside the synced folder',
                                   target=str(extra['git_dir'])))
        elif location['service'] and git_dir and (git_dir == vault / '.git' or vault in git_dir.parents):
            warnings.append('The .git folder is inside a ' + location['service'] + ' folder. File sync services corrupt '
                            'git data; add --git-dir <path outside the synced folder>.')
        ignore_lines, foreign = _ignore_lines(vault)
        if foreign:
            warnings.append('.claude/scripts holds your own files (' + ', '.join(foreign[:5]) + '); only Beyin engine '
                            'files are ignored there.')
        for name, lines in (('.gitignore', ignore_lines), ('.gitattributes', list(GITATTRIBUTES_LINES))):
            path = vault / name
            try:
                existing = path.read_text(encoding='utf-8')
            except FileNotFoundError:
                existing = ''
            merged = _merged_block(existing, lines)
            if merged != existing:
                actions.append(_action('write', 'Add Beyin multi-device lines to ' + name, path=name, content=merged))
        if repository:
            tracked = _tracked_machine_files(vault)
            if tracked:
                warnings.append('Git already tracks machine-specific installation files: ' + ', '.join(tracked[:8]) +
                                ('...' if len(tracked) > 8 else '') + '. .gitignore does not untrack them; follow '
                                '"Daha önce izlenen kurulum dosyalarını ayırmak" in docs/v3/MULTI-MACHINE.md on every machine.')
        remotes = _remotes(vault) if repository else {}
        name = chosen['remote_name']
        if extra['bare'] and not (extra['bare'] / 'HEAD').exists():
            actions.append(_action('git_bare', 'Create the central bare repository on this machine',
                                   target=str(extra['bare']), branch=chosen['branch']))
        if extra['remote']:
            if remotes.get(name) != extra['remote']:
                actions.append(_action('git_remote', ('Point' if name in remotes else 'Add') + ' remote "' + name + '"',
                                       name=name, url=extra['remote'], display=_redact(extra['remote']),
                                       replace=name in remotes))
        elif name not in remotes:
            raise DevicesError('no remote named "' + name + '"; pass --remote <url>' +
                               (' or --bare <path>' if chosen['role'] == 'hub' else '') +
                               (' or --hub-host with --hub-path' if chosen['role'] == 'client' else ''))
        url = extra['remote'] or remotes.get(name, '')
        if _transport(url) == 'https':
            warnings.append('The remote uses HTTPS. A scheduled round cannot type a password: set up a git credential '
                            'helper or use an SSH remote. Use a private repository; the vault holds personal notes.')
        if chosen['interval']:
            scheduler = scheduler_spec(vault, state, chosen['interval'], home=home)
            if scheduler['files'] or scheduler['load']:
                actions.append(_action('scheduler', 'Run a sync round every ' + str(chosen['interval']) + ' minutes (' +
                                       scheduler['kind'] + ')', spec=scheduler))
            else:
                manual.append(scheduler['manual'])
            if scheduler['kind'] == 'launchd' and location['service']:
                warnings.append('macOS blocks background jobs from iCloud Drive, Documents and Desktop. Give Full Disk '
                                'Access to this Python (' + sys.executable + ') in System Settings > Privacy & Security, '
                                'or the scheduled round fails with "Operation not permitted".')
        else:
            manual.append('No schedule was requested. Run `beyin.py devices --sync-once` when you start and finish work, '
                          'or apply again with --interval 15.')
        if level == 'hub':
            if not shutil.which('tailscale'):
                manual.append('Private network (recommended): install Tailscale on this device (https://tailscale.com/download), '
                              'sign in with the same account on every device and use the device name as --hub-host. '
                              'Do not expose SSH or the capture port to the public internet.')
            if chosen['role'] == 'hub':
                manual.append('Let each client reach this machine over SSH with a key (ssh-copy-id from the client). '
                              'Clients then run: beyin.py devices --apply --level hub --role client --hub-host <this machine> '
                              '--hub-path ' + (str(extra['bare']) if extra['bare'] else '<bare repository path>') + ' --interval 15')
            else:
                manual.append('Check the connection once by hand: ssh ' + (_redact(url).split('://', 1)[-1].split('/', 1)[0] or '<hub>') +
                              ' true. The first connection asks to trust the host key; a scheduled round cannot answer that.')

    stale = stored.get('scheduler')
    if stale and (not scheduler or scheduler.get('label') != stale.get('label') or not chosen['interval']):
        actions.insert(0, _action('scheduler_remove', 'Remove the previous sync schedule', spec=stale))
    capture = None
    if extra['capture']:
        capture = _capture_plan(vault, state, extra, home=home)
        actions += capture['actions']
        manual += capture['manual']
        warnings += capture['warnings']
    installed = scheduler if scheduler and (scheduler['files'] or scheduler['load']) else None
    settings = {**chosen, 'schema': 1,
                'scheduler': {'kind': installed['kind'], 'label': installed['label'],
                              'files': sorted(installed['files']), 'unload': installed['unload']} if installed else None,
                'capture': {'host': capture['host'], 'port': capture['port']} if capture else stored.get('capture')}
    if level == 'solo':
        settings['capture'] = None
    return {'level': level, 'phone_sync': chosen['phone_sync'], 'role': chosen['role'],
            'actions': actions, 'manual_steps': manual, 'warnings': warnings, 'settings': settings,
            'changes_settings': settings != stored}


def public_plan(result):
    """The plan as shown to a person: file bodies and remote credentials stay out of it."""
    shown = []
    for action in result['actions']:
        item = {'kind': action['kind'], 'summary': action['summary']}
        for key in ('path', 'target', 'name', 'display', 'branch'):
            if key in action:
                item[key] = action[key]
        spec = action.get('spec')
        if spec:
            item['scheduler'] = spec.get('kind')
            item['files'] = sorted(spec.get('files') or [])
        if 'files' in action:
            item['files'] = sorted(action['files'])
        shown.append(item)
    return {key: value for key, value in result.items() if key not in ('actions', 'settings')} | {'actions': shown}


# ---------------------------------------------------------------- apply

def _run(argv, timeout=60):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except FileNotFoundError:
        return 127, argv[0] + ' not found'
    except subprocess.TimeoutExpired:
        return 124, 'timeout'
    return done.returncode, _tail(done.stderr or done.stdout)


def _require(done, what):
    if done.returncode != 0:
        raise DevicesError(what + ' failed: ' + _tail(done.stderr or done.stdout))


def _execute(vault, state, action):
    kind = action['kind']
    if kind == 'git_init':
        _require(_git(vault, 'init', '-b', action['branch']), 'git init')
    elif kind == 'git_dir':
        target = Path(action['target'])
        if target.exists() and any(target.iterdir()):
            raise DevicesError('--git-dir target already exists and is not empty: ' + str(target))
        target.parent.mkdir(parents=True, exist_ok=True)
        _require(_git(vault, 'init', '--separate-git-dir', str(target)), 'moving the git directory')
    elif kind == 'git_bare':
        target = Path(action['target'])
        target.parent.mkdir(parents=True, exist_ok=True)
        exe = _git_exe()
        done = subprocess.run([exe, 'init', '--bare', '-b', action['branch'], str(target)], capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=60)
        _require(done, 'git init --bare')
    elif kind == 'git_remote':
        verb = ('set-url', action['name'], action['url']) if action['replace'] else ('add', action['name'], action['url'])
        _require(_git(vault, 'remote', *verb), 'git remote')
    elif kind == 'write':
        _atomic(Path(vault) / action['path'], action['content'])
    elif kind == 'token':
        path = Path(action['path'])
        if not path.exists():
            _atomic(path, 'BEYIN_INGEST_TOKEN=' + secrets.token_urlsafe(32) + '\n', private=True)
    elif kind in ('scheduler', 'service'):
        spec = action['spec'] if kind == 'scheduler' else action
        for argv in spec.get('unload', []):
            _run(argv)  # a previous copy may not exist; replacing it is the point
        for path, content in spec['files'].items():
            _atomic(path, content)
        for argv in spec['load']:
            code, output = _run(argv)
            if code != 0:
                raise DevicesError(' '.join(argv[:3]) + ' failed: ' + output)
    elif kind == 'scheduler_remove':
        _remove_scheduler(action['spec'])
    else:
        raise DevicesError('unknown action: ' + kind)


def _remove_scheduler(spec):
    """Undo a schedule this module installed. The record comes from the runtime state; anything
    that does not look like what scheduler_spec writes is ignored instead of executed."""
    if not isinstance(spec, dict) or not isinstance(spec.get('label'), str) or 'eyin' not in spec['label']:
        return
    label = spec['label']
    for argv in spec.get('unload') or []:
        if (isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv)
                and argv[0] in ('launchctl', 'systemctl', 'schtasks') and any(label in item for item in argv)):
            _run(argv)
    for path in spec.get('files') or []:
        if not isinstance(path, str) or label not in Path(path).name:
            continue
        try:
            Path(path).unlink()
        except OSError:
            pass


def apply(vault, state, options, home=None):
    """Run the plan in order and store the settings last. Stops at the first failed action."""
    vault, state = Path(vault), Path(state)
    planned = plan(vault, state, options, home=home)
    done = []
    for action in planned['actions']:
        try:
            _execute(vault, state, action)
        except (DevicesError, OSError) as exc:
            return {**public_plan(planned), 'status': 'failed', 'applied': done,
                    'failed': {'summary': action['summary'], 'error': str(exc)},
                    'note': 'Settings were not changed. Fix the cause and run the same command again; finished actions are skipped.'}
        done.append(action['summary'])
    Path(state).mkdir(parents=True, exist_ok=True)
    _atomic(Path(state) / SETTINGS_FILE, json.dumps(planned['settings'], ensure_ascii=False, indent=2) + '\n')
    return {**public_plan(planned), 'status': 'applied', 'applied': done}


# ---------------------------------------------------------------- one sync round

def _lock(handle):
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _device_label(settings):
    return settings.get('device_name') or ('mac' if sys.platform == 'darwin' else
                                           'windows' if sys.platform.startswith('win') else 'linux')


def _round(vault, settings):
    remote, branch = settings['remote_name'], settings['branch']
    git_dir = _git_dir(vault)
    if git_dir is None or not _is_repo(vault):
        return {'status': 'error', 'reason': 'the vault is not a git repository'}
    busy = _in_progress(git_dir)
    if busy:
        return {'status': 'blocked', 'reason': 'an unfinished ' + busy + ' is open in the vault; finish or abort it by hand'}
    current = _git(vault, 'symbolic-ref', '--short', '-q', 'HEAD', timeout=20).stdout.strip()
    if current != branch:
        return {'status': 'blocked', 'reason': 'the vault is on branch "' + (current or 'detached HEAD') +
                '", the sync branch is "' + branch + '"'}
    done = _git(vault, 'add', '-A')
    if done.returncode != 0:
        return {'status': 'error', 'reason': 'git add failed: ' + _tail(done.stderr)}
    committed = False
    if _git(vault, 'diff', '--cached', '--quiet', timeout=60).returncode == 1:
        message = 'Beyin sync ' + _device_label(settings) + ' ' + time.strftime('%Y-%m-%d %H:%M')
        done = _git(vault, 'commit', '-q', '-m', message)
        if done.returncode != 0:
            return {'status': 'error', 'reason': 'git commit failed (is user.name and user.email set?): ' + _tail(done.stderr)}
        committed = True
    done = _git(vault, 'fetch', '-q', remote)
    if done.returncode != 0:
        return {'status': 'offline', 'committed': committed,
                'reason': 'could not reach remote "' + remote + '": ' + _tail(done.stderr)}
    result = {'committed': committed, 'pulled': False, 'conflicts': []}
    upstream = remote + '/' + branch
    if _git(vault, 'rev-parse', '-q', '--verify', 'refs/remotes/' + upstream, timeout=20).returncode == 0:
        if not _git(vault, 'rev-parse', '-q', '--verify', 'HEAD', timeout=20).stdout.strip():
            done = _git(vault, 'merge', '-q', '--ff-only', upstream)  # an empty local repository joins the remote
            incoming = True
        else:
            incoming = _git(vault, 'rev-list', '--count', 'HEAD..' + upstream, timeout=30).stdout.strip() not in ('', '0')
            done = _git(vault, 'rebase', '--autostash', upstream) if incoming else None
        if done is not None and done.returncode != 0:
            conflicts = [line for line in _git(vault, 'diff', '--name-only', '--diff-filter=U', timeout=30).stdout.splitlines() if line]
            if _in_progress(git_dir):
                _git(vault, 'rebase', '--abort')
            return {**result, 'status': 'conflict', 'conflicts': conflicts[:50],
                    'reason': 'the same lines changed on two devices; local commits are kept and the remote changes were '
                              'not applied. Run `git pull --rebase` in the vault and resolve the listed files.'
                              if conflicts else 'could not apply remote changes: ' + _tail(done.stderr)}
        result['pulled'] = incoming
    head = _git(vault, 'rev-parse', '-q', '--verify', 'HEAD', timeout=20).stdout.strip()
    if not head:
        return {**result, 'status': 'ok', 'pushed': False, 'reason': 'nothing to synchronise yet'}
    remote_head = _git(vault, 'rev-parse', '-q', '--verify', 'refs/remotes/' + upstream, timeout=20).stdout.strip()
    if remote_head == head:
        return {**result, 'status': 'ok', 'pushed': False, 'head': head[:12]}
    done = _git(vault, 'push', '-q', '-u', remote, branch)
    if done.returncode != 0:
        return {**result, 'status': 'retry', 'pushed': False, 'head': head[:12],
                'reason': 'push was not accepted (another device pushed first, or no write access): ' + _tail(done.stderr)}
    return {**result, 'status': 'ok', 'pushed': True, 'head': head[:12]}


def sync_once(vault, state):
    """One round: commit local changes, rebase onto the remote, push. Never leaves conflict
    markers in the working tree. Records the outcome in the runtime state."""
    vault, state = Path(vault), Path(state)
    settings, valid = read_settings(state)
    if not valid:
        raise DevicesError(SETTINGS_FILE + ' in the runtime state is invalid; fix or remove it first')
    if settings['level'] not in ('computers', 'hub'):
        raise DevicesError(OFF_MESSAGE)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / LOCK_FILE, 'a+b') as handle:
        if not _lock(handle):
            return {'status': 'already_running'}
        started = time.time()
        try:
            result = _round(vault, settings)
        except DevicesError as exc:
            result = {'status': 'error', 'reason': str(exc)}
        result['at'] = _utc(started)
        result['seconds'] = round(time.time() - started, 1)
        _atomic(state / RECORD_FILE, json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        return result


def remove_scheduler(vault, state):
    settings, valid = read_settings(state)
    if not valid:
        raise DevicesError(SETTINGS_FILE + ' in the runtime state is invalid; fix or remove it first')
    spec = settings.get('scheduler')
    if not spec:
        return {'status': 'unchanged', 'scheduler': None}
    _remove_scheduler(spec)
    settings.update({'scheduler': None, 'interval': 0})
    _atomic(Path(state) / SETTINGS_FILE, json.dumps(settings, ensure_ascii=False, indent=2) + '\n')
    return {'status': 'removed', 'scheduler': spec.get('kind'), 'label': spec.get('label')}
