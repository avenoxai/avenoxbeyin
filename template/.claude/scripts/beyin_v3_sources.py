"""Authorized, hash-backed text extraction. Source bytes never become instructions.

PDF/DOCX parser selection/installation belongs to C2/D4. A trusted configured
reader receives bounded bytes on stdin, never a path or filesystem grant.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import threading
import sys

from beyin_v3_workspaces import (Registry, LocalAuthority, WorkspaceError, canonical_bytes,
    content_hash, envelope, parse_document, safe_path, validate_document, _valid_local_locator, local_root_matches)

MAX_SOURCE_BYTES=32*1024*1024
MAX_OUTPUT_BYTES=4*1024*1024
TEXT_SUFFIXES={'.md','.txt','.py','.js','.ts','.tsx','.jsx','.json','.toml','.yaml','.yml','.csv','.rst','.sh','.sql','.c','.h','.cpp','.rs','.go','.java'}
STATES={'ready','partial','requires_ocr','requires_password','unsupported','missing_dependency','failed'}


def default_readers(vault):
    from beyin_v3_extract_reader import PYPDF_NAME
    helper=Path(__file__).resolve().with_name('beyin_v3_extract_reader.py')
    import shutil
    native={}
    for name in ('pdftoppm','tesseract'):
        path=shutil.which(name)
        if path:
            info=Path(path).stat();native[name]=[path,info.st_size,info.st_mtime_ns]
    # Language packages can change independently of the tesseract binary.
    # Their actual bytes and selected prefix are part of extraction identity.
    prefix=os.environ.get('TESSDATA_PREFIX','');native['tessdata_prefix']=prefix
    roots=[Path(prefix)] if prefix else []
    recognizer=shutil.which('tesseract')
    if recognizer:
        try:
            languages=subprocess.run([recognizer,'--list-langs'],capture_output=True,timeout=3)
            header=languages.stdout.decode('utf-8',errors='replace').splitlines()[0]
            if '"' in header:roots.append(Path(header.split('"')[1]))
        except (OSError,subprocess.TimeoutExpired,IndexError):pass
    for root in dict.fromkeys(roots):
        for lang in ('tur','eng'):
            path=root/(lang+'.traineddata')
            try:
                if path.is_file() and path.stat().st_size<=64*1024*1024:
                    native[str(path)]=content_hash(path.read_bytes())
            except OSError:pass
    settings=content_hash(canonical_bytes({'ocr_native':native,'ocr_languages':'tur+eng','helper_sha256':content_hash(helper.read_bytes()),'limits':[MAX_SOURCE_BYTES,MAX_OUTPUT_BYTES]}))
    command=[sys.executable,'-I','-S','-B',str(helper)]
    return {'.docx':{'command':command,'id':'stdlib_docx','version':'1','settings_sha256':settings},
            '.pdf':{'command':command+['--ocr','--wheel',str(Path(vault)/'.claude/parser-assets'/PYPDF_NAME)],
                    'id':'pypdf','version':'6.21.0','settings_sha256':settings}}


def probe_capabilities(vault,*,ocr_probe=None):
    """C name/state adapter for D's one installed PDF/DOCX/OCR probe.

    ocr_probe is a deprecated ignored compatibility argument. D verifies its
    pinned installed raster asset once; caller bytes must not start a second
    OCR pass or override the installed capability. Workflow is availability.
    """
    from beyin_v3_parser_probe import document_capabilities
    capabilities={c['name']:c['state'] for c in document_capabilities(vault)}
    capabilities.update(text='ready',markdown='ready')
    try:
        from beyin_v3_workspace_workflow import WorkflowRunner
        capabilities['workflow']='ready' if callable(WorkflowRunner.advance) else 'failed'
    except ImportError:capabilities['workflow']='missing_dependency'
    return capabilities


def run_reader(command,request,timeout=20):
    if not command or not all(isinstance(x,str) and x for x in command):
        raise WorkspaceError('missing_dependency')
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output,errors=bytearray(),bytearray();overflow=[]
    def drain(stream,buffer,limit):
        try:
            while True:
                block=stream.read(65536)
                if not block:break
                if len(buffer)+len(block)>limit:
                    overflow.append(True);process.kill();break
                buffer.extend(block)
        finally:stream.close()
    def send():
        try:process.stdin.write(canonical_bytes(request));process.stdin.close()
        except (OSError,BrokenPipeError):pass
    threads=[threading.Thread(target=drain,args=(process.stdout,output,MAX_OUTPUT_BYTES),daemon=True),
             threading.Thread(target=drain,args=(process.stderr,errors,65536),daemon=True),
             threading.Thread(target=send,daemon=True)]
    for thread in threads:thread.start()
    try:process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill();process.wait();raise WorkspaceError('extractor_timeout') from exc
    finally:
        for thread in threads:thread.join(timeout=1)
    if overflow or process.returncode!=0:
        raise WorkspaceError('extractor_failed')
    try:
        return parse_document(bytes(output).decode('utf-8'))
    except UnicodeDecodeError as exc:
        raise WorkspaceError('invalid_extractor_output') from exc


def extract_document_bytes(vault,raw,format,*,readers=None):
    """Shared document-extraction/0.1.0 service for workspace and Yakala.

    Input is bounded bytes, never a source path, grant, shell or memory intent.
    Each caller owns authorization, source freshness and persistence. Chunks are
    untrusted source data; extraction does not approve or learn their contents.
    """
    if not isinstance(raw,bytes) or len(raw)>MAX_SOURCE_BYTES:raise WorkspaceError('source_too_large')
    digest=content_hash(raw);config=(default_readers(vault) if readers is None else readers).get('.'+format)
    if format not in ('pdf','docx') or config is None:
        return {'state':'unsupported' if format not in ('pdf','docx') else 'missing_dependency','chunks':[],
            'issues':[{'code':'unsupported' if format not in ('pdf','docx') else 'missing_dependency'}],'source_sha256':digest,'extractor':None}
    extractor={k:config[k] for k in ('id','version','settings_sha256')}
    try:
        value=run_reader(config['command'],{'format':format,'bytes_base64':base64.b64encode(raw).decode(),
            'source_sha256':digest,'limits':{'max_output_bytes':MAX_OUTPUT_BYTES}},timeout=config.get('timeout',20))
        if set(value)-{'state','chunks','issues'} or value.get('state') not in STATES or not isinstance(value.get('chunks'),list):raise WorkspaceError('invalid_extractor_output')
        for chunk in value['chunks']:
            if not isinstance(chunk,dict) or set(chunk)!={'text','fragment'} or not isinstance(chunk['text'],str):raise WorkspaceError('invalid_extractor_output')
            validate_document(envelope('SourceRef',{'source_id':'document','relative_path':'source.'+format,
                'content_sha256':digest,'fragment':chunk['fragment'],'extractor':extractor}))
        if value['state']=='ready' and not any(c['text'].strip() for c in value['chunks']):
            value={'state':'partial','chunks':[],'issues':[{'code':'empty_extraction'}]}
    except (OSError,WorkspaceError) as exc:
        value={'state':'failed','chunks':[],'issues':[{'code':getattr(exc,'code','extractor_unavailable')}]}
    return dict(value,source_sha256=digest,extractor=extractor)


class SourceResolver:
    def __init__(self,registry,*,readers=None,cache_write=True):
        self.cache_write=bool(cache_write)
        self.registry=registry;self.authority=LocalAuthority(registry);self.readers=default_readers(registry.vault) if readers is None else readers

    def resolve(self,workspace_id,source_id,name,grant_ref,*,access='read',audience='internal',archived=False):
        snapshot=self.registry.snapshot()
        if snapshot is None:raise WorkspaceError('workspace_unavailable')
        profiles={p['id']:p for p in snapshot['profiles']}
        profile=profiles.get(workspace_id)
        if not profile or (profile['lifecycle']!='active' and not (archived and profile['lifecycle']=='archived')):
            raise WorkspaceError('scope_denied')
        if source_id not in profile['source_ids']:raise WorkspaceError('scope_denied')
        descriptors={s['id']:s for s in snapshot['sources']}
        binding=self.registry.bindings(snapshot)[0].get(source_id)
        if binding is None:raise WorkspaceError('source_unbound')
        if not self.authority.allows(grant_ref,binding,workspace_id,access,audience):raise WorkspaceError('scope_denied')
        root=Path(binding['locator'])
        if not _valid_local_locator(root):
            raise WorkspaceError('source_unavailable')
        if access=='write_memory':
            if descriptors[source_id]['kind']!='vault' or not local_root_matches(root,self.registry.vault):
                raise WorkspaceError('external_source_write_denied')
            if not any(name.startswith(r.rstrip('/')+'/') for r in profile['note_roots']) and not name.startswith('tasks/'):
                raise WorkspaceError('write_root_denied')
        path=safe_path(root,name,existing=access=='read')
        if access=='read' and descriptors[source_id]['kind']=='vault':
            # Binary attachments need their own approved directory source; a
            # shared memory-vault binding cannot invent attachment record scope.
            if path.suffix.lower() in ('.pdf','.docx'):raise WorkspaceError('scope_denied')
            from beyin_v3 import sensitive_source,_rejected_inference
            from beyin_v3_sync import parse
            if sensitive_source(name):raise WorkspaceError('scope_denied')
            metadata,_=parse(path.read_text(encoding='utf-8'))
            if (metadata.get('trust')=='untrusted' or metadata.get('trusted') is False
                    or metadata.get('kind')=='untrusted' or metadata.get('status')=='untrusted'
                    or _rejected_inference(metadata) or (metadata.get('visibility')=='private' and audience!='private')
                    or (audience=='public' and metadata.get('visibility','internal')!='public')):
                raise WorkspaceError('scope_denied')
            from beyin_v3_workspace_context import scope_ids
            # Scope is explicit metadata or a trusted adoption, never a link edge.
            record=dict(metadata,id=metadata.get('id','md-'+content_hash(name)[:24]))
            if workspace_id not in scope_ids(self.registry,record):raise WorkspaceError('scope_denied')
        return path,profile,binding

    def read(self,source_ref,workspace_id,grant_ref,*,audience='internal',archived=False):
        validate_document(envelope('SourceRef',source_ref))
        path,_,_=self.resolve(workspace_id,source_ref['source_id'],source_ref['relative_path'],grant_ref,audience=audience,archived=archived)
        if path.stat().st_size>MAX_SOURCE_BYTES:raise WorkspaceError('source_too_large')
        raw=path.read_bytes()
        if content_hash(raw)!=source_ref['content_sha256']:raise WorkspaceError('stale_source')
        fresh,_,_=self.resolve(workspace_id,source_ref['source_id'],source_ref['relative_path'],grant_ref,audience=audience,archived=archived)
        if content_hash(fresh.read_bytes())!=source_ref['content_sha256']:raise WorkspaceError('stale_source')
        return raw

    def extract(self,workspace_id,source_id,name,grant_ref,*,audience='internal',archived=False):
        path,_,_=self.resolve(workspace_id,source_id,name,grant_ref,audience=audience,archived=archived)
        if path.stat().st_size>MAX_SOURCE_BYTES:raise WorkspaceError('source_too_large')
        raw=path.read_bytes();digest=content_hash(raw);suffix=path.suffix.lower()
        config=self.readers.get(suffix)
        if suffix in TEXT_SUFFIXES:
            extractor={'id':'markdown' if suffix=='.md' else 'text','version':'1','settings_sha256':content_hash(b'{}')}
        elif suffix in ('.pdf','.docx'):
            if config is None:
                return {'state':'missing_dependency','chunks':[],'source_sha256':digest,'issues':[{'code':'missing_dependency'}]}
            extractor={k:config[k] for k in ('id','version','settings_sha256')}
        else:return {'state':'unsupported','chunks':[],'source_sha256':digest,'issues':[{'code':'unsupported'}]}
        key=content_hash(canonical_bytes([source_id,name,digest,extractor]))
        cache=safe_path(self.registry.state,'workspace-extraction/'+key+'.json')
        result=None
        if cache.exists():
            value=parse_document(cache.read_bytes())
            if value.get('sha256')==content_hash(canonical_bytes(value.get('result'))):result=value['result']
        if result is None:
            if suffix in TEXT_SUFFIXES:
                try:
                    text=raw.decode('utf-8-sig')
                    if '\0' in text:raise UnicodeError()
                    result={'state':'ready','chunks':[{'text':text,'fragment':{'kind':'whole'}}] if text.strip() else [],'issues':[]}
                except UnicodeError:result={'state':'partial','chunks':[],'issues':[{'code':'invalid_text_encoding'}]}
            else:
                value=extract_document_bytes(self.registry.vault,raw,suffix[1:],readers=self.readers)
                result={k:value[k] for k in ('state','chunks','issues')}
            if result['state']=='ready' and not result['chunks']:
                result={'state':'partial','chunks':[],'issues':[{'code':'empty_extraction'}]}
            from beyin_v3_sync import atomic
            if self.cache_write and result['state']=='ready':atomic(cache,canonical_bytes({'sha256':content_hash(canonical_bytes(result)),'result':result}).decode())
        chunks=[]
        for chunk in result['chunks']:
            if not isinstance(chunk.get('text'),str) or set(chunk)!={'text','fragment'}:
                raise WorkspaceError('invalid_extractor_output')
            sr={'source_id':source_id,'relative_path':name,'content_sha256':digest,'fragment':chunk['fragment'],'extractor':extractor}
            validate_document(envelope('SourceRef',sr))
            chunks.append({'text':chunk['text'],'source_ref':sr})
        # Cache, extraction and even a completed process cannot preserve revoked access.
        fresh,_,_=self.resolve(workspace_id,source_id,name,grant_ref,audience=audience,archived=archived)
        if content_hash(fresh.read_bytes())!=digest:raise WorkspaceError('stale_source')
        return {'state':result['state'],'chunks':chunks,'source_sha256':digest,'issues':result.get('issues',[])}

    def files(self,workspace_id,source_id,grant_ref,*,limit=1000,audience='internal',archived=False):
        snapshot=self.registry.snapshot();bindings,_=self.registry.bindings(snapshot)
        binding=bindings.get(source_id)
        if not binding or not self.authority.allows(grant_ref,binding,workspace_id,audience=audience):
            raise WorkspaceError('scope_denied')
        root=Path(binding['locator']);found=[]
        profiles={p['id']:p for p in snapshot['profiles']};profile=profiles.get(workspace_id)
        if (not profile or (profile['lifecycle']!='active' and not (archived and profile['lifecycle']=='archived')) or source_id not in profile['source_ids']
                or not _valid_local_locator(root)):
            raise WorkspaceError('scope_denied')
        # os.walk with pruned names avoids traversing symlink trees or dependency dirs.
        import os
        for parent,dirs,names in os.walk(root,followlinks=False):
            dirs[:]=sorted(d for d in dirs if not d.startswith('.') and d not in ('node_modules','__pycache__','receipts')
                           and not (Path(parent)/d).is_symlink())
            for name in sorted(names):
                path=Path(parent)/name
                if name.startswith('.') or path.is_symlink() or path.suffix.lower() not in TEXT_SUFFIXES|{'.pdf','.docx'}:continue
                relative=path.relative_to(root).as_posix()
                self.resolve(workspace_id,source_id,relative,grant_ref,audience=audience,archived=archived)
                found.append(relative)
                if len(found)>=limit:return found
        return found
