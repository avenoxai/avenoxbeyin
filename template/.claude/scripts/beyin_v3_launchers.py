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
        body = ('@echo off\r\n'
                'chcp 65001 >nul\r\n'
                'setlocal DisableDelayedExpansion\r\n' +
                _cmd_quote(sys.executable) + ' "%~dp0beyin.py" update %*\r\n'
                'set "BEYIN_UPDATE_EXIT=%errorlevel%"\r\n'
                'pause\r\n'
                'exit /b %BEYIN_UPDATE_EXIT%\r\n')
        return {'Beyni Guncelle.cmd': body.encode('utf-8')}
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
