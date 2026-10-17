"""Explicit Windows desktop probe. It never writes a card or reads browser history.

Run from the repo root; focus your target during the countdown:
  py -3.12 tests/manual_windows_capture.py --backend wgc --delay 5 --output wgc.png
  py -3.12 tests/manual_windows_capture.py --backend printwindow --delay 5 --output printwindow.png
  py -3.12 tests/manual_windows_capture.py --backend auto --delay 5 --output auto.png
  py -3.12 tests/manual_windows_capture.py --url --delay 5
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'template/.claude/scripts'))
import beyin_v3_yakala as y


def main(argv=None):
    parser = argparse.ArgumentParser(description='Windows yakalama: WGC/PrintWindow ve kesin adres tanısı')
    parser.add_argument('--backend', choices=('wgc', 'printwindow', 'auto'), default='auto')
    parser.add_argument('--delay', type=float, default=5)
    parser.add_argument('--output', type=Path, default=Path('yakala-probe.png'))
    parser.add_argument('--url', action='store_true', help='Yalnız UIA adresini ölç; ekran görüntüsü alma')
    args = parser.parse_args(argv)
    if os.name != 'nt':
        parser.error('Bu komut gerçek Windows masaüstü gerektirir.')
    if not 0 <= args.delay <= 60:
        parser.error('--delay 0–60 saniye olmalı.')
    print('Hedef pencereye geçin. %.1f saniye sonra o pencere okunacak.' % args.delay, flush=True)
    time.sleep(args.delay)
    context = y.windows_context(read_url=False)
    hwnd = context.get('hwnd', 0)
    start = time.monotonic()
    if args.url:
        value = y.windows_browser_url(hwnd, context.get('uygulama'))
        result = {'app': context.get('uygulama'), 'hwnd': hwnd, 'url': value,
                  'elapsed_seconds': round(time.monotonic() - start, 4)}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0 if value else 2
    methods = ('wgc', 'printwindow') if args.backend == 'auto' else (args.backend,)
    attempts = []
    for backend in methods:
        result = y._windows_read(backend, {'hwnd': hwnd}, y.WGC_TIMEOUT if backend == 'wgc' else y.PRINTWINDOW_TIMEOUT)
        attempts.append({'backend': backend, 'error': result.get('error'), 'method': result.get('method')})
        data = result.get('png')
        info = y.png_info(data) if isinstance(data, bytes) else None
        if info and info[:2] == (result.get('width'), result.get('height')):
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(data)
            print(json.dumps({'method': result['method'], 'size': list(info[:2]), 'bytes': len(data),
                              'output': str(args.output.resolve()), 'hwnd': hwnd, 'app': context.get('uygulama'),
                              'elapsed_seconds': round(time.monotonic() - start, 4), 'attempts': attempts},
                             ensure_ascii=True, indent=2))
            return 0
    print(json.dumps({'error': 'Görüntü alınamadı; yanlış pencere/pano ile doldurulmadı.',
                      'hwnd': hwnd, 'attempts': attempts, 'elapsed_seconds': round(time.monotonic() - start, 4)},
                     ensure_ascii=True, indent=2))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
