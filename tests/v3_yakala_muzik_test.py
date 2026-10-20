"""Opt-in music bridge: fixtures exercise consent, real pipes/proxy, credentials and replay safety.

No paid service, user account, source platform or agent client is contacted. Synthetic audio is
locally generated; the optional real ffmpeg check is not a platform/recognition accuracy claim.
"""
import io
import json
import math
import os
from pathlib import Path
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import wave

from v3_yakala_ajan_test import AgentFiles, yakala


SOURCE = 'https://www.youtube.com/shorts/abcdefghijk?tracking=remove-me'
MEDIA = 'https://r1.example.googlevideo.com/videoplayback?signature=source-only'
TOKEN = 'TEST_audd_secret_123456'


def fixture_wav(seconds=1):
    output = io.BytesIO()
    with wave.open(output, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b''.join(struct.pack('<h', int(10000 * math.sin(index * 440 * 2 * math.pi / 16000)))
                                    for index in range(int(16000 * seconds))))
    return output.getvalue()


class MusicFiles(AgentFiles):
    def new_job(self, extract=False, upload=False, network='acik', source=SOURCE, delivery=None):
        self.configure(ag=network)
        saved, card = self.card(url=source)
        consent = {'schema': 1, 'extract': extract, 'upload': 'audd' if upload else None}
        sent = yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Sarkiyi bul ve mesru baglanti ver',
                                    delivery_id=delivery, music=consent if extract or upload else None)
        job = yakala._agent_load_job(self.vault, self.state, sent['job_id'])
        # A running job's tools need the live worker identity and deadline (#384 review).
        job.update(state='running', worker_pid=os.getpid(),
                   worker_identity=yakala._agent_process_identity(os.getpid()),
                   runtime_deadline_ns=time.monotonic_ns() + 900 * 1000000000)
        yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json', job)
        return job

    def token(self):
        # macOS temp names can start with /var while the real directory is /private/var.
        source = Path(self.tmp.name).resolve() / 'audd-input.txt'
        source.write_text(TOKEN, encoding='utf-8')
        result = yakala.configure_agent_music(self.vault, self.state, 'audd', source)
        self.assertNotIn(TOKEN, json.dumps(result))
        return yakala._agent_music_credential_path(self.vault, self.state)

    def fake_flow(self, job, result=None, error=None):
        metadata = {'status': 'metadata', 'recognized': False, 'candidates': [], 'description': ''}
        provider = result or {'status': 'recognized', 'recognized': True, 'provider': 'audd',
                              'title': 'Fixture song', 'artist': 'Fixture artist', 'listening_links': []}
        patches = [mock.patch.object(yakala, '_agent_music_info', return_value=metadata),
                   mock.patch.object(yakala, '_agent_music_ytdlp', return_value={'url': MEDIA, 'protocol': 'https'}),
                   mock.patch.object(yakala, '_agent_music_http', return_value={'body': fixture_wav(),
                                                                               'content_type': 'audio/wav'}),
                   mock.patch.object(yakala, '_agent_music_sample', return_value=fixture_wav()),
                   mock.patch.object(yakala, '_agent_music_audd', return_value=provider, side_effect=error)]
        mocks = [item.start() for item in patches]
        for item in patches:
            self.addCleanup(item.stop)
        return mocks


class MusicConsentTest(MusicFiles):
    def test_default_job_has_no_media_permission_even_when_request_says_download(self):
        job = self.new_job(network='kisitli')
        self.assertNotIn('music', job)
        self.assertEqual(yakala._agent_music_permissions(job), {'schema': 1, 'extract': False, 'upload': None})
        with mock.patch.object(yakala, '_agent_music_info', return_value={}), mock.patch.object(yakala, '_agent_music_ytdlp') as external:
            result = yakala._agent_tool_call(self.vault, self.state, job['id'], 'music_identify', {})
        self.assertEqual(result['status'], 'approval_required')
        external.assert_not_called()

    def test_open_network_alone_does_not_grant_upload_or_extraction(self):
        job = self.new_job(network='acik')
        with mock.patch.object(yakala, '_agent_music_info', return_value={}), mock.patch.object(yakala, '_agent_music_ytdlp') as external:
            result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'approval_required')
        self.assertIn('--ses-isle', result['next_cli'])
        external.assert_not_called()

    def test_upload_flag_without_local_extraction_rejected_before_enqueue(self):
        self.configure(ag='acik')
        saved, _ = self.card(url=SOURCE)
        with self.assertRaises(ValueError):
            yakala.enqueue_agent(self.vault, self.state, saved['id'], 'Sarkiyi bul',
                                  music={'schema': 1, 'extract': False, 'upload': 'audd'})
        self.assertEqual(yakala._agent_jobs(self.vault, self.state), [])
        self.spawned.assert_not_called()

    def test_extraction_permission_cannot_override_restricted_network(self):
        with self.assertRaises(ValueError):
            self.new_job(extract=True, network='kisitli')
        self.spawned.assert_not_called()

    def test_model_cannot_supply_any_url_path_command_token_or_permission(self):
        job = self.new_job()
        with mock.patch.object(yakala, '_agent_music_tool') as external:
            for name in ('music_info', 'music_identify'):
                for key in ('url', 'path', 'command', 'token', 'upload', 'consent', 'extract'):
                    with self.subTest(name=name, key=key), self.assertRaises(ValueError):
                        yakala._agent_tool_call(self.vault, self.state, job['id'], name, {key: True})
        external.assert_not_called()

    def test_cli_permissions_are_durable_before_spawn(self):
        self.configure(ag='acik')
        saved, _ = self.card(url=SOURCE)
        captured = []
        self.spawned.side_effect = lambda *_: captured.append(yakala._agent_jobs(self.vault, self.state))
        with mock.patch.object(sys, 'stdout', io.StringIO()):
            self.assertEqual(yakala.main(['ajan-gonder', saved['id'], '--istek', 'Sarkiyi tani', '--ses-isle',
                                          '--tani-servise-gonder'], vault=self.vault, state=self.state), 0)
        self.assertEqual(captured[0][0]['music'], {'schema': 1, 'extract': True, 'upload': 'audd'})
        self.assertEqual(captured[0][0]['state'], 'queued')

    def test_delivery_retry_cannot_elevate_existing_consent(self):
        job = self.new_job(delivery='1' * 32)
        with self.assertRaises(ValueError):
            yakala.enqueue_agent(self.vault, self.state, job['card_id'], job['request'], delivery_id='1' * 32,
                                  music={'schema': 1, 'extract': True, 'upload': 'audd'})
        self.assertNotIn('music', yakala._agent_load_job(self.vault, self.state, job['id']))

    def test_tampered_non_boolean_consent_is_rejected_on_load(self):
        job = self.new_job()
        for permissions in ({'schema': 1, 'extract': 1, 'upload': None},
                            {'schema': 1, 'extract': True, 'upload': 'custom-service'},
                            {'schema': 1, 'extract': True, 'upload': 'audd', 'url': MEDIA}):
            job['music'] = permissions
            yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json', job)
            with self.subTest(permissions=permissions), self.assertRaises(ValueError):
                yakala._agent_load_job(self.vault, self.state, job['id'])

    def test_complete_closes_new_music_tools(self):
        job = self.new_job()
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        (folder / 'result.json').write_text('{}', encoding='utf-8')
        for name in ('music_info', 'music_identify'):
            with self.assertRaises(ValueError):
                yakala._agent_tool_call(self.vault, self.state, job['id'], name, {})


class MusicMetadataTest(MusicFiles):
    def test_source_canonicalization_strips_queries_except_youtube_id(self):
        self.assertEqual(yakala._agent_music_source(SOURCE), ('youtube', 'https://www.youtube.com/watch?v=abcdefghijk'))
        self.assertEqual(yakala._agent_music_source('https://www.instagram.com/reel/ABC123/?igsh=personal'),
                         ('instagram', 'https://www.instagram.com/reel/ABC123/'))
        self.assertEqual(yakala._agent_music_source('https://www.tiktok.com/@user/video/123456789/?token=secret'),
                         ('tiktok', 'https://www.tiktok.com/@user/video/123456789'))

    def test_unsupported_playlist_search_embedded_credentials_and_wrong_ports_rejected(self):
        for url in ('https://youtube.com/playlist?list=AAA', 'https://youtube.com/results?search_query=secret',
                    'https://youtube.com/watch?v=abcdefghijk&v=second', 'https://youtu.be/bad',
                    'https://user:pass@youtube.com/watch?v=abcdefghijk', 'http://youtu.be/abcdefghijk',
                    'https://youtube.com:444/watch?v=abcdefghijk', 'https://youtube.com.evil.invalid/watch?v=abcdefghijk'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                yakala._agent_music_source(url)

    def test_tiktok_music_metadata_and_original_audio_are_separate_hints(self):
        data = {'itemInfo': {'itemStruct': {'music': {'title': 'Orijinal ses', 'authorName': 'Fixture user',
                                                    'original': True}}}}
        body = ('<html><meta property="og:title" content="Video">'
                '<script type="application/json">' + json.dumps(data) + '</script></html>').encode()
        result = yakala._agent_music_metadata(body, 'text/html', SOURCE)
        self.assertFalse(result['recognized'])
        self.assertTrue(result['candidates'][0]['original_audio'])
        self.assertEqual(result['candidates'][0]['verification'], 'source_metadata_only')

    def test_instagram_and_jsonld_music_fields_are_read(self):
        data = {'music_info': {'music_asset_info': {'title': 'Fixture song', 'display_artist': 'Fixture artist'}}}
        result = yakala._agent_music_metadata(json.dumps(data).encode(), 'application/json', SOURCE)
        self.assertEqual(result['candidates'][0]['artist'], 'Fixture artist')
        result = yakala._agent_music_metadata(json.dumps({'@type': 'MusicRecording', 'name': 'Second',
                                                        'byArtist': {'name': 'Another'}}).encode(), 'application/json', SOURCE)
        self.assertEqual(result['candidates'][0]['title'], 'Second')

    def test_youtube_description_can_be_read_from_initial_player_response(self):
        body = b'<html><script>var ytInitialPlayerResponse = {"videoDetails":{"shortDescription":"Music: Test"}};</script></html>'
        result = yakala._agent_music_metadata(body, 'text/html', SOURCE)
        self.assertEqual(result['description'], 'Music: Test')
        self.assertFalse(result['recognized'])

    def test_metadata_requests_only_original_url_once_and_never_grants_download_capability(self):
        job = self.new_job(network='kisitli')
        page = {'body': b'<html><meta property="og:title" content="Fixture"></html>', 'url': SOURCE,
                'content_type': 'text/html'}
        with mock.patch.object(yakala, '_agent_music_http', return_value=page) as fetched:
            first = yakala._agent_tool_call(self.vault, self.state, job['id'], 'music_info', {})
            again = yakala._agent_tool_call(self.vault, self.state, job['id'], 'music_info', {})
        self.assertEqual(first['title'], 'Fixture')
        self.assertTrue(again['cached'])
        self.assertEqual(fetched.call_args.args, (SOURCE,))
        self.assertEqual(fetched.call_count, 1)
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        self.assertFalse((folder / 'source-urls.json').exists())

    def test_crashed_metadata_attempt_is_not_repeated(self):
        job = self.new_job()
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        yakala._agent_create_json(folder / 'music-info-attempt.json', {'schema': 1, 'attempted': True})
        with mock.patch.object(yakala, '_agent_music_http') as fetched:
            result = yakala._agent_music_tool(self.vault, self.state, job, 'music_info')
        self.assertEqual(result['status'], 'unavailable')
        fetched.assert_not_called()


class MusicCredentialsTest(MusicFiles):
    def test_service_is_disabled_until_user_configures_key(self):
        result = yakala.configure_agent_music(self.vault, self.state)
        self.assertIsNone(result['provider'])
        self.assertFalse(self.state.exists())

    def test_key_roundtrip_outside_vault_never_enters_config_job_prompt(self):
        keypath = self.token()
        job = self.new_job(extract=True, upload=True)
        self.assertEqual(yakala._agent_music_key(self.vault, self.state), TOKEN)
        self.assertFalse(keypath.resolve().is_relative_to(self.vault))
        self.assertNotIn(TOKEN, json.dumps(job))
        self.assertNotIn(TOKEN, yakala._agent_prompt(self.vault, job))
        self.assertNotIn(TOKEN, self.config_path.read_text(encoding='utf-8'))
        if os.name != 'nt':
            self.assertEqual(keypath.stat().st_mode & 0o777, 0o600)

    def test_key_off_switch_removes_service_without_touching_job_consent(self):
        keypath = self.token()
        job = self.new_job(extract=True, upload=True)
        yakala.configure_agent_music(self.vault, self.state, 'kapali')
        self.assertFalse(keypath.exists())
        self.assertEqual(yakala._agent_music_permissions(job)['upload'], 'audd')
        self.assertIsNone(yakala._agent_music_key(self.vault, self.state))

    def test_vault_internal_state_or_input_key_is_rejected(self):
        for state in (self.vault, self.vault / '.private-state'):
            with self.assertRaises(ValueError):
                yakala.configure_agent_music(self.vault, state)
        inputpath = self.vault / 'secret.txt'
        inputpath.write_text(TOKEN, encoding='utf-8')
        with self.assertRaises(ValueError):
            yakala.configure_agent_music(self.vault, self.state, 'audd', inputpath)

    @unittest.skipIf(os.name == 'nt', 'POSIX permission mode; Windows DPAPI checked separately')
    def test_world_readable_key_is_rejected(self):
        keypath = self.token()
        keypath.chmod(0o644)
        with self.assertRaises(ValueError):
            yakala._agent_music_key(self.vault, self.state)

    @unittest.skipUnless(os.name == 'nt', 'Requires actual Windows CurrentUser DPAPI')
    def test_windows_dpapi_ciphertext_and_roundtrip(self):
        raw = TOKEN.encode()
        encrypted = yakala._agent_music_dpapi(raw, True)
        self.assertNotIn(raw, encrypted)
        self.assertEqual(yakala._agent_music_dpapi(encrypted, False), raw)

    def test_ambient_credentials_proxy_and_code_environment_do_not_reach_tools(self):
        environment = {'AUDD_API_TOKEN': TOKEN, 'BEYIN_AUDD_TOKEN': TOKEN, 'HTTP_PROXY': 'http://bad/',
                       'HTTPS_PROXY': 'http://bad/', 'ALL_PROXY': 'http://bad/', 'NO_PROXY': '*',
                       'PYTHONPATH': str(self.vault), 'SSL_CERT_FILE': str(self.vault / 'evil.pem')}
        with mock.patch.dict(os.environ, environment):
            child = yakala._agent_music_process_env()
            agent = yakala._agent_env(self.state)
        for name in environment:
            self.assertNotIn(name, child)
        self.assertNotIn('AUDD_API_TOKEN', agent)
        self.assertNotIn('BEYIN_AUDD_TOKEN', agent)

    def test_noninteractive_key_entry_without_file_is_rejected(self):
        with mock.patch.object(sys, 'stdin', mock.Mock(isatty=lambda: False)), mock.patch('getpass.getpass') as secret:
            with self.assertRaises(ValueError):
                yakala.configure_agent_music(self.vault, self.state, 'audd')
        secret.assert_not_called()

    def test_token_error_echo_and_untrusted_links_are_not_returned(self):
        result = yakala._agent_music_audd_result({'status': 'error', 'error': {'message': TOKEN}}, TOKEN)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertFalse(result['recognized'])
        found = {'title': 'Fixture', 'artist': 'Artist', 'album': TOKEN,
                 'song_link': 'https://lis.tn/123?token=' + TOKEN,
                 'spotify': {'external_urls': {'spotify': 'https://evil.invalid/listen'}},
                 'apple_music': {'url': 'https://music.apple.com/tr/album/123?tracking=x#fragment'}}
        result = yakala._agent_music_audd_result({'status': 'success', 'result': found}, TOKEN)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(result['listening_links'], ['https://music.apple.com/tr/album/123'])
        self.assertIsNone(result['confidence'])

    def test_raw_secret_is_removed_before_string_truncation(self):
        for token in (TOKEN, 'x' * 400):
            response = {'status': 'success', 'result': {'title': 'prefix' * 48 + token, 'artist': 'Artist'}}
            with self.subTest(length=len(token)), self.assertRaises(ValueError):
                yakala._agent_music_audd_result(response, token)

    def test_apple_track_identity_query_is_preserved_without_tracking(self):
        self.assertEqual(yakala._agent_music_listening_link('https://music.apple.com/us/album/example/123?i=456&uo=4'),
                         'https://music.apple.com/us/album/example/123?i=456')
        self.assertEqual(yakala._agent_music_listening_link('https://music.apple.com/us/album/example/123?i=private-text'),
                         'https://music.apple.com/us/album/example/123')


class MusicReplayAndServiceTest(MusicFiles):
    def test_expired_worker_lease_during_extraction_stops_before_media_request(self):
        self.token()
        job = self.new_job(extract=True, upload=True)
        _, extract, download, local, service = self.fake_flow(job)

        def expire(*_args):
            current = yakala._agent_load_job(self.vault, self.state, job['id'])
            current['runtime_deadline_ns'] = 0
            yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json', current)
            return {'url': MEDIA, 'protocol': 'https'}

        extract.side_effect = expire
        result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'unavailable')
        for call in (download, local, service):
            call.assert_not_called()

    def test_dead_worker_after_sampling_cannot_start_a_provider_post(self):
        self.token()
        job = self.new_job(extract=True, upload=True)
        _, _, _, local, service = self.fake_flow(job)

        def lose_worker(*_args):
            current = yakala._agent_load_job(self.vault, self.state, job['id'])
            current['worker_identity'] = dict(current['worker_identity'], birth='different-worker')
            yakala._agent_json(yakala._agent_job_dir(self.vault, self.state, job['id']) / 'job.json', current)
            return fixture_wav()

        local.side_effect = lose_worker
        result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'unavailable')
        service.assert_not_called()
        self.assertFalse((yakala._agent_job_dir(self.vault, self.state, job['id']) /
                          'music-provider-attempt.json').exists())

    def test_worker_lease_is_checked_after_durable_provider_marker(self):
        self.token()
        job = self.new_job(extract=True, upload=True)
        *_, service = self.fake_flow(job)
        original_create = yakala._agent_create_json

        def expire_after_marker(path, value):
            original_create(path, value)
            if Path(path).name == 'music-provider-attempt.json':
                current = yakala._agent_load_job(self.vault, self.state, job['id'])
                current['runtime_deadline_ns'] = 0
                yakala._agent_json(Path(path).parent / 'job.json', current)

        with mock.patch.object(yakala, '_agent_create_json', side_effect=expire_after_marker):
            result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'unavailable')
        service.assert_not_called()
        self.assertTrue((yakala._agent_job_dir(self.vault, self.state, job['id']) /
                         'music-provider-attempt.json').exists())

    def test_missing_service_performs_no_extract_download_or_upload(self):
        job = self.new_job(extract=True, upload=True)
        with mock.patch.object(yakala, '_agent_music_info', return_value={}), mock.patch.object(yakala, '_agent_music_ytdlp') as external:
            result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'unavailable')
        self.assertIn('anahtar yok', result['message'])
        external.assert_not_called()

    def test_extraction_without_upload_consent_never_sends_sample(self):
        self.token()
        job = self.new_job(extract=True)
        _, _, _, local, service = self.fake_flow(job)
        result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'approval_required')
        local.assert_called_once()
        service.assert_not_called()
        self.assertNotIn('path', result)

    def test_provider_attempt_is_durable_before_request_and_cached_after(self):
        self.token()
        job = self.new_job(extract=True, upload=True)
        _, extract, download, local, service = self.fake_flow(job)
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])

        def complete(sample, token, deadline):
            self.assertTrue((folder / 'music-provider-attempt.json').exists())
            self.assertEqual(token, TOKEN)
            return {'status': 'recognized', 'recognized': True, 'provider': 'audd'}

        service.side_effect = complete
        first = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        again = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertTrue(first['recognized'])
        self.assertTrue(again['cached'])
        for call in (extract, download, local, service):
            self.assertEqual(call.call_count, 1)
        self.assertFalse((folder / 'downloads').exists())
        self.assertFalse(any(path.suffix in ('.mp3', '.mp4', '.wav') for path in folder.iterdir()))
        for path in folder.glob('*.json'):
            self.assertNotIn(TOKEN, path.read_text(encoding='utf-8'))

    def test_provider_timeout_or_missing_result_after_crash_is_never_resent(self):
        self.token()
        job = self.new_job(extract=True, upload=True)
        *_, service = self.fake_flow(job, error=TimeoutError('arbitrary credential ' + TOKEN))
        result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'unavailable')
        self.assertNotIn(TOKEN, json.dumps(result))
        folder = yakala._agent_job_dir(self.vault, self.state, job['id'])
        (folder / 'music-result.json').unlink()  # interrupted between charged request and durable result
        again = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(again['status'], 'unavailable')
        self.assertEqual(service.call_count, 1)

    def test_service_disabled_during_extraction_prevents_new_upload(self):
        self.token()
        job = self.new_job(extract=True, upload=True)
        _, extract, _, _, service = self.fake_flow(job)

        def disable(*_args):
            yakala.configure_agent_music(self.vault, self.state, 'kapali')
            return {'url': MEDIA, 'protocol': 'https'}

        extract.side_effect = disable
        result = yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
        self.assertEqual(result['status'], 'unavailable')
        service.assert_not_called()
        self.assertFalse((yakala._agent_job_dir(self.vault, self.state, job['id']) / 'music-provider-attempt.json').exists())

    def test_disable_cannot_return_between_final_authorization_and_post(self):
        self.token()
        job = self.new_job(extract=True, upload=True)
        *_, service = self.fake_flow(job)
        at_marker, release_marker, disabled = threading.Event(), threading.Event(), threading.Event()
        order, failures = [], []
        original_create = yakala._agent_create_json

        def paused_marker(path, value):
            if Path(path).name == 'music-provider-attempt.json':
                at_marker.set()
                if not release_marker.wait(2):
                    raise RuntimeError('Fixture authorization barrier timed out')
            return original_create(path, value)

        def provider(*_args):
            order.append('post')
            return {'status': 'unmatched', 'recognized': False, 'provider': 'audd'}

        def invoke():
            try:
                yakala._agent_music_tool(self.vault, self.state, job, 'music_identify')
            except BaseException as exc:
                failures.append(exc)

        def disable():
            try:
                yakala.configure_agent_music(self.vault, self.state, 'kapali')
                order.append('off_returned')
                disabled.set()
            except BaseException as exc:
                failures.append(exc)

        service.side_effect = provider
        with mock.patch.object(yakala, '_agent_create_json', side_effect=paused_marker):
            sender = threading.Thread(target=invoke, daemon=True)
            sender.start()
            self.assertTrue(at_marker.wait(1))
            off = threading.Thread(target=disable, daemon=True)
            off.start()
            try:
                self.assertFalse(disabled.wait(0.05))
            finally:
                release_marker.set()
            sender.join(2)
            off.join(2)
        self.assertFalse(failures, failures)
        self.assertFalse(sender.is_alive())
        self.assertFalse(off.is_alive())
        self.assertEqual(order, ['post', 'off_returned'])
        self.assertIsNone(yakala._agent_music_key(self.vault, self.state))

    def test_no_match_is_not_reported_as_song_identified(self):
        result = yakala._agent_music_audd_result({'status': 'success', 'result': None}, TOKEN)
        self.assertEqual(result['status'], 'unmatched')
        self.assertFalse(result['recognized'])
        self.assertNotIn('title', result)

    def test_audd_posts_only_fixed_filename_audio_and_token_not_private_text_or_source_url(self):
        with mock.patch.object(yakala, '_agent_music_http', return_value={'body': b'{"status":"success","result":null}'}) as send:
            result = yakala._agent_music_audd(fixture_wav(), TOKEN, time.monotonic() + 5)
        self.assertEqual(result['status'], 'unmatched')
        self.assertEqual(send.call_args.args, ('https://api.audd.io/',))
        body = send.call_args.kwargs['body']
        self.assertIn(b'filename="sample.wav"', body)
        self.assertIn(TOKEN.encode(), body)
        self.assertNotIn(SOURCE.encode(), body)
        self.assertNotIn(str(self.vault).encode(), body)
        self.assertNotIn(b'name="url"', body)


class MusicMediaBoundaryTest(MusicFiles):
    def test_format_selection_ignores_manifest_drm_live_and_offsite_media(self):
        good = {'url': MEDIA, 'protocol': 'https', 'vcodec': 'none', 'acodec': 'aac', 'filesize': 3000}
        info = {'formats': [dict(good, url='https://evil.invalid/audio.mp4', filesize=1),
                            dict(good, protocol='m3u8_native', filesize=2),
                            dict(good, fragments=[{'url': 'private'}], filesize=3),
                            dict(good, has_drm=True, filesize=4), good]}
        self.assertEqual(yakala._agent_music_media_url(info, 'youtube'), MEDIA)
        for invalid in (dict(info, is_live=True), dict(info, has_drm=True),
                        {'url': 'https://r1.googlevideo.com/list.m3u8', 'protocol': 'https'},
                        {'formats': [dict(good, filesize=yakala._AGENT_MUSIC_SOURCE_BYTES + 1)]}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                yakala._agent_music_media_url(invalid, 'youtube')

    def test_playlists_and_html_cannot_be_disguised_by_file_extension(self):
        for data in (b'#EXTM3U\nhttps://localhost/private', b'ffconcat version 1.0\nfile /etc/passwd',
                     b'<MPD><BaseURL>http://127.0.0.1</BaseURL></MPD>', b'<html>audio.mp3</html>'):
            with self.assertRaises(ValueError):
                yakala._agent_music_demuxer(data)

    def test_sample_argv_accepts_only_pipe_protocol_and_magic_selected_demuxer(self):
        job = self.new_job()
        with mock.patch.object(yakala, '_agent_music_executable', return_value='/trusted/ffmpeg'), mock.patch.object(
                yakala, '_agent_music_run', return_value=b'\0' * 32000) as run:
            output = yakala._agent_music_sample(self.vault, self.state, job, fixture_wav(), time.monotonic() + 5)
        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index('-protocol_whitelist') + 1], 'pipe')
        self.assertEqual(argv[argv.index('-format_whitelist') + 1], 'wav')
        self.assertEqual(argv[argv.index('-i') + 1], 'pipe:0')
        self.assertEqual(argv[-1], 'pipe:1')
        self.assertEqual(run.call_args.kwargs['input_bytes'], fixture_wav())
        with wave.open(io.BytesIO(output), 'rb') as decoded:
            self.assertEqual(decoded.getnchannels(), 1)
            self.assertEqual(decoded.getframerate(), 16000)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'Optional ffmpeg absent; fixture cannot run')
    def test_real_ffmpeg_truncates_synthetic_thirteen_second_wav_to_twelve_seconds(self):
        job = self.new_job()
        output = yakala._agent_music_sample(self.vault, self.state, job, fixture_wav(13), time.monotonic() + 5)
        self.assertEqual(len(output), 384044)
        with wave.open(io.BytesIO(output), 'rb') as decoded:
            self.assertEqual(decoded.getnframes(), 192000)
            self.assertEqual(decoded.getsampwidth(), 2)
            self.assertEqual(decoded.getnchannels(), 1)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'Optional ffmpeg absent; fixture cannot run')
    def test_real_ffmpeg_default_moov_at_end_mp4_and_faststart_give_same_sample(self):
        job = self.new_job()
        source = Path(self.tmp.name) / 'sine.wav'
        source.write_bytes(fixture_wav(13))
        samples = []
        for fast in (False, True):
            target = Path(self.tmp.name) / ('fast.mp4' if fast else 'ordinary.mp4')
            argv = [shutil.which('ffmpeg'), '-v', 'error', '-nostdin', '-i', str(source), '-c:a', 'aac']
            if fast:
                argv += ['-movflags', '+faststart']
            subprocess.run([*argv, str(target)], check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=5)
            media = target.read_bytes()
            self.assertEqual(media.index(b'moov') < media.index(b'mdat'), fast)
            samples.append(yakala._agent_music_sample(self.vault, self.state, job, media, time.monotonic() + 5))
        self.assertEqual(len(samples[0]), 384044)
        self.assertEqual(samples[0], samples[1])

    def test_mp4_relocation_validates_all_container_and_offset_bounds(self):
        def atom(name, payload=b''):
            return struct.pack('>I4s', len(payload) + 8, name) + payload

        ftyp, mdat = atom(b'ftyp', b'isom\0\0\0\0'), atom(b'mdat', b'fixture audio data')
        original_offset = len(ftyp) + 8
        for wide in (False, True):
            tag, code = (b'co64', '>Q') if wide else (b'stco', '>I')
            table = atom(tag, struct.pack('>II', 0, 1) + struct.pack(code, original_offset))
            moov = atom(b'moov', atom(b'trak', atom(b'mdia', atom(b'minf', atom(b'stbl', table)))))
            data = ftyp + mdat + moov
            fixed = yakala._agent_music_streamable_mp4(data)
            self.assertLess(fixed.index(b'moov'), fixed.index(b'mdat'))
            moved_offset = struct.unpack_from(code, fixed, fixed.index(tag) + 12)[0]
            self.assertEqual(moved_offset, original_offset + len(moov))
            self.assertEqual(fixed[moved_offset:moved_offset + len(b'fixture audio data')], b'fixture audio data')
            for bad in (data[:-1], ftyp + mdat + moov + moov,
                        ftyp + mdat + atom(b'moof') + moov,
                        ftyp + mdat + mdat + moov,
                        ftyp + mdat + b'\0\0\0\0' + moov[4:]):
                with self.subTest(wide=wide, bad=bad[-20:]), self.assertRaises(ValueError):
                    yakala._agent_music_streamable_mp4(bad)
            invalid = atom(tag, struct.pack('>II', 0, 1) + struct.pack(code, (1 << (64 if wide else 32)) - 1))
            with self.assertRaises(ValueError):
                yakala._agent_music_streamable_mp4(ftyp + mdat + atom(b'moov', atom(b'stbl', invalid)))

    def test_mp4_relocation_rejects_excessive_entries_deep_and_fragmented_boxes(self):
        def atom(name, payload=b''):
            return struct.pack('>I4s', len(payload) + 8, name) + payload

        ftyp, mdat = atom(b'ftyp', b'isom\0\0\0\0'), atom(b'mdat', b'fixture audio data')
        for forbidden in (b'cmov', b'mvex', b'iloc', b'saio', b'saiz'):
            with self.subTest(forbidden=forbidden), self.assertRaises(ValueError):
                yakala._agent_music_streamable_mp4(ftyp + mdat + atom(b'moov', atom(forbidden)))
        excess = atom(b'stco', struct.pack('>II', 0, 250001))
        with self.assertRaises(ValueError):
            yakala._agent_music_streamable_mp4(ftyp + mdat + atom(b'moov', atom(b'stbl', excess)))
        deep = atom(b'stbl')
        for _ in range(15):
            deep = atom(b'trak', deep)
        with self.assertRaises(ValueError):
            yakala._agent_music_streamable_mp4(ftyp + mdat + atom(b'moov', deep))

    def test_mp4_largesize_headers_are_bounded_and_relocated(self):
        def atom(name, payload=b''):
            return struct.pack('>I4s', len(payload) + 8, name) + payload
        ftyp, mdat = atom(b'ftyp', b'isom\0\0\0\0'), atom(b'mdat', b'fixture')
        table = atom(b'stco', struct.pack('>III', 0, 1, len(ftyp) + 8))
        inner = atom(b'stbl', table)
        moov = struct.pack('>I4sQ', 1, b'moov', len(inner) + 16) + inner
        fixed = yakala._agent_music_streamable_mp4(ftyp + mdat + moov)
        self.assertLess(fixed.index(b'moov'), fixed.index(b'mdat'))
        with self.assertRaises(ValueError):
            yakala._agent_music_streamable_mp4(ftyp + mdat + struct.pack('>I4sQ', 1, b'moov', 999999999))

    def test_executable_cannot_come_from_vault_or_shell_launcher(self):
        for path in (self.vault / 'yt-dlp.exe', Path(self.tmp.name) / 'yt-dlp.cmd'):
            path.write_text('not executable', encoding='utf-8')
            with mock.patch.object(shutil, 'which', return_value=str(path)), self.assertRaises(ValueError):
                yakala._agent_music_executable('yt-dlp', self.vault, self.state)

    def test_extractor_argv_disables_plugins_commands_javascript_and_remote_components(self):
        job = self.new_job()
        gateway = mock.MagicMock()
        gateway.__enter__.return_value.url = 'http://127.0.0.1:1234'
        with mock.patch.object(yakala, '_agent_music_executable', return_value='/trusted/yt-dlp'), mock.patch.object(
                yakala, '_AgentMusicProxy', return_value=gateway), mock.patch.object(yakala, '_agent_music_run',
                return_value=b'{"id":"abcdefghijk"}') as run:
            yakala._agent_music_ytdlp(self.vault, self.state, job, 'youtube',
                                     'https://www.youtube.com/watch?v=abcdefghijk', time.monotonic() + 30)
        argv = run.call_args.args[0]
        for flag in ('--ignore-config', '--no-plugin-dirs', '--no-js-runtimes', '--no-remote-components',
                     '--no-exec', '--no-cookies', '--no-cookies-from-browser', '--no-cache-dir', '--simulate'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], ['--', 'https://www.youtube.com/watch?v=abcdefghijk'])
        self.assertEqual(argv[argv.index('--use-extractors') + 1], 'Youtube')

    def test_real_subprocess_stdout_flood_is_bounded_and_killed(self):
        folder = Path(self.tmp.name)
        command = [sys.executable, '-I', '-c', 'import os; os.write(1, b"x" * 1048576)']
        with self.assertRaises((ValueError, TimeoutError)):
            yakala._agent_music_run(command, folder, 3, 1024)


class MusicNetworkBoundaryTest(unittest.TestCase):
    def test_stalled_dns_hits_deadline_and_never_connects_later(self):
        release, finished = threading.Event(), threading.Event()

        def stalled(_):
            release.wait(1)
            finished.set()
            return (None, [(socket.AF_INET, '8.8.8.8', 443)])

        with mock.patch.object(yakala, '_agent_public_addresses', side_effect=stalled), mock.patch.object(socket, 'socket') as opened:
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                yakala._agent_music_connect('https://youtube.com/', started + 0.05)
            self.assertLess(time.monotonic() - started, 0.4)
            release.set()
            self.assertTrue(finished.wait(0.5))
            opened.assert_not_called()

    def test_slow_http_headers_are_cut_off_at_absolute_deadline(self):
        client, server = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(server.close)

        def drip():
            try:
                server.recv(8192)
                for byte in b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n':
                    server.sendall(bytes([byte]))
                    time.sleep(0.02)
            except OSError:
                pass

        writer = threading.Thread(target=drip, daemon=True)
        writer.start()
        started = time.monotonic()
        with mock.patch.object(yakala, '_agent_music_connect', return_value=client):
            with self.assertRaises((OSError, ValueError)):
                yakala._agent_music_http('https://youtube.com/watch?v=abcdefghijk', platform='youtube',
                                         deadline=started + 0.1, limit=1024)
        self.assertLess(time.monotonic() - started, 0.5)
        writer.join(0.5)

    def test_allowlist_matches_dns_suffix_boundaries_and_https_only(self):
        self.assertEqual(yakala._agent_music_allowed_url(MEDIA, 'youtube').hostname, 'r1.example.googlevideo.com')
        for url in ('https://evilgooglevideo.com/a', 'https://googlevideo.com.evil.invalid/a',
                    'http://googlevideo.com/a', 'https://u:p@googlevideo.com/a',
                    'https://googlevideo.com:444/a', 'https://127.0.0.1/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                yakala._agent_music_allowed_url(url, 'youtube')

    def test_mixed_public_private_dns_is_rejected_before_socket_creation(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with mock.patch.object(socket, 'getaddrinfo', return_value=answers), mock.patch.object(socket, 'socket') as opened:
            with self.assertRaises(ValueError):
                yakala._agent_music_connect('https://youtube.com/', time.monotonic() + 5, tls=False)
        opened.assert_not_called()

    def test_pinned_connection_never_resolves_host_a_second_time(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        channel = mock.Mock()
        with mock.patch.object(socket, 'getaddrinfo', return_value=answers) as dns, mock.patch.object(socket, 'socket', return_value=channel):
            result = yakala._agent_music_connect('https://youtube.com/', time.monotonic() + 5, tls=False)
        self.assertIs(result, channel)
        self.assertEqual(dns.call_count, 1)
        channel.connect.assert_called_once_with(('8.8.8.8', 443))

    def test_proxy_refuses_ip_userinfo_nonstandard_port_and_offsite_authorities(self):
        gateway = yakala._AgentMusicProxy('youtube', time.monotonic() + 5)
        for authority in ('127.0.0.1:443', '[::1]:443', 'youtube.com:80', 'youtube.com:444',
                          'username@youtube.com:443', 'evil.invalid:443', 'youtube.com.evil.invalid:443'):
            with self.subTest(authority=authority), self.assertRaises(ValueError):
                gateway.target(authority)

    def test_proxy_budget_cannot_be_reset_by_new_connections(self):
        gateway = yakala._AgentMusicProxy('youtube', time.monotonic() + 5)
        for _ in range(yakala._AGENT_MUSIC_PROXY_REQUESTS):
            gateway.consume(request=True)
        with self.assertRaises(ValueError):
            gateway.consume(request=True)
        another = yakala._AgentMusicProxy('youtube', time.monotonic() + 5)
        another.consume(yakala._AGENT_MUSIC_PROXY_BYTES)
        with self.assertRaises(ValueError):
            another.consume(1)

    def test_http_redirect_to_private_offsite_or_downgrade_is_rejected_without_followup(self):
        for location in ('https://127.0.0.1/private', 'http://youtube.com/watch', 'https://evil.invalid/track'):
            response = mock.Mock(status=302)
            response.getheader.side_effect = lambda key, default=None: location if key == 'Location' else default
            connection = mock.Mock()
            connection.getresponse.return_value = response
            with mock.patch('http.client.HTTPConnection', return_value=connection), mock.patch.object(
                    yakala, '_agent_music_connect', return_value=mock.Mock()) as connect:
                with self.subTest(location=location), self.assertRaises(ValueError):
                    yakala._agent_music_http('https://youtube.com/watch?v=abcdefghijk', platform='youtube',
                                             deadline=time.monotonic() + 5, limit=1000)
            self.assertEqual(connect.call_count, 1)

    def test_provider_redirect_never_replays_secret_post_to_other_host(self):
        response = mock.Mock(status=307)
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch('http.client.HTTPConnection', return_value=connection), mock.patch.object(
                yakala, '_agent_music_connect', return_value=mock.Mock()) as connect:
            with self.assertRaises(ValueError):
                yakala._agent_music_http('https://api.audd.io/', deadline=time.monotonic() + 5,
                                         limit=1000, body=TOKEN.encode(), content_type='multipart/form-data')
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(connection.request.call_args.args[:2], ('POST', '/'))

    def test_live_proxy_authentication_and_bounded_numeric_tunnel(self):
        class Echo(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.sendall(self.request.recv(1000))
        with socketserver.TCPServer(('127.0.0.1', 0), Echo) as origin:
            worker = threading.Thread(target=origin.handle_request, daemon=True)
            worker.start()
            with mock.patch.object(yakala, '_agent_music_connect', side_effect=lambda *_args, **_kw:
                                   socket.create_connection(origin.server_address, timeout=2)) as connect:
                with yakala._AgentMusicProxy('youtube', time.monotonic() + 5) as gateway:
                    with socket.create_connection(gateway.server.server_address, timeout=2) as rejected:
                        rejected.sendall(b'CONNECT youtube.com:443 HTTP/1.1\r\n\r\n')
                        self.assertIn(b'407', rejected.recv(1000))
                    connect.assert_not_called()
                    with socket.create_connection(gateway.server.server_address, timeout=2) as client:
                        header = 'CONNECT youtube.com:443 HTTP/1.1\r\nProxy-Authorization: ' + gateway.auth + '\r\n\r\n'
                        client.sendall(header.encode())
                        self.assertIn(b'200', client.recv(1000))
                        client.sendall(b'fixture payload')
                        self.assertEqual(client.recv(1000), b'fixture payload')
                    self.assertEqual(gateway.requests, 1)
                    self.assertEqual(gateway.bytes, len(b'fixture payload') * 2)
            worker.join(2)


if __name__ == '__main__':
    unittest.main()
