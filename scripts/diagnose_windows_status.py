"""Temporary diagnostic only: time unchanged native Windows capture lifecycle tests."""
import faulthandler
import json
from pathlib import Path
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
import v3_yakala_windows_test as cases

faulthandler.enable()
faulthandler.dump_traceback_later(90, repeat=True)
original_entry = cases.WindowsInstalledTest.entry
calls = []


def timed_entry(self, *args):
    start = time.monotonic()
    print('ENTRY_START ' + json.dumps({'args': args, 'at': start}), flush=True)
    try:
        return original_entry(self, *args)
    finally:
        row = {'args': args, 'seconds': round(time.monotonic() - start, 3)}
        calls.append(row)
        print('ENTRY_END ' + json.dumps(row), flush=True)


cases.WindowsInstalledTest.entry = timed_entry
for attempt in range(1, 6):
    print('LIFECYCLE_ATTEMPT ' + str(attempt), flush=True)
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(cases.WindowsInstalledTest)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        sys.exit(1)
print('WINDOWS_MODULE_START', flush=True)
suite = unittest.defaultTestLoader.loadTestsFromModule(cases)
result = unittest.TextTestRunner(verbosity=2).run(suite)
faulthandler.cancel_dump_traceback_later()
print('ENTRY_TIMINGS ' + json.dumps(calls), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
