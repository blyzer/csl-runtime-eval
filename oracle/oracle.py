#!/usr/bin/env python3
import hashlib, json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from oracle.validation import validate_fixture, validate_query
from collections import defaultdict, deque

QUALITY={"LEXICAL":0,"PROBABLE":1,"DERIVED":2,"EXACT":3,"VERIFIED":4}

def canon_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()

def digest_payload(payload):
    return "sha256:"+hashlib.sha256(canon_bytes(payload)).hexdigest()

class PreparedOracle:
    """Validate and index one fixture for multiple queries; callers must not mutate it."""
    def __init__(self, fx):
        validate_fixture(fx)
        self.fx = fx
        self.ents={e["id"]:e for e in fx["entities"]}
        out=defaultdict(list); inc=defaultdict(list)
        for r in fx["relations"]:
            out[r["subject"]].append(r); inc[r["object"]].append(r)
        for idx in (out, inc):
            for rows in idx.values(): rows.sort(key=lambda r:(r["subject"],r["relation"],r["object"]))
        self.out, self.inc = out, inc

    def execute(self, q):
        validate_query(q)
        fx, ents, out, inc = self.fx, self.ents, self.out, self.inc
        truncated = False
        def ev(node):
            nonlocal truncated
            op=node["op"]
            if op=="RESOLVE":
                if "name" in node: return {e["id"] for e in ents.values() if fx["strings"][e["name_sid"]]==node["name"]}
                return {node["entity_id"]} if node["entity_id"] in ents else set()
            if op=="RELATED":
                base=ev(node["input"]); rel=node.get("relation"); direction=node.get("direction","OUT"); got=set()
                idx=out if direction=="OUT" else inc
                for x in base:
                    for r in idx[x]:
                        if not rel or r["relation"]==rel: got.add(r["object"] if direction=="OUT" else r["subject"])
                return got
            if op=="TRAVERSE":
                base=ev(node["input"]); rels=set(node.get("relations",[])); direction=node.get("direction","OUT"); depth=node.get("max_depth",1); cap=node.get("max_paths",100000); idx=out if direction=="OUT" else inc
                seen=set(base); result=set(); dq=deque((x,0) for x in sorted(base)); steps=0
                while dq and steps<cap:
                    x,d=dq.popleft()
                    if d>=depth: continue
                    for r in idx[x]:
                        if rels and r["relation"] not in rels: continue
                        y=r["object"] if direction=="OUT" else r["subject"]; steps+=1
                        if y not in seen: seen.add(y); result.add(y); dq.append((y,d+1))
                        if steps>=cap:
                            truncated=True
                            break
                return result
            if op=="FILTER":
                base=ev(node["input"]) if "input" in node else set(ents); kind=node.get("kind"); return {x for x in base if not kind or ents[x]["kind"]==kind}
            raise ValueError(f"unsupported op {op}")
        ids=sorted(ev(q))
        selected=set(ids)
        props=[]
        minq=q.get("evidence",{}).get("min_quality")
        epoch=q.get("evidence",{}).get("freshness_epoch")
        for e in fx["evidence"]:
            if e["subject"] in selected or e["object"] in selected:
                if minq and QUALITY[e["quality"]] < QUALITY[minq]: continue
                if epoch is not None and e["freshness_epoch"] != epoch: continue
                props.append({k:e[k] for k in ("proposition","subject","relation","object","polarity","quality","freshness_epoch","lineage")})
        props.sort(key=lambda x:(x["subject"],x["relation"],x["object"],x["proposition"],x["lineage"],x["polarity"],x["quality"],x["freshness_epoch"]))
        payload={"entities":ids,"propositions":props,"knowledge":{"model":"open-world"},"completeness":{"entity_set":"TRUNCATED" if truncated else "COMPLETE" if fx.get("complete",False) else "OBSERVED"}}
        return {"schema":"csl.eval.result/v0.1","query_id":q["query_id"],"snapshot":fx["snapshot"],**payload,"digest":digest_payload(payload)}

def execute(fx, q):
    return PreparedOracle(fx).execute(q)

if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--fixture',required=True);p.add_argument('--query',required=True);a=p.parse_args()
    print(json.dumps(execute(json.load(open(a.fixture)),json.load(open(a.query))),sort_keys=True))
