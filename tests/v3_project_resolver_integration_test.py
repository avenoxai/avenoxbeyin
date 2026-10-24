"""Project identity signals never create workspace authority."""
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
from v3_package_helpers import inherited_env
from v3_workspace_helpers import install_fixture
from beyin_v3_projects import resolve_rules_project, resolve_project_from_query
from beyin_v3_workspace_context import bound_project_for_cwd, request_scope
from beyin_v3_sync import SyncEngine
from beyin_v3_workspaces import WorkspaceError
from beyin_v3_bridge import origin
from beyin_v3 import render_context

class ResolverIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve()
        self.runtime,self.spec,self.request=install_fixture(self.base,'academic')
        self.vault=self.runtime.registry.vault;self.state=self.runtime.registry.state

    def test_explicit_nested_mapping_and_external_binding(self):
        (self.vault/'projects/outer/inner').mkdir(parents=True)
        (self.vault/'.beyin-projects.json').write_text(json.dumps({'folders':{'projects/outer':'outer','projects/outer/inner':'inner'}}),encoding='utf-8')
        cwd=self.vault/'projects/outer/inner'
        self.assertEqual(resolve_rules_project(self.vault,cwd),'inner')
        self.assertEqual(resolve_rules_project(self.vault,cwd,explicit='academic'),'academic')
        external=self.base/'academic-source-a'
        self.assertEqual(bound_project_for_cwd(self.vault,self.state,external),'academic')
        self.assertEqual(origin({'cwd':str(external)},'claude',self.vault,self.state)['project'],'academic')
        same_name=self.base/'elsewhere/academic';same_name.mkdir(parents=True)
        self.assertIsNone(bound_project_for_cwd(self.vault,self.state,same_name))
        self.assertIsNone(resolve_rules_project(self.vault,same_name,state=self.state))

    def test_query_alias_collision_and_unicode_boundary(self):
        for name in ('alpha','beta','ödev'):(self.vault/'projects'/name).mkdir(parents=True)
        (self.vault/'.beyin-projects.json').write_text(json.dumps({'folders':{'projects/alpha':'beta'}}),encoding='utf-8')
        self.assertEqual(resolve_project_from_query(self.vault,'alpha hakkında'),'beta')
        self.assertIsNone(resolve_project_from_query(self.vault,'beta ve ödev'))
        self.assertIsNone(resolve_project_from_query(self.vault,'önödevson'))
        self.assertEqual(resolve_project_from_query(self.vault,'ödev hakkında'),'ödev')

    def test_configured_folder_alias_routes_rules_but_distinct_owners_stay_ambiguous(self):
        folder = self.vault / 'projects/client'
        folder.mkdir(parents=True)
        (self.vault / '.beyin-projects.json').write_text(
            json.dumps({'folders': {'projects/client': 'alpha'}}), encoding='utf-8')
        self.assertEqual(resolve_project_from_query(self.vault, 'client projesinde devam'), 'alpha')
        self.assertEqual(resolve_rules_project(self.vault, self.base, query='client projesinde devam'), 'alpha')
        rules = self.vault / '🔮 850-Companion/Kurallar.md'
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text('- [kapsam: alpha] ALPHA RULE CANARY\n', encoding='utf-8')
        SyncEngine(self.vault, self.state).sync()
        hook = Path(__file__).resolve().parents[1] / 'template/.claude/scripts/beyin_v3_hook.py'
        payload = {'hook_event_name': 'SessionStart', 'session_id': 'mapped-client',
                   'event_id': 'mapped-client', 'cwd': str(self.base),
                   'prompt': 'client projesinde devam'}
        result = subprocess.run([sys.executable, str(hook), '--vault', str(self.vault),
                                 '--state', str(self.state), '--harness', 'claude'],
                                input=json.dumps(payload), capture_output=True, text=True,
                                encoding='utf-8', env=inherited_env(BEYIN_V3_NO_SPAWN='1'), timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ALPHA RULE CANARY', result.stdout)
        other = self.vault / 'Projeler/client'
        other.mkdir(parents=True)
        (self.vault / '.beyin-projects.json').write_text(
            json.dumps({'folders': {'projects/client': 'alpha', 'Projeler/client': 'beta'}}),
            encoding='utf-8')
        self.assertIsNone(resolve_project_from_query(self.vault, 'client projesinde devam'))

    def test_authorized_rule_layers_render_within_budget(self):
        for source,text in [('projects/academic/Kurallar.md','PROJECT CANARY'),('Companion/Kurallar.md','GLOBAL CANARY')]:
            path=self.vault/source;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('---\n'+json.dumps({'id':source,'kind':'note','visibility':'internal','workspace_id':'academic'})+'\n---\n'+text,encoding='utf-8')
        SyncEngine(self.vault,self.state).sync();self.runtime.activate()
        context=self.runtime.legacy_context('codex','Algoritmalar',project='academic',workspace_session='rules',budget_chars=3000)
        text,_=render_context(context,3000)
        self.assertTrue(text);self.assertLessEqual(len(text),3000)
        self.assertIn('PROJECT CANARY',text);self.assertIn('GLOBAL CANARY',text)
        self.assertIn('proje > workspace > global',text)
        self.assertLess(text.index('[project]'),text.index('[workspace]'))
        self.assertLess(text.index('[workspace]'),text.index('[global]'))
        path=self.vault/'projects/academic/Kurallar.md'
        path.write_text(path.read_text(encoding='utf-8').replace('PROJECT CANARY','PROJECT CANARY '*1000),encoding='utf-8')
        SyncEngine(self.vault,self.state).sync()
        context=self.runtime.legacy_context('codex','Algoritmalar',project='academic',workspace_session='rules',budget_chars=3000)
        text,_=render_context(context,3000)
        self.assertTrue(text);self.assertIn('[truncated]',text);self.assertIn('GLOBAL CANARY',text)

    def test_hook_binding_routes_neutral_query_and_never_falls_back(self):
        install_fixture(self.base,'custom');self.runtime.activate()
        hook=Path(__file__).resolve().parents[1]/'template/.claude/scripts/beyin_v3_hook.py'
        env=inherited_env();env['BEYIN_V3_NO_SPAWN']='1'
        def invoke(project=None):
            payload={'hook_event_name':'SessionStart','session_id':'bound','event_id':'bound-'+str(project),
                'cwd':str(self.base/'academic-source-a'),'prompt':'devam'}
            if project:payload['project']=project
            run=subprocess.run([sys.executable,str(hook),'--vault',str(self.vault),'--state',str(self.state),
                '--harness','claude'],input=json.dumps(payload),capture_output=True,text=True,encoding='utf-8',env=env,timeout=20)
            self.assertEqual(run.returncode,0,run.stderr)
            return json.loads(run.stdout)['hookSpecificOutput']['additionalContext']
        text=invoke();self.assertIn('"workspace_id": "academic"',text)
        text=invoke('custom');self.assertIn('"workspace_id": "custom"',text)
        text=invoke('unknown');self.assertNotIn('academic-topic',text);self.assertNotIn('custom-topic',text)

    def test_binding_ambiguity_revocation_and_small_budgets(self):
        from beyin_v3_workspaces import LocalAuthority
        external=self.base/'academic-source-a'
        self.assertEqual(bound_project_for_cwd(self.vault,self.state,external),'academic')
        # The authority file is test-owned; remove the existing grant to model revocation.
        # Locate the actual local authority storage without inventing its schema.
        authority=LocalAuthority(self.runtime.registry)
        data=authority._load();data['grants'].clear()
        from unittest.mock import patch
        with patch.object(LocalAuthority,'_load',return_value=data):
            self.assertIsNone(bound_project_for_cwd(self.vault,self.state,external))
        for budget in (0,1,13,30,100):
            request=dict(self.request['data'],budget_chars=max(1,budget))
            profile=self.runtime.registry.snapshot()['profiles'][0]
            rules,truncated=self.runtime.behavior_rules(profile,request,{'academic-topic'})
            self.assertLessEqual(sum(len(json.dumps(rule,ensure_ascii=False))+2 for rule in rules),request['budget_chars']//4)

    def test_ambiguous_bindings_and_unadopted_rules_stay_closed(self):
        from unittest.mock import patch
        from beyin_v3_workspaces import Registry, LocalAuthority
        install_fixture(self.base,'custom')
        original=Registry.bindings
        external=self.base/'academic-source-a'
        def overlapping(registry,snapshot=None):
            bindings,digest=original(registry,snapshot)
            for sid,binding in bindings.items():
                if sid.startswith('custom-'):
                    binding['locator']=str(external)
            return bindings,digest
        with patch.object(Registry,'bindings',overlapping),patch.object(LocalAuthority,'allows',return_value=True):
            self.assertIsNone(bound_project_for_cwd(self.vault,self.state,external))
        folder=self.vault/'Companion';folder.mkdir(exist_ok=True)
        (folder/'Kurallar.md').write_text('UNADOPTED GLOBAL CANARY',encoding='utf-8')
        SyncEngine(self.vault,self.state).sync();self.runtime.activate()
        result=self.runtime.legacy_context('codex','Algoritmalar',project='academic',workspace_session='scope',budget_chars=3000)
        self.assertNotIn('UNADOPTED GLOBAL CANARY',json.dumps(result))
        # Even a valid legacy scope mapping cannot expand active workspace authority.
        denied=self.runtime.legacy_context('codex','UNADOPTED GLOBAL CANARY',project='unknown',workspace_session='scope',budget_chars=3000)
        self.assertFalse(denied['records']);self.assertFalse(denied['working_rules'])
