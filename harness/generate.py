#!/usr/bin/env python3
"""Streaming deterministic generator; memory O(entity count), not O(edge count)."""
import argparse, json, random
from pathlib import Path
PRESETS = {'SMOKE': (1000,5000), 'S': (100_000,1_000_000), 'M': (1_000_000,10_000_000)}
REL = ['CALLS','REFERENCES','IMPLEMENTS','OVERRIDES','CONTAINS']
def generate(path,n,m,seed,shape='mixed'):
    def edges():
        r=random.Random(seed)
        for i in range(m):
            s,o=r.randint(1,n),r.randint(1,n)
            if shape=='cycle': s,o=i%n+1,(i+1)%n+1
            if shape=='fanout': s,o=1,i%n+1
            yield {'subject':s,'relation':REL[i%5],'object':o}
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('w') as f:
        f.write('{"schema":"csl.eval.fixture/v0.1","snapshot":"S0","epoch":1,')
        def array(name,items):
            f.write(json.dumps(name)+':[')
            for i,item in enumerate(items):
                if i:f.write(',')
                f.write(json.dumps(item,separators=(',',':')))
            f.write(']')
        array('strings',(f'sym{i}' for i in range(n)));f.write(',')
        array('entities',({'id':i+1,'kind':'METHOD' if i%3 else 'TYPE','name_sid':i,'container':None} for i in range(n)));f.write(',')
        array('relations',edges());f.write(',')
        array('evidence',({'proposition':i+1,**e,'polarity':'POSITIVE','quality':'EXACT','freshness_epoch':1,'lineage':i%8} for i,e in enumerate(edges())))
        f.write('}')
    path.with_suffix('.meta.json').write_text(json.dumps({'entities':n,'edges':m,'seed':seed,'shape':shape},sort_keys=True)+'\n')
def main():
    p=argparse.ArgumentParser();p.add_argument('--preset',choices=PRESETS,default='SMOKE');p.add_argument('--entities',type=int);p.add_argument('--edges',type=int);p.add_argument('--seed',type=int,default=20260927);p.add_argument('--output','--out',required=True);p.add_argument('--shape',choices=['mixed','cycle','fanout'],default='mixed');a=p.parse_args()
    n,m=PRESETS[a.preset];n=a.entities if a.entities is not None else n;m=a.edges if a.edges is not None else m
    if n<1 or m<0:p.error('entities >= 1 and edges >= 0 required')
    generate(a.output,n,m,a.seed,a.shape)
    print(json.dumps({'entities':n,'edges':m,'seed':a.seed,'output':a.output}))
if __name__=='__main__':main()
