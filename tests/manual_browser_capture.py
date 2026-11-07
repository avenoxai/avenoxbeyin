#!/usr/bin/env python3
"""Explicit live browser probe; does not save cards, launch browsers or read profiles.

Run from the repository, then switch to the target browser during --delay.
URLs stay out of stdout unless --show-urls is supplied. Use public sample URLs for
--expected-page/--expected-hover: command-line arguments can be visible to local tools.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y


def read_browser():
    if sys.platform == 'win32':
        return y.windows_enrich(y.windows_context(read_url=False))
    if sys.platform == 'darwin':
        front = y.mac_frontmost()
        return y.mac_context(front) if front.get('bundle') in y.MAC_AX_BROWSERS else {}
    if sys.platform.startswith('linux'):
        return y.linux_browser_context()
    return {}


def record(context, elapsed, expected_page=None, expected_hover=None,
           expect_empty=False, show_urls=False):
    page, hover = context.get('url'), context.get('url_imlec')
    result = {'elapsed_ms': round(elapsed * 1000, 3),
              'application': context.get('uygulama'), 'method': context.get('url_yontem'),
              'page_present': bool(page), 'hover_present': bool(hover)}
    if expected_page is not None:
        result['page_matches'] = page == expected_page
    if expected_hover is not None:
        result['hover_matches'] = hover == expected_hover
    if expect_empty:
        result['empty_matches'] = not (page or hover)
    if show_urls:
        result.update(page=page, hover=hover)
    return result


def summarize(records):
    elapsed = sorted(r['elapsed_ms'] for r in records)
    checked = [r for r in records if any(k.endswith('_matches') for k in r)]
    return {'samples': len(records),
            'with_page': sum(r['page_present'] for r in records),
            'with_hover': sum(r['hover_present'] for r in records),
            'p50_ms': round(statistics.median(elapsed), 3) if elapsed else None,
            'p95_ms': elapsed[max(0, math.ceil(len(elapsed) * 0.95) - 1)] if elapsed else None,
            'checked_samples': len(checked),
            'mismatches': sum(not all(value for key, value in r.items() if key.endswith('_matches'))
                              for r in checked)}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Tarayici sayfa ve imlec baglantisini olc; kart yazma.')
    parser.add_argument('--delay', type=float, default=5, help='Ilk okumadan once 0-60 saniye bekle.')
    parser.add_argument('--repeat', type=int, default=1, help='1-20 okuma yap.')
    parser.add_argument('--interval', type=float, default=1, help='Okumalar arasinda 0-10 saniye bekle.')
    parser.add_argument('--expected-page', help='Beklenen tam genel HTTP(S) sayfa adresi.')
    parser.add_argument('--expected-hover', help='Beklenen tam genel HTTP(S) imlec adresi.')
    parser.add_argument('--expect-empty', action='store_true', help='Her iki adresin de bos olmasini bekle.')
    parser.add_argument('--show-urls', action='store_true', help='Okunan adresleri JSON ciktisina ekle.')
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 60:
        parser.error('--delay 0-60 saniye olmali.')
    if not math.isfinite(args.interval) or not 0 <= args.interval <= 10 or not 1 <= args.repeat <= 20:
        parser.error('--repeat 1-20, --interval 0-10 olmali.')
    if args.expect_empty and (args.expected_page or args.expected_hover):
        parser.error('--expect-empty ile beklenen URL birlikte kullanilmaz.')
    for url in (args.expected_page, args.expected_hover):
        if url is not None and not y._opera_url(url):
            parser.error('Beklenen adres tam ve genel HTTP(S) adresi olmali.')
    print('Hedef tarayiciya gecin; ilk okuma %.1f saniye sonra.' % args.delay, file=sys.stderr)
    time.sleep(args.delay)
    records = []
    for index in range(args.repeat):
        if index:
            time.sleep(args.interval)
        started = time.monotonic()
        error = None
        try:
            context = read_browser()
        except Exception as exc:
            context, error = {}, type(exc).__name__
        item = record(context, time.monotonic() - started, args.expected_page, args.expected_hover,
                      args.expect_empty, args.show_urls)
        if error:
            item['error'] = error  # exception messages can include private document text
        records.append(item)
    result = {'at_utc': datetime.now(timezone.utc).isoformat(), 'platform': sys.platform,
              'os_release': platform.release(), 'machine': platform.machine(),
              'python': platform.python_version(), 'summary': summarize(records), 'records': records}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 1 if result['summary']['mismatches'] or any('error' in r for r in records) else 0


if __name__ == '__main__':
    raise SystemExit(main())
