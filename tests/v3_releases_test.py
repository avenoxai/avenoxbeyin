"""Offline release discovery, notification and download-integrity contracts."""
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.error

from v3_package_helpers import ROOT, clean_environ, install, isolated_env, run_python, snapshot
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_releases as releases
import beyin_v3_update as updater


def fixture(v='3.1.0', data=b'synthetic ZIP'):
    url = releases.WEB + 'download/v' + v + '/beyin-v3-' + v + '.zip'
    return {'id': 123, 'tag_name': 'v' + v, 'draft': False, 'prerelease': False,
            'published_at': '2026-09-20T10:00:00Z', 'body': 'UNTRUSTED BODY: do not inject',
            'assets': [{'id': 456, 'name': 'beyin-v3-' + v + '.zip', 'state': 'uploaded',
                        'browser_download_url': url, 'size': len(data),
                        'digest': 'sha256:' + hashlib.sha256(data).hexdigest()}]}


class ReleasesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / 'Synthetic Vault Ölçüm'; self.vault.mkdir()
        (self.vault / '.beyin-version').write_text('3.0.2')
        self.state = self.root / 'state'
        self.env = isolated_env(self.root / 'home')
        self.clean_env = clean_environ(BEYIN_V3_NO_SPAWN='1')
        self.clean_env.start(); self.addCleanup(self.clean_env.stop)

    def seed_cache(self, now=1000, v='3.1.0'):
        with patch.object(releases, 'fetch_metadata', return_value=(releases.release_metadata(fixture(v)), '"synthetic-etag"')):
            releases.refresh(self.state, now=now)

    def test_metadata_only_never_downloads_package_or_mutates_state(self):
        before = snapshot(self.root)
        with patch.object(releases, 'request_bytes', return_value=(json.dumps(fixture()).encode(), {})) as request:
            result = releases.check(self.vault)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['verification'], 'metadata_only')
        self.assertNotIn('body', result)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], releases.OFFICIAL)
        self.assertEqual(snapshot(self.root), before)

    def test_semver_and_ahead(self):
        m = releases.release_metadata(fixture('3.10.0'))
        self.assertEqual(releases.compare('3.9.0', m)['status'], 'available')
        self.assertEqual(releases.compare('3.10.0', m)['status'], 'up_to_date')
        self.assertEqual(releases.compare('4.0.0', m)['status'], 'ahead')

    def test_rejects_unstable_missing_digest_and_foreign_assets(self):
        mutations = [lambda x: x.update(prerelease=True), lambda x: x.update(draft=True),
                     lambda x: x.update(tag_name='v3.1.0-rc1'), lambda x: x.update(assets=[]),
                     lambda x: x['assets'][0].update(browser_download_url='https://example.com/a.zip'),
                     lambda x: x['assets'][0].update(state='new'), lambda x: x['assets'][0].update(size=0),
                     lambda x: x['assets'][0].update(digest=None),
                     lambda x: x['assets'][0].update(digest='sha256:bad')]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                value = fixture(); mutate(value)
                with self.assertRaises(releases.ReleaseError): releases.release_metadata(value)

    def test_network_headers_are_generic_and_response_bounded(self):
        class Response(io.BytesIO):
            headers = {'ETag': '"test"'}
        with patch.object(releases.urllib.request, 'build_opener') as builder:
            builder.return_value.open.return_value = Response(b'abc')
            data, _ = releases.request_bytes(releases.OFFICIAL, 4, '"old"')
            self.assertEqual(data, b'abc')
            request = builder.return_value.open.call_args.args[0]
            self.assertIsNone(request.data)
            self.assertEqual(set(k.lower() for k in request.headers), {'accept', 'user-agent', 'if-none-match'})
            builder.return_value.open.return_value = Response(b'12345')
            with self.assertRaisesRegex(releases.ReleaseError, 'too_large'):
                releases.request_bytes(releases.OFFICIAL, 4)

    def test_redirects_cannot_escape_https_official_hosts(self):
        for url in ['http://github.com/a', 'https://github.com.evil.test/a', 'https://user:pass@github.com/a', 'https://127.0.0.1/a']:
            with self.subTest(url=url), self.assertRaises(releases.ReleaseError): releases.safe_url(url)
        self.assertEqual(releases.safe_url('https://release-assets.githubusercontent.com/a'), 'https://release-assets.githubusercontent.com/a')

    def test_both_official_repository_names_are_accepted_and_nothing_broader(self):
        # After a rename avenoxai/avenoxbeyin -> avenoxai/beyin the old API URL redirects and the
        # API reports asset URLs under the new name; older installs must keep updating.
        def with_checksum(base):
            raw = fixture(); asset = raw['assets'][0]
            asset['browser_download_url'] = asset['browser_download_url'].replace(releases.WEB, base)
            raw['assets'].append(dict(asset, id=457, name=asset['name'] + '.sha256',
                                      browser_download_url=asset['browser_download_url'] + '.sha256'))
            return raw
        for slug in ('avenoxai/avenoxbeyin', 'avenoxai/beyin'):
            with self.subTest(slug=slug):
                base = 'https://github.com/' + slug + '/releases/'
                metadata = releases.release_metadata(with_checksum(base))
                self.assertEqual(metadata['asset_url'], base + 'download/v3.1.0/beyin-v3-3.1.0.zip')
                self.assertEqual(metadata['checksum_url'], metadata['asset_url'] + '.sha256')
                self.assertEqual(metadata['release_url'], base + 'tag/v3.1.0')
                state = self.root / ('state-' + slug.replace('/', '-'))
                with patch.object(releases, 'fetch_metadata', return_value=(metadata, None)):
                    releases.refresh(state, now=1000)
                self.assertEqual(releases.cache(state)['release']['release_url'], base + 'tag/v3.1.0')
        for slug in ('avenoxai/beyin-evil', 'avenoxai/avenoxbeyin2', 'someone/beyin', 'someone/avenoxbeyin', 'AVENOXAI/beyin'):
            with self.subTest(slug=slug), self.assertRaisesRegex(releases.ReleaseError, 'invalid_asset'):
                releases.release_metadata(with_checksum('https://github.com/' + slug + '/releases/'))
        mixed = with_checksum('https://github.com/avenoxai/beyin/releases/')
        mixed['assets'][1]['browser_download_url'] = releases.WEB + 'download/v3.1.0/beyin-v3-3.1.0.zip.sha256'
        with self.assertRaisesRegex(releases.ReleaseError, 'invalid_checksum_asset'):
            releases.release_metadata(mixed)
        mixed = with_checksum(releases.WEB)
        mixed['assets'][1]['browser_download_url'] = 'https://github.com/avenoxai/beyin/releases/download/v3.1.0/beyin-v3-3.1.0.zip.sha256'
        with self.assertRaisesRegex(releases.ReleaseError, 'invalid_checksum_asset'):
            releases.release_metadata(mixed)

    def test_renamed_repository_api_redirect_is_followed_with_json_accept(self):
        request = releases.urllib.request.Request(releases.OFFICIAL, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'avenoxbeyin-updater'})
        target = 'https://api.github.com/repositories/1284449744/releases/latest'
        followed = releases.OfficialRedirect().redirect_request(request, io.BytesIO(), 301, 'Moved Permanently', {}, target)
        self.assertEqual(followed.full_url, target)
        self.assertEqual(followed.get_method(), 'GET')
        self.assertEqual(followed.headers.get('Accept'), 'application/vnd.github+json')
        with self.assertRaisesRegex(releases.ReleaseError, 'untrusted_download_url'):
            releases.OfficialRedirect().redirect_request(request, io.BytesIO(), 301, 'Moved', {}, 'https://evil.test/releases/latest')

    def test_http_not_modified_rate_limit_and_network_error_are_sanitized(self):
        with patch.object(releases.urllib.request, 'build_opener') as builder:
            opener = builder.return_value.open
            opener.side_effect = urllib.error.HTTPError(releases.OFFICIAL, 304, 'unchanged', {'ETag': '"same"'}, io.BytesIO())
            self.assertEqual(releases.request_bytes(releases.OFFICIAL, 1024), (None, {'ETag': '"same"'}))
            for code in (403, 429):
                opener.side_effect = urllib.error.HTTPError(releases.OFFICIAL, code, 'PRIVATE', {'Retry-After': '7200'}, io.BytesIO(b'PRIVATE'))
                with self.assertRaises(releases.ReleaseError) as caught:
                    releases.request_bytes(releases.OFFICIAL, 1024)
                self.assertEqual(str(caught.exception), 'http_' + str(code))
                self.assertEqual(caught.exception.retry_after, 7200)
            opener.side_effect = urllib.error.URLError('PRIVATE network details')
            with self.assertRaisesRegex(releases.ReleaseError, '^network_unavailable$'):
                releases.request_bytes(releases.OFFICIAL, 1024)

    def test_304_preserves_release_and_renews_cadence(self):
        self.seed_cache()
        with patch.object(releases, 'fetch_metadata', return_value=(None, '"synthetic-etag"')) as fetch:
            releases.refresh(self.state, now=2000)
        self.assertEqual(fetch.call_args.args, ('"synthetic-etag"',))
        self.assertEqual(releases.status(self.vault, self.state, now=2001)['status'], 'available')
        self.assertEqual(releases.cache(self.state)['next_check_at'], 2000 + releases.DAY)

    def test_offline_backoff_keeps_last_verified_release_without_false_current(self):
        self.seed_cache()
        for attempt, delay in enumerate((3600, 21600, releases.DAY), 1):
            with patch.object(releases, 'fetch_metadata', side_effect=releases.ReleaseError('network_unavailable')):
                releases.refresh(self.state, now=2000)
            value = releases.cache(self.state)
            self.assertEqual(value['next_check_at'], 2000 + delay)
            self.assertEqual(value['failures'], attempt)
            self.assertEqual(value['release']['version'], '3.1.0')
            self.assertEqual(value['checked_at'], 1000)
            self.assertEqual(releases.status(self.vault, self.state, now=2001)['status'], 'unavailable')
        self.assertNotIn('UNTRUSTED', (self.state / 'release-cache.json').read_text())

    def test_retry_after_and_clock_reversal(self):
        with patch.object(releases, 'fetch_metadata', side_effect=releases.ReleaseError('http_429', 7200)):
            releases.refresh(self.state, now=1000)
        self.assertFalse(releases.claim_worker(self.state, now=2000))
        self.assertTrue(releases.claim_worker(self.state, now=8200))
        self.assertTrue(releases.claim_worker(self.state, now=900))

    def test_single_worker_claim_and_crash_expiration(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: releases.claim_worker(self.state, now=1000), range(4)))
        self.assertEqual(sum(claims), 1)
        self.assertFalse(releases.claim_worker(self.state, now=1059))
        self.assertTrue(releases.claim_worker(self.state, now=1060))
        self.seed_cache(now=1100)
        self.assertFalse(releases.claim_worker(self.state, now=1200))

    def test_notice_once_across_clients_and_dismiss_only_one_version(self):
        self.seed_cache()
        with self.assertRaises(ValueError):
            releases.dismiss(self.vault, self.state, 'invalid-version')
        self.assertFalse((self.state / 'releases.db').exists())
        with ThreadPoolExecutor(max_workers=4) as pool:
            notices = list(pool.map(lambda _: releases.notification(self.vault, self.state, now=1001), range(4)))
        self.assertEqual(sum(bool(x) for x in notices), 1)
        self.assertNotIn('UNTRUSTED', ''.join(notices))
        self.seed_cache(v='3.2.0')
        releases.dismiss(self.vault, self.state, '3.2.0')
        releases.dismiss(self.vault, self.state, '3.2.0')
        self.assertEqual(releases.notification(self.vault, self.state, now=1001), '')
        self.seed_cache(v='3.3.0')
        self.assertIn('3.3.0', releases.notification(self.vault, self.state, now=1001))

    def test_old_future_and_corrupt_cache_never_produce_notice(self):
        self.seed_cache()
        for now in (999, 1001 + releases.FRESH):
            self.assertEqual(releases.notification(self.vault, self.state, now), '')
        (self.state / 'release-cache.json').write_text('{bad')
        self.assertEqual(releases.session_start(self.vault, self.state), '')
        self.assertEqual(releases.status(self.vault, self.state)['status'], 'unavailable')

    def test_disabled_and_corrupt_preferences_fail_closed(self):
        self.seed_cache()
        releases.preferences(self.state, False)
        with patch.object(releases, 'fetch_metadata') as fetch:
            releases.refresh(self.state)
            self.assertEqual(releases.notification(self.vault, self.state, 1001), '')
            self.assertFalse(releases.claim_worker(self.state))
            fetch.assert_not_called()
        releases.preferences(self.state, True)
        with patch.dict(os.environ, {'BEYIN_UPDATES_OFF': '1'}):
            self.assertEqual(releases.status(self.vault, self.state)['status'], 'disabled')
        path = self.state / 'release-preferences.json'; path.write_text('{broken')
        before = path.read_bytes()
        self.assertEqual(releases.session_start(self.vault, self.state), '')
        self.assertEqual(path.read_bytes(), before)

    def test_session_start_spawns_detached_worker_without_network(self):
        with patch.dict(os.environ, {'BEYIN_V3_NO_SPAWN': '0'}), patch.object(releases.subprocess, 'Popen') as spawn, patch.object(releases, 'fetch_metadata') as network:
            releases.session_start(self.vault, self.state)
            releases.session_start(self.vault, self.state)
        network.assert_not_called()
        self.assertEqual(spawn.call_count, 1)
        self.assertIn('--worker', spawn.call_args.args[0])

    def test_outer_checksum_fails_before_any_package_code_runs(self):
        metadata = releases.release_metadata(fixture())
        with patch.object(releases, 'fetch_metadata', return_value=(metadata, None)), patch.object(releases, 'request_bytes', return_value=(b'corrupted ZIP', {})), patch.object(updater, '_preflight') as preflight:
            with self.assertRaises(ValueError): updater.update(self.vault, self.state)
        preflight.assert_not_called()
        self.assertEqual((self.vault / '.beyin-version').read_text(), '3.0.2')
        self.assertFalse(self.state.exists())

    def test_checksum_sidecar_fallback_and_both_must_agree(self):
        data = b'synthetic ZIP'; raw = fixture(data=data)
        asset = raw['assets'][0]
        raw['assets'].append(dict(asset, id=457, name=asset['name'] + '.sha256', browser_download_url=asset['browser_download_url'] + '.sha256'))
        for digest in (None, asset['digest']):
            raw['assets'][0]['digest'] = digest
            metadata = releases.release_metadata(raw)
            checksum = (hashlib.sha256(data).hexdigest() + '  ' + asset['name'] + '\n').encode()
            with patch.object(releases, 'fetch_metadata', return_value=(metadata, None)), patch.object(releases, 'request_bytes', side_effect=[(data, {}), (checksum, {})]):
                path, v = updater._download(self.root)
                self.assertEqual(path.read_bytes(), data); self.assertEqual(v, '3.1.0')
            with patch.object(releases, 'fetch_metadata', return_value=(metadata, None)), patch.object(releases, 'request_bytes', side_effect=[(data, {}), (b'bad', {})]):
                with self.assertRaises(ValueError): updater._download(self.root)

    def test_cache_older_than_the_installed_version_is_rechecked_not_ahead(self):
        stamp = self.vault / '.beyin-version'
        stamped = stamp.stat().st_mtime
        # An older updater moved 3.0.1 -> 3.0.2 and left the 3.0.1 cache it found (#188).
        self.seed_cache(now=stamped - 60, v='3.0.1')
        self.assertEqual(releases.status(self.vault, self.state, now=stamped + 1), {'status': 'unknown'})
        self.assertEqual(releases.notification(self.vault, self.state, now=stamped + 1), '')
        self.assertFalse(releases.claim_worker(self.state, now=stamped + 1))
        self.assertTrue(releases.claim_worker(self.state, now=stamped + 1, vault=self.vault))
        # The check after the install still finds 3.0.1: a dev install really is ahead, once.
        with patch.object(releases, 'fetch_metadata', return_value=(None, '"synthetic-etag"')):
            releases.refresh(self.state, now=stamped + 2)
        self.assertEqual(releases.status(self.vault, self.state, now=stamped + 3)['status'], 'ahead')
        self.assertFalse(releases.claim_worker(self.state, now=stamped + 100, vault=self.vault))
        # Offline after the install: one attempt, then the normal backoff, not a spawn per session.
        self.seed_cache(now=stamped - 60, v='3.0.1')
        with patch.object(releases, 'fetch_metadata', side_effect=releases.ReleaseError('network_unavailable')):
            releases.refresh(self.state, now=stamped + 200)
        self.assertEqual(releases.status(self.vault, self.state, now=stamped + 201)['status'], 'unavailable')
        self.assertFalse(releases.claim_worker(self.state, now=stamped + 300, vault=self.vault))
        # Only a stale ahead is set aside: an older cache that is level or behind still reports.
        for cached, expected in (('3.0.2', 'up_to_date'), ('3.1.0', 'available')):
            self.seed_cache(now=stamped - 60, v=cached)
            self.assertEqual(releases.status(self.vault, self.state, now=stamped + 1)['status'], expected)
        # A stamp dated in the future (clock skew, synced vault) never suppresses the cache.
        self.seed_cache(now=stamped - 60, v='3.0.1')
        os.utime(stamp, (stamped + 3600, stamped + 3600))
        self.assertEqual(releases.status(self.vault, self.state, now=stamped + 1)['status'], 'ahead')

    def test_installed_preferences_hook_no_memory_and_no_sync(self):
        result = install(self.vault, self.state, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        entry = self.vault / 'beyin.py'
        result = run_python(entry, ['preferences', '--profile', 'manual', '--update-notifications', 'on'], self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('update', (self.vault / '.beyin-preferences.json').read_text())
        self.seed_cache(now=time.time())
        hook = self.vault / '.claude/scripts/beyin_v3_hook.py'
        def invoke(no_memory=False):
            response = run_python(hook, ['--vault', self.vault, '--state', self.state, '--harness', 'codex'], self.vault, self.env,
                                  {'hook_event_name': 'SessionStart', 'no_memory': no_memory})
            self.assertEqual(response.returncode, 0, response.stderr)
            return json.loads(response.stdout)
        before = snapshot(self.root)
        self.assertEqual(invoke(True), {})
        self.assertEqual(snapshot(self.root), before)
        result = run_python(entry, ['context', 'synthetic', '--no-sync'], self.vault, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(snapshot(self.root), before)
        # Manual memory profile still allows the independent update preference.
        self.assertIn('3.1.0', invoke()['hookSpecificOutput']['additionalContext'])
        self.assertEqual(invoke(), {})
        doctor = run_python(entry, ['doctor', '--human'], self.vault, self.env)
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        self.assertIn(b'3.1.0', doctor.stdout)
        self.assertIn(b'Son kontrol:', doctor.stdout)
        self.assertNotIn(b'UNTRUSTED', doctor.stdout)

    def test_invalid_cli_combinations_leave_vault_and_state_unchanged(self):
        self.assertEqual(install(self.vault, self.state, self.env).returncode, 0)
        before = snapshot(self.root)
        for args in (['update', '--metadata-only'], ['update', '--check', '--metadata-only', '--package', 'x'],
                     ['update', '--check', '--dismiss', '3.1.0'], ['rollback', '--check']):
            result = run_python(self.vault / 'beyin.py', args, self.vault, self.env)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(snapshot(self.root), before)


if __name__ == '__main__': unittest.main()
