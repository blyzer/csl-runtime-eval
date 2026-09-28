"""Deterministic, hand-expected semantic cases shared by process conformance."""
from copy import deepcopy
from pathlib import Path
import json
BASE=json.loads((Path(__file__).parent/'golden_fixture.json').read_text())
def query(op='RESOLVE',**kw): return {'schema':'csl.eval.query/v0.1','query_id':'case','op':op,**kw}
def cases():
    fx=deepcopy(BASE)
    fx['strings']+=['D','E','F','B']
    fx['entities'] += [{'id':i,'kind':'TYPE' if i==4 else 'METHOD','name_sid':i-1} for i in range(4,8)]
    fx['relations'] += [{'subject':s,'relation':r,'object':o} for s,r,o in [(3,'CALLS',4),(4,'CALLS',5),(5,'CALLS',6),(3,'CALLS',1),(1,'REFERENCES',4),(2,'CALLS',4)]]
    resolve={'op':'RESOLVE','entity_id':1}
    def c(name,q,ids,fixture=None,**checks):return name,deepcopy(fixture or fx),q,ids,checks
    yield c('resolve-known',query(entity_id=1),[1])
    yield c('resolve-missing',query(entity_id=999),[])
    yield c('resolve-ambiguous',query(name='B'),[2,7])
    yield c('outgoing',query('RELATED',input=resolve),[2,4])
    yield c('incoming',query('RELATED',input=resolve,direction='IN'),[3])
    for depth,ids in [(2,[2,3,4,5]),(4,[2,3,4,5,6]),(8,[2,3,4,5,6])]:
        yield c(f'depth-{depth}',query('TRAVERSE',input=resolve,max_depth=depth),ids)
    yield c('cycle',query('TRAVERSE',input=resolve,relations=['CALLS'],max_depth=8),[2,3,4,5,6])
    yield c('duplicate-paths',query('TRAVERSE',input=resolve,max_depth=2),[2,3,4,5])
    yield c('relation-filter',query('RELATED',input=resolve,relation='CALLS'),[2])
    yield c('entity-filter',query('FILTER',input={'op':'RELATED','input':resolve},kind='TYPE'),[4])
    yield c('scan-kind',query('FILTER',kind='TYPE'),[4])
    yield c('minimum-quality',query(entity_id=2,evidence={'min_quality':'VERIFIED'}),[2],propositions=0)
    yield c('current',query(entity_id=2,evidence={'freshness_epoch':7}),[2],propositions=2)
    yield c('stale',query(entity_id=2,evidence={'freshness_epoch':8}),[2],propositions=0)
    conflict=deepcopy(fx);conflict['evidence'].append({**conflict['evidence'][0],'polarity':'NEGATIVE'})
    yield c('conflict',query(entity_id=2),[2],conflict,propositions=3)
    complete=deepcopy(fx);complete['complete']=True
    yield c('empty-complete',query(entity_id=999),[],complete,completeness='COMPLETE')
    yield c('empty-observed',query(entity_id=999),[],completeness='OBSERVED')
    yield c('ordering',query('FILTER'),list(range(1,8)))
    shuffled=deepcopy(conflict)
    for k in ['entities','relations','evidence']:shuffled[k].reverse()
    yield c('insertion-independent',query(entity_id=2),[2],shuffled,propositions=3)
    yield c('persist-reload',query('FILTER'),list(range(1,8)),json.loads(json.dumps(fx)))
    yield c('cap',query('TRAVERSE',input=resolve,max_depth=8,max_paths=1),[2],completeness='TRUNCATED')
    yield c('incoming-traverse',query('TRAVERSE',input={'op':'RESOLVE','entity_id':6},direction='IN',max_depth=2),[4,5])
    yield c('mixed-relations',query('TRAVERSE',input=resolve,relations=['CALLS','REFERENCES'],max_depth=2),[2,3,4,5])

    # A hash iteration order must not decide which edge wins a traversal cap.
    yield c('multiple-seeds-cap',query('TRAVERSE',input={'op':'FILTER'},max_depth=8,max_paths=1),[],completeness='TRUNCATED')
    seeds=deepcopy(fx)
    seeds['entities'][0]['kind']='TYPE'
    yield c('selected-seeds-cap',query('TRAVERSE',input={'op':'FILTER','kind':'TYPE'},max_depth=8,max_paths=1),[2],seeds,completeness='TRUNCATED')
    sparse=deepcopy(fx)
    sparse['entities'].append({'id':2**64-1,'kind':'TYPE','name_sid':0})
    sparse['relations'].append({'subject':1,'relation':'CONTAINS','object':2**64-1})
    yield c('sparse-u64-id',query(entity_id=2**64-1),[2**64-1],sparse)
    yield c('lexical-relation-cap',query('TRAVERSE',input=resolve,max_paths=2),[2,2**64-1],sparse,completeness='TRUNCATED')
    evidence=deepcopy(fx)
    for quality in ['LEXICAL','PROBABLE','DERIVED','VERIFIED']:
        evidence['evidence'].append({**evidence['evidence'][0],'quality':quality})
    yield c('typed-quality-order',query(entity_id=2),[2],evidence,propositions=6)

def malformed():
    q=query(entity_id=1)
    for name,mutate in [
        ('malformed-fixture',lambda f:f.pop('entities')),
        ('invalid-relation',lambda f:f['relations'][0].update(relation='BOGUS')),
        ('invalid-reference',lambda f:f['relations'][0].update(object=999)),
        ('incompatible-fixture',lambda f:f.update(schema='v9')),
        ('duplicate-id',lambda f:f['entities'].append(f['entities'][0])),
        ('invalid-name-sid',lambda f:f['entities'][0].update(name_sid=999))]:
        f=deepcopy(BASE);mutate(f);yield name,f,q
    yield 'malformed-query',BASE,query('RELATED')
    yield 'incompatible-query',BASE,{**q,'schema':'v9'}
    yield 'invalid-query-relation',BASE,query('RELATED',input={'op':'RESOLVE','entity_id':1},relation='BOGUS')

    yield 'nested-version',BASE,query('RELATED',input={'op':'RESOLVE','entity_id':1,'schema':'v9'})
    yield 'unused-invalid-input',BASE,query(entity_id=1,input={'op':'RELATED'})
    yield 'invalid-bound',BASE,query('TRAVERSE',input={'op':'RESOLVE','entity_id':1},max_depth=0)

    for name,mutate in [
        ('unknown-fixture-field',lambda f:f.update(extra=True)),
        ('unknown-entity-field',lambda f:f['entities'][0].update(extra=True)),
        ('unknown-edge-field',lambda f:f['relations'][0].update(extra=True)),
        ('unknown-evidence-field',lambda f:f['evidence'][0].update(extra=True)),
        ('zero-id',lambda f:f['entities'][0].update(id=0)),
        ('overflow-id',lambda f:f['entities'][0].update(id=2**64)),
        ('overflow-name-sid',lambda f:f['entities'][0].update(name_sid=2**32)),
        ('invalid-container',lambda f:f['entities'][0].update(container=999)),
        ('invalid-quality',lambda f:f['evidence'][0].update(quality='BOGUS')),
        ('zero-proposition',lambda f:f['evidence'][0].update(proposition=0)),
        ('overflow-lineage',lambda f:f['evidence'][0].update(lineage=2**32)),
    ]:
        f=deepcopy(BASE);mutate(f);yield name,f,q
