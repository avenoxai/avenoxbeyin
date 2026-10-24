"""Small workspace fixture for authorized navigation regressions."""
from pathlib import Path
import tempfile

from beyin_v3_workspace_discovery import design
from beyin_v3_workspaces import Registry, LocalAuthority, envelope, render_document


class NavigationFixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.vault = self.root / 'vault'
        self.vault.mkdir()
        self.state = self.root / 'state'
        self.registry = Registry(self.vault, self.state)
        proposal = design(self.vault, self.state, dict(
            workspace_id='observatory', name='Gözlem evi', purpose='Bağlantıları izlemek',
            design=dict(record_types=[], relation_types=['related_to'], working_rules=[], workflows=[])))
        profile = proposal['effective_profile']
        profile['data'].update(lifecycle='active', source_ids=['memory'], note_roots=['notes/workspaces'])
        self.registry.compare_and_save(envelope('RegistryFormat',
            dict(registry_id='synthetic', layout='per_workspace_json')), None)
        self.registry.compare_and_save(envelope('SourceDescriptor',
            dict(id='memory', kind='vault', label='memory')), None)
        binding = dict(source_id='memory', machine_id='synthetic-machine', revision=1,
                       locator=str(self.vault), grants=[dict(workspace_id='observatory', access='read')],
                       authority_ref='synthetic-approval')
        target = self.state / 'workspace-bindings/memory.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(render_document(envelope('SourceBinding', binding)))
        self.registry.compare_and_save(profile, None)
        self.authority = LocalAuthority(self.registry)
        self.authority.record_approval('synthetic-approval', machine_id='synthetic-machine',
            permissions=[dict(workspace_id='observatory', source_id='memory', locator=str(self.vault), access='read')],
            audiences=('internal',), evidence='Explicit synthetic navigation approval')
        self.state.joinpath('workspace-runtime.json').write_text('{"enabled":true}')

    def close(self):
        self.temp.cleanup()
