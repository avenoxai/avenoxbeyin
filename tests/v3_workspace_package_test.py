"""Builder helpers and shared behavior survive ZIP-only installation."""
import hashlib
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import shutil
import unittest
import zipfile

from v3_package_helpers import build_package, isolated_env, run_python

ROOT = Path(__file__).resolve().parents[1]


def native_ocr_ready():
    if not shutil.which('tesseract') or not shutil.which('pdftoppm'):return False
    try:
        result=subprocess.run(['tesseract','--list-langs'],capture_output=True,text=True,timeout=3)
        return result.returncode==0 and {'tur','eng'}.issubset(set(result.stdout.splitlines()))
    except (OSError,subprocess.TimeoutExpired):return False


def text_pdf():
    text = b'BT /F1 12 Tf 72 720 Td (Synthetic course source quartz) Tj ET'
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(text)).encode() + b' >>\nstream\n' + text + b'\nendstream']
    raw, offsets = b'%PDF-1.4\n', []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(raw)); raw += str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(raw); raw += b'xref\n0 6\n0000000000 65535 f \n'
    raw += b''.join(('%010d 00000 n \n' % offset).encode() for offset in offsets)
    return raw + b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' + str(xref).encode() + b'\n%%EOF\n'
