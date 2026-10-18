"""G0 per-workspace registry, strict contract validation and local capabilities.

No model, network, third-party package or implicit filesystem grant. Registry
presence alone does not enable enforcement; activation is local and explicit.
"""
from contextlib import contextmanager
from datetime import date
import base64
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import stat
import sys
import unicodedata

from beyin_v3_workspace_schema import SCHEMA

VERSION = 'workspace/0.1.0'
MAX_BYTES = 4 * 1024 * 1024


class WorkspaceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def content_hash(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceError('duplicate_json_key')
        result[key] = value
    return result


def parse_document(raw):
    if len(raw) > MAX_BYTES:
        raise WorkspaceError('document_too_large')
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode('utf-8')
        return json.loads(raw, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(WorkspaceError('invalid_number')))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise WorkspaceError('invalid_json') from exc


def _matches(value, schema, depth=0):
    if depth > 64:
        raise WorkspaceError('document_too_deep')
    if '$ref' in schema:
        return _matches(value, SCHEMA['$defs'][schema['$ref'].split('/')[-1]], depth+1)
    def equal(a, b):
        return type(a) is type(b) and a == b if isinstance(a, bool) or isinstance(b, bool) else a == b
    if 'const' in schema and not equal(value, schema['const']):
        return False
    if 'enum' in schema and not any(equal(value, item) for item in schema['enum']):
        return False
    kinds = schema.get('type', [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    types = {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
             'integer': type(value) is int, 'number': type(value) in (int, float),
             'boolean': type(value) is bool, 'null': value is None}
    if kinds and not any(types[k] for k in kinds):
        return False
    if type(value) is float and not math.isfinite(value):
        return False
    if type(value) in (int, float) and value < schema.get('minimum', value):
        return False
    if isinstance(value, str):
        if len(value) < schema.get('minLength', 0) or ('pattern' in schema and re.search(schema['pattern'], value) is None):
            return False
        if schema.get('format') == 'date':
            try:
                if len(value) != 10 or date.fromisoformat(value).isoformat() != value:
                    return False
            except ValueError:
                return False
    if isinstance(value, dict):
        if not all(isinstance(k, str) for k in value) or not set(schema.get('required', [])) <= value.keys():
            return False
        props, extra = schema.get('properties', {}), schema.get('additionalProperties', True)
        for key, item in value.items():
            rule = props.get(key, extra)
            if rule is False or (isinstance(rule, dict) and not _matches(item, rule, depth+1)):
                return False
        for key, dependencies in schema.get('dependentRequired', {}).items():
            if key in value and not set(dependencies) <= value.keys():
                return False
    if isinstance(value, list):
        if len(value) < schema.get('minItems', 0):
            return False
        if 'items' in schema and not all(_matches(v, schema['items'], depth+1) for v in value):
            return False
    if 'allOf' in schema and not all(_matches(value, s, depth+1) for s in schema['allOf']):
        return False
    if 'anyOf' in schema and not any(_matches(value, s, depth+1) for s in schema['anyOf']):
        return False
    if 'oneOf' in schema and sum(_matches(value, s, depth+1) for s in schema['oneOf']) != 1:
        return False
    if 'if' in schema and _matches(value, schema['if'], depth+1) and not _matches(value, schema.get('then', {}), depth+1):
        return False
    return True


def relative_name(name):
    if (not isinstance(name, str) or not name or len(name) > 1024 or '\\' in name or ':' in name
            or any(ord(c) < 32 for c in name) or name.startswith('/')
            or any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in name.split('/'))):
        raise WorkspaceError('unsafe_relative_path')
    for part in name.split('/'):
        if any(c in part for c in '<>"|?*') or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I):
            raise WorkspaceError('unsafe_relative_path')
    return name


def safe_path(root, name, existing=False):
    root = Path(root).resolve()
    path = root
    for part in relative_name(name).split('/'):
        path = path / part
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
                raise WorkspaceError('redirected_path')
    if not path.resolve().is_relative_to(root) or (existing and not path.is_file()):
        raise WorkspaceError('source_unavailable')
    return path


def validate_workflow(workflow):
    steps = {step['id']: step for step in workflow['steps']}
    if len(steps) != len(workflow['steps']) or len(steps) > 64:
        raise WorkspaceError('invalid_workflow_steps')
    done = set()
    while len(done) < len(steps):
        ready = {id for id, step in steps.items() if id not in done and set(step['depends_on']) <= done}
        if not ready:
            raise WorkspaceError('workflow_cycle_or_missing_step')
        done.update(ready)
    for id, step in steps.items():
        ancestors = set(step['depends_on'])
        todo = list(ancestors)
        while todo:
            for parent in steps[todo.pop()]['depends_on']:
                if parent not in ancestors:
                    ancestors.add(parent); todo.append(parent)
        for item in step['inputs'].values():
            if item.get('ref', '').startswith('steps.') and item['ref'].split('.')[1] not in ancestors:
                raise WorkspaceError('workflow_forward_reference')


def validate_document(document):
    try:
        if not _matches(document, SCHEMA):
            raise WorkspaceError('invalid_schema')
        if len(canonical_bytes(document)) > MAX_BYTES:
            raise WorkspaceError('document_too_large')
    except (TypeError, RecursionError, OverflowError) as exc:
        raise WorkspaceError('invalid_schema') from exc
    kind, data = document['type'], document['data']
    if kind == 'WorkspaceProfile':
        if len(data['source_ids']) != len(set(data['source_ids'])):
            raise WorkspaceError('duplicate_source')
        for path in data['note_roots']:
            relative_name(path)
        for workflow in data['workflows']:
            validate_workflow(workflow)
    if kind == 'WorkspaceSpec':
        validate_document(envelope('WorkspaceProfile',data['desired_profile']))
    if kind == 'RegistrySnapshot':
        for profile in data['profiles']:
            validate_document(envelope('WorkspaceProfile',profile))
    if kind == 'WorkflowDefinition':
        validate_workflow(data)
    if kind == 'SourceRef':
        relative_name(data['relative_path'])
        f = data['fragment']
        if f['kind'] == 'lines' and f['end'] < f['start']:
            raise WorkspaceError('invalid_fragment')
    if kind == 'ProvisionPlan':
        body = {k:v for k,v in data.items() if k not in ('plan_id','plan_sha256')}
        if content_hash(canonical_bytes(body)) != data['plan_sha256']:
            raise WorkspaceError('plan_changed')
        targets = set()
        for op in data['operations']:
            relative_name(op['target']['relative_path'])
            key = (op['target']['root'], unicodedata.normalize('NFC',op['target']['relative_path']).casefold())
            if key in targets or content_hash(op['content_utf8']) != op['after_sha256']:
                raise WorkspaceError('invalid_write_set')
            targets.add(key)
    return document


def envelope(kind, data):
    return validate_document({'contract_version': VERSION, 'type': kind, 'data': data})


def render_document(document):
    validate_document(document)
    return (json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode('utf-8')


def validate_for_provision(document):
    """D1/D2 callback convention: raise on rejection; None on acceptance."""
    validate_document(document)


def workspace_metadata(vault, state, action, workspace_id=None, *, limit=20):
    """Registry metadata only: no bindings, source reads, store, sync or writes.

    This is deliberately not a WorkContext or a capability/authority check.
    The existing snapshot reader keeps its pending-operation barrier.
    """
    if action not in ('list', 'inspect') or type(limit) is not int or not 1 <= limit <= 100:
        raise WorkspaceError('invalid_metadata_request')
    if (action == 'list' and workspace_id is not None) or (action == 'inspect' and
            (not isinstance(workspace_id, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}', workspace_id))):
        raise WorkspaceError('invalid_metadata_request')
    snapshot = Registry(vault, state).snapshot()
    profiles = sorted((snapshot or {}).get('profiles', []), key=lambda profile: profile['id'])
    if action == 'inspect':
        profiles = [profile for profile in profiles if profile['id'] == workspace_id]
        if not profiles:
            raise WorkspaceError('workspace_not_found')
    items = []
    for profile in profiles[:limit]:
        item = {key: profile[key] for key in ('id', 'name', 'revision', 'lifecycle')}
        item.update(source_count=len(profile['source_ids']), note_root_count=len(profile['note_roots']),
                    working_rule_count=len(profile['working_rules']), workflow_count=len(profile['workflows']))
        if action == 'inspect':
            # Only portable configuration identifiers, never local locators or note bodies.
            item.update(source_ids=profile['source_ids'][:20], note_roots=profile['note_roots'][:20])
        items.append(item)
    return {'workspace_metadata': {'action': action, 'read_only': True,
        'registry_id': (snapshot or {}).get('registry_id'),
        'snapshot_sha256': (snapshot or {}).get('snapshot_sha256'),
        'workspaces': items, 'total': len(profiles), 'truncated': len(profiles) > limit}}


class Registry:
    def __init__(self, vault, state):
        self.vault, self.state = Path(vault).resolve(), Path(state).resolve()
        if not self.vault.is_dir() or self.state.is_relative_to(self.vault):
            raise WorkspaceError('invalid_registry_roots')

    def read(self, root, name, kind=None):
        path = safe_path(root, name, True)
        document = validate_document(parse_document(path.read_bytes()))
        if kind and document['type'] != kind:
            raise WorkspaceError('wrong_document_type')
        return document

    def pending(self):
        return safe_path(self.state, 'workspace-provision/pending.json').exists()

    def snapshot(self, *, allow_pending=False, registry_id=None):
        if self.pending() and not allow_pending:
            raise WorkspaceError('operation_pending')
        base = safe_path(self.vault, '.beyin-workspaces')
        if not base.exists():
            return None if registry_id is None else dict(registry_id=registry_id,files=[],profiles=[],sources=[],snapshot_sha256=content_hash(canonical_bytes([])))
        fmt = self.read(self.vault, '.beyin-workspaces/format.json', 'RegistryFormat')['data']
        files, profiles, sources, seen = [], [], [], set()
        paths = [safe_path(self.vault, '.beyin-workspaces/format.json', True)]
        for folder in ('workspaces','sources'):
            directory = safe_path(self.vault, '.beyin-workspaces/'+folder)
            if directory.exists():
                paths.extend(sorted(directory.glob('*.json')))
        for path in sorted(paths):
            relative = path.relative_to(self.vault).as_posix()
            raw = safe_path(self.vault, relative, True).read_bytes()
            doc = validate_document(parse_document(raw))
            folded = unicodedata.normalize('NFC',relative).casefold()
            if folded in seen:
                raise WorkspaceError('registry_name_collision')
            seen.add(folded)
            if relative != '.beyin-workspaces/format.json':
                kind = 'WorkspaceProfile' if path.parent.name == 'workspaces' else 'SourceDescriptor'
                if doc['type'] != kind or path.stem != doc['data']['id']:
                    raise WorkspaceError('registry_identity_mismatch')
                (profiles if kind == 'WorkspaceProfile' else sources).append(doc['data'])
            files.append({'relative_path':relative,'content_sha256':content_hash(raw)})
        for item in files:
            if content_hash(safe_path(self.vault,item['relative_path'],True).read_bytes()) != item['content_sha256']:
                raise WorkspaceError('snapshot_changed')
        if self.pending() and not allow_pending:
            raise WorkspaceError('operation_pending')
        ids = {source['id'] for source in sources}
        if any(set(p['source_ids'])-ids for p in profiles):
            raise WorkspaceError('missing_source_descriptor')
        return envelope('RegistrySnapshot',dict(registry_id=fmt['registry_id'],files=files,profiles=profiles,sources=sources,
            snapshot_sha256=content_hash(canonical_bytes(files))))['data']

    def bindings(self, snapshot=None):
        snapshot = snapshot or self.snapshot()
        bindings = {}
        for source in (snapshot or {}).get('sources',[]):
            name = 'workspace-bindings/'+source['id']+'.json'
            if safe_path(self.state,name).exists():
                b = self.read(self.state,name,'SourceBinding')['data']
                if b['source_id'] != source['id']:
                    raise WorkspaceError('binding_identity_mismatch')
                bindings[source['id']] = b
        vector = [{'source_id':id,'revision':b['revision'],'content_sha256':content_hash(canonical_bytes(envelope('SourceBinding',b)))}
                  for id,b in sorted(bindings.items())]
        return bindings, content_hash(canonical_bytes(vector))

    @contextmanager
    def coordinated(self,*,_authority_control=False):
        """Shared update lock; only local authority mutations may cross barriers.

        The private control path never reads a mixed registry snapshot or permits
        normal readers/writers to bypass pending publication.
        """
        self.state.mkdir(parents=True,exist_ok=True,mode=0o700)
        path = safe_path(self.state,'update-lock.sqlite3')
        db = sqlite3.connect(path,timeout=5)
        try:
            db.execute('BEGIN IMMEDIATE')
            if not _authority_control and self.pending():
                raise WorkspaceError('operation_pending')
            if not _authority_control and safe_path(self.state,'update-journal.json').exists():
                raise WorkspaceError('updater_operation_pending')
            yield
        except sqlite3.OperationalError as exc:
            raise WorkspaceError('busy') from exc
        finally:
            db.close()

    def compare_and_save(self, document, expected_hash):
        validate_document(document)
        kind, data = document['type'], document['data']
        if kind not in ('WorkspaceProfile','SourceDescriptor','RegistryFormat'):
            raise WorkspaceError('registry_ownership_required')
        name = '.beyin-workspaces/format.json' if kind=='RegistryFormat' else '.beyin-workspaces/'+(
            'workspaces' if kind=='WorkspaceProfile' else 'sources')+'/'+data['id']+'.json'
        from beyin_v3_sync import atomic
        with self.coordinated():
            path = safe_path(self.vault,name)
            old = path.read_bytes() if path.exists() else None
            if (content_hash(old) if old is not None else None) != expected_hash:
                raise WorkspaceError('revision_conflict')
            if old is None and 'revision' in data and data['revision'] != 1:
                raise WorkspaceError('revision_conflict')
            if old is not None and 'revision' in data:
                previous=validate_document(parse_document(old))['data']
                if data['revision'] != previous['revision']+1:
                    raise WorkspaceError('revision_conflict')
            if kind=='WorkspaceProfile':
                for source_id in data['source_ids']:
                    self.read(self.vault,'.beyin-workspaces/sources/'+source_id+'.json','SourceDescriptor')
            atomic(path,render_document(document).decode('utf-8'))
        return content_hash(path.read_bytes())

    def capture_files(self):
        """Complete configuration bytes only. Pure read; never scans source roots."""
        if self.pending():
            raise WorkspaceError('operation_pending')
        return self._raw_inventory()

    def _raw_inventory(self):
        files = {}
        for root, base in [('vault','.beyin-workspaces'),('state','workspace-bindings')]:
            directory = safe_path(getattr(self,root),base)
            if directory.exists():
                for path in sorted(directory.rglob('*.json')):
                    name = path.relative_to(getattr(self,root)).as_posix()
                    raw = safe_path(getattr(self,root),name,True).read_bytes()
                    if len(raw)>MAX_BYTES:
                        raise WorkspaceError('document_too_large')
                    files[(root,name)] = raw
        return files


def _valid_local_locator(path):
    """Allow macOS's OS-owned /var alias, but not arbitrary symlink redirects."""
    if not path.is_absolute() or '..' in path.parts or not path.is_dir():
        return False
    resolved = path.resolve()
    if sys.platform == 'win32':
        # Windows resolves 8.3 short names (RUNNER~1) to long names.
        # Accept spelling normalization, never a symlink or junction redirect.
        cursor = Path(path.anchor)
        for component in path.parts[1:]:
            cursor = cursor / component
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_reparse_tag', 0):
                return False
        return True
    if resolved == path:
        return True
    if (sys.platform == 'darwin' and path.parts[:2] == ('/', 'var')
            and Path('/var').is_symlink()
            and Path('/var').resolve() == Path('/private/var')):
        # /var -> /private/var is a system alias; any *additional* redirect fails.
        return resolved == Path('/private') / path.relative_to('/')
    return False


def local_root_matches(locator, root):
    """Compare validated local aliases without changing exact grant locators."""
    try:
        path = Path(locator)
        return _valid_local_locator(path) and path.resolve() == Path(root).resolve()
    except (OSError, RuntimeError, ValueError):
        return False


class LocalAuthority:
    """Privileged approval adapter; not inferred from profiles, sources or model refs.

    The UI/active agent must have obtained the concrete user's authority before
    record_approval/approve_plan. This is tool policy, not an OS security boundary.
    """
    def __init__(self, registry):
        self.registry=registry

    def _load(self):
        p=safe_path(self.registry.state,'workspace-authorities.json')
        if not p.exists():
            return {'version':1,'machine_id':None,'grants':{},'plans':{}}
        data=parse_document(p.read_bytes())
        if set(data)!={'version','machine_id','grants','plans'} or data['version']!=1:
            raise WorkspaceError('invalid_local_authority')
        return data

    def _save(self,data):
        from beyin_v3_sync import atomic
        atomic(safe_path(self.registry.state,'workspace-authorities.json'),canonical_bytes(data).decode())

    def record_approval(self, reference, *, machine_id, permissions, audiences=('internal',), evidence):
        if not reference or not evidence or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}',machine_id):
            raise WorkspaceError('explicit_authority_required')
        for permission in permissions:
            if set(permission)!={'workspace_id','source_id','locator','access'} or permission['access'] not in ('read','write_memory'):
                raise WorkspaceError('invalid_local_permission')
            path=Path(permission['locator'])
            if not _valid_local_locator(path):
                raise WorkspaceError('invalid_local_locator')
            if permission['access']=='write_memory' and path.resolve()!=self.registry.vault:
                raise WorkspaceError('external_source_write_denied')
        if set(audiences)-{'public','internal','private'}:
            raise WorkspaceError('invalid_audience')
        with self.registry.coordinated(_authority_control=True):
            data=self._load()
            if data['machine_id'] not in (None,machine_id):
                raise WorkspaceError('binding_machine_conflict')
            data['machine_id']=machine_id
            data['grants'][reference]={'permissions':permissions,'audiences':list(audiences),'evidence':evidence}
            self._save(data)

    def revoke(self,reference):
        with self.registry.coordinated(_authority_control=True):
            data=self._load();data['grants'].pop(reference,None);self._save(data)

    def allows(self,reference,binding,workspace_id,access='read',audience='internal'):
        data=self._load(); grant=data['grants'].get(reference)
        own=data['grants'].get(binding['authority_ref'])
        permission={'workspace_id':workspace_id,'source_id':binding['source_id'],'locator':binding['locator'],'access':access}
        return bool(grant and own and binding['machine_id']==data['machine_id'] and audience in grant['audiences']
                    and permission in grant['permissions'] and permission in own['permissions']
                    and {'workspace_id':workspace_id,'access':access} in binding['grants'])

    def approve_plan(self,document,*,evidence):
        validate_document(document)
        if document['type']!='ProvisionPlan' or not evidence:
            raise WorkspaceError('plan_authority_required')
        plan=document['data']
        with self.registry.coordinated():
            data=self._load()
            if not set(plan['required_authority'])<=data['grants'].keys():
                raise WorkspaceError('authority_required')
            files=self.registry._raw_inventory()
            self._validate_baseline(plan,files)
            for op in plan['operations']:
                if op['ownership']=='binding':
                    binding=validate_document(parse_document(op['content_utf8']))['data']
                    for grant in binding['grants']:
                        if not self.allows(binding['authority_ref'],binding,grant['workspace_id'],grant['access']):
                            raise WorkspaceError('scope_denied')
            data['plans'][plan['plan_sha256']]={'evidence':evidence,'files':[
                {'root':root,'name':name,'bytes':base64.b64encode(raw).decode()} for (root,name),raw in sorted(files.items())]}
            self._save(data)

    def approve_revert(self,document,*,evidence):
        validate_document(document)
        if document['type']!='ProvisionPlan' or not evidence:raise WorkspaceError('plan_authority_required')
        with self.registry.coordinated(_authority_control=True):
            data=self._load();approval=data['plans'].get(document['data']['plan_sha256'])
            if approval is None:raise WorkspaceError('authority_required')
            approval['revert_authorized']=evidence;self._save(data)

    @staticmethod
    def _validate_baseline(plan,files):
        vector=[{'relative_path':name,'content_sha256':content_hash(raw)} for (root,name),raw in sorted(files.items()) if root=='vault']
        binding_vector=[]
        for (root,name),raw in sorted(files.items()):
            if root=='state':
                doc=validate_document(parse_document(raw));b=doc['data']
                binding_vector.append({'source_id':b['source_id'],'revision':b['revision'],'content_sha256':content_hash(canonical_bytes(doc))})
        binding_vector.sort(key=lambda x:x['source_id'])
        if content_hash(canonical_bytes(vector))!=plan['snapshot_sha256'] or content_hash(canonical_bytes(binding_vector))!=plan['binding_snapshot_sha256']:
            raise WorkspaceError('snapshot_changed')

    def check_authority(self,document,action):
        validate_document(document)
        data=self._load();plan=document['data']
        if plan['plan_sha256'] not in data['plans'] or not set(plan['required_authority'])<=data['grants'].keys():
            raise WorkspaceError('authority_required')
        if action=='revert' and not data['plans'][plan['plan_sha256']].get('revert_authorized'):
            raise WorkspaceError('revert_authority_required')
        for op in plan['operations']:
            if op['ownership']=='binding':
                raw=op['content_utf8']
                if action=='revert':
                    name=op['target']['relative_path']
                    previous=next((item for item in data['plans'][plan['plan_sha256']]['files'] if item['root']=='state' and item['name']==name),None)
                    if previous is None:
                        if op['before_sha256'] is not None:raise WorkspaceError('invalid_local_authority')
                        continue
                    raw=base64.b64decode(previous['bytes'],validate=True)
                binding=validate_document(parse_document(raw))['data']
                for grant in binding['grants']:
                    if not self.allows(binding['authority_ref'],binding,grant['workspace_id'],grant['access']):
                        raise WorkspaceError('scope_denied')

    def check_snapshot(self,document,observations):
        """Normalize only this approved plan's old/new files during D2 recovery."""
        self.check_authority(document,'recover')
        plan=document['data'];approval=self._load()['plans'][plan['plan_sha256']]
        before={(x['root'],x['name']):base64.b64decode(x['bytes'],validate=True) for x in approval['files']}
        now=self.registry._raw_inventory()
        for op in plan['operations']:
            key=(op['target']['root'],op['target']['relative_path'])
            actual=content_hash(now[key]) if key in now else None
            if actual not in (op['before_sha256'],op['after_sha256']):
                raise WorkspaceError('source_hash_conflict')
            if key in before:
                now[key]=before[key]
            else:
                now.pop(key,None)
        if now!=before:
            raise WorkspaceError('snapshot_changed')
        self._validate_baseline(plan,now)


def execute_authority(vault,state,action,payload):
    """Trusted active-agent/UI adapter to C1 authority, never implicit apply consent.

    Evidence must describe the concrete human authorization already obtained by
    the caller. A string supplied by a source document is not human authority;
    this CLI has the same tool-policy boundary as the existing Python API.
    """
    fields={'grant':{'reference','machine_id','permissions','audiences','evidence'},
            'approve-plan':{'plan','evidence'},'approve-revert':{'plan','evidence'},
            'revoke':{'reference','evidence'}}
    if action not in fields or not isinstance(payload,dict) or set(payload)!=fields[action]:
        raise WorkspaceError('invalid_authority_request')
    evidence=payload['evidence']
    if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>4096:
        raise WorkspaceError('explicit_authority_required')
    if 'reference' in payload and (not isinstance(payload['reference'],str) or not payload['reference'].strip() or len(payload['reference'])>512):
        raise WorkspaceError('invalid_authority_request')
    if action=='grant':
        permissions=payload['permissions'];audiences=payload['audiences']
        if (not isinstance(payload['machine_id'],str) or not isinstance(permissions,list) or len(permissions)>1024
                or not isinstance(audiences,list) or not audiences or not all(isinstance(a,str) for a in audiences)
                or len(set(audiences))!=len(audiences)):
            raise WorkspaceError('invalid_authority_request')
        for permission in permissions:
            if not isinstance(permission,dict) or set(permission)!={'workspace_id','source_id','locator','access'}:
                raise WorkspaceError('invalid_authority_request')
            if any(not isinstance(permission[k],str) for k in permission):raise WorkspaceError('invalid_authority_request')
            if any(not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,95}',permission[k]) for k in ('workspace_id','source_id')):
                raise WorkspaceError('invalid_authority_request')
    authority=LocalAuthority(Registry(vault,state))
    if action=='grant':
        authority.record_approval(payload['reference'],machine_id=payload['machine_id'],permissions=permissions,audiences=audiences,evidence=evidence)
        current=authority._load()['grants'].get(payload['reference'])
        verified=current=={'permissions':permissions,'audiences':audiences,'evidence':evidence}
    elif action=='revoke':
        authority.revoke(payload['reference'])
        verified=payload['reference'] not in authority._load()['grants']
    else:
        document=payload['plan'];validate_document(document)
        if document['type']!='ProvisionPlan':raise WorkspaceError('invalid_schema')
        if action=='approve-plan':authority.approve_plan(document,evidence=evidence)
        else:authority.approve_revert(document,evidence=evidence)
        current=authority._load()['plans'].get(document['data']['plan_sha256'],{})
        verified=current.get('evidence' if action=='approve-plan' else 'revert_authorized')==evidence
    if not verified:raise WorkspaceError('authority_readback_conflict')
    return envelope('OperationResult',{'operation_id':'workspace-authority','operation_status':'applied',
        'workspace_health':'not_tested','parts':[{'id':action.replace('-','_'),'state':'verified'}],
        'capabilities':[],'verification':[{'check':'local_authority_readback','state':'passed','evidence_refs':[]}],
        'issues':[],'next_action':'Yetki kaydı doğrulandı; plan uygulaması ve günlük alan doğrulaması ayrı yürütülür.'}),0
