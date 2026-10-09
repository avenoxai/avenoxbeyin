"""Native clickable wrappers around the single installed updater."""
import os
from pathlib import Path
import shlex
import sys


def _cmd_quote(value):
    value = str(value)
    if any(char in value for char in '\r\n\x00"'):
        raise ValueError('Unsupported launcher path')
    return '"' + value.replace('%', '%%') + '"'


def _desktop_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'


def plan_launchers(vault, state):
    """Return only native launcher bytes; the installer owns permissions/journaling."""
    vault = Path(vault).resolve()
    entry = vault / 'beyin.py'
    if any(char in str(vault) + sys.executable for char in '\r\n\x00'):
        raise ValueError('Unsupported launcher path')
    if os.name == 'nt':
        # ASCII filename/content avoids cmd.exe's legacy codepage for Unicode paths;
        # use PowerShell's UTF-16 encoded script to carry exact path characters.
        # The update this file starts rewrites it, and cmd.exe resumes at the old byte
        # offset: changing this layout ends the first update before pause and exit /b.
        import base64
        script = '& ' + ' '.join("'" + str(v).replace("'", "''") + "'" for v in [sys.executable, entry, 'update'])
        script += '; exit $LASTEXITCODE'
        encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
        system = os.environ.get('SYSTEMROOT') or os.environ.get('WINDIR') or r'C:\Windows'
        launcher = Path(system) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        body = ('@echo off\r\nsetlocal DisableDelayedExpansion\r\n' + _cmd_quote(launcher) +
                ' -NoProfile -NonInteractive -EncodedCommand ' + encoded + '\r\n' +
                'set "BEYIN_UPDATE_EXIT=%errorlevel%"\r\npause\r\nexit /b %BEYIN_UPDATE_EXIT%\r\n')
        return {'Beyni Guncelle.cmd': body.encode('ascii')}
    name = 'Beyni Güncelle.command' if sys.platform == 'darwin' else 'Beyni Güncelle.sh'
    shell = ('#!/bin/sh\n' + shlex.join([sys.executable, str(entry), 'update']) +
             '\nbeyin_update_exit=$?\nprintf "\\nKapatmak icin Enter tusuna basin. "\n' +
             'read -r beyin_update_reply || true\nexit "$beyin_update_exit"\n')
    files = {name: shell.encode('utf-8')}
    if sys.platform.startswith('linux'):
        desktop = ('[Desktop Entry]\nType=Application\nName=Beyni Güncelle\n' +
                   'Comment=Kurulu beyni resmi yeni surume guncelle\nTerminal=true\n' +
                   'Exec=/bin/sh ' + _desktop_quote(vault / name) + '\nIcon=system-software-update\n')
        files['Beyni Güncelle.desktop'] = desktop.encode('utf-8')
    return files
