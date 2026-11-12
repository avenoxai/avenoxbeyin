#!/usr/bin/env python3
"""Reproduce fictional Workspace examples offline in a temporary installed vault.

No user vault, home directory, network, or model is consulted. The optional
--output directory receives only selected synthetic Markdown and a small report.
"""
import argparse
import json
import os
from pathlib import Path
import posixpath
import re
import subprocess
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import quote, unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from v3_package_helpers import isolated_env, install


class Example:
    def __init__(self, base):
        self.vault = base / 'vault'
        self.state = base / 'state'
        self.vault.mkdir()
        self.env = isolated_env(base / 'isolated-home')
        installed = install(self.vault, self.state, self.env)
        if installed.returncode:
            raise RuntimeError(installed.stderr.decode('utf-8', errors='replace'))
        self.request = base / 'request.json'
        self.outputs = {}
        self.checks = []

    def cli(self, command, payload=None, *args):
        if payload is not None:
            self.request.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            args = ('--file', str(self.request), *args)
        result = subprocess.run([sys.executable, str(self.vault / 'beyin.py'),
            '--state', str(self.state), '--json', command, *args], env=self.env,
            capture_output=True, text=True, encoding='utf-8', timeout=60)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return json.loads(result.stdout)

    def read(self, source):
        return self.cli('note-read', None, source)

    def create(self, source, text, project, **metadata):
        return self.cli('note-create', dict(source=source, text=text.rstrip(), metadata=dict(
            kind='note', project=project, visibility='internal', **metadata)))

    def edit(self, source, old, new):
        snapshot = self.read(source)
        assert snapshot['content'].count(old) == 1, (source, old)
        return self.cli('note-edit', dict(source=source, expected_sha256=snapshot['sha256'],
            edits=[dict(old=old, new=new)]))

    def editor(self, source, old, new):
        """An ordinary external editor save, deliberately before the next sync."""
        path = self.vault / source
        content = path.read_bytes().decode('utf-8')
        assert content.count(old) == 1, (source, old)
        path.write_bytes(content.replace(old, new).encode('utf-8'))

    def save(self, name, source):
        content = self.read(source)['content']
        body = content.split('\n---\n', 1)[1] if content.startswith('---\n') else content
        body = re.sub(r'<!--.*?-->', '', body, flags=re.S)
        # Snapshots live beside the exported vault, rather than inside its notes.
        body = re.sub(r'(?<=\]\()([^)]+)(?=\))', lambda match: quote('vault/' +
            posixpath.normpath(posixpath.join(posixpath.dirname(source), unquote(match[0]))), safe='/'), body)
        self.outputs[name] = body.rstrip() + '\n'

    def replay(self):
        paths = list(self.vault.rglob('*.md'))
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.cli('sync')
        assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}

    def verify_links(self):
        for path in (self.vault / 'notes').rglob('*.md'):
            for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
                assert (path.parent / unquote(target)).resolve().is_file(), (path.name, target)


def career(example):
    home = 'notes/Kariyer/Ana Sayfa.md'
    evening = 'notes/Kariyer/Aksam.md'
    daytime = 'notes/Kariyer/Gunduz.md'
    invitation = example.vault / 'notes/Kariyer/Davet.txt'
    invitation.parent.mkdir(parents=True)
    invitation.write_text('Tamamen uydurma Mavi Pusula daveti.\n', encoding='utf-8')
    for source, name in ((evening, 'Akşam'), (daytime, 'Gündüz')):
        example.create(source, '# Mavi Pusula — ' + name + '\n\n'
            'Özgün davet: [Davet](Davet.txt)\n', 'kariyer', id='ornek-' + name,
            title='Mavi Pusula — ' + name, facts={'gorusme': '6 Nisan 2099'})
    request = dict(source=home, expected_sha256=None, title='Kariyerim', project='kariyer',
        selections=[dict(source=source, fields=[dict(field='facts.gorusme',
            label='Görüşme tarihi', editable=True)]) for source in (evening, daytime)])
    assert example.cli('homepage-create', request)['status'] == 'written'
    block = example.read(home)['content'].split('### [Mavi Pusula — Gündüz]', 1)[0]
    example.edit(home, block, block.replace('**Kişisel notum:**',
        '**Kişisel notum:** yol süresini ve adresi sor'))
    example.save('career-created.md', home)
    untouched = (example.vault / daytime).read_bytes()
    original_invitation = invitation.read_bytes()
    example.edit(evening, '6 Nisan 2099', '7 Nisan 2099')
    assert '-->7 Nisan 2099<!--' in example.read(home)['content']
    example.save('career-from-source.md', home)
    # Target the complete first cell: the same date also occurs on the other row.
    content = example.read(home)['content']
    cell = re.search(r'<!-- beyin-field:.*?-->7 Nisan 2099<!-- /beyin-field -->', content)[0]
    example.edit(home, cell, cell.replace('-->7 Nisan 2099<!--', '-->9 Nisan 2099<!--'))
    assert '9 Nisan 2099' in example.read(evening)['content']
    assert untouched == (example.vault / daytime).read_bytes()
    assert original_invitation == invitation.read_bytes()
    example.save('career-from-homepage.md', home)
    example.replay()
    example.checks += ['public homepage-create without prepared cells',
        'source edit refreshes the correct row', 'homepage note-edit writes the correct source',
        'other record and original invitation preserved', 'byte and mtime preserving replay']

    # Deliberately fail the installed synchronizer after its real source write.
    # The failure hook belongs to this demonstration, never to product runtime.
    sys.path.insert(0, str(example.vault / '.claude/scripts'))
    import beyin_v3_homepages as homepages
    from beyin_v3_sync import SyncEngine
    example.editor(home, '-->9 Nisan 2099<!--', '-->11 Nisan 2099<!--')
    original_edit = homepages._edit

    def interrupt(engine, snapshot, old, new):
        if snapshot['source'] == home:
            raise RuntimeError('synthetic interruption after source write')
        return original_edit(engine, snapshot, old, new)

    with patch.dict('os.environ', example.env, clear=True):
        with patch.object(homepages, '_edit', interrupt):
            try:
                SyncEngine(example.vault, example.state).sync()
            except RuntimeError as error:
                assert str(error) == 'synthetic interruption after source write'
            else:
                raise AssertionError('interruption was not reached')
    source_bytes = (example.vault / evening).read_bytes()
    assert '11 Nisan 2099' in source_bytes.decode('utf-8')
    example.editor(home, 'yol süresini ve adresi sor', 'adresi ve giriş kapısını sor')
    source_path = example.vault / evening
    # A fixed old timestamp detects a same-byte rewrite even on coarse filesystems.
    os.utime(source_path, ns=(1_600_000_000_000_000_000,) * 2)
    source_mtime = source_path.stat().st_mtime_ns
    example.cli('sync')
    assert source_bytes == source_path.read_bytes()
    assert source_mtime == source_path.stat().st_mtime_ns
    assert 'adresi ve giriş kapısını sor' in example.read(home)['content']
    example.save('career-recovered.md', home)
    example.replay()
    example.checks += ['installed-runtime interruption after source write',
        'latest personal note preserved on recovery without source rewrite']

    # Two external editor saves diverge on one field; the other row still advances.
    example.editor(evening, '11 Nisan 2099', '12 Nisan 2099')
    example.editor(home, '-->11 Nisan 2099<!--', '-->13 Nisan 2099<!--')
    example.editor(daytime, '6 Nisan 2099', '8 Nisan 2099')
    conflict = example.cli('sync')['workspace_homepages']['conflicts']
    assert len(conflict) == 1 and conflict[0]['field'] == 'facts.gorusme'
    assert conflict[0]['source_value'] == '12 Nisan 2099'
    assert conflict[0]['homepage_value'] == '13 Nisan 2099'
    assert '-->8 Nisan 2099<!--' in example.read(home)['content']
    example.save('career-conflict.md', home)
    example.editor(home, '-->13 Nisan 2099<!--', '-->12 Nisan 2099<!--')
    example.cli('sync')
    example.replay()
    before = (example.vault / home).stat().st_mtime_ns
    assert example.cli('homepage-create', request)['status'] == 'unchanged'
    assert before == (example.vault / home).stat().st_mtime_ns
    example.save('career-resolved.md', home)
    example.checks += ['field-local conflict while another row advances',
        'explicit source-value choice converges', 'homepage-create replay preserves user layout']


def lessons(example):
    # Every name, file and sentence is authored here independently of user data.
    courses = [('Bulut Bahçeleri', 'Bulut-Bahceleri'),
               ('Ses Haritaları', 'Ses-Haritalari'), ('Işık Atölyesi', 'Isik-Atolyesi')]
    home = 'notes/Dersler/Ana Sayfa.md'
    rows = []
    originals = {}
    for title, slug in courses:
        directory = example.vault / 'sources/Ornek-Dersler' / slug
        directory.mkdir(parents=True)
        for filename, text in [('Kaynak.txt', 'Uydurma ders kaynağı.\n'),
                               ('Calisma-Dosyasi.txt', 'Uydurma ödev/alıştırma dosyası.\n')]:
            path = directory / filename
            path.write_text(text, encoding='utf-8')
            originals[path] = path.read_bytes()
        note = 'notes/Dersler/' + slug + '.md'
        example.create(note, '# ' + title + '\n\n## Kaynaklar\n\n'
            '[Ders kaynağı](../../sources/Ornek-Dersler/' + slug + '/Kaynak.txt)\n\n'
            '## Ödev dosyaları\n\n[Çalışma dosyası](../../sources/Ornek-Dersler/' + slug +
            '/Calisma-Dosyasi.txt)\n\nBu dosyanın varlığı açık görev veya teslim yükümlülüğü göstermez.\n\n'
            '## Teslim ve plan\n\nDoğrulanmış teslim tarihi yok. Çalışma sırası belirtilmedi.\n\n'
            '## Kişisel notum\n\n', 'dersler', title=title)
        rows.append('| [' + title + '](' + slug + '.md) | [' + 'Kaynak](../../sources/Ornek-Dersler/' +
            slug + '/Kaynak.txt) | [Ödev dosyası](../../sources/Ornek-Dersler/' + slug +
            '/Calisma-Dosyasi.txt) | Belirtilmedi |')
    example.create(home, '# Derslerim\n\nKaynaklara, ödev dosyalarına ve ders notlarıma buradan ulaşırım.\n\n'
        '## Dersler ve dosyalar\n\n| Ders notu | Kaynak | Ödev dosyası | Doğrulanmış teslim |\n'
        '| --- | --- | --- | --- |\n' + '\n'.join(rows) + '\n\n'
        'Ödev dosyası bağlantısı açık görev anlamına gelmez.\n\n'
        '## Teslimler\n\nHenüz doğrulanmış bir teslim bilgisi verilmedi.\n\n'
        '## Çalışma planım\n\nHenüz bir ders veya çalışma sırası seçilmedi.\n\n'
        '## Kişisel notum\n\nKaynak bağlantıları burada toplu dursun.\n', 'dersler', title='Derslerim')
    example.edit(home, 'Kaynak bağlantıları burada toplu dursun.',
        'Kaynak bağlantıları burada toplu dursun; teslim bilgisi gelince ilgili ders notuna ekle.')
    example.cli('sync')
    example.replay()
    assert originals == {p: p.read_bytes() for p in originals}
    for path in (example.vault / 'notes/Dersler').glob('*.md'):
        assert '"facts"' not in path.read_text(encoding='utf-8')
    assert not list((example.vault / 'tasks').glob('*.md'))
    example.save('lessons-homepage.md', home)
    example.save('lessons-course.md', 'notes/Dersler/Bulut-Bahceleri.md')
    example.checks += ['general lessons navigation through note-create/note-edit',
        'no invented facts, tasks, deadlines, progress, or study choice',
        'latest lessons note, originals, and no-write replay preserved']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='write readable snapshots and report here')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='workspace-fiction-') as temporary:
        example = Example(Path(temporary))
        career(example)
        lessons(example)
        example.verify_links()
        report = dict(status='passed', checks=example.checks + ['all original Markdown links resolve'],
            data='independently authored fiction', real_obsidian_gui=False,
            conversational_agent_e2e=False)
        if args.output:
            args.output.mkdir(parents=True, exist_ok=True)
            for name, text in example.outputs.items():
                (args.output / name).write_text(text, encoding='utf-8')
            # Export only authored fictional data, never installed runtime,
            # companion starters, state, logs, or a user's existing files.
            for directory in ('notes/Kariyer', 'notes/Dersler', 'sources/Ornek-Dersler'):
                for path in (example.vault / directory).rglob('*'):
                    if path.is_file():
                        target = args.output / 'vault' / path.relative_to(example.vault)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(path.read_bytes())
            exported = list((args.output / 'vault').rglob('*.md')) + [args.output / name for name in example.outputs]
            for path in exported:
                for target in re.findall(r'\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
                    assert (path.parent / unquote(target)).resolve().is_file(), (path.name, target)
            report['exported_links'] = 'passed'
            (args.output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
