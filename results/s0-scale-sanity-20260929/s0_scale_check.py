import json, sys, time, tempfile, os, resource
from pathlib import Path
sys.path.insert(0,'.')
from harness.s0 import Client, context_of, std_queries
from harness.benchctl import executable
from harness.generate import generate
from oracle.session_model import SessionModel
from oracle.compact import CompactOracle
fx_path=Path('corpus/synthetic/S-s0.json')
if not fx_path.exists(): generate(fx_path,100_000,1_000_000,20260928,'mixed')
t=time.time(); fx=json.loads(fx_path.read_text()); model=SessionModel.from_fixture(fx); print('model built',round(time.time()-t,1),'s',flush=True)
t=time.time(); want,size=model.state_digest(); print('model digest',want[:24],size,'bytes',round(time.time()-t,1),'s',flush=True)
batch=[{'op':'ADD_ENTITY','id':100_001,'kind':'TYPE','name':'added','container':None},{'op':'ADD_RELATION','subject':100_001,'relation':'CALLS','object':1}]
model2=model.copy(); model2.mutate(batch); want2,_=model2.state_digest(); print('model digest after mutate',want2[:24],flush=True)
q={'schema':'csl.eval.query/v0.1','query_id':'depth-4','op':'TRAVERSE','input':{'op':'RESOLVE','entity_id':1},'max_depth':4,'max_paths':100000}
q2={'schema':'csl.eval.query/v0.1','query_id':'in','op':'RELATED','input':{'op':'RESOLVE','entity_id':1},'direction':'IN'}
co=CompactOracle(fx_path); ref={}
for qq in (q,q2):
    co.execute_to(qq,'/tmp/ref.json'); ref[qq['query_id']]=Path('/tmp/ref.json').read_bytes()
del model
out={}
ms=lambda ns: round(ns/1e6,1)
for name in ('rust','zig'):
    exe=executable(name); repo=Path(tempfile.mkdtemp(prefix=f's0scale-{name}-'))
    r={}
    c=Client(exe,repo,max_line_bytes=1<<20)
    t0=time.perf_counter_ns(); o=c.open_fixture(fx_path); r['open_ms']=ms(o.ns); r['strategy']=o.message['strategy']
    d=c.call('state_digest'); r['digest_ok']=d.message['state_digest']==want; r['state_digest_ms_candidate']=d.message['state_digest_ms']; r['state_digest_ms_host']=ms(d.ns); r['bytes_processed']=d.message['bytes_processed']
    for qq in (q,q2):
        resp=c.call('query',query=qq); r[f'query_{qq["query_id"]}_ok']=resp.result_bytes()==ref[qq['query_id']]; r[f'query_{qq["query_id"]}_ms']=ms(resp.ns); r[f'query_{qq["query_id"]}_chunked']=resp.payload is not None
    s=c.call('snapshot'); r['snapshot_ms']=ms(s.ns); sid=s.message['snapshot_id']; r['snapshot_bytes']=os.path.getsize(repo/(sid+'.snap')) if (repo/(sid+'.snap')).exists() else [p.stat().st_size for p in repo.iterdir()]
    m=c.call('mutate',batch=batch); r['mutate_ms_full_rebuild']=ms(m.ns); r['mutate_ok']=m.ok and m.generation==1
    d2=c.call('state_digest'); r['digest_after_mutate_ok']=d2.message['state_digest']==want2
    st=c.call('stats'); r['stats']={k:st.message['stats'][k] for k in ('live_heap_bytes','peak_heap_bytes','allocations_total')}
    rs=c.call('restore',snapshot_id=sid); r['restore_ms']=ms(rs.ns); r['restore_generation']=rs.generation
    d3=c.call('state_digest'); r['digest_after_restore_ok']=d3.message['state_digest']==want
    c.close()
    # cold restore: process start -> open(empty) -> restore -> first query
    t0=time.perf_counter_ns(); c=Client(exe,repo); o=c.open_empty(context_of(fx)); t_open=time.perf_counter_ns()-t0
    rs=c.call('restore',snapshot_id=sid); qr=c.call('query',query=q)
    r['cold']={'spawn_to_open_ms':ms(t_open),'restore_ms':ms(rs.ns),'first_query_ms':ms(qr.ns),'ttfq_ms':ms(time.perf_counter_ns()-t0),'query_ok':qr.result_bytes()==ref['depth-4'],'digest_ok':c.call('state_digest').message['state_digest']==want}
    c.close()
    out[name]=r; print(name,json.dumps(r),flush=True)
    r['host_note']='local Mac, exploratory, SDK overlay; conformance check only'
Path('/tmp/s0-scale.json').write_text(json.dumps({'model_digest':want,'model_digest_after_mutate':want2,'results':out},indent=2))
