mod model;
use model::{Edge, Entity, Evidence, Fixture};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
const REL: [&str; 5] = ["CALLS", "REFERENCES", "IMPLEMENTS", "OVERRIDES", "CONTAINS"];
const KIND: [&str; 7] = [
    "TYPE", "METHOD", "FUNCTION", "FIELD", "MODULE", "FILE", "VARIABLE",
];
const QUALITY: [&str; 5] = ["LEXICAL", "PROBABLE", "DERIVED", "EXACT", "VERIFIED"];
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
fn id(v: &Value) -> Result<u64> {
    v.as_u64()
        .filter(|n| *n > 0)
        .ok_or_else(|| "invalid id".into())
}
fn member(v: &Value, values: &[&str]) -> bool {
    v.as_str().is_some_and(|s| values.contains(&s))
}
fn fields(v: &Value, allowed: &[&str]) -> Result<()> {
    let map = v.as_object().ok_or("expected object")?;
    if map.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err("unknown field".into());
    }
    Ok(())
}
fn valid_query(q: &Value) -> Result<()> {
    fields(
        q,
        &[
            "schema",
            "query_id",
            "op",
            "entity_id",
            "name",
            "input",
            "relation",
            "relations",
            "direction",
            "max_depth",
            "max_paths",
            "kind",
            "evidence",
        ],
    )?;
    if q.get("schema").is_some_and(|s| s != "csl.eval.query/v0.1")
        || q.get("query_id").is_some_and(|s| !s.is_string())
    {
        return Err("invalid query metadata".into());
    }
    for key in ["max_depth", "max_paths"] {
        if q.get(key).is_some_and(|v| {
            v.as_u64()
                .is_none_or(|n| n == 0 || (key == "max_depth" && n > 64))
        }) {
            return Err("invalid query bound".into());
        }
    }
    if q.get("entity_id").is_some_and(|v| id(v).is_err())
        || q.get("name").is_some_and(|v| !v.is_string())
    {
        return Err("invalid resolve".into());
    }
    if q["op"] == "RESOLVE" && (q.get("name").is_some() == q.get("entity_id").is_some()) {
        return Err("resolve requires exactly one selector".into());
    }
    if let Some(e) = q.get("evidence") {
        fields(e, &["min_quality", "freshness_epoch"])?;
        if e.get("min_quality").is_some_and(|v| !member(v, &QUALITY))
            || e.get("freshness_epoch")
                .is_some_and(|v| v.as_u64().is_none())
        {
            return Err("invalid evidence predicate".into());
        }
    }
    if !member(&q["op"], &["RESOLVE", "RELATED", "TRAVERSE", "FILTER"])
        || q.get("relation").is_some_and(|v| !member(v, &REL))
        || q.get("kind").is_some_and(|v| !member(v, &KIND))
        || q.get("direction")
            .is_some_and(|v| !member(v, &["OUT", "IN"]))
        || q.get("relations").is_some_and(|v| {
            !v.as_array()
                .is_some_and(|a| a.iter().all(|r| member(r, &REL)))
        })
    {
        return Err("invalid query enum".into());
    }
    if (q["op"] == "RELATED" || q["op"] == "TRAVERSE") && q.get("input").is_none() {
        return Err("missing input".into());
    }
    if let Some(input) = q.get("input") {
        valid_query(input)?;
    }
    Ok(())
}
type Set = HashSet<u64>;
type Index = HashMap<u64, Vec<Edge>>;
struct Selection {
    ids: Vec<u64>,
    props: Vec<Evidence>,
    truncated: bool,
}
struct Store<'a> {
    fx: &'a Fixture,
    entities: HashMap<u64, Entity>,
    out: Index,
    inc: Index,
}
impl<'a> Store<'a> {
    fn new(fx: &'a Fixture) -> Result<Self> {
        if fx.schema != "csl.eval.fixture/v0.1" {
            return Err("invalid fixture schema".into());
        }
        let mut entities = HashMap::new();
        for e in &fx.entities {
            if e.id == 0
                || e.name_sid as usize >= fx.strings.len()
                || entities.insert(e.id, *e).is_some()
            {
                return Err("invalid entity".into());
            }
        }
        for e in &fx.entities {
            if e.container.is_some_and(|id| !entities.contains_key(&id)) {
                return Err("invalid container".into());
            }
        }
        let mut out: Index = HashMap::new();
        let mut inc: Index = HashMap::new();
        for e in &fx.relations {
            if !entities.contains_key(&e.subject) || !entities.contains_key(&e.object) {
                return Err("invalid edge".into());
            }
            out.entry(e.subject).or_default().push(*e);
            inc.entry(e.object).or_default().push(*e);
        }
        for e in &fx.evidence {
            if e.proposition == 0
                || !entities.contains_key(&e.subject)
                || !entities.contains_key(&e.object)
            {
                return Err("invalid evidence".into());
            }
        }
        for idx in [&mut out, &mut inc] {
            for rows in idx.values_mut() {
                rows.sort_unstable_by_key(|e| (e.subject, e.relation.as_str(), e.object));
            }
        }
        Ok(Self {
            fx,
            entities,
            out,
            inc,
        })
    }
    fn eval(&self, q: &Value, truncated: &mut bool) -> Result<Set> {
        valid_query(q)?;
        let op = q["op"].as_str().ok_or("missing op")?;
        if q.get("relation").is_some_and(|v| !member(v, &REL))
            || q.get("kind").is_some_and(|v| !member(v, &KIND))
            || q.get("direction")
                .is_some_and(|v| !member(v, &["OUT", "IN"]))
        {
            return Err("invalid query enum".into());
        }
        if op == "RESOLVE" {
            if let Some(name) = q["name"].as_str() {
                return Ok(self
                    .fx
                    .entities
                    .iter()
                    .filter(|e| self.fx.strings[e.name_sid as usize] == name)
                    .map(|e| e.id)
                    .collect());
            }
            let i = id(&q["entity_id"])?;
            return Ok(self
                .entities
                .contains_key(&i)
                .then_some(i)
                .into_iter()
                .collect());
        }
        let base = if op == "FILTER" && q.get("input").is_none() {
            self.fx.entities.iter().map(|e| e.id).collect()
        } else {
            self.eval(&q["input"], truncated)?
        };
        let mut seeds: Vec<_> = base.iter().copied().collect();
        seeds.sort_unstable();
        if op == "FILTER" {
            return Ok(seeds
                .into_iter()
                .filter(|i| {
                    q.get("kind")
                        .is_none_or(|k| self.entities[i].kind.as_str() == k.as_str().unwrap())
                })
                .collect());
        }
        if op != "RELATED" && op != "TRAVERSE" {
            return Err("unsupported op".into());
        }
        let depth = q["max_depth"].as_u64().unwrap_or(1);
        let cap = q["max_paths"].as_u64().unwrap_or(100000);
        if depth == 0 || depth > 64 || cap == 0 {
            return Err("invalid bound".into());
        }
        if q.get("relations").is_some_and(|rs| {
            !rs.as_array()
                .is_some_and(|rs| rs.iter().all(|r| member(r, &REL)))
        }) {
            return Err("invalid relations".into());
        }
        let incoming = q["direction"] == "IN";
        let idx = if incoming { &self.inc } else { &self.out };
        let mut result = Set::new();
        let mut seen = base;
        let mut queue: Vec<_> = seeds.into_iter().map(|i| (i, 0)).collect();
        let mut pos = 0;
        let mut steps = 0;
        while pos < queue.len() {
            let (i, d) = queue[pos];
            pos += 1;
            if d >= depth && op == "TRAVERSE" {
                continue;
            }
            for e in idx.get(&i).into_iter().flatten() {
                if op == "RELATED"
                    && q.get("relation")
                        .is_some_and(|r| r.as_str() != Some(e.relation.as_str()))
                {
                    continue;
                }
                if op == "TRAVERSE"
                    && q["relations"].as_array().is_some_and(|rs| {
                        !rs.is_empty()
                            && !rs.iter().any(|r| r.as_str() == Some(e.relation.as_str()))
                    })
                {
                    continue;
                }
                let y = if incoming { e.subject } else { e.object };
                steps += 1;
                if op == "RELATED" {
                    result.insert(y);
                } else if seen.insert(y) {
                    result.insert(y);
                    queue.push((y, d + 1));
                }
                if op == "TRAVERSE" && steps >= cap {
                    *truncated = true;
                    return Ok(result);
                }
            }
        }
        Ok(result)
    }
    fn select(&self, q: &Value) -> Result<Selection> {
        if q["schema"] != "csl.eval.query/v0.1" || !q["query_id"].is_string() {
            return Err("invalid query schema".into());
        }
        let mut truncated = false;
        let selected = self.eval(q, &mut truncated)?;
        let mut ids: Vec<_> = selected.iter().copied().collect();
        ids.sort_unstable();
        let minimum = q["evidence"]["min_quality"]
            .as_str()
            .and_then(|v| QUALITY.iter().position(|x| *x == v));
        let mut props: Vec<_> = self
            .fx
            .evidence
            .iter()
            .filter(|e| selected.contains(&e.subject) || selected.contains(&e.object))
            .filter(|e| minimum.is_none_or(|m| e.quality as usize >= m))
            .filter(|e| {
                q["evidence"]["freshness_epoch"]
                    .as_u64()
                    .is_none_or(|epoch| e.freshness_epoch == epoch)
            })
            .copied()
            .collect();
        props.sort_unstable_by_key(|e| {
            (
                e.subject,
                e.relation.as_str(),
                e.object,
                e.proposition,
                e.lineage,
                e.polarity.as_str(),
                e.quality.as_str(),
                e.freshness_epoch,
            )
        });
        Ok(Selection {
            ids,
            props,
            truncated,
        })
    }
    fn encode(&self, q: &Value, selection: Selection) -> Result<Value> {
        let payload = json!({"entities":selection.ids,"propositions":selection.props,"knowledge":{"model":"open-world"},"completeness":{"entity_set":if selection.truncated{"TRUNCATED"}else if self.fx.complete{"COMPLETE"}else{"OBSERVED"}}});
        let digest = format!("sha256:{:x}", Sha256::digest(serde_json::to_vec(&payload)?));
        let mut result = payload;
        result["schema"] = json!("csl.eval.result/v0.1");
        result["snapshot"] = json!(self.fx.snapshot);
        result["query_id"] = q["query_id"].clone();
        result["digest"] = json!(digest);
        Ok(result)
    }
    fn execute(&self, q: &Value) -> Result<Value> {
        self.encode(q, self.select(q)?)
    }
}
fn main_run() -> Result<Value> {
    let args: Vec<String> = std::env::args().collect();
    let cmd = args.get(1).map(String::as_str).unwrap_or("info");
    let arg = |key: &str| -> Result<&str> {
        args.windows(2)
            .find(|w| w[0] == key)
            .map(|w| w[1].as_str())
            .ok_or_else(|| format!("missing {key}").into())
    };
    if cmd == "info" {
        return Ok(
            json!({"candidate":"rust","status":"typed-hash-v1","representation":"typed-hash-v1"}),
        );
    }
    if cmd == "boundary" {
        let size: usize = arg("--size")?.parse()?;
        let repeat: usize = arg("--repeat")?.parse()?;
        if repeat == 0 {
            return Err("repeat must be positive".into());
        }
        let input = vec![42u8; size];
        let mut times = Vec::new();
        let mut digest = String::new();
        for _ in 0..repeat {
            let start = std::time::Instant::now();
            let output = std::hint::black_box(&input).to_vec();
            std::hint::black_box(&output);
            times.push(start.elapsed().as_nanos());
            digest = format!("sha256:{:x}", Sha256::digest(&output));
        }
        return Ok(
            json!({"strategy":"pure-rust","payload_bytes":size,"latency_ns":times,"calls_per_query":0,"bytes_copied":size,"output_allocations_per_call":if size==0{0}else{1},"result_digest":digest}),
        );
    }
    let path = arg(if cmd == "workload" {
        "--corpus"
    } else {
        "--fixture"
    })?;
    let start = std::time::Instant::now();
    let bytes = std::fs::read(path)?;
    let fx: Fixture = serde_json::from_slice(&bytes)?;
    let load_ns = start.elapsed().as_nanos();
    let start = std::time::Instant::now();
    let store = Store::new(&fx)?;
    let index_ns = start.elapsed().as_nanos();
    match cmd {
        "load" => Ok(json!({"entities":store.entities.len(),"snapshot":fx.snapshot})),
        "query" => store.execute(&serde_json::from_slice(&std::fs::read(arg("--query")?)?)?),
        "workload" => {
            let workload = arg("--id")?;
            if !["W1", "W2"].contains(&workload) {
                return Err("unknown workload".into());
            }
            let params: Value = serde_json::from_str(arg("--params")?)?;
            if args.iter().any(|v| v == "--profile") {
                let start = std::time::Instant::now();
                let selection = store.select(&params["query"])?;
                let query_ns = start.elapsed().as_nanos();
                let start = std::time::Instant::now();
                let result = store.encode(&params["query"], selection)?;
                std::hint::black_box(serde_json::to_vec(&result)?);
                let result_ns = start.elapsed().as_nanos();
                Ok(
                    json!({"profile_schema":"csl.eval.profile/v0.1", "representation":"typed-hash-v1", "phases_ns":{"load":load_ns,"index":index_ns,"query":query_ns,"result":result_ns},"result":result}),
                )
            } else {
                store.execute(&params["query"])
            }
        }
        _ => Err("unknown command".into()),
    }
}
fn main() {
    match main_run() {
        Ok(v) => println!("{v}"),
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
