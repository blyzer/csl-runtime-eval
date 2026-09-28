"""W11 build evidence; fresh output directories, explicit cache scope."""
import os, subprocess, tempfile, time
from pathlib import Path
from harness.benchctl import ROOT, available, environment, save, source_digest, tool

def measure():
    records=[]
    for candidate in ('rust','zig','hybrid'):
        if not available(candidate):
            records.append({'candidate':candidate,'state':'SKIP','reason':'toolchain NOT AVAILABLE'});continue
        with tempfile.TemporaryDirectory(prefix='csl-w11-') as temp:
            out=Path(temp);env=os.environ.copy()
            if tool('zig'):env['ZIG']=tool('zig')
            if candidate=='zig':
                cmd=[tool('zig'),'build','-Doptimize=ReleaseFast','--prefix',str(out/'install'),'--cache-dir',str(out/'cache')]
                artifact=out/'install/bin/csl-eval-zig'
            else:
                cmd=[tool('cargo'),'build','--release','--manifest-path',str(ROOT/f'prototypes/{candidate}/Cargo.toml'),'--target-dir',str(out/'target')]
                artifact=out/f'target/release/csl-eval-{candidate}'
            for mode in ['clean-output','no-op']:
                start=time.perf_counter_ns();p=subprocess.run(cmd,cwd=ROOT/f'prototypes/{candidate}',env=env,capture_output=True,text=True);elapsed=time.perf_counter_ns()-start
                (ROOT/f'results/raw/w11-{candidate}-{mode}.log').write_text(p.stdout+p.stderr)
                records.append({'candidate':candidate,'workload':'W11','state':'PASS' if p.returncode==0 else 'FAIL','mode':mode,'elapsed_ns':elapsed,'artifact_bytes':artifact.stat().st_size if p.returncode==0 else None,'cache_scope':'fresh candidate output for clean-output; downloaded dependencies and Zig global/kernel caches may be warm','environment':environment(),'source_digest':source_digest()})
                if p.returncode:break
    save(ROOT/'results/w11/builds.json',records)
    return {'state':'FAIL' if any(r['state']=='FAIL' for r in records) else 'PASS','records':len(records)}
