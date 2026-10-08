"""Small, text-free stable release discovery. Never imports downloaded code."""
from contextlib import contextmanager
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
sys.dont_write_bytecode = True

OFFICIAL = 'https://api.github.com/repos/avenoxai/avenoxbeyin/releases/latest'
WEB = 'https://github.com/avenoxai/avenoxbeyin/releases/'
# Exact official repository slugs. A GitHub rename (avenoxai/avenoxbeyin -> avenoxai/beyin)
# keeps the old API URL working through a redirect, but the API then reports asset URLs under
# the new name. Both names are accepted, nothing broader: the old name must never be recreated.
OFFICIAL_WEBS = tuple('https://github.com/' + slug + '/releases/' for slug in ('avenoxai/avenoxbeyin', 'avenoxai/beyin'))
MAX_PACKAGE = 32 * 1024 * 1024
DAY = 86400
FRESH = 7 * DAY


class ReleaseError(ValueError):
    def __init__(self, code, retry_after=0):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


def version(value):
    if not isinstance(value, str):
        raise ReleaseError('invalid_version')
    match = re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[a-zA-Z0-9.-]+)?(?:[+][a-zA-Z0-9.-]+)?', value)
    if not match:
        raise ReleaseError('invalid_version')
    return tuple(map(int, match.groups()))


def safe_url(url):
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme != 'https' or parts.username or parts.password or parts.port not in (None, 443)
            or parts.hostname not in {'api.github.com', 'github.com', 'release-assets.githubusercontent.com', 'objects.githubusercontent.com'}):
        raise ReleaseError('untrusted_download_url')
    return url


class OfficialRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, safe_url(newurl))


def request_bytes(url, limit, etag=None):
    headers = {'Accept': 'application/vnd.github+json' if url == OFFICIAL else 'application/octet-stream',
               'User-Agent': 'avenoxbeyin-updater'}
    if etag:
        headers['If-None-Match'] = etag
    request = urllib.request.Request(safe_url(url), headers=headers)
    try:
        with urllib.request.build_opener(OfficialRedirect()).open(request, timeout=5) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ReleaseError('response_too_large')
            return data, response.headers
    except urllib.error.HTTPError as exc:
        with exc:
            if exc.code == 304:
                return None, exc.headers
            retry = 0
            try:
                retry = max(float(exc.headers.get('Retry-After', 0)),
                            float(exc.headers.get('X-RateLimit-Reset', 0)) - time.time())
                if not math.isfinite(retry): retry = 0
            except (ValueError, TypeError):
                pass
            raise ReleaseError('http_' + str(exc.code), max(0, retry)) from None
    except (OSError, urllib.error.URLError):
        raise ReleaseError('network_unavailable') from None


def release_metadata(release):
    if not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False:
        raise ReleaseError('no_stable_release')
    tag = release.get('tag_name', '')
    v = tag.removeprefix('v') if isinstance(tag, str) else ''
    version(v)
    expected = 'beyin-v3-' + v + '.zip'
    assets = release.get('assets', [])
    if not isinstance(assets, list): raise ReleaseError('invalid_assets')
    matches = [a for a in assets if isinstance(a, dict) and a.get('name') == expected]
    if len(matches) != 1: raise ReleaseError('missing_package')
    asset = matches[0]
    web = next((base for base in OFFICIAL_WEBS if asset.get('browser_download_url') == base + 'download/' + tag + '/' + expected), None)
    if asset.get('state') != 'uploaded' or web is None:
        raise ReleaseError('invalid_asset')
    url = web + 'download/' + tag + '/' + expected
    size = asset.get('size')
    if type(size) is not int or not 0 < size <= MAX_PACKAGE: raise ReleaseError('invalid_asset_size')
    digest = asset.get('digest')
    if digest is not None and not (isinstance(digest, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', digest)):
        raise ReleaseError('invalid_asset_digest')
    checksum = [a for a in assets if isinstance(a, dict) and a.get('name') == expected + '.sha256']
    checksum_url = None
    if checksum:
        checksum_url = url + '.sha256'
        if (len(checksum) != 1 or checksum[0].get('state') != 'uploaded'
                or checksum[0].get('browser_download_url') != checksum_url):
            raise ReleaseError('invalid_checksum_asset')
    if not digest and not checksum_url: raise ReleaseError('missing_package_digest')
    published = release.get('published_at')
    if not isinstance(published, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', published):
        raise ReleaseError('invalid_release_date')
    if type(release.get('id')) is not int or type(asset.get('id')) is not int:
        raise ReleaseError('invalid_release_id')
    return {'release_id': release['id'], 'version': v, 'published_at': published,
            'release_url': web + 'tag/' + tag, 'asset_id': asset['id'], 'asset_name': expected,
            'asset_url': url, 'asset_size': size, 'asset_sha256': digest[7:] if digest else None,
            'checksum_url': checksum_url}


def fetch_metadata(etag=None):
    data, headers = request_bytes(OFFICIAL, 1024 * 1024, etag)
    tag = headers.get('ETag')
    if tag is not None and (not isinstance(tag, str) or len(tag) > 300 or '\n' in tag or '\r' in tag):
        tag = None
    if data is None: return None, tag or etag
    try:
        value = release_metadata(json.loads(data))
    except (TypeError, KeyError, json.JSONDecodeError, UnicodeError):
        raise ReleaseError('invalid_release_metadata') from None
    return value, tag


def installed_version(vault):
    path = Path(vault) / '.beyin-version'
    current = path.read_text(encoding='utf-8').strip() if path.exists() else '0.0.0'
    version(current)
    return current


def compare(current, metadata):
    old, new = version(current), version(metadata['version'])
    return dict(metadata, current_version=current, status='available' if new > old else 'up_to_date' if new == old else 'ahead',
                verification='metadata_only')


def check(vault):
    metadata, _ = fetch_metadata()
    if metadata is None: raise ReleaseError('unexpected_not_modified')
    return compare(installed_version(vault), metadata)


def read_json(path):
    path = Path(path)
    if path.is_symlink(): raise ReleaseError('unsafe_state_file')
    if not path.exists(): return None
    with path.open('rb') as stream:
        data = stream.read(16385)
    if len(data) > 16384: raise ReleaseError('invalid_state_file')
    return json.loads(data)


def atomic_json(path, value):
    path = Path(path)
    if path.is_symlink(): raise ReleaseError('unsafe_state_file')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.release-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, ensure_ascii=True)
            out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def preferences(state, enabled=None):
    path = Path(state) / 'release-preferences.json'
    value = read_json(path)
    if value is None: value = {'schema': 1, 'notifications': True}
    if (not isinstance(value, dict) or value.get('schema') != 1
            or type(value.get('notifications')) is not bool or set(value) != {'schema', 'notifications'}):
        raise ReleaseError('invalid_update_preferences')
    if enabled is not None:
        value['notifications'] = bool(enabled)
        atomic_json(path, value)
    return dict(value, effective=value['notifications'] and os.environ.get('BEYIN_UPDATES_OFF') != '1')


def cache(state):
    value = read_json(Path(state) / 'release-cache.json')
    if value is None: return {}
    if not isinstance(value, dict) or value.get('schema') != 1: raise ReleaseError('invalid_release_cache')
    for key in ('checked_at', 'next_check_at', 'attempted_at'):
        number = value.get(key, 0)
        if type(number) not in (int, float) or not math.isfinite(number): raise ReleaseError('invalid_release_cache')
    if type(value.get('failures', 0)) is not int or not 0 <= value.get('failures', 0) <= 3:
        raise ReleaseError('invalid_release_cache')
    if value.get('etag') is not None and (not isinstance(value['etag'], str) or len(value['etag']) > 300 or any(c in value['etag'] for c in '\r\n')):
        raise ReleaseError('invalid_release_cache')
    if 'release' in value:
        m = value['release']
        if not isinstance(m, dict): raise ReleaseError('invalid_release_cache')
        version(m.get('version'))
        # Cached text is untrusted too: only a canonical version-derived URL is ever displayed.
        if m.get('release_url') not in [web + 'tag/' + prefix + m['version'] for web in OFFICIAL_WEBS for prefix in ('', 'v')]:
            raise ReleaseError('invalid_release_cache')
    return value


def predates_install(vault, value, now, key='checked_at'):
    """A cached latest older than the installed version, checked before that version was stamped.

    An update or rollback run by an older updater leaves the cache it found (#188): the cached
    latest then describes the release before the update, and the installed version reads as
    ahead of it until the cache expires. Only that case is set aside; a dev install ahead of a
    check made after it, and every other status, keep the cache. A stamp dated in the future
    (clock skew, a synced vault) never counts, so a refresh always settles it. The worker gate
    asks with key='attempted_at': one attempt per install, an offline failure keeps its backoff.
    """
    try:
        release = value.get('release')
        if not release or version(installed_version(vault)) <= version(release.get('version')):
            return False
        stamped = (Path(vault) / '.beyin-version').stat().st_mtime
        return value.get(key, value.get('checked_at', 0)) < stamped <= now
    except (ValueError, OSError, TypeError, AttributeError):
        return False


def status(vault, state, now=None):
    now = time.time() if now is None else now
    try:
        if not preferences(state)['effective']: return {'status': 'disabled'}
        value = cache(state)
        if not value: return {'status': 'unknown'}
        checked = value.get('checked_at', 0)
        if not value.get('release') or value.get('last_error_code') or not 0 <= now - checked <= FRESH:
            return {'status': 'unavailable', 'checked_at': checked, 'reason': value.get('last_error_code') or 'stale_cache'}
        if predates_install(vault, value, now): return {'status': 'unknown'}
        # Do not leak or display arbitrary extra fields from a user-edited cache.
        m = value['release']
        return dict(compare(installed_version(vault), {'version': m['version'], 'release_url': m['release_url']}), checked_at=checked)
    except (ValueError, OSError, TypeError, KeyError):
        return {'status': 'unavailable', 'reason': 'invalid_update_state'}


@contextmanager
def coordination(state):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state / 'release-notices.sqlite3'
    if path.is_symlink(): raise ReleaseError('unsafe_state_file')
    db = sqlite3.connect(path, timeout=0.1)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS claims (name TEXT PRIMARY KEY, at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS notices (vault TEXT, version TEXT, PRIMARY KEY(vault,version))')
            yield db
    finally:
        db.close()


def claim_worker(state, now=None, vault=None):
    now = time.time() if now is None else now
    try:
        if not preferences(state)['effective']: return False
        value = cache(state)
        # Clock reversal resets cadence, never suppresses checks indefinitely.
        due = value.get('next_check_at', 0)
        if (value.get('attempted_at', value.get('checked_at', 0)) <= now < due
                and not (vault is not None and predates_install(vault, value, now, 'attempted_at'))):
            return False
        with coordination(state) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT at FROM claims WHERE name='worker'").fetchone()
            if row and 0 <= now - row[0] < 60: return False
            db.execute("INSERT OR REPLACE INTO claims VALUES ('worker',?)", (now,))
        return True
    except (ValueError, OSError, sqlite3.Error, TypeError):
        return False


def refresh(state, now=None):
    now = time.time() if now is None else now
    try:
        if not preferences(state)['effective']: return
        previous = cache(state)
        metadata, etag = fetch_metadata(previous.get('etag') if previous.get('release') else None)
        if metadata is None:
            metadata = previous.get('release')
            if metadata is None: raise ReleaseError('unexpected_not_modified')
        value = {'schema': 1, 'checked_at': now, 'attempted_at': now, 'next_check_at': now + DAY,
                 'etag': etag, 'release': metadata, 'failures': 0}
    except ReleaseError as exc:
        previous = locals().get('previous', {})
        failures = min(previous.get('failures', 0) + 1, 3)
        value = dict(previous, schema=1, failures=failures, attempted_at=now,
                     next_check_at=now + max((3600, 21600, DAY)[failures - 1], exc.retry_after),
                     last_error_code=exc.code)
    atomic_json(Path(state) / 'release-cache.json', value)


def vault_key(vault):
    return hashlib.sha256(str(Path(vault).resolve()).encode()).hexdigest()[:24]


def dismiss(vault, state, target):
    version(target)
    with coordination(state) as db:
        db.execute('INSERT OR IGNORE INTO notices VALUES (?,?)', (vault_key(vault), target))
    return {'status': 'dismissed', 'version': target}


def notification(vault, state, now=None):
    result = status(vault, state, now)
    if result['status'] != 'available': return ''
    try:
        with coordination(state) as db:
            changed = db.execute('INSERT OR IGNORE INTO notices VALUES (?,?)', (vault_key(vault), result['version'])).rowcount
        if not changed: return ''
        command = 'py -3' if os.name == 'nt' else 'python3'
        return ('Beyin ' + result['version'] + ' hazir. Surum notlari: ' + result['release_url'] +
                '\nGuncelle: ' + command + ' beyin.py update (vault klasorunde) veya ajanina "beynimi guncelle" de.\n')
    except (ValueError, OSError, sqlite3.Error):
        return ''


def session_start(vault, state):
    """No network on the hook path; an independent short-lived process handles discovery."""
    try:
        text = notification(vault, state)
        if os.environ.get('BEYIN_V3_NO_SPAWN') != '1' and claim_worker(state, vault=vault):
            options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL,
                       'stderr': subprocess.DEVNULL, 'close_fds': True}
            if os.name == 'nt': options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            else: options['start_new_session'] = True
            subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(state)], **options)
        return text
    except Exception:
        return locals().get('text', '')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', required=True, type=Path)
    args = parser.parse_args()
    # Bound the entire detached process, including DNS and a slow streaming peer.
    watchdog = threading.Timer(10, lambda: os._exit(1))
    watchdog.daemon = True
    watchdog.start()
    try:
        refresh(args.worker)
    except Exception:
        pass
    finally:
        watchdog.cancel()
