"""Embedded CLI results leave native human output streams owned by their caller."""
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class NativeTTY(io.TextIOWrapper):
    def isatty(self):
        return True


class CliOutputTest(unittest.TestCase):
    def test_receipt_return_result_preserves_native_tty_and_emits_nothing(self):
        spec = importlib.util.spec_from_file_location('beyin_output_test_cli', ROOT/'scripts/beyin_v3.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root/'vault'; vault.mkdir()
            (vault/'notes').mkdir()
            (vault/'notes/source.md').write_bytes('Kaynak ölçüm\n'.encode('utf-8'))
            streams = [NativeTTY(io.BytesIO(), encoding='cp1252', errors='backslashreplace') for _ in range(2)]
            self.addCleanup(streams[0].close); self.addCleanup(streams[1].close)
            before = [(stream.encoding, stream.errors) for stream in streams]
            with patch.object(sys, 'stdout', streams[0]), patch.object(sys, 'stderr', streams[1]), patch.object(sys, 'stdin', io.StringIO()):
                result, code = cli.main(['--vault', str(vault), '--state', str(root/'state'),
                    'receipt', '--event-id', 'output-ownership', '--summary', 'Ölçüm kaydedildi. Öğrenilen: yok',
                    '--ref', 'notes/source.md'], return_result=True)
                error, error_code = cli.main(['--vault', str(vault), '--state', str(root/'state'),
                    'receipt', '--event-id', 'output-error', '--summary', 'Eksik kaynak. Öğrenilen: yok',
                    '--ref', 'notes/missing.md'], return_result=True)
            self.assertEqual(error_code, 1)
            self.assertIn('error', error)
            self.assertEqual(code, 0)
            self.assertIsInstance(result, dict)
            self.assertEqual([(stream.encoding, stream.errors) for stream in streams], before)
            for stream in streams:
                self.assertEqual(stream.buffer.getvalue(), b'')


if __name__ == '__main__':
    unittest.main()
