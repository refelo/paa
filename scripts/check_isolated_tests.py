from pathlib import Path
import json
import os
import sys
import time
import unittest

ROOT=Path(__file__).resolve().parents[1]
QA=ROOT/'.local/qa'
runtime=QA/'runtime'
temporary=QA/'tmp'
(runtime/'.local').mkdir(parents=True,exist_ok=True)
temporary.mkdir(parents=True,exist_ok=True)
os.environ.update(PAA_WORKSPACE=str(runtime),TMP=str(temporary),TEMP=str(temporary),PYTHONDONTWRITEBYTECODE='1')
os.environ.pop('VOYAGE_API_KEY',None)
sys.dont_write_bytecode=True
os.chdir(runtime)
sys.path.insert(0,str(ROOT))

# Guard this test process against accidentally reaching a real service. Git fixture
# subprocesses run only in their own temporary directories.
def audit(event,args):
    if event in {'socket.connect','socket.getaddrinfo','socket.bind'}:
        raise RuntimeError('Isolated tests may not access network')
    if event=='subprocess.Popen':
        command=args[1]
        first=command[0] if isinstance(command,(list,tuple)) else command.lstrip().split(' ',1)[0].strip('"')
        program=Path(str(args[0] or first)).name.lower()
        if program not in {'git','git.exe'}:
            raise RuntimeError('Unexpected subprocess in isolated tests: '+program)
        cwd=Path(args[2] or Path.cwd()).resolve()
        readonly_revision='rev-parse' in (command if isinstance(command,str) else ' '.join(command))
        if not cwd.is_relative_to(temporary.resolve()) and not (cwd.is_relative_to(ROOT) and readonly_revision):
            raise RuntimeError('Git test subprocess outside isolated temporary root')
    write_path=None
    if event=='open':
        name,mode,flags=args
        if isinstance(name,(str,bytes,os.PathLike)) and ((isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))):
            write_path=name
    elif event in {'os.remove','os.mkdir','os.rmdir'}:
        write_path=args[0]
    elif event in {'os.rename','os.replace'}:
        for item in args[:2]:
            if not Path(item).resolve().is_relative_to(ROOT):
                raise RuntimeError('Test rename outside public project')
    if write_path is not None and not Path(os.fsdecode(write_path)).resolve().is_relative_to(ROOT):
        # Windows null sink is not a filesystem artifact.
        if str(write_path).lower() not in {'nul',os.devnull.lower()}:
            raise RuntimeError('Test write outside public project')
sys.addaudithook(audit)
start=time.monotonic()
sys.path.insert(0,str(ROOT/'tests'))
suite=(unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:]) if len(sys.argv)>1
       else unittest.defaultTestLoader.discover(str(ROOT/'tests')))
with (QA/'unittest.log').open('w',encoding='utf-8') as log:
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
summary={'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
         'skipped':len(result.skipped),'seconds':round(time.monotonic()-start,2),
         'network':'blocked by Python audit hook','data_root':'isolated public .local/qa/runtime',
         'real_eagle':False,'global_registration':False}
(QA/'test-summary.json').write_text(json.dumps(summary,indent=2)+'\n',encoding='utf-8')
print(json.dumps(summary))
if not result.wasSuccessful():
    for case,error in result.failures+result.errors:
        print(str(case)+'\n'+error[-2400:])
raise SystemExit(not result.wasSuccessful())
