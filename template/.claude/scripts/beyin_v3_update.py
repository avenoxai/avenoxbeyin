"""Verified stable-release updates with local journal recovery. Standard library only."""
from contextlib import contextmanager, nullcontext
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

OFFICIAL = 'https://api.github.com/repos/avenoxai/avenoxbeyin/releases/latest'
MAX_PACKAGE = 32 * 1024 * 1024
# The installer mirrors each starter skill into both roots, so a package may carry an
# exemption for either copy. Mirrors install_v3.MANAGED_SKILL_PATHS.
MANAGED_SKILL_PATHS = frozenset(root + '/skills/' + name + '/SKILL.md' for root in ('.agents', '.claude')
                                for name in ('beyin', 'beyin-doktor', 'beyin-guncelle'))


def transaction_hook(phase, index=None):
    """Fault-injection seam; no behavior in normal use."""


def digest(data):
    return hashlib.sha256(data).hexdigest()


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)', value):
        raise ValueError('stable semantic version required')
    return tuple(map(int, value.split('.')))


def allowed(name):
    return name in ('scripts/install_v3.py', 'scripts/beyin_v3.py', 'scripts/beyin_entry.py') or bool(re.fullmatch(r'template/\.claude/scripts/beyin_v3(?:_[a-z]+)*\.py', name)) or bool(re.fullmatch(r'template/\.agents/skills/(beyin|beyin-doktor|beyin-guncelle)/SKILL\.md', name))


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.beyin-update-')
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            fd = os.open(path.parent, os.O_RDONLY)
            try: os.fsync(fd)
            finally: os.close(fd)
        except OSError:
            pass
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def encode(data):
    return base64.b64encode(data).decode() if data is not None else None


def decode(data):
    return base64.b64decode(data) if data is not None else None


def jbytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()


def unchanged(actual, *expected):
    """Managed files are UTF-8 text, so a CRLF rewrite by git autocrlf or an editor is not an edit."""
    return any(actual == value or (actual is not None and value is not None and
                                   actual.replace(b'\r\n', b'\n') == value.replace(b'\r\n', b'\n'))
               for value in expected)


def roots(vault, state):
    vault, state = Path(vault).resolve(), Path(state).resolve()
    if not vault.is_dir() or state.is_relative_to(vault):
        raise ValueError('existing vault and dedicated external state required')
    return vault, state


def current_version(vault):
    path = vault / '.beyin-version'
    value = path.read_text(encoding='utf-8').strip() if path.exists() else '0.0.0'
    version(value)
    return value


def validate_package(package):
    with zipfile.ZipFile(package) as archive:
        infos = archive.infolist()
        names = [entry.filename for entry in infos]
        if len(names) != len(set(names)) or sum(i.file_size for i in infos) > MAX_PACKAGE:
            raise ValueError('duplicate or oversized package')
        for entry in infos:
            name = entry.filename
            if name != 'manifest.json' and not allowed(name):
                raise ValueError('package path outside allowlist')
            if PurePosixPath(name).is_absolute() or '..' in PurePosixPath(name).parts or '\\' in name or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('unsafe archive entry')
        manifest = json.loads(archive.read('manifest.json'))
        if manifest.get('schema') != 1 or manifest.get('runtime_schema', 1) != 1 or not isinstance(manifest.get('files'), dict):
            raise ValueError('unsupported package schema')
        version(manifest.get('version'))
        if manifest.get('migrations', []) not in ([], ['v2-cutover']):
            raise ValueError('unsupported migration')
        legacy_skill_hashes = manifest.get('legacy_skill_hashes', {})
        if not isinstance(legacy_skill_hashes, dict) or any(
                name not in MANAGED_SKILL_PATHS or not isinstance(values, list) or
                not all(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value) for value in values)
                for name, values in legacy_skill_hashes.items()):
            raise ValueError('invalid legacy skill hashes')
        minimum = manifest.get('min_python', '3.11')
        if not isinstance(minimum, str) or not re.fullmatch(r'\d+\.\d+', minimum) or tuple(map(int, minimum.split('.'))) > sys.version_info[:2]:
            raise ValueError('unsupported Python runtime')
        if set(names) != set(manifest['files']) | {'manifest.json'}:
            raise ValueError('manifest/archive file mismatch')
        files = {}
        for name, expected in manifest['files'].items():
            data = archive.read(name)
            if not isinstance(expected, str) or digest(data) != expected:
                raise ValueError('package checksum mismatch')
            files[name] = data
        required = {'scripts/install_v3.py', 'scripts/beyin_entry.py', 'scripts/beyin_v3.py', 'template/.claude/scripts/beyin_v3.py', 'template/.claude/scripts/beyin_v3_update.py'}
        if not required <= files.keys():
            raise ValueError('incomplete release package')
        return manifest, files


def _download(directory):
    import beyin_v3_releases as releases
    metadata, _ = releases.fetch_metadata()
    if metadata is None: raise ValueError('release metadata missing')
    data, _ = releases.request_bytes(metadata['asset_url'], MAX_PACKAGE)
    if data is None or len(data) != metadata['asset_size']:
        raise ValueError('release asset size mismatch')
    actual = digest(data)
    expected = metadata['asset_sha256']
    if expected and actual != expected:
        raise ValueError('release asset checksum mismatch')
    if metadata['checksum_url']:
        checksum, _ = releases.request_bytes(metadata['checksum_url'], 1024)
        try:
            line = checksum.decode('ascii').strip()
        except (AttributeError, UnicodeError):
            raise ValueError('invalid release checksum file') from None
        match = re.fullmatch(r'([a-f0-9]{64})  ' + re.escape(metadata['asset_name']), line)
        if not match or match[1] != actual:
            raise ValueError('release checksum file mismatch')
    elif not expected:
        raise ValueError('release package has no external checksum')
    path = Path(directory) / metadata['asset_name']
    path.write_bytes(data)
    return path, metadata['version']


@contextmanager
def locked(vault, state):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    connections = []
    try:
        # Use the same SQLite writer lock as participating runtime operations.
        for path in (state / 'update-lock.sqlite3', state / 'memory.sqlite3'):
            connection = sqlite3.connect(path, timeout=5)
            connections.append(connection)
            connection.execute('BEGIN IMMEDIATE')
        yield
    except sqlite3.OperationalError as exc:
        raise ValueError('runtime/updater busy; retry after active work finishes') from exc
    finally:
        for connection in reversed(connections): connection.close()


def _destination(vault, state, operation):
    root = vault if operation['scope'] == 'vault' else state
    name = operation['name']
    if Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError('unsafe transaction path')
    path = root / name
    # Resolve both sides now: a root resolved before it existed gains a Windows redirect
    # (MSIX-virtualised AppData, junction) once created. Links that leave it still fail.
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('transaction target escapes root')
    return path


def _runtime_health(state):
    path = state / 'memory.sqlite3'
    if not path.exists(): return
    connection = sqlite3.connect(path)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('installed runtime database integrity check failed')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for name, required in {'records': {'id','payload'}, 'receipts': {'id','payload'}, 'events': {'sequence','event_type','record_id','revision','record'}}.items():
            if name in tables:
                columns = {row[1] for row in connection.execute('PRAGMA table_info(' + name + ')')}
                if not required <= columns:
                    raise ValueError('installed runtime schema unsupported: ' + name)
    finally:
        connection.close()


def _apply(vault, state, journal, migration=None):
    for index, operation in enumerate(journal['operations']):
        path = _destination(vault, state, operation)
        if operation['name'] == '.beyin-version':
            _runtime_health(state)
            for prior in journal['operations'][:index]:
                target = _destination(vault, state, prior)
                actual_prior = target.read_bytes() if target.exists() else None
                if actual_prior != decode(prior['new']):
                    raise ValueError('managed file changed before version verification')
            if migration is not None and journal['direction'] == 'update':
                migration[0].finalize_migration(vault, state, migration[1])
                marker = state / 'v2-migration.json'
                journal['migration_result'] = encode(marker.read_bytes()) if marker.exists() else None
                atomic(state / 'update-journal.json', jbytes(journal))
        before, after = decode(operation['old']), decode(operation['new'])
        actual = path.read_bytes() if path.exists() else None
        if actual != after:
            if actual != before:
                raise ValueError('update conflict: managed target changed ' + operation['name'])
            if after is None:
                path.unlink(missing_ok=True)
            else:
                atomic(path, after)
                if operation.get('new_mode') is not None: path.chmod(operation['new_mode'])
        if path.exists() and operation.get('new_mode') is not None:
            path.chmod(operation['new_mode'])
        transaction_hook('after_replace', index)
    if journal['direction'] == 'update':
        atomic(state / 'last-update.json', jbytes(journal))
    else:
        (state / 'last-update.json').unlink(missing_ok=True)
    (state / 'update-journal.json').unlink(missing_ok=True)


@contextmanager
def _migration_guard(vault, state, directory, original=None):
    module_path = directory / 'beyin_v3_migrate.py'
    if not module_path.exists():
        yield None
        return
    sys.path.insert(0, str(directory))
    try:
        spec = importlib.util.spec_from_file_location('beyin_update_migration', module_path)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        with module.migration_guard(vault, state) as current:
            yield module, original or current
    finally:
        sys.path.remove(str(directory))


def _recover_locked(vault, state):
    path = state / 'update-journal.json'
    if not path.exists(): return {'status': 'noop', 'version': current_version(vault)}
    journal = json.loads(path.read_text(encoding='utf-8'))
    if journal.get('vault') != str(vault): raise ValueError('journal belongs to another vault')
    with tempfile.TemporaryDirectory(prefix='beyin-recover-') as temporary:
        directory = Path(temporary)
        for operation in journal['operations']:
            if operation['scope'] == 'vault' and operation['name'].startswith('.claude/scripts/') and operation['name'].endswith('.py') and operation['new']:
                (directory / Path(operation['name']).name).write_bytes(decode(operation['new']))
        with _migration_guard(vault, state, directory, journal.get('migration_plan')) as migration:
            _apply(vault, state, journal, migration)
    return {'status': 'recovered', 'version': current_version(vault)}


def recover(vault, state):
    vault, state = roots(vault, state)
    with locked(vault, state): return _recover_locked(vault, state)


def _preflight(stage, files):
    for name, data in files.items():
        if name.endswith('.py'):
            compile(data, name, 'exec')
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    test_vault, test_state = stage.parent / 'probe-vault', stage.parent / 'probe-state'
    test_vault.mkdir()
    (test_vault / 'proof.md').write_text('---\n{"id":"release-probe","kind":"fact","status":"active"}\n---\nsynthetic checksum verification\n', encoding='utf-8')
    receipt = stage.parent / 'probe-receipt.json'
    receipt.write_text(json.dumps({'event_id':'release-probe','summary':'Synthetic release validation','refs':['proof.md']}), encoding='utf-8')
    environment = os.environ.copy()
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    for command in (['init'], ['sync'], ['context','checksum'], ['receipt','--file',str(receipt)]):
        result = subprocess.run([sys.executable,str(stage/'scripts/beyin_v3.py'),'--vault',str(test_vault),'--state',str(test_state)]+command,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, env=environment)
        if result.returncode:
            raise ValueError('release synthetic runtime preflight failed: ' + command[0])
        try: payload = json.loads(result.stdout)
        except ValueError as exc: raise ValueError('release preflight returned invalid result') from exc
        if command[0] == 'context' and not payload.get('records'):
            raise ValueError('release preflight could not retrieve source')
        if command[0] in ('sync','receipt') and payload.get('status') != 'succeeded':
            raise ValueError('release preflight did not succeed')


def update(vault, state, package=None, check=False):
    vault, state = roots(vault, state)
    with tempfile.TemporaryDirectory(prefix='beyin-release-') as temporary:
        expected = None
        if package is None: package, expected = _download(temporary)
        manifest, files = validate_package(package)
        if expected and manifest['version'] != expected: raise ValueError('release tag/package mismatch')
        stage = Path(temporary) / 'package'
        _preflight(stage, files)
        old, new = current_version(vault), manifest['version']
        if version(new) < version(old): raise ValueError('downgrade requires rollback')
        if check: return {'status': 'noop' if new == old else 'available', 'current_version': old, 'version': new}
        with locked(vault, state):
            _recover_locked(vault, state)
            old = current_version(vault)
            if version(new) <= version(old):
                if new == old: return {'status': 'noop', 'version': old}
                raise ValueError('downgrade requires rollback')
            with _migration_guard(vault, state, stage / 'template/.claude/scripts') as migration:
                spec = importlib.util.spec_from_file_location('beyin_release_installer', stage / 'scripts/install_v3.py')
                installer = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(installer)
                plan = installer.install(vault, state, plan_only=True, version=new,
                                         legacy_hashes=manifest.get('legacy_hashes', {}),
                                         legacy_skill_hashes=manifest.get('legacy_skill_hashes', {}))
                trust_review = False
                for name in ('.claude/settings.local.json', '.codex/hooks.json', '.agents/hooks.json'):
                    if name in plan['planned']:
                        path = vault / name
                        before = json.loads(path.read_bytes()) if path.exists() else {}
                        after = json.loads(plan['planned'][name])
                        key = 'beyin-v3' if name == '.agents/hooks.json' else 'hooks'
                        trust_review = trust_review or before.get(key) != after.get(key)
                operations = []
                for name, data in plan['planned'].items():
                    if name == '.beyin-version': continue
                    path = vault / name
                    previous = path.read_bytes() if path.exists() else None
                    operations.append({'scope': 'vault', 'name': name, 'old': encode(previous), 'new': encode(data), 'old_mode': stat.S_IMODE(path.stat().st_mode) if path.exists() else None, 'new_mode': plan.get('modes', {}).get(name, 0o644)})
                path = state / 'v3-install.json'
                operations.append({'scope': 'state', 'name': 'v3-install.json', 'old': encode(path.read_bytes() if path.exists() else None), 'new': encode(jbytes(plan['manifest']))})
                path = vault / '.beyin-version'
                operations.append({'scope': 'vault', 'name': '.beyin-version', 'old': encode(path.read_bytes() if path.exists() else None), 'new': encode((new + '\n').encode())})
                journal = {'schema': 1, 'vault': str(vault), 'direction': 'update', 'from_version': old, 'to_version': new, 'operations': operations, 'migration_plan': migration[1] if migration else None, 'migration_backup': encode((state/'v2-migration.json').read_bytes() if (state/'v2-migration.json').exists() else None)}
                atomic(state / 'update-journal.json', jbytes(journal))
                _apply(vault, state, journal, migration)
        return {'status': 'updated', 'from_version': old, 'version': new, 'trust_review_required': trust_review}


def _merge_json(base, current, target):
    absent = object()
    def merge(b, c, t):
        if c == b: return t
        if t == b: return c
        if c == t: return c
        if all(isinstance(v, dict) for v in (b, c, t)):
            result = {}
            for key in b.keys() | c.keys() | t.keys():
                value = merge(b.get(key, absent), c.get(key, absent), t.get(key, absent))
                if value is not absent: result[key] = value
            return result
        raise ValueError('overlapping configuration edit')
    return jbytes(merge(json.loads(base), json.loads(current), json.loads(target)))


def rollback(vault, state):
    vault, state = roots(vault, state)
    with locked(vault, state):
        pending = state / 'update-journal.json'
        backup = pending if pending.exists() else state / 'last-update.json'
        if not backup.exists(): raise ValueError('no update backup available')
        original = json.loads(backup.read_text(encoding='utf-8'))
        if original.get('vault') != str(vault): raise ValueError('backup belongs to another vault')
        # A pending rollback already contains the intended restore direction.
        # Reversing it again would restore the upgrade instead of finishing rollback.
        if pending.exists() and original.get('direction') == 'rollback':
            _apply(vault, state, original)
            return {'status': 'rolled_back' if (vault/'.beyin-version').exists() else 'uninstalled', 'version': current_version(vault) if (vault/'.beyin-version').exists() else None}
        operations = []
        for item in original['operations']:
            path = _destination(vault, state, item)
            actual = path.read_bytes() if path.exists() else None
            restore = decode(item['old'])
            if not unchanged(actual, decode(item['new']), restore):
                if actual is not None and restore is not None and item['name'] in ('.claude/settings.local.json', '.claude/settings.json', '.codex/hooks.json', '.agents/hooks.json'):
                    restore = _merge_json(decode(item['new']), actual, restore)
                else:
                    raise ValueError('rollback conflict: changed managed file ' + item['name'] +
                                     (' (deleted)' if actual is None else ' (content differs)'))
            operations.append(dict(item, old=encode(actual), new=encode(restore), old_mode=item.get('new_mode'), new_mode=item.get('old_mode')))
        if 'migration_result' in original:
            marker = state / 'v2-migration.json'
            actual = marker.read_bytes() if marker.exists() else None
            if actual != decode(original['migration_result']):
                raise ValueError('migration marker changed; rollback conflict')
            operations.append({'scope':'state','name':'v2-migration.json','old':encode(actual),'new':original.get('migration_backup')})
        operations.sort(key=lambda item: item['name'] == '.beyin-version')
        journal = {'schema': 1, 'vault': str(vault), 'direction': 'rollback', 'operations': operations}
        atomic(pending, jbytes(journal))
        _apply(vault, state, journal)
        return {'status': 'rolled_back' if (vault/'.beyin-version').exists() else 'uninstalled', 'version': current_version(vault) if (vault/'.beyin-version').exists() else None}
