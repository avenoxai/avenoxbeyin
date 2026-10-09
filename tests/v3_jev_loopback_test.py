"""A loopback advisor endpoint never goes through an HTTP proxy. Offline: loopback servers only."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from v3_package_helpers import windows_runtime_env

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
sys.path.insert(0, str(ROOT / 'extensions/laya'))
import beyin_v3_jev_client as client

KEY = 'MARKERLOOPBACKKEY'
PROXY_NAMES = ('HTTP_PROXY', 'http_proxy', 'HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy')


def serve(test, reply):
    """Start a loopback HTTP server in a thread; every request is recorded before reply()."""
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(size) or b'{}')
            hits.append(dict(path=self.path, headers=dict(self.headers), body=body))
            status, payload = reply(body)
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return 'http://127.0.0.1:%d' % server.server_address[1], hits


def scores(body):
    return 200, dict(answers={name: dict(type='score', score=1.5) for name in body.get('questions', {})},
                     usage=dict(input_tokens=4, output_tokens=1))


class LoopbackProxyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        self.bridge, self.bridge_hits = serve(self, scores)
        self.proxy, self.proxy_hits = serve(self, scores)
        env = {name: self.proxy for name in PROXY_NAMES}
        env['TYPESAFE_API_KEY'] = KEY
        env.update(windows_runtime_env())
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def card(self):
        return dict(id='one', title='Sentetik', statement='Kisa not.', scope='user', domains=['demo'])

    def test_the_fake_proxy_receives_a_default_urllib_call(self):
        # Control: without the fix urllib sends http://127.0.0.1 through HTTP_PROXY.
        request = urllib.request.Request(self.bridge + '/v1/systemone', data=b'{"questions": {}}', method='POST',
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.build_opener().open(request, timeout=5) as response:
            response.read()
        self.assertEqual(len(self.proxy_hits), 1)
        self.assertEqual(self.bridge_hits, [])

    def test_loopback_bridge_is_called_directly_with_proxies_set(self):
        (self.state / 'jev.json').write_text(json.dumps(dict(mode='shadow', base_url=self.bridge)), encoding='utf-8')
        advice = client.evaluate(self.state, 'kisa notlar', [self.card()])
        self.assertFalse(advice['degraded'], advice['diagnostics'])
        self.assertEqual(advice['scores'], {'one': 1.5})
        self.assertEqual(len(self.bridge_hits), 1)
        self.assertEqual(self.bridge_hits[0]['path'], '/v1/systemone')
        self.assertEqual(self.proxy_hits, [])

    def test_only_loopback_endpoints_drop_the_proxy(self):
        def proxies(endpoint):
            # ProxyHandler({}) registers no *_open method, so the opener keeps no proxy handler at all.
            handlers = [h for h in client._opener(endpoint).handlers if isinstance(h, urllib.request.ProxyHandler)]
            self.assertLessEqual(len(handlers), 1)
            return handlers[0].proxies if handlers else {}
        for endpoint in ('http://127.0.0.1:8765/v1/systemone', 'http://localhost:8765/v1/systemone',
                         'http://[::1]:8765/v1/systemone', 'https://127.0.0.1/v1/systemone'):
            self.assertEqual(proxies(endpoint), {}, endpoint)
        # A remote provider behind a corporate proxy keeps working.
        self.assertEqual(proxies('https://api.typesafe.ai/v1/systemone').get('https'), self.proxy)


class TelemetryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name)
        patcher = patch.dict(os.environ, {'TYPESAFE_API_KEY': KEY, **windows_runtime_env()}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rows(self):
        path = self.state / 'jev-calls.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def test_rows_carry_provider_and_wire_request_count(self):
        client.set_mode(self.state, 'shadow')
        card = dict(id='one', title='A', statement='B', scope='user', domains=['demo'])
        first = client.evaluate(self.state, 'q', [card], transport=lambda *a: scores(a[1])[1])
        second = client.evaluate(self.state, 'q', [card], transport=lambda *a: scores(a[1])[1])
        self.assertEqual((first['http_requests'], second['http_requests']), (1, 0))
        self.assertTrue(second['cache_hit'])
        rows = self.rows()
        self.assertEqual([r['provider'] for r in rows], ['typesafe', 'typesafe'])
        self.assertEqual([(r['network_requests'], r['http_requests']) for r in rows], [(1, 1), (0, 0)])
        self.assertEqual(client._recent(self.state)['network_requests'], 1)
        self.assertNotIn(KEY, (self.state / 'jev-calls.jsonl').read_text(encoding='utf-8'))

    def test_free_text_provider_is_not_logged(self):
        client.log_event(self.state, dict(purpose='retrieval', mode='on', provider='Https://x.invalid', http_requests=2))
        row = self.rows()[0]
        self.assertNotIn('provider', row)
        self.assertEqual(row['http_requests'], 2)


if __name__ == '__main__':
    unittest.main()
