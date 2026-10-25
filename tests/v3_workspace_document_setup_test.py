"""System prerequisite steps are concrete, read-only and never OCR readiness."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'template/.claude/scripts'))
import beyin_v3_document_setup as setup
from beyin_v3_parser_assets import managed_files
from v3_package_helpers import ROOT, isolated_env, build_package, run_python
import zipfile


class DocumentSetupTest(unittest.TestCase):
    def test_platform_package_steps_are_bounded_without_execution(self):
        for system, distro, manager, package in [
            ('linux', {'ID': 'ubuntu'}, 'apt-get', 'tesseract-ocr-tur'),
            ('linux', {'ID': 'arch'}, 'pacman', 'tesseract-data-tur'),
            ('linux', {'ID': 'fedora'}, 'dnf', 'tesseract-langpack-tur'),
            ('darwin', {}, 'brew', 'tesseract-lang'),
            ('win32', {}, 'winget', 'UB-Mannheim.TesseractOCR')]:
            with self.subTest(system=system, distro=distro):
                with patch.object(setup.sys, 'platform', system), patch.object(setup.platform, 'freedesktop_os_release', return_value=distro), \
                        patch.object(setup.shutil, 'which', side_effect=lambda name: '/trusted/'+name), \
                        patch('subprocess.run', side_effect=AssertionError('package plan must not execute')):
                    actions, detail = setup._system_actions()
                self.assertTrue(actions)
                self.assertEqual(actions[0]['argv'][0], '/trusted/'+manager)
                self.assertIn(package, str(actions))
                self.assertNotIn('accept-source-agreements', str(actions))
        with patch.object(setup.sys, 'platform', 'linux'), patch.object(setup.platform, 'freedesktop_os_release', return_value={'ID': 'unknown'}):
            actions, detail = setup._system_actions()
        self.assertEqual(actions, [])
        self.assertIn('identify_distribution', detail)

    def test_missing_languages_does_not_reinstall_existing_binaries(self):
        with patch.object(setup.sys, 'platform', 'linux'), patch.object(setup.platform, 'freedesktop_os_release', return_value={'ID':'ubuntu'}), patch.object(setup.shutil, 'which', return_value='/trusted/apt-get'):
            actions, detail=setup._system_actions(missing_tools=(),missing_languages=True)
        self.assertEqual(actions[0]['argv'],['/trusted/apt-get','install','tesseract-ocr-eng','tesseract-ocr-tur'])

    def test_real_probe_missing_tools_preserves_assets_and_returns_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, raw in managed_files().items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
            before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.dict('os.environ', {'PATH': ''}), patch.object(setup.sys, 'platform', 'linux'), \
                    patch.object(setup.platform, 'freedesktop_os_release', return_value={'ID': 'debian'}), \
                    patch.object(setup, '_system_actions', return_value=([{'argv':['apt-get','install','poppler-utils','tesseract-ocr-tur'], 'system_authority_required':True}], 'repeat_probe')):
                report=setup.document_setup(root)
            self.assertEqual(report['status'], 'dependencies_required')
            self.assertEqual(report['workspace_health'], 'verification_pending')
            self.assertTrue(report['system_actions'][0]['action_sha256'])
            self.assertIn('no_portable_native_bundle', report['offline'])
            self.assertEqual(before,{str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_zip_installer_reports_missing_native_without_claiming_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);env=isolated_env(base/'home')
            package=base/'package'
            with zipfile.ZipFile(build_package(base/'release.zip','3.10.1',env)) as archive:
                archive.extractall(package)
            vault,state=base/'vault',base/'state';vault.mkdir()
            # Absolute Python still runs with no native tool on PATH.
            env['PATH']=''
            result=run_python(package/'scripts/install_v3.py',['--vault',vault,'--state',state,'--workspace-documents'],base,env)
            self.assertEqual(result.returncode,5,result.stderr.decode())
            report=json.loads(result.stdout)
            self.assertEqual(report['status'],'installed')
            caps={c['name']:c['state'] for c in report['workspace_documents']['capabilities']}
            self.assertEqual(caps,{'pdf_text':'ready','docx_text':'ready','ocr_tr_en':'missing_dependency'})
            self.assertEqual(report['workspace_documents']['readiness_scope'],'current_installation')
            self.assertTrue((vault/'beyin.py').exists())
            self.assertFalse((state/'workspace-authorities.json').exists())

    def test_installer_document_ready_and_plan_preserve_existing_vault(self):
        from v3_workspace_package_test import native_ocr_ready
        if not native_ocr_ready():
            self.skipTest('native TR/EN OCR tools/languages unavailable')
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);env=isolated_env(base/'home');env['PATH']=os.environ.get('PATH','')
            vault,state=base/'vault',base/'state';vault.mkdir()
            (vault/'identity.md').write_bytes(b'PRESERVE IDENTITY')
            result=run_python(ROOT/'scripts/install_v3.py',['--vault',vault,'--state',state,'--workspace-documents'],base,env)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            report=json.loads(result.stdout)['workspace_documents']
            self.assertEqual(report['status'],'ready')
            self.assertEqual(report['system_actions'],[])
            self.assertEqual(report['workspace_health'],'verification_pending')
            from v3_package_helpers import snapshot
            before=(snapshot(vault),snapshot(state))
            result=run_python(ROOT/'scripts/install_v3.py',['--vault',vault,'--state',state,'--plan','--workspace-documents'],base,env)
            self.assertEqual(result.returncode,0,result.stderr.decode())
            self.assertEqual(json.loads(result.stdout)['workspace_documents']['status'],'ready')
            self.assertEqual(before,(snapshot(vault),snapshot(state)))
            self.assertEqual((vault/'identity.md').read_bytes(),b'PRESERVE IDENTITY')

    def test_zip_update_crash_recover_and_rollback_of_new_setup_module(self):
        from v3_package_helpers import rewrite_zip
        from v3_update_test import load_updater
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);env=isolated_env(base/'home');vault,state=base/'vault',base/'state';vault.mkdir()
            old=build_package(base/'old.zip','3.10.1',env)
            name='template/.claude/scripts/beyin_v3_document_setup.py'
            def remove_setup(files):
                files.pop(name)
                manifest=json.loads(files['manifest.json']);manifest['files'].pop(name)
                files['manifest.json']=json.dumps(manifest).encode()
            rewrite_zip(old,old,remove_setup)
            package=base/'old-package'
            with zipfile.ZipFile(old) as archive:archive.extractall(package)
            installed=run_python(package/'scripts/install_v3.py',['--vault',vault,'--state',state],base,env)
            self.assertEqual(installed.returncode,0,installed.stderr.decode())
            target=vault/'.claude/scripts/beyin_v3_document_setup.py'
            self.assertFalse(target.exists())
            source=vault/'user-source.md';source.write_bytes(b'PRESERVE USER SOURCE')
            probe=vault/'.claude/parser-assets/ocr-probe-tr-en.pdf';probe_before=probe.read_bytes()
            release=build_package(base/'new.zip','3.10.2',env);updater=load_updater()
            def crash(phase,index=None):
                if phase=='after_replace' and target.exists():raise OSError('synthetic setup update interruption')
            with patch.object(updater,'transaction_hook',crash), self.assertRaises(OSError):
                updater.update(vault,state,release)
            self.assertEqual((vault/'.beyin-version').read_text().strip(),'3.10.1')
            self.assertTrue((state/'update-journal.json').exists())
            updater.recover(vault,state)
            self.assertTrue(target.exists())
            self.assertEqual((vault/'.beyin-version').read_text().strip(),'3.10.2')
            updater.rollback(vault,state)
            self.assertFalse(target.exists())
            self.assertEqual((vault/'.beyin-version').read_text().strip(),'3.10.1')
            self.assertEqual(probe.read_bytes(),probe_before)
            self.assertEqual(source.read_bytes(),b'PRESERVE USER SOURCE')
