"""The live probe's reporting is testable without reading the desktop."""
import contextlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manual_browser_capture as probe


class BrowserProbeTest(unittest.TestCase):
    def test_urls_and_titles_are_not_logged_by_default(self):
        context = {'url': 'https://example.org/private?secret=123',
                   'url_imlec': 'https://example.org/other', 'baslik': 'Private title',
                   'uygulama': 'Firefox', 'url_yontem': 'windows-msaa-gecko'}
        result = probe.record(context, 0.125, expected_page=context['url'])
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('Private title', json.dumps(result))
        self.assertTrue(result['page_matches'])
        self.assertEqual(result['elapsed_ms'], 125)
        self.assertEqual(probe.record(context, 0, show_urls=True)['page'], context['url'])

    def test_empty_and_wrong_page_are_distinct_outcomes(self):
        records = [probe.record({}, 0.1, expect_empty=True),
                   probe.record({'url': 'https://example.org/wrong'}, 0.3,
                                expected_page='https://example.org/right')]
        self.assertEqual(probe.summarize(records), dict(samples=2, with_page=1, with_hover=0,
                                                       p50_ms=200, p95_ms=300,
                                                       checked_samples=2, mismatches=1))

    def test_cli_reports_comparison_failure_without_native_calls(self):
        output = io.StringIO()
        with patch.object(probe, 'read_browser', return_value={}) as read, \
                patch.object(probe.time, 'sleep'), contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(io.StringIO()):
            code = probe.main(['--delay', '0', '--repeat', '2', '--expected-page', 'https://example.org/'])
        self.assertEqual(code, 1)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(json.loads(output.getvalue())['summary']['mismatches'], 2)

    def test_no_observed_url_is_not_automatically_a_pass(self):
        output = io.StringIO()
        with patch.object(probe, 'read_browser', return_value={}), patch.object(probe.time, 'sleep'), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            code = probe.main(['--delay', '0'])
        self.assertEqual(code, 0)  # a probe completed, no claimed correctness verdict
        summary = json.loads(output.getvalue())['summary']
        self.assertEqual((summary['with_page'], summary['checked_samples']), (0, 0))

    def test_invalid_delay_and_expectations_fail_before_any_native_read(self):
        for args in (['--delay', 'nan'], ['--interval', 'inf'], ['--repeat', '0'],
                     ['--expected-page', 'example.org'],
                     ['--expect-empty', '--expected-hover', 'https://example.org/']):
            with self.subTest(args=args), patch.object(probe, 'read_browser') as read, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                probe.main(args)
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
