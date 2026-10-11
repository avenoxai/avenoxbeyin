"""Temporary probe: what a Windows shortcut written through PowerShell keeps of a Unicode path. Not for merge."""
import base64
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOSTILE = "Örnek Beyin & Şifre'nin ’ \U0001f4e5 kasası"
PLAIN = 'Ornek Beyin'


def say(*parts):
    sys.stderr.write('PROBE-LNK| ' + ' '.join(str(p) for p in parts) + '\n')
    sys.stderr.flush()


def units(text):
    raw = text.encode('utf-16le', 'surrogatepass')
    return ','.join(str(int.from_bytes(raw[i:i + 2], 'little')) for i in range(0, len(raw), 2))


def quote(value):
    return "'" + re.sub("(['‘’‚‛])", r'\1\1', str(value)) + "'"


def ps(script):
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    done = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                          capture_output=True, timeout=120)
    return done.returncode, done.stdout.decode('utf-8', 'replace').strip(), done.stderr.decode('utf-8', 'replace').strip()


UNITS = "[string]::Join(',', ([int[]][char[]]%s))"


def wscript_write(link, target, arguments, hotkey=None):
    lines = ['$s = (New-Object -ComObject WScript.Shell).CreateShortcut(' + quote(link) + ')',
             '$s.TargetPath = ' + quote(target), '$s.Arguments = ' + quote(arguments), '$s.WindowStyle = 7']
    if hotkey:
        lines.append('$s.Hotkey = ' + quote(hotkey))
    lines.append('$s.Save()')
    return ps('\n'.join(lines))


def wscript_read(link):
    return ps('$s = (New-Object -ComObject WScript.Shell).CreateShortcut(' + quote(link) + ')\n' +
              "'args=' + " + UNITS % '$s.Arguments' + "\n'hotkey=' + $s.Hotkey\n'target=' + " + UNITS % '$s.TargetPath')


def shell_read(link):
    link = Path(link)
    return ps('$f = (New-Object -ComObject Shell.Application).NameSpace(' + quote(link.parent) + ').ParseName(' + quote(link.name) + ')\n'
              '$l = $f.GetLink\n' + "'args=' + " + UNITS % '$l.Arguments' + "\n'target=' + " + UNITS % '$l.Path' +
              "\n'hotkey=' + $l.Hotkey + ' show=' + $l.ShowCommand")


def shell_write(link, target, arguments):
    link = Path(link)
    return ps('[IO.File]::WriteAllBytes(' + quote(link) + ', [byte[]]@())\n'
              '$l = (New-Object -ComObject Shell.Application).NameSpace(' + quote(link.parent) + ').ParseName(' + quote(link.name) + ').GetLink\n'
              '$l.Path = ' + quote(target) + '\n$l.Arguments = ' + quote(arguments) + "\n$l.Description = 'probe'\n$l.ShowCommand = 7\n$l.Save()")


@unittest.skipUnless(os.name == 'nt', 'Windows only')
class ShortcutProbe(unittest.TestCase):
    def test_zz_shortcut_probe(self):
        root = Path(tempfile.mkdtemp(prefix='v3-lnk-probe-')).resolve()
        ascii_dir, turkish_dir, emoji_dir = root / 'plain', root / 'kısayollar', root / '\U0001f4e5 kutu'
        for folder in (ascii_dir, turkish_dir, emoji_dir):
            folder.mkdir()
        target = sys.executable
        hostile_args = 'dinle --vault "' + str(root / HOSTILE) + '" --tus ctrl+alt+shift+f9'
        plain_args = 'dinle --vault "' + str(root / PLAIN) + '" --tus ctrl+alt+shift+f9'
        say('expected hostile args units:', units(hostile_args))
        say('code pages:', ps("'ansi=' + [System.Text.Encoding]::Default.CodePage + ' oem=' + [Console]::OutputEncoding.CodePage + "
                              "' culture=' + (Get-Culture).Name + ' ps=' + $PSVersionTable.PSVersion"))
        for label, text in (('hostile', HOSTILE), ('kisayollar', 'kısayollar'), ('Ornek-with-O-umlaut', 'Örnek')):
            try:
                say('mbcs strict', label, '->', text.encode('mbcs', 'strict'))
            except Exception as error:
                say('mbcs strict', label, '-> ERROR', type(error).__name__, error)
        code, out, err = ps('$x = ' + quote(HOSTILE) + '\n' + UNITS % '$x')
        say('A literal round trip | rc', code, '| equal', out == units(HOSTILE), '| out', out, '| err', err[:300])
        runs = [('B1 wscript ascii link + plain args + CTRL+ALT+B', ascii_dir / 'b1.lnk', plain_args, 'CTRL+ALT+B'),
                ('B2 wscript ascii link + plain args + Alt+Ctrl+B', ascii_dir / 'b2.lnk', plain_args, 'Alt+Ctrl+B'),
                ('B3 wscript ascii link + plain args + no key', ascii_dir / 'b3.lnk', plain_args, None),
                ('B4 wscript ascii link + hostile args', ascii_dir / 'b4.lnk', hostile_args, None),
                ('C1 wscript dotless-i folder + plain args', turkish_dir / 'c1.lnk', plain_args, None),
                ('C2 wscript emoji folder + plain args', emoji_dir / 'c2.lnk', plain_args, None)]
        for label, link, arguments, hotkey in runs:
            code, out, err = wscript_write(link, target, arguments, hotkey)
            say(label, '| write rc', code, '| exists', link.exists(), '| err', err[:400].replace('\n', ' / '))
            if link.exists():
                code, out, err = wscript_read(link)
                say('    wscript read rc', code, '| args equal', ('args=' + units(arguments)) in out, '|', out.replace('\n', ' ; ')[:700], '| err', err[:200])
                code, out, err = shell_read(link)
                say('    shell   read rc', code, '| args equal', ('args=' + units(arguments)) in out, '|', out.replace('\n', ' ; ')[:700], '| err', err[:200])
        for label, link, arguments in (('E1 shell.application ascii link + hostile args', ascii_dir / 'e1.lnk', hostile_args),
                                       ('E2 shell.application dotless-i folder + hostile args', turkish_dir / 'e2.lnk', hostile_args),
                                       ('E3 shell.application emoji folder + hostile args', emoji_dir / 'e3.lnk', hostile_args)):
            code, out, err = shell_write(link, target, arguments)
            size = link.stat().st_size if link.exists() else None
            say(label, '| write rc', code, '| size', size, '| err', err[:400].replace('\n', ' / '))
            code, out, err = shell_read(link)
            say('    shell   read rc', code, '| args equal', ('args=' + units(arguments)) in out, '|', out.replace('\n', ' ; ')[:700], '| err', err[:200])
            code, out, err = wscript_read(link)
            say('    wscript read rc', code, '| args equal', ('args=' + units(arguments)) in out, '|', out.replace('\n', ' ; ')[:300], '| err', err[:200])
