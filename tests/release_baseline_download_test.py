"""Verify the release workflow's baseline downloader against local HTTP assets."""

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/download_v3_release_baselines.sh'
FAILED_ASSET = '/v3.0.2/beyin-v3-3.0.2.zip.sha256'
PARTIAL_ASSET = '/v3.1.0/beyin-v3-3.1.0.zip'


class ReleaseBaselineDownloadTest(unittest.TestCase):
    def run_download(self, corrupt=False, persistent_failure=False, partial_failure=False):
        assets = {}
        for version in ('3.0.2', '3.1.0'):
            name = f'beyin-v3-{version}.zip'
            data = f'synthetic release {version}'.encode()
            digest = '0' * 64 if corrupt else hashlib.sha256(data).hexdigest()
            assets[f'/v{version}/{name}'] = data
            assets[f'/v{version}/{name}.sha256'] = f'{digest}  {name}\n'.encode()

        class Handler(BaseHTTPRequestHandler):
            failures = 0
            partials = 0

            def do_GET(self):
                if self.path == FAILED_ASSET and (persistent_failure or not self.failures):
                    type(self).failures += 1
                    self.send_error(500, 'synthetic Release Assets failure')
                    return
                data = assets.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                if self.path == PARTIAL_ASSET and partial_failure and not self.partials:
                    type(self).partials += 1
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data[:5])
                    self.close_connection = True
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                url = f'http://127.0.0.1:{server.server_port}'
                result = subprocess.run(['bash', str(SCRIPT), url], cwd=tmp,
                                        env=dict(os.environ, NO_PROXY='127.0.0.1', no_proxy='127.0.0.1'),
                                        text=True, capture_output=True)
                files = sorted(path.name for path in (Path(tmp) / 'baseline').glob('*'))
                return result, files, Handler.failures, Handler.partials
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_transient_http_500_downloads_both_versions(self):
        result, files, failures, _partials = self.run_download()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(failures, 1)
        self.assertEqual(files, [
            'beyin-v3-3.0.2.zip', 'beyin-v3-3.0.2.zip.sha256',
            'beyin-v3-3.1.0.zip', 'beyin-v3-3.1.0.zip.sha256',
        ])

    def test_bad_checksum_stops_build(self):
        result, _files, _failures, _partials = self.run_download(corrupt=True)
        self.assertNotEqual(result.returncode, 0)

    def test_persistent_http_500_stops_without_complete_checksum(self):
        result, files, failures, _partials = self.run_download(persistent_failure=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failures, 4)
        self.assertNotIn('beyin-v3-3.0.2.zip.sha256', files)

    def test_truncated_200_retries_and_verifies_complete_zip(self):
        result, files, _failures, partials = self.run_download(partial_failure=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(partials, 1)
        self.assertIn('beyin-v3-3.1.0.zip', files)


if __name__ == '__main__':
    unittest.main()
