"""Optional POSIX real-Codex regression: no account, hosted model, or network source.

Set BEYIN_CODEX_BINARY to an official Codex executable, then run this module.
A loopback-only Responses fixture drives the real CLI and real Yakala MCP bridge.
Default repository CI skips this test because Codex is not a project dependency.
Windows uses a read-only compatibility profile, not the read-deny contract tested here.
"""
import hashlib
import http.server
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from tests.v3_yakala_ajan_test import AgentFiles, yakala


@unittest.skipUnless(os.environ.get('BEYIN_CODEX_BINARY'), 'Optional official Codex binary not configured')
@unittest.skipIf(os.name == 'nt', 'Windows uses read-only compatibility; this tests the POSIX root-deny profile')
class CodexNativeReadBoundaryTest(AgentFiles):
    def trust_fixture_hook(self, binary, env, config_file):
        """Review only the synthetic hook through Codex's own discovery output."""
        process = subprocess.Popen([binary, 'app-server'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, text=True, env=env, cwd=self.vault)
        messages = queue.Queue()

        def read():
            for line in process.stdout:
                messages.put(json.loads(line))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()

        def request(identifier, method, params):
            process.stdin.write(json.dumps({'id': identifier, 'method': method, 'params': params}) + '\n')
            process.stdin.flush()
            while True:
                response = messages.get(timeout=10)
                if response.get('id') == identifier:
                    self.assertNotIn('error', response)
                    return response['result']

        try:
            request(1, 'initialize', {'clientInfo': {'name': 'yakala-boundary-fixture', 'version': '1'},
                                      'capabilities': {'experimentalApi': True}})
            process.stdin.write('{"method":"initialized"}\n')
            process.stdin.flush()
            result = request(2, 'hooks/list', {'cwds': [str(self.vault)]})
            hooks = [hook for hook in result['data'][0]['hooks']
                     if hook['sourcePath'] == str(config_file.parent / 'hooks.json')]
            self.assertEqual(len(hooks), 1, result)
            hook = hooks[0]
            self.assertEqual(hook['eventName'], 'sessionStart')
            with config_file.open('a', encoding='utf-8') as handle:
                handle.write('\n[hooks.state.' + json.dumps(hook['key']) + ']\n'
                             'enabled = true\ntrusted_hash = ' + json.dumps(hook['currentHash']) + '\n')
        finally:
            process.terminate()
            process.wait(timeout=10)
            process.stdin.close()
            reader.join(2)
            process.stdout.close()

    def test_native_patch_cannot_probe_content_but_real_bridge_can_complete(self):
        self.run_boundary()

    def test_selected_legacy_sandbox_profile_cannot_reopen_native_reads(self):
        self.run_boundary(selected_profile=True)

    def run_boundary(self, selected_profile=False):
        binary = str(Path(os.environ['BEYIN_CODEX_BINARY']).resolve())
        self.configure('codex', executable=binary, extra_args=['--model', 'gpt-5.5'])
        _, job, _ = self.queued(request='Synthetic local fixture only.')
        job['state'] = 'running'
        yakala._agent_prepare_runtime(self.vault, self.state, job)
        knowledge = self.vault / 'knowledge'
        knowledge.mkdir(exist_ok=True)
        ordinary = knowledge / 'ordinary.md'
        ordinary.write_text('SYNTHETIC_VISIBLE_VALUE\n', encoding='utf-8')
        outside = self.vault.parent / 'outside.md'
        outside.write_text('SYNTHETIC_VISIBLE_VALUE\n', encoding='utf-8')
        private = knowledge / 'private.md'
        private.write_text('SYNTHETIC_PRIVATE_VALUE\n', encoding='utf-8')
        control = self.vault / 'AGENTS.md'
        control.write_text('SYNTHETIC_VAULT_RULE: use the authorized MCP bridge.\n', encoding='utf-8')
        originals = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in (ordinary, outside, private, control)}

        def custom(identifier, text):
            return {'type': 'custom_tool_call', 'call_id': identifier, 'name': 'apply_patch', 'input': text}

        def mcp(identifier, name, arguments):
            return {'type': 'function_call', 'call_id': identifier,
                    'namespace': 'mcp__yakala', 'name': name, 'arguments': json.dumps(arguments)}

        items = []
        pairs = []
        targets = ('knowledge/ordinary.md', str(ordinary), '../outside.md', str(outside))
        untouched_destinations = []
        for index, target in enumerate(targets):
            for operation in ('update', 'move'):
                pair = []
                for correct in (True, False):
                    identifier = f'{index}_{operation}_{correct}'
                    pair.append(identifier)
                    guess = 'SYNTHETIC_VISIBLE_VALUE' if correct else 'SYNTHETIC_WRONG_VALUE'
                    moved = target + '.moved'
                    move = f'*** Move to: {moved}\n' if operation == 'move' else ''
                    body = f'*** Begin Patch\n*** Update File: {target}\n{move}@@\n-{guess}\n+replacement\n*** End Patch'
                    items.append(custom(identifier, body))
                    untouched_destinations.append((self.vault / moved).resolve())
                pairs.append(pair)
            items.append(custom(f'{index}_delete', f'*** Begin Patch\n*** Delete File: {target}\n*** End Patch'))
            for suffix in ('', '.new'):
                added = target + suffix
                items.append(custom(f'{index}_add{suffix}', f'*** Begin Patch\n*** Add File: {added}\n+replacement\n*** End Patch'))
                if suffix:
                    untouched_destinations.append((self.vault / added).resolve())
        patch_count = len(items)
        items += [
            mcp('bridge_read', 'read_note', {'path': 'knowledge/ordinary.md'}),
            mcp('bridge_rule', 'read_note', {'path': 'AGENTS.md'}),
            mcp('bridge_private', 'read_note', {'path': 'knowledge/private.md'}),
            mcp('bridge_write', 'write_note', {'title': 'Synthetic result', 'content': 'SYNTHETIC_RESULT_BODY'}),
            mcp('bridge_complete', 'complete', {'status': 'completed', 'summary': 'Synthetic result submitted.'}),
        ]
        requests = []
        errors = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def do_POST(self):
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
                    requests.append(body)
                    index = len(requests) - 1
                    rid = f'resp_fixture_{index}'
                    item = items[index] if index < len(items) else {
                        'type': 'message', 'id': 'done', 'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': 'Synthetic fixture complete.'}]}
                    events = [
                        {'type': 'response.created', 'response': {'id': rid}},
                        {'type': 'response.output_item.done', 'item': item},
                        {'type': 'response.completed', 'response': {'id': rid,
                         'usage': {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}}},
                    ]
                    raw = ''.join('data: ' + json.dumps(e) + '\n\n' for e in events).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/event-stream')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except Exception as error:
                    errors.append(repr(error))

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.shutdown)
        config_dir = Path(self.tmp.name).resolve() / 'codex-fixture-config'
        config_dir.mkdir()
        config_file = config_dir / 'config.toml'
        # A permissive *existing* profile and legacy setting must not survive into
        # the per-job profile. No user config is read or rewritten in this test.
        config_file.write_text('\n'.join([
            'model_provider = "fixture"', 'sandbox_mode = "workspace-write"',
            '[permissions.beyin_yakala.filesystem]', '":root" = "write"',
            '[model_providers.fixture]', 'name = "synthetic local fixture"',
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"',
            'wire_api = "responses"', 'requires_openai_auth = false',
            'supports_websockets = false', 'request_max_retries = 0',
            'stream_max_retries = 0', 'stream_idle_timeout_ms = 10000',
        ]), encoding='utf-8')
        # Lifecycle hooks are trusted local code. The new model-tool profile
        # must not silently disable normal memory startup or widen file grants.
        hook_script = config_dir / 'session_start.py'
        hook_script.write_text(
            'import json, pathlib\n'
            'print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", '
            '"additionalContext": "SYNTHETIC_MEMORY_HOOK " + '
            'pathlib.Path(' + repr(str(control)) + ').read_text(encoding="utf-8")}}))\n', encoding='utf-8')
        hook_argv = [sys.executable, str(hook_script)]
        hook_command = subprocess.list2cmdline(hook_argv) if os.name == 'nt' else shlex.join(hook_argv)
        (config_dir / 'hooks.json').write_text(json.dumps({'hooks': {'SessionStart': [
            {'hooks': [{'type': 'command', 'command': hook_command}]}]}}), encoding='utf-8')
        env = {key: os.environ[key] for key in ('PATH', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP')
               if key in os.environ}
        env.update(CODEX_HOME=str(config_dir), TERM='dumb', NO_PROXY='127.0.0.1,localhost')
        self.trust_fixture_hook(binary, env, config_file)
        config_hash = hashlib.sha256(config_file.read_bytes()).hexdigest()
        profile_file = config_dir / 'fixture_legacy.config.toml'
        profile_file.write_text('sandbox_mode = "read-only"\n', encoding='utf-8')
        profile_hash = hashlib.sha256(profile_file.read_bytes()).hexdigest()
        with patch.object(yakala, '_agent_executable', return_value=[binary]):
            argv = yakala._agent_command(self.vault, self.state, job, job['config'])
        if selected_profile:
            # Extra stress case for Codex's supported config-layer selector.
            # Yakala itself does not expose --profile in its extra_args.
            argv[1:1] = ['--profile', 'fixture_legacy']
        invalid_argv = list(argv)
        invalid_argv[1:1] = ['-c', 'unknown_boundary_contract=true']
        invalid = subprocess.run(invalid_argv, input='Synthetic invalid configuration.', cwd=self.vault,
                                 env=env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(invalid.returncode, 0, invalid.stdout)
        self.assertIn('unknown configuration field', invalid.stderr)
        self.assertFalse(requests, 'Invalid permission config reached the model endpoint')
        result = subprocess.run(argv, input=yakala._agent_prompt(self.vault, job), cwd=self.vault,
                                env=env, capture_output=True, text=True, timeout=55)
        outputs = {}
        for request in requests:
            for item in request.get('input', []):
                if item.get('type', '').endswith('_call_output'):
                    outputs[item.get('call_id')] = item.get('output')
        evidence = {'argv': argv, 'exit_code': result.returncode, 'requests': len(requests),
                    'selected_config_profile': selected_profile,
                    'patch_calls': patch_count, 'outputs': outputs,
                    'initial_tools': requests[0].get('tools', []) if requests else [],
                    'memory_hook_context': 'SYNTHETIC_MEMORY_HOOK' in json.dumps(requests[0]) if requests else False,
                    'invalid_config_exit_code': invalid.returncode, 'invalid_config_error': invalid.stderr,
                    'stdout': result.stdout, 'stderr': result.stderr,
                    'config_unchanged': hashlib.sha256(config_file.read_bytes()).hexdigest() == config_hash}
        destination = os.environ.get('BEYIN_CODEX_EVIDENCE')
        if destination:
            destination = Path(destination)
            if selected_profile:
                destination = destination.with_name(destination.stem + '-selected-profile' + destination.suffix)
            destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(evidence['memory_hook_context'], result.stderr)
        self.assertFalse(errors, errors)
        self.assertEqual(len(outputs), len(items), result.stderr)
        for correct, incorrect in pairs:
            self.assertEqual(outputs[correct], outputs[incorrect], correct)
        for item in items[:patch_count]:
            output = str(outputs[item['call_id']])
            self.assertNotIn('SYNTHETIC_VISIBLE_VALUE', output)
            self.assertNotIn('SYNTHETIC_WRONG_VALUE', output)
            self.assertNotIn('Failed to find expected lines', output)
            self.assertRegex(output, r'(?i)(rejected|failed to read|denied|not permitted)')
        for path, digest in originals.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest, str(path))
        self.assertTrue(all(not path.exists() for path in untouched_destinations))
        self.assertIn('SYNTHETIC_VISIBLE_VALUE', str(outputs['bridge_read']))
        self.assertIn('SYNTHETIC_VAULT_RULE', str(outputs['bridge_rule']))
        self.assertNotIn('SYNTHETIC_PRIVATE_VALUE', str(outputs['bridge_private']))
        self.assertIn('Hidden and sensitive notes are not available', str(outputs['bridge_private']))
        manifest = json.loads((yakala._agent_job_dir(self.vault, self.state, job['id']) / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['status'], 'completed')
        self.assertTrue(manifest['notes'])
        self.assertIn('SYNTHETIC_RESULT_BODY', (self.vault / manifest['notes'][0]).read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(config_file.read_bytes()).hexdigest(), config_hash)
        self.assertEqual(hashlib.sha256(profile_file.read_bytes()).hexdigest(), profile_hash)


if __name__ == '__main__':
    unittest.main()
