"""#600: conservative missing-section warnings, using only synthetic sources."""
import io
import json
import os
import shlex
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from email.message import Message
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y
from v3_package_helpers import inherited_env, isolated_path

HEADINGS = [f'How does synthetic topic {number} work?' for number in range(10)]
WORDS = 'This is a synthetic explanation of the captured topic. ' * 5


def source(headings=HEADINGS, tag='main'):
    return f'<html><{tag}>' + ''.join(f'<section><h3>{title}</h3><p>{WORDS}</p></section>'
                                    for title in headings) + f'</{tag}></html>'


def extracted(headings=HEADINGS):
    return '\n\n'.join(f'### {title}\n\n{WORDS}' for title in headings).strip()


class Response(io.BytesIO):
    def __init__(self, html, content_type='text/html; charset=utf-8'):
        super().__init__(html.encode() if isinstance(html, str) else html)
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        self.read_sizes, self.delivered = [], 0

    def read(self, size=-1):
        self.read_sizes.append(size)
        return super().read(size)

    def read1(self, size=-1):
        self.read_sizes.append(size)
        part = super().read1(size)
        self.delivered += len(part)
        return part


class CompletenessTest(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory(prefix='yakala-completeness-')
        self.addCleanup(work.cleanup)
        self.vault = Path(work.name)
        self.saved = y.capture(self.vault, url='https://example.com/article')
        self.found = {key: None for key in ('npx', 'yt_dlp', 'whisper', 'pdftotext', 'ffmpeg')}
        self.found['npx'] = 'fixture-npx'
        self.opener = Mock()
        self.mock_tools = patch.object(y, 'tools', return_value=self.found)
        self.mock_tools.start()
        self.addCleanup(self.mock_tools.stop)
        dns = patch.object(y, '_safe_network_url', side_effect=lambda url: url)
        dns.start()
        self.addCleanup(dns.stop)

    def run_capture(self, html, markdown, *, error=None, content_type='text/html; charset=utf-8'):
        response = Response(html, content_type)
        self.opener.reset_mock()
        self.opener.open.return_value = response
        self.opener.open.side_effect = error
        with patch.object(y, '_run', return_value=(0, json.dumps({'content': markdown}), '')), \
                patch.object(y, '_public_opener', return_value=self.opener):
            result = y.process(self.vault, [self.saved['id']])
        card = y.find_card(self.vault, self.saved['id'])
        return result['kartlar'][0], card['meta'], (self.vault / card['meta']['ham']).read_text(), response

    def test_missing_sections_warn_in_result_card_and_human_output_without_appending_source(self):
        text = extracted(HEADINGS[:6])
        entry, meta, raw, response = self.run_capture(source(), text)
        self.assertEqual(entry['durum'], 'cikarildi')
        self.assertEqual(entry['yontem'], 'defuddle')
        self.assertIn('uyari', meta)
        self.assertEqual(meta['uyari'], entry['not'])
        self.assertIn('10', entry['not'])
        self.assertIn('4', entry['not'])
        self.assertIn('eksik olabilir', entry['not'])
        self.assertIn(entry['not'], y.human({'kartlar': [entry]}, 'isle'))
        self.assertIn(text, raw)
        self.assertNotIn(HEADINGS[-1], raw)
        self.opener.open.assert_called_once()
        self.assertEqual(set(response.read_sizes), {65536})  # bounded chunks, each checked against the deadline

    def test_heading_formatting_case_entities_duplicates_and_boilerplate_do_not_warn(self):
        card = y.find_card(self.vault, self.saved['id'])
        y.write_card(card['path'], dict(card['meta'], uyari='Previous section warning'), card['body'])
        titles = ['Error Analysis &amp; Data Collection', '<em>Choosing</em> a Useful Metric',
                  'When to <code>review</code> results', 'Reading [literal] brackets',
                  'Collecting data for a useful evaluation', 'Using a stable comparison dataset']
        html = source(titles + titles)
        noise = source(HEADINGS, 'div')
        for wrapper in ('nav', 'aside', 'header', 'footer', 'template'):
            html = html.replace('</main>', f'<{wrapper}>{noise}</{wrapper}></main>')
        for attrs in ('hidden', 'aria-hidden="true"', 'style="display: none"',
                      'style="visibility:hidden"', 'role="navigation"', 'class="sidebar"'):
            html = html.replace('</main>', f'<div {attrs}>{noise}</div></main>')
        md = ('ERROR ANALYSIS & Data Collection\n===============================\n\n'
              '## [Choosing a Useful Metric](https://example.com/metric)\n\n'
              'When to `review` results\n----------------------\n\n'
              '### Reading [literal] brackets\n\n'
              '## Collecting **data** for a useful evaluation\n\n'
              '## Using a stable comparison dataset\n\n' + WORDS)
        entry, meta, _, _ = self.run_capture(html, md)
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)

    def test_small_or_ambiguous_evidence_never_claims_incompleteness(self):
        fixtures = [(source(), extracted(HEADINGS[:8])),  # fewer than 3 missing
                    (source(HEADINGS[:4]), extracted(HEADINGS[:2])),  # too few source headings
                    (source(), extracted(['Unrelated topic on another response'])),  # no overlap
                    (source(tag='div'), extracted(HEADINGS[:6])),  # no semantic region
                    (source(tag='article') * 2, extracted(HEADINGS[:6])),  # multiple articles
                    (source() * 2, extracted(HEADINGS[:6]))]  # multiple main regions
        for html, md in fixtures:
            with self.subTest(html=html[:30], md=md[:30]):
                entry, meta, _, _ = self.run_capture(html, md)
                self.assertNotIn('uyari', meta)
                self.assertNotIn('not', entry)

    def test_retries_preserve_warning_when_unverifiable_and_clear_when_headings_present(self):
        first, meta, _, _ = self.run_capture(source(), extracted(HEADINGS[:6]))
        self.assertIn('uyari', meta)
        for html, error in (('', OSError('offline')), ('<html>no comparable structure</html>', None)):
            with self.subTest(error=error):
                entry, current, raw, _ = self.run_capture(html, extracted(), error=error)
                self.assertEqual(entry['durum'], 'cikarildi')
                self.assertEqual(current['uyari'], meta['uyari'])
                self.assertEqual(entry['not'], current['uyari'])
                self.assertIn(extracted(), raw)
        entry, meta, _, _ = self.run_capture(source(), extracted())
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)

    def test_fetch_failure_and_non_html_keep_extracted_content_without_new_warning(self):
        for error, content_type in ((OSError('offline'), 'text/html'), (None, 'application/json')):
            with self.subTest(error=error, content_type=content_type):
                entry, meta, raw, _ = self.run_capture(source(), extracted(HEADINGS[:6]),
                                                      error=error, content_type=content_type)
                self.assertEqual(entry['durum'], 'cikarildi')
                self.assertNotIn('uyari', meta)
                self.assertIn(extracted(HEADINGS[:6]), raw)
                self.opener.open.assert_called_once()

    def test_oversize_source_does_not_clear_existing_warning_from_partial_evidence(self):
        _, before, _, _ = self.run_capture(source(), extracted(HEADINGS[:6]))
        html = source(HEADINGS[:6]) + '<!--' + 'x' * 5_000_000
        entry, meta, _, response = self.run_capture(html, extracted(HEADINGS[:6]))
        self.assertEqual(meta['uyari'], before['uyari'])
        self.assertEqual(entry['not'], meta['uyari'])
        self.assertEqual(set(response.read_sizes), {65536})
        self.assertLessEqual(response.delivered, 5_000_000 + 65536)  # stops once the limit is passed

    def test_failed_defuddle_reuses_single_stdlib_fetch(self):
        entry, _, raw, _ = self.run_capture(source(), '')
        self.assertEqual(entry['yontem'], 'http')
        self.assertIn(HEADINGS[-1], raw)
        self.opener.open.assert_called_once()

    def test_generic_types_inside_code_are_not_mistaken_for_html_tags(self):
        titles = HEADINGS[:3] + ['Using List&lt;T&gt; safely', 'Using Map&lt;K,V&gt; safely', 'Using Set&lt;T&gt; safely']
        md = extracted(HEADINGS[:3] + ['Using `List<T>` safely', 'Using ``Map<K,V>`` safely', 'Using `Set<T>` safely'])
        self.assertEqual(y._completeness_check(source(titles), md), {'uyari': None})
        titles = HEADINGS[:3] + [f'Literal [option]({n}) syntax' for n in range(3)]
        md = extracted(HEADINGS[:3] + [f'Literal `[option]({n})` syntax' for n in range(3)])
        self.assertEqual(y._completeness_check(source(titles), md), {'uyari': None})

    def test_backslash_escaped_tags_in_headings_are_text_not_html(self):
        # Defuddle (Turndown) escapes "<" in prose: a real MDN heading came out as
        # "### Styling the \<details> element" and was counted as missing.
        titles = HEADINGS[:3] + ['Styling the &lt;details&gt; element', 'Using &lt;canvas&gt; for charts',
                                 'Wrapping text in &lt;pre&gt; blocks', 'Reading [literal] brackets safely']
        md = extracted(HEADINGS[:3] + ['Styling the \\<details> element', 'Using \\<canvas\\> for charts',
                                       'Wrapping text in \\<pre> blocks', 'Reading \\[literal\\] brackets safely'])
        self.assertEqual(y._completeness_check(source(titles), md), {'uyari': None})  # was a false warning

    def test_autolink_text_is_not_mistaken_for_html_tags(self):
        links = ['https://example.com/one', 'https://example.com/two', 'person@example.com']
        titles = HEADINGS[:3] + ['Documentation for ' + link for link in links]
        md = extracted(HEADINGS[:3] + ['Documentation for <' + link + '>' for link in links])
        self.assertEqual(y._completeness_check(source(titles), md), {'uyari': None})

    def test_line_breaks_and_raw_text_do_not_create_false_missing_headings(self):
        titles = HEADINGS[:3] + [f'How does<br>topic {n} work?' for n in range(3)]
        md = extracted(HEADINGS[:3] + [f'How does topic {n} work?' for n in range(3)])
        self.assertEqual(y._completeness_check(source(titles), md), {'uyari': None})
        for tag in ('textarea', 'xmp', 'iframe', 'title'):
            html = source(HEADINGS[:3]).replace('</main>', f'<{tag}>' +
                                                source(HEADINGS[3:6], 'div') + f'</{tag}></main>')
            with self.subTest(tag=tag):
                self.assertEqual(y._completeness_check(html, extracted(HEADINGS[:3])), {})

    def test_article_inside_main_wins_over_outer_headings(self):
        html = '<main><h2>Other page-level material to ignore</h2>' + source(tag='article') + '</main>'
        entry, meta, _, _ = self.run_capture(html, extracted(HEADINGS[:6]))
        self.assertIn('uyari', meta)
        self.assertIn('10', entry['not'])

    def test_disjoint_main_and_article_are_ambiguous(self):
        main = source(HEADINGS[:3])
        article = source(HEADINGS[:6], 'article')
        for html in (main + article, article + main):
            with self.subTest(prefix=html[:20]):
                self.assertEqual(y._completeness_check(html, extracted(HEADINGS[:3])), {})

    def test_threshold_requires_both_count_and_fraction_and_matching_context(self):
        titles = [f'How does synthetic topic {number} work?' for number in range(13)]
        for total, retained, warns in ((12, 9, True), (13, 10, False), (6, 3, True), (6, 2, False)):
            with self.subTest(total=total, retained=retained):
                check = y._completeness_check(source(titles[:total]), extracted(titles[:retained]))
                self.assertEqual(bool(check.get('uyari')), warns)

    def test_structure_and_heading_budgets_are_unknown_not_complete(self):
        for html in ('<div>' * 257 + source() + '</div>' * 257,
                     source([f'Synthetic heading number {n}' for n in range(513)]),
                     source(HEADINGS * 205), '<main><h3>Unclosed source heading'):
            with self.subTest(prefix=html[:50]):
                self.assertEqual(y._completeness_check(html, extracted()), {})

    def test_revalidation_failure_preserves_extraction_and_never_opens_socket(self):
        card = y.find_card(self.vault, self.saved['id'])
        with patch.object(y, 'via_defuddle', return_value=extracted(HEADINGS[:6])), \
                patch.object(y, '_safe_network_url', side_effect=[card['meta']['url'], ValueError('private DNS')]), \
                patch.object(y, '_public_opener') as opener:
            text, method, warning = y.extract(self.vault, card, self.found)
        self.assertEqual((text, method, warning), (extracted(HEADINGS[:6]), 'defuddle', None))
        opener.assert_not_called()

    def test_any_failure_inside_the_optional_check_keeps_the_extracted_page(self):
        # html.parser raises AssertionError (3.11 before 3.11.14) or NotImplementedError on some
        # malformed declarations; such pages must still be captured, just without new evidence.
        for error in (AssertionError('unknown status keyword'), NotImplementedError('error()'), RecursionError()):
            with self.subTest(error=type(error).__name__), patch.object(y, '_completeness_check', side_effect=error):
                entry, meta, raw, _ = self.run_capture(source(), extracted(HEADINGS[:6]))
                self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'defuddle'))
                self.assertNotIn('uyari', meta)
                self.assertIn(extracted(HEADINGS[:6]), raw)

    def test_markup_stripping_stays_linear_on_hostile_page_text(self):
        # Unbounded patterns took ~14 s each on these 12k-character lines (quadratic backtracking).
        size = 12_000
        for md in ('[' * size, '`' * 2_000 + 'a' * size, '[a](' * (size // 4), '<' * size, '<a@' * (size // 3)):
            with self.subTest(md=md[:8]):
                started = time.monotonic()
                self.assertEqual(y._completeness_check(source(), extracted(HEADINGS[:6]) + '\n\n' + md)['uyari'][:20],
                                 'Metin eksik olabilir')
                self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(y._completeness_check(source(), 'x' * 5_000_001), {})  # larger than any source it came from

    def test_slow_source_cannot_hold_isle_past_the_check_budget(self):
        import socket
        import urllib.request
        html, seen = source().encode(), []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path.startswith('/hop/'):  # each redirect is quick enough alone, the chain is not
                    time.sleep(0.4)
                    hop = int(self.path.rsplit('/', 1)[1])
                    self.send_response(302)
                    self.send_header('Location', f'/hop/{hop + 1}' if hop < 6 else '/slow')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                try:  # every byte arrives well within the per-read timeout, the whole page takes ~4 s
                    for start in range(0, len(html), len(html) // 20 + 1):
                        self.wfile.write(html[start:start + len(html) // 20 + 1])
                        self.wfile.flush()
                        time.sleep(0.2)
                except OSError:
                    pass

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 10)
        self.addCleanup(server.shutdown)
        dns, connect, opener = socket.getaddrinfo, socket.create_connection, urllib.request.build_opener

        def answer(host, port, *args, **kwargs):
            if host == 'example.com':
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]
            return dns(host, port, *args, **kwargs)

        def local_transport(address, timeout=30, source_address=None):
            self.assertEqual(address[0], '8.8.8.8')
            return connect(('127.0.0.1', server.server_port), timeout, source_address)

        card = {'meta': {'url': f'http://example.com:{server.server_port}/slow'}, 'body': ''}
        with patch.object(socket, 'getaddrinfo', side_effect=answer), \
                patch.object(socket, 'create_connection', side_effect=local_transport), \
                patch.object(urllib.request, 'build_opener', side_effect=lambda *handlers:
                             opener(urllib.request.ProxyHandler({}), *handlers)), \
                patch.object(y, 'CHECK_BUDGET', 1), patch.object(y, 'CHECK_TIMEOUT', 1), \
                patch.object(y, 'via_defuddle', return_value=extracted(HEADINGS[:6])):
            started = time.monotonic()
            info = {}
            self.assertEqual(y.extract(self.vault, card, self.found, info=info),
                             (extracted(HEADINGS[:6]), 'defuddle', None))
            self.assertLess(time.monotonic() - started, 2.5)
            self.assertNotIn('uyari', info)  # an unfinished read is unknown, not evidence either way
            self.assertIn(HEADINGS[-1], y.via_stdlib(card['meta']['url']))  # the fallback keeps its own limits
            seen.clear()
            started = time.monotonic()
            self.assertIsNone(y._fetch_html(card['meta']['url'].replace('/slow', '/hop/0'), complete=True,
                                            budget=y.CHECK_BUDGET))
            self.assertLess(time.monotonic() - started, 2)
            self.assertLess(len(seen), 5)  # no further redirect is followed once the budget is spent

    # Second report on #600: Defuddle 0.19.4 removes every class token ending in ":hidden", so a
    # Tailwind note box with "empty:hidden" (visible whenever it has content) vanishes even though
    # all headings survive and the heading comparison alone would clear the warning.
    NOTES = ['Keep each question atomic: one decision per question in the evaluator.',
             'If the provider refuses any question the run fails and writes no scores.',
             'Pin a version such as jev-1.13.0 when an evaluation threshold should stay stable.']

    def with_notes(self, headings=HEADINGS, notes=NOTES, cls='text-secondary prose empty:hidden', tag='div'):
        html = source(headings)
        for note in notes:
            html = html.replace('</p></section>', f'</p><{tag} class="{cls}"><p>{note}</p></{tag}></section>', 1)
        return html

    def test_lost_empty_hidden_notes_warn_even_when_every_heading_survives(self):
        card = y.find_card(self.vault, self.saved['id'])
        y.write_card(card['path'], dict(card['meta'], uyari='Previous section warning'), card['body'])
        entry, meta, raw, _ = self.run_capture(self.with_notes(), extracted())
        self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'defuddle'))
        self.assertIn('empty:hidden', meta['uyari'])
        self.assertIn(' 3 metin blogu', meta['uyari'])
        self.assertNotIn('h2/h3', meta['uyari'])
        self.assertEqual(entry['not'], meta['uyari'])
        self.assertIn(extracted(), raw)
        self.assertNotIn(self.NOTES[0], raw)
        # Defuddle's own retry can keep hidden elements: then the notes are present and the warning clears.
        md = extracted() + '\n\n' + '\n\n'.join('> **Note:** ' + note.replace('jev-1.13.0', '`jev-1.13.0`')
                                               for note in self.NOTES)
        entry, meta, _, _ = self.run_capture(self.with_notes(), md)
        self.assertNotIn('uyari', meta)
        self.assertNotIn('not', entry)

    def test_both_signals_are_reported_together(self):
        check = y._completeness_check(self.with_notes(), extracted(HEADINGS[:6]))
        self.assertIn('10 farkli h2/h3 basliginin 4 tanesi', check['uyari'])
        self.assertIn('(empty:hidden) 3 metin blogu', check['uyari'])
        self.assertEqual(check['uyari'].count('Metin eksik olabilir'), 1)

    def test_notes_alone_need_same_page_evidence_and_follow_defuddles_class_rule(self):
        few = HEADINGS[:4]
        self.assertIn('empty:hidden', y._completeness_check(self.with_notes(few), extracted(few))['uyari'])
        self.assertEqual(y._completeness_check(self.with_notes(few), extracted(few[:2])), {})  # other page?
        self.assertEqual(y._completeness_check(self.with_notes(few), extracted(['Unrelated heading elsewhere'])), {})
        for cls, tag in (('empty:hidden md:block', 'div'),  # a responsive show class: Defuddle keeps it
                         ('[&:empty]:hidden', 'div'),  # arbitrary variant: Defuddle keeps it
                         ('md:hidden', 'div'),  # viewport copies are often duplicates: not claimed
                         ('Empty:Hidden', 'div'),  # Defuddle compares class tokens case-sensitively
                         ('empty:hidden', 'aside'), ('empty:hidden', 'nav')):  # boilerplate is not compared
            with self.subTest(cls=cls, tag=tag):
                self.assertEqual(y._completeness_check(self.with_notes(cls=cls, tag=tag), extracted()),
                                 {'uyari': None})
        short = ['Copy', 'Was this page useful?', 'Edit on GitHub']  # fewer than five words
        self.assertEqual(y._completeness_check(self.with_notes(notes=short), extracted()), {'uyari': None})
        outside = source().replace('</main>', '</main><div class="empty:hidden"><p>' + self.NOTES[0] + '</p></div>')
        self.assertEqual(y._completeness_check(outside, extracted()), {'uyari': None})

    def test_note_blocks_compare_per_paragraph_and_list_item(self):
        items = ''.join(f'<li>{note}</li>' for note in self.NOTES)
        html = source().replace('</main>', f'<div class="empty:hidden"><p>Before you start, read these rules '
                                f'carefully.</p><ol>{items}</ol></div></main>')
        md = extracted() + '\n\nBefore you start, read these rules carefully.\n\n' + '\n'.join(
            f'{n}. {note}' for n, note in enumerate(self.NOTES, 1))
        self.assertEqual(y._completeness_check(html, md), {'uyari': None})  # list numbers do not split a match
        self.assertIn('(empty:hidden) 4 metin blogu', y._completeness_check(html, extracted())['uyari'])
        nested = source().replace('</main>', '<div class="empty:hidden"><p>Keep each <code>question</code> '
                                  'atomic, with <a href="/x">one decision</a> per question.</p></div></main>')
        md = extracted() + '\n\nKeep each `question` atomic, with [one decision](/x) per question.'
        self.assertEqual(y._completeness_check(nested, md), {'uyari': None})

    def test_info_is_optional_but_warning_is_still_returned(self):
        card = y.find_card(self.vault, self.saved['id'])
        with patch.object(y, 'via_defuddle', return_value=extracted(HEADINGS[:6])), \
                patch.object(y, '_fetch_html', return_value=source()):
            text, method, warning = y.extract(self.vault, card, self.found)
        self.assertEqual((text, method), (extracted(HEADINGS[:6]), 'defuddle'))
        self.assertIn('eksik olabilir', warning)

    @unittest.skipUnless(os.name == 'posix', 'the installed npx stand-in is a shell wrapper')
    def test_real_installed_cli_warns_with_one_guarded_source_retrieval(self):
        vault, state, bin_dir = (self.vault / name for name in ('installed-vault', 'state', 'bin'))
        vault.mkdir()
        bin_dir.mkdir()
        env = inherited_env(HOME=str(self.vault / 'home'), APPDATA=str(self.vault / 'appdata'),
                            BEYIN_V3_NO_SPAWN='1', PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8')
        # The fixture serves only synthetic HTML over loopback. No configured proxy or external
        # network can be used by this subprocess. Production URL/DNS/redirect guards still run.
        for key in list(env):
            if key.lower().endswith('_proxy'):
                env.pop(key)
        installed = subprocess.run([sys.executable, str(ROOT / 'scripts/install_v3.py'),
                                    '--vault', str(vault), '--state', str(state)], cwd=ROOT, env=env,
                                   capture_output=True, text=True, encoding='utf-8', timeout=120)
        self.assertEqual(installed.returncode, 0, installed.stderr)
        (bin_dir / 'answer.json').write_text(json.dumps({'content': extracted(HEADINGS[:6])}))
        helper = bin_dir / 'helper.py'
        helper.write_text("import sys\nfrom pathlib import Path\n"
                          "root = Path(__file__).parent\n"
                          "with (root / 'calls.txt').open('a') as calls: calls.write(repr(sys.argv[1:]) + '\\n')\n"
                          "print((root / 'answer.json').read_text())\n")
        npx = bin_dir / 'npx'
        npx.write_text('#!/bin/sh\nexec ' + shlex.join([sys.executable, str(helper)]) + ' "$@"\n')
        npx.chmod(0o755)
        seen = []
        html = source().encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path == '/article':
                    self.send_response(302)
                    self.send_header('Location', '/final')
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Content-Length', str(len(html)))
                self.end_headers()
                self.wfile.write(html)

            def log_message(self, *_):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        self.addCleanup(server.server_close)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(server.shutdown)
        (bin_dir / 'sitecustomize.py').write_text(
            "import socket\nreal_connect = socket.create_connection\nreal_dns = socket.getaddrinfo\n"
            "def dns(host, port, *args, **kwargs):\n"
            "    if host == '127.0.0.1': return real_dns(host, port, *args, **kwargs)\n"
            "    assert host == 'example.com', host\n"
            "    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', port))]\n"
            "def connect(address, timeout=30, source_address=None):\n"
            "    assert address[0] == '8.8.8.8', address\n"
            f"    return real_connect(('127.0.0.1', {server.server_port}), timeout, source_address)\n"
            "socket.getaddrinfo = dns\nsocket.create_connection = connect\n")
        env.update(PYTHONPATH=str(bin_dir), PATH=isolated_path(bin_dir))

        def cli(*args):
            result = subprocess.run([sys.executable, str(vault / 'beyin.py'), 'yakala', *args],
                                    cwd=vault, env=env, capture_output=True, text=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        added = cli('ekle', f'http://example.com:{server.server_port}/article')
        self.assertEqual(seen, [])
        entry = cli('isle', added['id'])['kartlar'][0]
        self.assertEqual((entry['durum'], entry['yontem']), ('cikarildi', 'defuddle'))
        self.assertIn('not', entry)
        self.assertEqual(seen, ['/article', '/final'])
        self.assertEqual(len((bin_dir / 'calls.txt').read_text().splitlines()), 1)
        meta = y.find_card(vault, added['id'])['meta']
        self.assertEqual(entry['not'], meta['uyari'])
        self.assertIn('eksik olabilir', entry['not'])
        self.assertIn(entry['not'], y.human({'kartlar': [entry]}, 'isle'))
        self.assertNotIn(HEADINGS[-1], (vault / entry['ham']).read_text())

    def test_no_html_check_for_video_clipped_text_or_files(self):
        selections = [('text', {'text': WORDS}), ('file', {}),
                      ('video', {'url': 'https://www.youtube.com/watch?v=fixture'})]
        file = self.vault / 'fixture.txt'
        file.write_text(WORDS)
        selections[1][1]['files'] = [file]
        with patch.object(y, '_public_opener', side_effect=AssertionError('unexpected fetch')), \
                patch.object(y, 'via_youtube', return_value=(WORDS, 'defuddle', None)):
            for _, args in selections:
                card = y.find_card(self.vault, y.capture(self.vault, **args)['id'])
                self.assertTrue(y.extract(self.vault, card, self.found, info={})[0])
            clipped = y.find_card(self.vault, self.saved['id'])
            clipped['body'] = '## Icerik\n\n' + WORDS
            self.assertEqual(y.extract(self.vault, clipped, self.found, info={})[1], 'web-clipper')


if __name__ == '__main__':
    unittest.main()
