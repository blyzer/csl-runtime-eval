#!/usr/bin/env python3
"""Process-only orchestration. Oracle correctness precedes accepted measurements."""
import argparse, hashlib, json, os, platform, shutil, statistics, subprocess, sys, tempfile, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from oracle.oracle import execute, digest_payload
from oracle.validation import VALIDATORS, validate_fixture
from harness.generate import generate, PRESETS
CANDIDATES=('rust','zig','hybrid')
SEED=20260927

def load(p):return json.loads(Path(p).read_text())
def save(p,obj):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,indent=2)+'\n')
def tool(name):
    if name=='zig' and os.environ.get('ZIG'):return os.environ['ZIG']
    found=shutil.which(name)
    if found:return found
    local=ROOT/'.venv/bin'/name
    return str(local) if local.exists() else None

def environment():
    tools={}
    for name in ['python3','rustc','cargo','zig','git']:
        path=tool(name)
        if not path:tools[name]={'state':'NOT AVAILABLE','path':None,'version':None};continue
        p=subprocess.run([path,'version' if name=='zig' else '--version'],capture_output=True,text=True)
        tools[name]={'state':'PASS' if p.returncode==0 else 'FAIL','path':path,'version':(p.stdout+p.stderr).strip()}
    return {'tools':tools,'os':platform.platform(),'architecture':platform.machine(),'uname':list(platform.uname()),'hardware':platform.processor() or None,'cpu_count':os.cpu_count(),'runtime_python':sys.executable,'sdk_workaround':(ROOT/'.venv/bin/xcrun').exists()}

def available(candidate):return all(tool(t) for t in {'rust':['rustc','cargo'],'zig':['zig'],'hybrid':['rustc','cargo','zig']}[candidate])
def executable(c):return ROOT/(f'prototypes/zig/zig-out/bin/csl-eval-zig' if c=='zig' else f'prototypes/{c}/target/release/csl-eval-{c}')
def build(c):
    if not available(c):return {'candidate':c,'state':'SKIP','reason':'required toolchain NOT AVAILABLE'}
    env=os.environ.copy()
    if tool('zig'):env['ZIG']=tool('zig')
    cmd=[tool('zig'),'build','-Doptimize=ReleaseFast'] if c=='zig' else [tool('cargo'),'build','--release','--manifest-path',str(ROOT/f'prototypes/{c}/Cargo.toml')]
    start=time.perf_counter_ns();p=subprocess.run(cmd,cwd=ROOT/f'prototypes/{c}',env=env,capture_output=True,text=True);elapsed=time.perf_counter_ns()-start
    log=ROOT/f'results/raw/build-{c}.log';log.write_text(p.stdout+p.stderr)
    record={'candidate':c,'state':'PASS' if p.returncode==0 else 'FAIL','elapsed_ns':elapsed,'build_kind':'existing-cache','artifact_bytes':executable(c).stat().st_size if p.returncode==0 else None,'command':cmd}
    save(ROOT/f'results/raw/build-{c}.json',record);return record

def invoke(cmd):
    with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
        start=time.perf_counter_ns();p=subprocess.Popen([str(x) for x in cmd],stdout=out,stderr=err)
        if hasattr(os,'wait4'):
            _,status,usage=os.wait4(p.pid,0);p.returncode=os.waitstatus_to_exitcode(status)
            rss=int(usage.ru_maxrss*(1 if sys.platform=='darwin' else 1024))
        else:p.wait();rss=None
        elapsed=time.perf_counter_ns()-start;out.seek(0);err.seek(0)
        output=out.read();diagnostic=err.read().decode(errors='replace')
    return p.returncode,output,diagnostic,elapsed,rss

def process(c,command):
    code,out,err,ns,rss=invoke([executable(c),*command])
    if code:raise RuntimeError(f'{c} exited {code}: {err}')
    return json.loads(out),ns,rss

def validate(which='all'):
    if which in ('all','schemas'):
        import jsonschema
        for v in VALIDATORS.values():jsonschema.Draft202012Validator.check_schema(v.schema)
        for name,path in [('fixture','tests/golden_fixture.json'),('query','tests/golden_query.json'),('result','oracle/expected/golden_result.json')]:VALIDATORS[name].validate(load(ROOT/path))
    if which in ('all','oracle'):
        actual=execute(load(ROOT/'tests/golden_fixture.json'),load(ROOT/'tests/golden_query.json'))
        if actual!=load(ROOT/'oracle/expected/golden_result.json'):raise ValueError('golden oracle mismatch')
    return {'state':'PASS','validation':which}

def conformance(candidates=(),profile=False):
    if profile and any(c not in ("rust", "zig") for c in candidates):raise ValueError("phase conformance requires pure candidates")
    from tests.cases import cases,malformed
    records=[]
    with tempfile.TemporaryDirectory() as d:
        f,q=Path(d)/'fixture.json',Path(d)/'query.json'
        for name,fx,query,ids,checks in cases():
            expected=execute(fx,query)
            assert expected['entities']==ids,name
            if 'propositions' in checks:assert len(expected['propositions'])==checks['propositions'],name
            if 'completeness' in checks:assert expected['completeness']['entity_set']==checks['completeness'],name
            records.append({'candidate':'oracle','case':name,'state':'PASS','digest':expected['digest']})
            for c in candidates:
                if not available(c) or not executable(c).exists():records.append({'candidate':c,'case':name,'state':'SKIP'});continue
                save(f,fx);save(q,query)
                try:
                    command=['workload','--id','W2','--corpus',str(f),'--params',json.dumps({'query':query}),'--profile'] if profile else ['query','--fixture',str(f),'--query',str(q)]
                    actual,ns,_=process(c,command)
                    if profile:actual,_=unpack_profile(actual,ns)
                    assert actual==expected,'normalized result differs'
                    # Persist/reload and insertion-order independence are checked through a second process.
                    reversed_fx={**fx,**{key:list(reversed(fx[key])) for key in ['entities','relations','evidence']}}
                    save(f,reversed_fx);again,ns,_=process(c,command)
                    if profile:again,_=unpack_profile(again,ns)
                    assert again==expected,'insertion order or reload mismatch'
                    records.append({'candidate':c,'case':name,'state':'PASS','digest':actual['digest']})
                except Exception as e:records.append({'candidate':c,'case':name,'state':'FAIL','error':str(e)})
        for name,fx,query in malformed():
            try:execute(fx,query)
            except Exception:records.append({'candidate':'oracle','case':name,'state':'PASS','rejected':True})
            else:records.append({'candidate':'oracle','case':name,'state':'FAIL'})
            save(f,fx);save(q,query)
            for c in candidates:
                if not available(c) or not executable(c).exists():records.append({'candidate':c,'case':name,'state':'SKIP'});continue
                command=['workload','--id','W2','--corpus',str(f),'--params',json.dumps({'query':query}),'--profile'] if profile else ['query','--fixture',str(f),'--query',str(q)]
                code,*_=invoke([executable(c),*command]);records.append({'candidate':c,'case':name,'state':'PASS' if code else 'FAIL','rejected':bool(code)})
    save(ROOT/('results/conformance/phases.json' if profile else 'results/conformance/latest.json'),records)
    return {'state':'FAIL' if any(r['state']=='FAIL' for r in records) else 'SKIP' if any(r['state']=='SKIP' for r in records) else 'PASS','counts':{s:sum(r['state']==s for r in records) for s in ['PASS','FAIL','SKIP']}}

def queries(workload):
    def q(name,op,**kw):return {'schema':'csl.eval.query/v0.1','query_id':name,'op':op,**kw}
    base={'op':'RESOLVE','entity_id':1}
    if workload=='W1':return [q('lookup-first','RESOLVE',entity_id=1),q('lookup-missing','RESOLVE',entity_id=2**64-1),q('scan-type','FILTER',kind='TYPE')]
    return [q('outgoing','RELATED',input=base),q('incoming','RELATED',input=base,direction='IN'),*[q(f'depth-{d}','TRAVERSE',input=base,max_depth=d,max_paths=100000) for d in [2,4,8]],q('mixed-relations','TRAVERSE',input=base,relations=['CALLS','REFERENCES'],max_depth=4)]

def corpus_path(corpus):
    return ROOT/f'corpus/synthetic/{corpus}.json' if corpus in PRESETS else Path(corpus).resolve()

def profile_metadata(envelope, elapsed_ns):
    """Reject missing/unsupported timing metadata before accepting a measurement."""
    required = {'profile_schema', 'representation', 'phases_ns', 'result'}
    optional = {'phase_detail_ns'}
    if not isinstance(envelope, dict) or not required <= set(envelope) or set(envelope) - required - optional:
        raise ValueError('invalid profile envelope')
    fields = ('profile_schema', 'representation', 'phases_ns') + (('phase_detail_ns',) if 'phase_detail_ns' in envelope else ())
    for field in fields:
        import jsonschema
        jsonschema.validate(envelope[field], VALIDATORS['benchmark-record'].schema['properties'][field])
    phases = envelope['phases_ns']
    if any(type(value) is not int for value in phases.values()) or sum(phases.values()) > elapsed_ns:
        raise ValueError('invalid phase duration or phase sum exceeds process elapsed time')
    if 'phase_detail_ns' in envelope:
        detail = envelope['phase_detail_ns']
        if any(type(value) is not int for value in detail.values()):
            raise ValueError('invalid phase detail duration')
        if detail['decode'] + detail['construct'] != phases['load']:
            raise ValueError('decode+construct must equal load')
        if detail['materialize'] + detail['encode'] != phases['result']:
            raise ValueError('materialize+encode must equal result')
    return {key: envelope[key] for key in fields}

def unpack_profile(envelope, elapsed_ns):
    metadata = profile_metadata(envelope, elapsed_ns)
    VALIDATORS['result'].validate(envelope['result'])
    return envelope['result'], metadata

def benchmark(workload,candidate,corpus,repeat,profile=False):
    if repeat < 1:raise ValueError("repeat must be positive")
    if profile and candidate not in ("rust", "zig"):raise ValueError("phase profiling is available only for pure Rust/Zig")
    if not available(candidate):return {'state':'SKIP','candidate':candidate,'reason':'toolchain NOT AVAILABLE'}
    if not executable(candidate).exists():raise RuntimeError(f'build {candidate} first')
    path=corpus_path(corpus);fx=load(path);validate_fixture(fx);env=environment()
    metadata=load(path.with_suffix('.meta.json')) if path.with_suffix('.meta.json').exists() else {}
    dirty=subprocess.run([tool('git'),'status','--porcelain'],cwd=ROOT,capture_output=True,text=True).stdout.strip()!='' if tool('git') else None
    commit=subprocess.run([tool('git'),'rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True) if tool('git') else None
    records=[]
    source_hash=source_digest();corpus_hash=hashlib.sha256(path.read_bytes()).hexdigest()
    artifact_hash=hashlib.sha256(executable(candidate).read_bytes()).hexdigest()
    destination=ROOT/"results"/("phases" if profile else "")/workload.lower()
    for query in queries(workload):
        expected=execute(fx,query)
        for iteration in range(repeat):
            command=['workload','--id',workload,'--corpus',str(path),'--params',json.dumps({'query':query})]
            if profile:command.append('--profile')
            actual,ns,rss=process(candidate,command)
            measurement={}
            if profile:actual,measurement=unpack_profile(actual,ns)
            equal=actual==expected
            rec={'schema':'csl.eval.benchmark/v0.1','candidate':candidate,'candidate_commit':commit.stdout.strip() if commit is not None and commit.returncode==0 else None,'source_digest':source_hash,'working_tree_dirty':dirty,'workload':workload,'query':query['query_id'],'corpus':str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),'corpus_digest':corpus_hash,'seed':metadata.get('seed'),'shape':metadata.get('shape'),'iteration':iteration,'mode':'cold-process','elapsed_ns':ns,'rss_peak_bytes':rss,'bytes_per_entity':rss/len(fx['entities']) if rss is not None and fx['entities'] else None,'memory_scope':'whole process, includes JSON, indexes and result','artifact_bytes':executable(candidate).stat().st_size,'lookup_count':1 if query['op']=='RESOLVE' else 0,'result_digest':actual.get('digest'),'conformance':equal,'compiler':env['tools']['rustc' if candidate=='rust' else 'zig']['version'],'toolchains':env['tools'],'os':env['os'],'architecture':env['architecture'],'hardware':env['hardware']}
            rec.update(measurement);rec['artifact_digest']=artifact_hash;rec['sdk_workaround']=env['sdk_workaround']
            VALIDATORS['benchmark-record'].validate(rec);records.append(rec)
            if not equal:save(destination/f'failure-{candidate}.json',records);raise RuntimeError(f'{candidate} digest/result mismatch; measurement rejected')
    save(destination/f'{candidate}-{path.stem}.json',records)
    return {'state':'PASS','candidate':candidate,'workload':workload,'records':len(records)}

def source_digest():
    h=hashlib.sha256()
    for base in ['prototypes','oracle','schemas','harness']:
        for p in sorted((ROOT/base).rglob('*')):
            if p.is_file() and not set(p.parts)&{'target','.zig-cache','zig-out','__pycache__'} and p.suffix in ['.rs','.zig','.json','.py','.toml','.h','.lock']:
                h.update(str(p.relative_to(ROOT)).encode());h.update(p.read_bytes())
    return h.hexdigest()

def boundary(repeat):
    if any(not available(c) or not executable(c).exists() for c in CANDIDATES):return {'state':'SKIP','reason':'all three candidate controls required'}
    rows=[];env=environment()
    for size in [0,1024,65536,1048576,16777216]:
        group=[]
        for candidate,strategy in [('rust','pure-rust'),('zig','pure-zig'),('hybrid','A'),('hybrid','B')]:
            cmd=['boundary','--size',str(size),'--repeat',str(repeat)]
            if candidate=='hybrid':cmd+=['--strategy',strategy]
            result,_,_=process(candidate,cmd)
            assert result['result_digest']=='sha256:'+hashlib.sha256(bytes([42])*size).hexdigest()
            result.update(candidate=candidate,environment=env,mode='warm-process',source_digest=source_digest(),boundary_tax_ns=None)
            group.append(result)
        pure=min(statistics.median(r['latency_ns']) for r in group[:2])
        for r in group[2:]:r['boundary_tax_ns']=statistics.median(r['latency_ns'])-pure
        rows+=group
    save(ROOT/'results/w10/boundary.json',rows)
    commit=subprocess.run([tool('git'),'rev-parse','HEAD'],cwd=ROOT,capture_output=True,text=True) if tool('git') else None
    flat=[]
    for row in rows:
        c=row['candidate']
        for iteration,ns in enumerate(row['latency_ns']):
            record={'schema':'csl.eval.benchmark/v0.1','candidate':c,'candidate_commit':commit.stdout.strip() if commit is not None and commit.returncode==0 else None,'source_digest':row['source_digest'],'workload':'W10','corpus':f"echo-{row['payload_bytes']}B",'seed':None,'iteration':iteration,'mode':'warm-process','elapsed_ns':ns,'rss_peak_bytes':None,'artifact_bytes':executable(c).stat().st_size,'result_digest':row['result_digest'],'conformance':True,'compiler':env['tools']['rustc' if c=='rust' else 'zig']['version'],'toolchains':env['tools'],'os':env['os'],'architecture':env['architecture'],'hardware':env['hardware'],'strategy':row['strategy'],'calls_per_query':row['calls_per_query'],'bytes_copied':row['bytes_copied'],'output_allocations_per_call':row['output_allocations_per_call'],'boundary_tax_ns':row['boundary_tax_ns'],'cancellation':'NOT IMPLEMENTED'}
            VALIDATORS['benchmark-record'].validate(record);flat.append(record)
    save(ROOT/'results/w10/records.json',flat)
    return {'state':'PASS','groups':len(rows),'records':len(flat)}

def report():
    return {'gate':'gate1','state':'PARTIAL','decision':None,'reason':'SMOKE correctness/build/ABI evidence only. S/M controlled measurements, representation tuning and remaining Gate workloads pending.','conformance':load(ROOT/'results/conformance/latest.json') if (ROOT/'results/conformance/latest.json').exists() else None}

def main():
    p=argparse.ArgumentParser();sp=p.add_subparsers(dest='cmd',required=True)
    v=sp.add_parser('validate');v.add_argument('--only',choices=['all','schemas','oracle'],default='all')
    pp=sp.add_parser('prepare');pp.add_argument('--preset','--corpus',choices=PRESETS,default='SMOKE');pp.add_argument('--seed',type=int,default=SEED)
    po=sp.add_parser('oracle');po.add_argument('--fixture',required=True);po.add_argument('--query',required=True)
    sp.add_parser('w11');sp.add_parser('env');b=sp.add_parser('build');b.add_argument('candidate',choices=CANDIDATES)
    c=sp.add_parser('conformance');c.add_argument('--candidate',action='append',choices=CANDIDATES,default=[]);c.add_argument('--profile',action='store_true')
    for cmd in ['run','compare']:
        r=sp.add_parser(cmd);r.add_argument('workload',choices=['W1','W2']);r.add_argument('--corpus',default='SMOKE');r.add_argument('--repeat',type=int,default=1);r.add_argument('--profile',action='store_true',help='pure Rust/Zig phase timings; compare omits hybrid')
        if cmd=='run':r.add_argument('--candidate',choices=CANDIDATES,required=True)
    w=sp.add_parser('w10');w.add_argument('--repeat',type=int,default=10)
    sp.add_parser('report').add_argument('gate',choices=['gate1'])
    args=p.parse_args()
    if getattr(args,'repeat',1)<1:p.error('repeat must be positive')
    if args.cmd=='validate':result=validate(args.only)
    elif args.cmd=='prepare':
        n,m=PRESETS[args.preset];generate(corpus_path(args.preset),n,m,args.seed);result={'state':'PASS','corpus':args.preset,'seed':args.seed}
    elif args.cmd=='oracle':result=execute(load(args.fixture),load(args.query))
    elif args.cmd=='env':result=environment();save(ROOT/'results/raw/environment.json',result)
    elif args.cmd=='build':result=build(args.candidate)
    elif args.cmd=='conformance':result=conformance(args.candidate,args.profile)
    elif args.cmd=='run':result=benchmark(args.workload,args.candidate,args.corpus,args.repeat,args.profile)
    elif args.cmd=='compare':result=[benchmark(args.workload,c,args.corpus,args.repeat,args.profile) for c in (('rust','zig') if args.profile else CANDIDATES)]
    elif args.cmd=='w10':result=boundary(args.repeat)
    elif args.cmd=='w11':
        from harness.builds import measure
        result=measure()
    else:result=report();save(ROOT/'results/gate1.json',result)
    print(json.dumps(result,sort_keys=True))
    if isinstance(result,dict) and result.get('state')=='FAIL':sys.exit(1)
if __name__=='__main__':main()
