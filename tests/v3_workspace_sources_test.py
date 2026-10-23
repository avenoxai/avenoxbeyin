import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from v3_workspace_registry_test import ROOT,FIX
from beyin_v3_workspaces import Registry,LocalAuthority,WorkspaceError,envelope,render_document,content_hash
from beyin_v3_sources import SourceResolver,run_reader,default_readers,probe_capabilities
from beyin_v3_workspace_context import membership_hash


class WorkspaceSourcesTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve();self.vault=self.base/'vault';self.vault.mkdir()
        self.state=self.base/'state';self.material=self.base/'materials';self.material.mkdir()
        self.registry=Registry(self.vault,self.state)
        self.registry.compare_and_save(envelope('RegistryFormat',{'registry_id':'test-brain','layout':'per_workspace_json'}),None)
        self.registry.compare_and_save(envelope('SourceDescriptor',{'id':'materials','kind':'directory','label':'Materyaller'}),None)
        self.profile=copy.deepcopy(json.loads((FIX/'academic/golden.json').read_text(encoding='utf-8'))['documents'][0])
        self.profile['data']['source_ids']=['materials'];self.registry.compare_and_save(self.profile,None)
        binding=envelope('SourceBinding',{'source_id':'materials','machine_id':'machine','revision':1,'locator':str(self.material),
            'grants':[{'workspace_id':'academic','access':'read'}],'authority_ref':'user'})
        path=self.state/'workspace-bindings/materials.json';path.parent.mkdir();path.write_bytes(render_document(binding))
        self.auth=LocalAuthority(self.registry);self.auth.record_approval('user',machine_id='machine',permissions=[{
            'source_id':'materials','workspace_id':'academic','access':'read','locator':str(self.material)}],evidence='synthetic explicit user request')
        self.resolver=SourceResolver(self.registry)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS system /var alias')
    def test_system_alias_preserves_context_read_and_revocation(self):
        from v3_workspace_helpers import install_fixture
        alias, canonical = Path('/var'), Path('/private/var')
        if not alias.is_symlink() or alias.resolve() != canonical or not self.base.is_relative_to(canonical):
            self.skipTest('System temporary directory alias is not present')
        fixture = self.base/'alias-fixture'; fixture.mkdir()
        runtime, spec, request = install_fixture(fixture, 'academic')
        bindings, _ = runtime.registry.bindings()
        permissions = []
        for binding in bindings.values():
            binding['locator'] = str(alias / Path(binding['locator']).relative_to(canonical))
            binding['revision'] += 1
            path = runtime.registry.state/'workspace-bindings'/(binding['source_id']+'.json')
            path.write_bytes(render_document(envelope('SourceBinding', binding)))
            permissions.extend(dict(g, source_id=binding['source_id'], locator=binding['locator'])
                               for g in binding['grants'])
        runtime.authority.record_approval('academic-user', machine_id='fixture-machine',
            permissions=permissions, evidence='Explicit synthetic approval of the same system-alias roots')
        runtime.adopt(envelope('WorkspaceSpec', spec), membership_hash(runtime.registry)); runtime.activate()
        context = runtime.context(request)['data']
        record = next(r for r in context['records'] if r['id']=='academic-learning')
        expected = (runtime.registry.vault/record['source_ref']['relative_path']).read_bytes()
        self.assertEqual(runtime.resolver.read(record['source_ref'], 'academic', 'academic-user'), expected)
        runtime.authority.revoke('academic-user')
        with self.assertRaisesRegex(WorkspaceError, 'scope_denied'):
            runtime.resolver.read(record['source_ref'], 'academic', 'academic-user')

    def test_text_content_search_ref_and_original_source_preserved(self):
        path=self.material/'ders.txt';path.write_text('Karmaşıklık açıklaması — örnek girdi.\n', encoding='utf-8')
        before=path.read_bytes();result=self.resolver.extract('academic','materials','ders.txt','user')
        self.assertEqual(result['state'],'ready');self.assertIn('Karmaşıklık',result['chunks'][0]['text'])
        source=result['chunks'][0]['source_ref'];self.assertEqual(source['content_sha256'],content_hash(before))
        self.assertEqual(self.resolver.read(source,'academic','user'),before)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.resolver.files('academic','materials','user'),['ders.txt'])

    def test_cache_is_not_authority_and_stale_ref_is_rejected(self):
        path=self.material/'ders.txt';path.write_text('önceki metin', encoding='utf-8')
        source=self.resolver.extract('academic','materials','ders.txt','user')['chunks'][0]['source_ref']
        path.write_text('güncel metin', encoding='utf-8')
        with self.assertRaisesRegex(WorkspaceError,'stale_source'):self.resolver.read(source,'academic','user')
        self.assertEqual(self.resolver.extract('academic','materials','ders.txt','user')['chunks'][0]['text'],'güncel metin')
        self.auth.revoke('user')
        with self.assertRaisesRegex(WorkspaceError,'scope_denied'):self.resolver.extract('academic','materials','ders.txt','user')

    def test_scope_escape_and_external_write_are_denied(self):
        (self.material/'ders.txt').write_text('içerik', encoding='utf-8')
        for workspace,name in [('other','ders.txt'),('academic','../vault/other.txt')]:
            with self.assertRaises(WorkspaceError):self.resolver.extract(workspace,'materials',name,'user')
        with self.assertRaises(WorkspaceError):self.resolver.resolve('academic','materials','ders.txt','user',access='write_memory')
        (self.material/'link.txt').symlink_to(self.vault/'outside.txt')
        with self.assertRaises(WorkspaceError):self.resolver.extract('academic','materials','link.txt','user')

    def test_missing_pdf_dependency_and_invalid_docx_are_not_empty_success(self):
        for name in ['ders.pdf','ders.docx']:
            (self.material/name).write_bytes(b'synthetic payload')
            result=self.resolver.extract('academic','materials',name,'user')
            self.assertEqual(result['state'],'missing_dependency' if name.endswith('.pdf') else 'failed');self.assertEqual(result['chunks'],[])

    def test_transport_receives_bytes_not_path_and_locations_survive(self):
        (self.material/'ders.pdf').write_bytes(b'fake synthetic PDF')
        script=self.base/'reader.py';script.write_text('import json,sys,base64\nr=json.load(sys.stdin)\nassert "path" not in r\nassert base64.b64decode(r["bytes_base64"])==b"fake synthetic PDF"\njson.dump({"state":"ready","chunks":[{"text":"PDF içerik","fragment":{"kind":"pdf_page","page":2}}],"issues":[]},sys.stdout)\n', encoding='utf-8')
        resolver=SourceResolver(self.registry,readers={'.pdf':{'command':[sys.executable,str(script)],'id':'synthetic-reader',
            'version':'1','settings_sha256':content_hash(b'{}')}})
        result=resolver.extract('academic','materials','ders.pdf','user')
        self.assertEqual(result['state'],'ready');self.assertEqual(result['chunks'][0]['source_ref']['fragment']['page'],2)

    def test_changed_or_revoked_during_extraction_cannot_publish(self):
        path=self.material/'ders.pdf';path.write_bytes(b'synthetic')
        resolver=SourceResolver(self.registry,readers={'.pdf':{'command':['unused'],'id':'synthetic','version':'1','settings_sha256':content_hash(b'{}')}})
        def revoke(*args,**kwargs):
            self.auth.revoke('user');return {'state':'ready','chunks':[{'text':'must not publish','fragment':{'kind':'pdf_page','page':1}}],'issues':[]}
        with patch('beyin_v3_sources.run_reader',revoke),self.assertRaisesRegex(WorkspaceError,'scope_denied'):
            resolver.extract('academic','materials','ders.pdf','user')

    def test_language_data_change_invalidates_extractor_settings(self):
        data=self.base/'tessdata';data.mkdir();(data/'tur.traineddata').write_bytes(b'first tur');(data/'eng.traineddata').write_bytes(b'first eng')
        with patch('shutil.which',return_value=None),patch.dict('os.environ',{'TESSDATA_PREFIX':str(data)}):
            before=default_readers(self.vault)['.pdf']['settings_sha256']
            (data/'tur.traineddata').write_bytes(b'new tur')
            after=default_readers(self.vault)['.pdf']['settings_sha256']
        self.assertNotEqual(before,after)

    def test_capability_adapter_uses_one_d_probe_and_does_not_repeat_ocr(self):
        with patch('beyin_v3_parser_probe.document_capabilities',return_value=[{'name':'ocr_tr_en','state':'missing_dependency'}]) as probe,patch('beyin_v3_sources.extract_document_bytes') as extract:
            result=probe_capabilities(self.vault,ocr_probe=(b'deprecated',('ignored',)))
        self.assertEqual(result['ocr_tr_en'],'missing_dependency');probe.assert_called_once_with(self.vault);extract.assert_not_called()

    def test_reader_rejects_non_utf8_json_bytes(self):
        # JSON reader transport is UTF-8 even when a child emits valid UTF-16 JSON.
        code = 'import sys;sys.stdout.buffer.write(b"\\xff\\xfe" + "{}".encode("utf-16-le"))'
        with self.assertRaisesRegex(WorkspaceError, 'invalid_extractor_output'):
            run_reader([sys.executable, '-c', code], {}, timeout=2)

    def test_legacy_encoded_user_source_is_not_guessed_or_rewritten(self):
        # A user-owned file has no machine JSON contract; a cp1252 byte must
        # not be silently interpreted as a different character.
        path = self.material/'legacy.txt'
        original = 'legacy café text'.encode('cp1252')
        path.write_bytes(original)
        extracted = self.resolver.extract('academic','materials','legacy.txt','user')
        self.assertEqual(extracted['state'], 'partial')
        self.assertEqual(extracted['chunks'], [])
        self.assertIn('invalid_text_encoding', [i['code'] for i in extracted['issues']])
        self.assertEqual(path.read_bytes(), original)

    def test_reader_timeout_crash_and_output_limit(self):
        for code,limit in [('import time;time.sleep(2)',0.05),('raise RuntimeError()',2),('print("x"*5000000)',2)]:
            with self.subTest(code=code),self.assertRaises(WorkspaceError):run_reader([sys.executable,'-c',code],{},timeout=limit)


if __name__=='__main__':unittest.main()
