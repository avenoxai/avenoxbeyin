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
from v3_workspace_test_io import load_utf8_json, run_json_cli

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


class WorkspacePackageTest(unittest.TestCase):
    def test_installed_zip_real_parser_probe_daily_context_and_canonical_update(self):
        # No checkout, test helpers or third-party site-packages in the worker.
        from v3_workspace_documents_test import unicode_pdf, docx_bytes
        fixture_root = ROOT / 'tests/fixtures/v3/workspaces/v0.1.0/academic'
        with tempfile.TemporaryDirectory(prefix='beyin-package-i1-') as tmp:
            base = Path(tmp); env = isolated_env(base / 'home')
            package = base / 'package'
            with zipfile.ZipFile(build_package(base / 'release.zip', '3.10.1', env)) as archive:
                archive.extractall(package)
            vault, state = base / 'Vault Türkçe', base / 'State'
            shutil.copytree(fixture_root / 'files/vault', vault)
            (vault / 'Core.md').write_bytes(b'PRESERVE SYNTHETIC IDENTITY')
            installed = run_python(package / 'scripts/install_v3.py', ['--vault', vault, '--state', state], base, env)
            self.assertEqual(installed.returncode, 0, installed.stderr.decode(errors='replace'))
            documents = {d['type']: copy.deepcopy(d) for d in load_utf8_json(fixture_root / 'input.json')['documents']}
            spec = documents['WorkspaceSpec']; permissions = []
            for binding, suffix in zip(spec['data']['binding_requests'], ('vault', 'source-a', 'source-b')):
                locator = vault if suffix == 'vault' else base / suffix
                if suffix != 'vault': shutil.copytree(fixture_root / 'files' / suffix, locator)
                binding['locator'] = str(locator)
                permissions.extend(dict(g, source_id=binding['source_id'], locator=str(locator)) for g in binding['grants'])
            pdf = base / 'source-a/okuma.pdf'
            pdf.write_bytes(unicode_pdf('Türkçe quartz öğrenim PDF kaynağı'))
            docx = base / 'source-a/okuma.docx'
            docx.write_bytes(docx_bytes('<w:p><w:r><w:t>Türkçe quartz DOCX öğrenim</w:t></w:r></w:p>'
                '<w:tbl><w:tr><w:tc><w:p><w:r><w:t>quartz Sonuç 82</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'))
            original_sources = {p: p.read_bytes() for p in (pdf, docx)}
            intent = next(d for d in load_utf8_json(fixture_root / 'golden.json')['documents'] if d['type'] == 'MemoryWriteIntent')
            request = documents['RequestContext']; request['data']['query'] += ' quartz'
            inputs = base / 'inputs.json'; inputs.write_text(json.dumps(dict(spec=spec, request=request, intent=intent,
                permissions=permissions), ensure_ascii=False), encoding='utf-8')
            worker = '''
import copy,json,sys
from pathlib import Path
vault,state=map(Path,sys.argv[1:3]);sys.path.insert(0,str(vault/'.claude/scripts'))
from beyin_v3_parser_probe import document_capabilities
from beyin_v3_builder import plan,provisioner
from beyin_v3_workspaces import Registry,LocalAuthority
from beyin_v3_sync import SyncEngine
from beyin_v3_workspace_context import membership_hash, WorkspaceRuntime
from beyin_v3_workspace_memory import MemoryIntentService
value=json.loads(Path(sys.argv[3]).read_bytes());spec=value['spec'];request=value['request']
capabilities={c['name']:c['state'] for c in document_capabilities(vault)}
assert all(capabilities[k]=='ready' for k in ('pdf_text','docx_text')),capabilities
assert 'ocr_tr_en' in capabilities
# This scenario requests document-backed daily work, not complete workflow dispatch.
spec['data']['required_capabilities']=['pdf_text','docx_text']
document,preview=plan(vault,state,spec,registry_id='fixture',capabilities=capabilities)
auth=LocalAuthority(Registry(vault,state))
auth.record_approval('fixture-user-request',machine_id='fixture-machine',permissions=value['permissions'],evidence='Synthetic explicit source grant')
auth.approve_plan(document,evidence='Synthetic exact plan preview consent')
assert provisioner(vault,state).apply(document,'installed-i1')['data']['workspace_health']=='verification_pending'
SyncEngine(vault,state).sync();runtime=WorkspaceRuntime(vault,state);runtime.adopt(spec, membership_hash(runtime.registry));runtime.activate()
context=runtime.context(request)['data'];records={r['id']:r for r in context['records']}
assert {'academic-topic','academic-task','academic-result','academic-learning'}<=records.keys()
assert any(r['source_ref']['fragment']=={'kind':'pdf_page','page':1} and 'Türkçe quartz öğrenim PDF' in r['text'] for r in records.values())
assert any(r['source_ref']['fragment']=={'kind':'docx_block','block':2,'table':1,'row':1,'cell':1} and 'quartz Sonuç 82' in r['text'] for r in records.values())
intent=value['intent'];intent['data'].update(context_token=context['context_token'],binding_snapshot_sha256=context['binding_snapshot_sha256'])
for change in intent['data']['changes']:
    record=records[change['target']['record_id']]
    change['target'].update(expected_revision=record['revision'],expected_source_sha256=record['source_ref']['content_sha256'])
    change['evidence_refs']=[records['academic-result']['source_ref']]
    for relation in change['relation_changes']:relation['evidence_refs']=list(change['evidence_refs'])
before=set(vault.rglob('*.md'));service=MemoryIntentService(runtime)
assert all(p['state']=='verified' for p in service.apply(intent)['data']['parts'])
assert service.apply(intent)['data']['operation_status']=='replayed'
assert set(vault.rglob('*.md'))==before
(vault/'installed-request.json').write_text(json.dumps(request),encoding='utf-8')
assert 'pypdf' not in sys.modules and not any('site-packages' in p for p in sys.path)
'''
            result = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', worker, str(vault), str(state), str(inputs)],
                cwd=vault, env=env, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            fresh = subprocess.run([sys.executable, '-I', '-S', '-B', str(vault / 'beyin.py'),
                'workspace-context', '--file', str(vault / 'installed-request.json')], cwd=vault, env=env,
                capture_output=True, timeout=30)
            self.assertEqual(fresh.returncode, 0, fresh.stderr.decode(errors='replace'))
            records = json.loads(fresh.stdout)['data']['records']
            learning = next(r for r in records if r['id'] == 'academic-learning')
            self.assertEqual(learning['revision'], 2); self.assertIn('Yeni sonuç', learning['text'])
            self.assertEqual((vault / 'Core.md').read_bytes(), b'PRESERVE SYNTHETIC IDENTITY')
            for path, raw in original_sources.items(): self.assertEqual(path.read_bytes(), raw)

    def test_zip_only_install_reinstall_preserves_identity_and_planner(self):
        with tempfile.TemporaryDirectory(prefix='beyin-workspace-') as directory:
            root = Path(directory)
            env = isolated_env(root / 'home')
            archive = build_package(root / 'release.zip', '3.10.1', env)
            package = root / 'Çıkarılan paket'
            with zipfile.ZipFile(archive) as release:
                manifest = json.loads(release.read('manifest.json'))
                name = 'template/.claude/scripts/beyin_v3_workspace_plan.py'
                self.assertEqual(hashlib.sha256(release.read(name)).hexdigest(), manifest['files'][name])
                release.extractall(package)
            self.assertFalse((package / '.git').exists())
            vault, state = root / 'Beyin Türkçe boşluk', root / 'Yerel durum'
            vault.mkdir()
            identity = vault / 'Core.md'
            identity.write_text('SENTETİK KULLANICI KİMLİĞİ\n', encoding='utf-8')
            before = identity.read_bytes()
            for attempt in range(2):
                installed = run_python(package / 'scripts/install_v3.py',
                    ['--vault', vault, '--state', state], root, env)
                self.assertEqual(installed.returncode, 0, installed.stderr.decode(errors='replace'))
                self.assertEqual(identity.read_bytes(), before)
                script = """
import sys
sys.path.insert(0, sys.argv[1])
from beyin_v3_workspace_plan import file_deltas, preview_files
result = preview_files(file_deltas({}, {('vault', 'notes/Örnek.md'): b'example'}))
assert result['write_set_hash'] and not result['truncated']
"""
                check = subprocess.run([sys.executable, '-I', '-c', script,
                    str(vault / '.claude/scripts')], cwd=root, env=env, capture_output=True, timeout=30)
                self.assertEqual(check.returncode, 0, check.stderr.decode(errors='replace'))
            skill = (vault / '.agents/skills/beyin/SKILL.md').read_text(encoding='utf-8')
            self.assertIn('Alan kurma ve geliştirme', skill)
            self.assertIn('needs_client_action', skill)
            lock = json.loads((vault / '.claude/parser-assets/pypdf.lock.json').read_bytes())
            wheel = vault / '.claude/parser-assets' / lock['filename']
            self.assertEqual(hashlib.sha256(wheel.read_bytes()).hexdigest(), lock['sha256'])
            self.assertFalse((vault / '.claude/scripts/beyin_v3_parser_assets.py').exists())
            self.assertIn('Redistribution', (wheel.parent / 'pypdf.LICENSE.txt').read_text())
            source = root / 'original.pdf'; source.write_bytes(text_pdf())
            before_source = source.read_bytes()
            # This explicit reader process is the third-party boundary. No global
            # site-packages, pip, venv, native library or core import is used.
            reader = '''
import hashlib,sys
from pathlib import Path
wheel=Path(sys.argv[1])
assert hashlib.sha256(wheel.read_bytes()).hexdigest()==sys.argv[3]
sys.path.insert(0,str(wheel))
from pypdf import PdfReader
assert 'course source quartz' in PdfReader(sys.argv[2]).pages[0].extract_text()
assert not any('site-packages' in entry for entry in sys.path)
assert 'docx' not in sys.modules and 'lxml' not in sys.modules
'''
            read = subprocess.run([sys.executable, '-I', '-S', '-B', '-c', reader,
                str(wheel), str(source), lock['sha256']], cwd=root, env=env, capture_output=True, timeout=30)
            self.assertEqual(read.returncode, 0, read.stderr.decode(errors='replace'))
            self.assertEqual(source.read_bytes(), before_source)
            # Packaged dependency works; C2 retrieval and a live client remain untested.

    def test_installed_zip_daily_academic_and_ceramics_holdout(self):
        with tempfile.TemporaryDirectory(prefix='beyin-installed-c-') as directory:
            root=Path(directory);env=isolated_env(root/'home')
            archive=build_package(root/'release.zip','3.10.1',env)
            package=root/'Paket';vault=root/'Beyin Türkçe boşluk';state=root/'Yerel durum';vault.mkdir()
            with zipfile.ZipFile(archive) as release:release.extractall(package)
            installed=run_python(package/'scripts/install_v3.py',['--vault',vault,'--state',state],root,env)
            self.assertEqual(installed.returncode,0,installed.stderr.decode(errors='replace'))
            # Child -I resolves product modules only from the installed vault;
            # fixture directory contains data, never a source-module search path.
            run=run_json_cli([sys.executable,'-I',str(ROOT/'tests/workspace_installed_scenario.py'),
                str(vault),str(state),str(ROOT/'tests/fixtures/v3/workspaces/v0.1.0'),str(package/'scripts/beyin_v3.py'),
                'require-ocr' if native_ocr_ready() else 'text-only'],
                cwd=root,env=env,capture_output=True,timeout=90)
            self.assertEqual(run.returncode,0,run.stderr)
            observed=json.loads(run.stdout)
            self.assertEqual([r['case'] for r in observed['scenarios']],['academic','ceramics'])
            self.assertTrue(all(r['fresh_process_revision']==2 for r in observed['scenarios']))

    def test_transport_is_reproducible_and_corruption_fails_closed(self):
        spec = importlib.util.spec_from_file_location('bundled_parser_asset',
            ROOT / 'template/.claude/scripts/beyin_v3_parser_assets.py')
        assets = importlib.util.module_from_spec(spec); spec.loader.exec_module(assets)
        generator_spec = importlib.util.spec_from_file_location('vendor_parser_asset',
            ROOT / 'scripts/vendor_workspace_pypdf.py')
        generator = importlib.util.module_from_spec(generator_spec); generator_spec.loader.exec_module(generator)
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / assets.NAME
            wheel.write_bytes(assets.managed_files()['.claude/parser-assets/' + assets.NAME])
            self.assertEqual(generator.generate(wheel), Path(spec.origin).read_text(encoding='utf-8'))
            assets.PAYLOAD = b'corrupt'
            with self.assertRaisesRegex(ValueError, 'integrity'):
                assets.managed_files()
            wheel.write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'checksum'):
                generator.generate(wheel)


if __name__ == '__main__':
    unittest.main()
