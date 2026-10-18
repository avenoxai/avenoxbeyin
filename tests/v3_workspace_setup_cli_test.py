"""CLI adapter contract only; the setup service owns catalog/planning behavior."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from v3_workspace_metadata_cli_test import load_cli
from v3_workspace_helpers import ROOT
from beyin_v3_workspaces import envelope


class WorkspaceSetupCliTest(unittest.TestCase):
    def test_all_compact_actions_dispatch_before_store_or_sync(self):
        cli=load_cli()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);vault=root/'vault';vault.mkdir();state=root/'state'
            request=root/'request.json';request.write_text('{"compact":"PRIVATE_REQUEST"}')
            result=envelope('OperationResult',{'operation_id':'workspace-setup','operation_status':'planned',
                'workspace_health':'not_tested','parts':[],'capabilities':[],'verification':[],
                'issues':[],'next_action':'Review the exact plan and obtain explicit authority.'})
            module=types.ModuleType('beyin_v3_workspace_setup')
            from unittest.mock import Mock
            module.execute=Mock(return_value=(result,0))
            with patch.dict(sys.modules,{'beyin_v3_workspace_setup':module}),patch.object(cli,'load_engine',side_effect=AssertionError('store forbidden')),patch.object(cli,'load_sync',side_effect=AssertionError('sync forbidden')):
                for action in ('catalog','prepare','organize'):
                    observed,code=cli.main(['--vault',str(vault),'--state',str(state),'workspace-setup',action,'--file',str(request)],return_result=True)
                    self.assertEqual(observed,result);self.assertEqual(code,0)
                    module.execute.assert_called_with(vault,state,action,{'compact':'PRIVATE_REQUEST'})
            self.assertFalse(state.exists());self.assertEqual(list(vault.iterdir()),[])

    def test_human_preflight_error_displays_code_and_change_target(self):
        spec=importlib.util.spec_from_file_location('workspace_failure_entry',ROOT/'scripts/beyin_entry.py')
        entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
        from beyin_v3_workspace_memory import CanonicalTargetError
        result,_=load_cli().workspace_failure(CanonicalTargetError('invalid_create_note_target','invalid-note'))
        line=entry.human_result(result,'workspace-write')
        self.assertIn('invalid_create_note_target',line);self.assertIn('invalid-note',line)
        self.assertIn('notes/',line)

    def test_human_prepare_displays_existing_preview_and_sanitizes_controls(self):
        spec=importlib.util.spec_from_file_location('workspace_setup_entry',ROOT/'scripts/beyin_entry.py')
        entry=importlib.util.module_from_spec(spec);spec.loader.exec_module(entry)
        result=envelope('OperationResult',{'operation_id':'workspace-setup','operation_status':'planned',
            'workspace_health':'not_tested','parts':[],'capabilities':[],'verification':[],
            'issues':[{'code':'setup_artifacts','detail':json.dumps({'preview':{'text':'Plan: custom\nYetki gerekli\x1b'}})}],
            'next_action':'Review and approve the exact plan.'})
        self.assertEqual(entry.human_result(result,'workspace-setup'),'Plan: custom\nYetki gerekli?')


if __name__=='__main__':unittest.main()
