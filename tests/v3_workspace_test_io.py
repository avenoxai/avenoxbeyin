"""JSON fixture and machine-CLI helpers for cross-platform workspace tests.

Workspace JSON fixtures and machine-readable CLI stdout are UTF-8 JSON;
stderr is diagnostic text and is not part of the JSON transport contract.
Human-readable CLI output is deliberately tested separately.
"""
import json
from pathlib import Path
import subprocess


def load_utf8_json(path):
    return json.loads(Path(path).read_bytes().decode('utf-8'))


def run_json_cli(argv, **kwargs):
    """Send JSON input as bytes; strictly decode only machine-readable stdout."""
    if '--human' in argv:
        raise ValueError('human CLI output must not use JSON protocol decoding')
    if any(name in kwargs for name in ('text', 'encoding', 'errors', 'universal_newlines')):
        raise TypeError('run_json_cli requires binary subprocess pipes')
    payload = kwargs.pop('input', None)
    if payload is not None:
        kwargs['input'] = payload.encode('utf-8') if isinstance(payload, str) else payload
    completed = subprocess.run(argv, **kwargs)
    if completed.stdout is not None:
        completed.stdout = completed.stdout.decode('utf-8', errors='strict')
    if completed.stderr is not None:
        # Diagnostics aren't protocol data: escape undecodable bytes visibly.
        completed.stderr = completed.stderr.decode('utf-8', errors='backslashreplace')
    return completed
