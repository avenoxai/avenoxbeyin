"""Synthetic native diagnostic only. Never publish or change product files."""
import json,os,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tests'))
from v3_yakala_pdf_warning_test import PdfWarningCliTest,write_pdf

def emit(kind,**data):print(json.dumps({'kind':kind,**data},ensure_ascii=True),flush=True)
def run(label,command,env,timeout=25):
 start=time.monotonic()
 try:
  p=subprocess.run(command,capture_output=True,env=env,timeout=timeout)
  emit(label,elapsed=round(time.monotonic()-start,3),returncode=p.returncode,stdout=p.stdout.decode('utf-8','replace')[:8000],stderr=p.stderr.decode('utf-8','replace')[:8000])
 except subprocess.TimeoutExpired as e:
  emit(label,elapsed=round(time.monotonic()-start,3),timeout=True,stdout=(e.stdout or b'').decode('utf-8','replace')[:8000],stderr=(e.stderr or b'').decode('utf-8','replace')[:8000])
 except OSError as e:emit(label,error=repr(e))

emit('host',python=sys.version,platform=sys.platform,pdftotext=shutil.which('pdftotext'))
run('version',[shutil.which('pdftotext') or 'pdftotext','-v'],os.environ.copy(),15)
WRAPPER=r'''
import json,sys,time
sys.path.insert(0,sys.argv[1])
import beyin_v3_yakala as y
helper=y.tools()['pdftotext']
print(json.dumps({'resolved_helper':helper}),flush=True)
start=time.monotonic()
try:
 result=y._run([helper,'-layout',sys.argv[2],'-'],timeout=20)
 print(json.dumps({'elapsed':time.monotonic()-start,'code':result[0],'stdout':result[1],'stderr':result[2]}),flush=True)
except BaseException as e:
 print(json.dumps({'elapsed':time.monotonic()-start,'error':repr(e)}),flush=True)
 raise
'''
for profile in ('inherited','isolated_missing','isolated_created'):
 f=PdfWarningCliTest();f.setUp()
 try:
  env=f.env.copy()
  keys=('HOME','USERPROFILE','APPDATA','LOCALAPPDATA','XDG_STATE_HOME')
  if profile=='inherited':
   for key in keys:
    if key in os.environ:env[key]=os.environ[key]
    else:env.pop(key,None)
  if profile=='isolated_created':
   for key in keys:Path(env[key]).mkdir(parents=True,exist_ok=True)
  emit('profile',profile=profile,exists={k:Path(env[k]).exists() if k in env else None for k in keys},helper=shutil.which('pdftotext',path=env.get('PATH')))
  files=(f.vault.parent/'searchable.pdf',f.vault/'📥 000-Inbox/Yakala/dosyalar/searchable.pdf')
  for path in files:
   path.parent.mkdir(parents=True,exist_ok=True);write_pdf(path,text=True)
   label=profile+('/captured_unicode' if path.is_relative_to(f.vault) else '/source_ascii')
   run(label+'/direct',[shutil.which('pdftotext') or 'pdftotext','-layout',str(path),'-'],env)
   run(label+'/installed_run',[sys.executable,'-c',WRAPPER,str(f.vault/'.claude/scripts'),str(path)],env)
 finally:f.doCleanups()

# Repeated paired launches distinguish Xpdf failures from the suspended job launcher.
f=PdfWarningCliTest();f.setUp()
try:
 path=f.vault/'📥 000-Inbox/Yakala/dosyalar/searchable.pdf';path.parent.mkdir(parents=True,exist_ok=True);write_pdf(path,text=True)
 source=f.vault.parent/'searchable.pdf';write_pdf(source,text=True)
 STRESS=r'''
import collections,json,subprocess,sys,time
sys.path.insert(0,sys.argv[1])
import beyin_v3_yakala as y
command=[y.tools()['pdftotext'],'-layout',sys.argv[2],'-']
counts=collections.Counter()
for attempt in range(40):
 for mode in ('ascii_direct','captured_direct'):
  command[2]=sys.argv[3] if mode=='ascii_direct' else sys.argv[2]
  started=time.monotonic()
  try:
   if mode=='installed_job':code,out,err=y._run(command,timeout=3)
   else:
    p=subprocess.run(command,capture_output=True,timeout=3,creationflags=y.NO_WINDOW if mode=='no_window' else 0)
    code,out,err=p.returncode,p.stdout.decode('utf-8','replace'),p.stderr.decode('utf-8','replace')
   ok=code==0 and 'Searchable PDF words' in out
   counts[mode+('/ok' if ok else '/failed')]+=1
   if not ok:print(json.dumps({'attempt':attempt,'mode':mode,'code':code,'stdout':out,'stderr':err,'elapsed':time.monotonic()-started}),flush=True)
  except BaseException as e:
   counts[mode+'/exception']+=1
   print(json.dumps({'attempt':attempt,'mode':mode,'exception':repr(e),'elapsed':time.monotonic()-started}),flush=True)
print(json.dumps({'paired_counts':dict(counts),'command':command}),flush=True)
'''
 run('paired_stress',[sys.executable,'-c',STRESS,str(f.vault/'.claude/scripts'),str(path),str(source)],f.env,timeout=240)
finally:f.doCleanups()
