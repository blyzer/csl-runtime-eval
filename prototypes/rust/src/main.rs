mod model;
mod session;
use model::{Edge, Entity, Evidence, Fixture};
use serde::Serialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::alloc::{GlobalAlloc, Layout, System};
use std::cell::{Cell, OnceCell};
use std::collections::{HashMap, HashSet};
use std::hash::{BuildHasherDefault, Hasher};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering::Relaxed};

/// Heap accounting (X-MEM). A pass-through wrapper around the system allocator:
/// when `--stats` is absent the only cost is one relaxed load and a branch per
/// allocator call. When on, it counts *requested* bytes at the allocator
/// interface (not allocator-internal overhead, which is unobservable here).
static STATS_ON: AtomicBool = AtomicBool::new(false);
static ALLOCATIONS: AtomicU64 = AtomicU64::new(0);
static TOTAL_REQUESTED: AtomicU64 = AtomicU64::new(0);
static TOTAL_FREED: AtomicU64 = AtomicU64::new(0);
static LIVE: AtomicI64 = AtomicI64::new(0);
static PEAK_LIVE: AtomicI64 = AtomicI64::new(0);
struct CountingAlloc;
fn note_alloc(size: usize) {
    ALLOCATIONS.fetch_add(1, Relaxed);
    TOTAL_REQUESTED.fetch_add(size as u64, Relaxed);
    let live = LIVE.fetch_add(size as i64, Relaxed) + size as i64;
    PEAK_LIVE.fetch_max(live, Relaxed);
}
// SAFETY: every method forwards to `System` with the caller's layout/pointer and only
// updates atomic counters afterwards, so the `GlobalAlloc` contract is inherited as-is.
unsafe impl GlobalAlloc for CountingAlloc {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc(layout) };
        if STATS_ON.load(Relaxed) && !ptr.is_null() {
            note_alloc(layout.size());
        }
        ptr
    }
    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        let ptr = unsafe { System.alloc_zeroed(layout) };
        if STATS_ON.load(Relaxed) && !ptr.is_null() {
            note_alloc(layout.size());
        }
        ptr
    }
    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        unsafe { System.dealloc(ptr, layout) };
        if STATS_ON.load(Relaxed) {
            TOTAL_FREED.fetch_add(layout.size() as u64, Relaxed);
            LIVE.fetch_sub(layout.size() as i64, Relaxed);
        }
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let new = unsafe { System.realloc(ptr, layout, new_size) };
        if STATS_ON.load(Relaxed) && !new.is_null() {
            ALLOCATIONS.fetch_add(1, Relaxed);
            let delta = new_size as i64 - layout.size() as i64;
            if delta >= 0 {
                TOTAL_REQUESTED.fetch_add(delta as u64, Relaxed);
            } else {
                TOTAL_FREED.fetch_add((-delta) as u64, Relaxed);
            }
            let live = LIVE.fetch_add(delta, Relaxed) + delta;
            PEAK_LIVE.fetch_max(live, Relaxed);
        }
        new
    }
}
#[global_allocator]
static GLOBAL: CountingAlloc = CountingAlloc;
fn heap_live() -> u64 {
    LIVE.load(Relaxed).max(0) as u64
}
/// All counters read at one instant, so a reported snapshot is coherent.
fn heap_snapshot() -> [u64; 5] {
    [
        ALLOCATIONS.load(Relaxed),
        TOTAL_REQUESTED.load(Relaxed),
        TOTAL_FREED.load(Relaxed),
        heap_live(),
        PEAK_LIVE.load(Relaxed).max(0) as u64,
    ]
}

/// Rust's `std::collections::HashMap` defaults to SipHash-1-3 (randomized,
/// HashDoS-resistant, deliberately not optimized for raw speed). Zig's
/// `std.AutoHashMap` defaults to Wyhash (fast, non-cryptographic). This is
/// an FxHash-style hasher (the same algorithm `rustc` itself uses
/// internally): rotate-xor-multiply, no crate dependency. It isolates the
/// hasher choice as a single experimental variable for the index cause
/// analysis (ADR-0005 STEP 3) without changing any data structure,
/// algorithm, or Zig's code at all.
#[derive(Default)]
struct FxHasher(u64);
const FX_SEED: u64 = 0x51_7c_c1_b7_27_22_0a_95;
impl Hasher for FxHasher {
    fn write(&mut self, bytes: &[u8]) {
        for chunk in bytes.chunks(8) {
            let mut buf = [0u8; 8];
            buf[..chunk.len()].copy_from_slice(chunk);
            self.write_u64(u64::from_ne_bytes(buf));
        }
    }
    fn write_u64(&mut self, n: u64) {
        self.0 = (self.0.rotate_left(5) ^ n).wrapping_mul(FX_SEED);
    }
    fn finish(&self) -> u64 {
        self.0
    }
}
type FxBuild = BuildHasherDefault<FxHasher>;
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
type Set = HashSet<u64, FxBuild>;
type Index = HashMap<u64, Vec<Edge>, FxBuild>;
struct Selection {
    ids: Vec<u64>,
    props: Vec<Evidence>,
    truncated: bool,
}
/// Interned name -> entity ids (ascending). Keys borrow the fixture's strings, so
/// interning copies no string data; only strings referenced by entities appear.
/// Deliberately the std default hasher (SipHash), not `FxBuild`: the Fx-style hasher
/// that suits the u64 ID maps degrades badly on short, structured string keys (8-char
/// hex names: ~12x slower index build, ~9x slower lookups in the W4.S5 pass-1 run).
type NameMap<'a> = HashMap<&'a str, Vec<u64>>;
struct Store<'a> {
    fx: &'a Fixture,
    entities: HashMap<u64, Entity, FxBuild>,
    out: Index,
    inc: Index,
    names: OnceCell<NameMap<'a>>,
    name_build_ns: Cell<u128>,
}
impl<'a> Store<'a> {
    /// Lazy name index: built the first time a name is resolved, and its build
    /// time is remembered so the harness can report it (`intern_ns`).
    fn names(&self) -> &NameMap<'a> {
        self.names.get_or_init(|| {
            let start = std::time::Instant::now();
            let mut map: NameMap<'a> = HashMap::default();
            for e in &self.fx.entities {
                map.entry(self.fx.strings[e.name_sid as usize].as_str())
                    .or_default()
                    .push(e.id);
            }
            for ids in map.values_mut() {
                ids.sort_unstable();
            }
            self.name_build_ns.set(start.elapsed().as_nanos());
            map
        })
    }
    /// All entities whose name equals `name`, ascending; empty if none.
    fn resolve_name(&self, name: &str) -> &[u64] {
        self.names().get(name).map_or(&[], Vec::as_slice)
    }
    /// Entity map build + container-reference validation: the "entities"
    /// index sub-phase (STEP 3 investigation).
    fn build_entities(fx: &'a Fixture) -> Result<HashMap<u64, Entity, FxBuild>> {
        let mut entities = HashMap::default();
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
        Ok(entities)
    }
    /// Outgoing/incoming adjacency build + edge/evidence-reference
    /// validation: the "adjacency" index sub-phase.
    fn build_adjacency(
        fx: &'a Fixture,
        entities: &HashMap<u64, Entity, FxBuild>,
    ) -> Result<(Index, Index)> {
        let mut out: Index = HashMap::default();
        let mut inc: Index = HashMap::default();
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
        Ok((out, inc))
    }
    /// Final adjacency-list sort: the "sort" index sub-phase.
    fn sort_adjacency(out: &mut Index, inc: &mut Index) {
        for idx in [out, inc] {
            for rows in idx.values_mut() {
                rows.sort_unstable_by_key(|e| (e.subject, e.relation.as_str(), e.object));
            }
        }
    }
    fn check_schema(fx: &Fixture) -> Result<()> {
        if fx.schema != "csl.eval.fixture/v0.1" {
            return Err("invalid fixture schema".into());
        }
        Ok(())
    }
    fn eval(&self, q: &Value, truncated: &mut bool) -> Result<Set> {
        let op = q["op"].as_str().ok_or("missing op")?;
        if op == "RESOLVE" {
            if let Some(name) = q["name"].as_str() {
                return Ok(self.resolve_name(name).iter().copied().collect());
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
        let mut result = Set::default();
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
    /// Validate once (the recursive tree walk already covers every nested
    /// `input`) and run the traversal/set logic. This is the "query" phase:
    /// it excludes result construction, which `materialize` owns.
    fn query(&self, q: &Value) -> Result<(Set, bool)> {
        if q["schema"] != "csl.eval.query/v0.1" || !q["query_id"].is_string() {
            return Err("invalid query schema".into());
        }
        valid_query(q)?;
        let mut truncated = false;
        let selected = self.eval(q, &mut truncated)?;
        Ok((selected, truncated))
    }
    /// Build the normalized in-memory result (sorted ids, filtered/sorted
    /// evidence) from a raw query outcome. This is the "materialize" phase.
    fn materialize(&self, q: &Value, selected: Set, truncated: bool) -> Selection {
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
        Selection {
            ids,
            props,
            truncated,
        }
    }
    fn select(&self, q: &Value) -> Result<Selection> {
        let (selected, truncated) = self.query(q)?;
        Ok(self.materialize(q, selected, truncated))
    }
    /// Serialize directly from typed structs to canonical bytes: no
    /// intermediate `serde_json::Value` tree, matching Zig's
    /// `std.json.stringifyAlloc` on typed structs (ADR-0004 fairness pass).
    /// Field order is declared alphabetically to match the oracle's
    /// `sort_keys=True` canonicalization; `Payload`'s field order is the
    /// digest input, `Envelope`'s is cosmetic (compared by parsed equality).
    fn encode(&self, q: &Value, selection: Selection) -> Result<Vec<u8>> {
        #[derive(Serialize)]
        struct Completeness<'a> {
            entity_set: &'a str,
        }
        #[derive(Serialize)]
        struct Knowledge {
            model: &'static str,
        }
        #[derive(Serialize)]
        struct Payload<'a> {
            completeness: Completeness<'a>,
            entities: &'a [u64],
            knowledge: Knowledge,
            propositions: &'a [Evidence],
        }
        #[derive(Serialize)]
        struct Envelope<'a> {
            completeness: Completeness<'a>,
            digest: String,
            entities: &'a [u64],
            knowledge: Knowledge,
            propositions: &'a [Evidence],
            query_id: &'a str,
            schema: &'static str,
            snapshot: &'a str,
        }
        let entity_set = if selection.truncated {
            "TRUNCATED"
        } else if self.fx.complete {
            "COMPLETE"
        } else {
            "OBSERVED"
        };
        let payload = Payload {
            completeness: Completeness { entity_set },
            entities: &selection.ids,
            knowledge: Knowledge {
                model: "open-world",
            },
            propositions: &selection.props,
        };
        let digest = format!("sha256:{:x}", Sha256::digest(serde_json::to_vec(&payload)?));
        let envelope = Envelope {
            completeness: Completeness { entity_set },
            digest,
            entities: &selection.ids,
            knowledge: Knowledge {
                model: "open-world",
            },
            propositions: &selection.props,
            query_id: q["query_id"].as_str().ok_or("missing query_id")?,
            schema: "csl.eval.result/v0.1",
            snapshot: &self.fx.snapshot,
        };
        Ok(serde_json::to_vec(&envelope)?)
    }
    fn execute(&self, q: &Value) -> Result<Vec<u8>> {
        self.encode(q, self.select(q)?)
    }
}
fn main_run() -> Result<String> {
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
            json!({"candidate":"rust","status":"typed-hash-v1","representation":"typed-hash-v1"})
                .to_string(),
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
        return Ok(json!({"strategy":"pure-rust","payload_bytes":size,"latency_ns":times,"calls_per_query":0,"bytes_copied":size,"output_allocations_per_call":if size==0{0}else{1},"result_digest":digest}).to_string());
    }
    let path = arg(if cmd == "workload" {
        "--corpus"
    } else {
        "--fixture"
    })?;
    let start = std::time::Instant::now();
    let bytes = std::fs::read(path)?;
    let read_ns = start.elapsed().as_nanos();
    let start = std::time::Instant::now();
    let fx: Fixture = serde_json::from_slice(&bytes)?;
    let parse_ns = start.elapsed().as_nanos();
    // decode and construct are fused: serde deserializes bytes directly into
    // the typed `Fixture`, with no intermediate generic representation to
    // isolate a separate construction pass from (see ADR-0004 fairness note;
    // Zig's `std.json.parseFromSlice` is fused the same way).
    let decode_ns = read_ns + parse_ns;
    let construct_ns: u128 = 0;
    let load_ns = decode_ns + construct_ns;
    let live_after_load = heap_live();
    Store::check_schema(&fx)?;
    let start = std::time::Instant::now();
    let entities = Store::build_entities(&fx)?;
    let entities_ns = start.elapsed().as_nanos();
    let start = std::time::Instant::now();
    let (mut out, mut inc) = Store::build_adjacency(&fx, &entities)?;
    let adjacency_ns = start.elapsed().as_nanos();
    let start = std::time::Instant::now();
    Store::sort_adjacency(&mut out, &mut inc);
    let sort_ns = start.elapsed().as_nanos();
    let index_ns = entities_ns + adjacency_ns + sort_ns;
    let live_after_index = heap_live();
    let store = Store {
        fx: &fx,
        entities,
        out,
        inc,
        names: OnceCell::new(),
        name_build_ns: Cell::new(0),
    };
    match cmd {
        "load" => Ok(json!({"entities":store.entities.len(),"snapshot":fx.snapshot}).to_string()),
        "query" => {
            let q: Value = serde_json::from_slice(&std::fs::read(arg("--query")?)?)?;
            Ok(String::from_utf8(store.execute(&q)?)?)
        }
        "workload" => {
            let workload = arg("--id")?;
            if !["W1", "W2", "W3", "W4"].contains(&workload) {
                return Err("unknown workload".into());
            }
            let params: Value = serde_json::from_str(arg("--params")?)?;
            let query = &params["query"];
            let profiled = args.iter().any(|v| v == "--profile");
            let stats = args.iter().any(|v| v == "--stats");
            if (stats || params.get("resolve").is_some()) && !profiled {
                return Err("--stats and resolve require --profile".into());
            }
            let resolve = match params.get("resolve") {
                None => None,
                Some(r) => {
                    let names: Vec<&str> = r["names"]
                        .as_array()
                        .ok_or("resolve.names must be an array")?
                        .iter()
                        .map(|n| n.as_str().ok_or("resolve.names must be strings"))
                        .collect::<std::result::Result<_, _>>()?;
                    let rounds = r["rounds"]
                        .as_u64()
                        .ok_or("resolve.rounds must be an integer")?;
                    if names.is_empty()
                        || rounds == 0
                        || (names.len() as u64).saturating_mul(rounds) > 1_000_000
                    {
                        return Err("invalid resolve parameters".into());
                    }
                    Some((names, rounds))
                }
            };
            if profiled {
                let start = std::time::Instant::now();
                let (selected, truncated) = store.query(query)?;
                let query_ns = start.elapsed().as_nanos();
                let live_after_query = heap_live();
                let start = std::time::Instant::now();
                let selection = store.materialize(query, selected, truncated);
                let materialize_ns = start.elapsed().as_nanos();
                let start = std::time::Instant::now();
                let result_bytes = store.encode(query, selection)?;
                let encode_ns = start.elapsed().as_nanos();
                std::hint::black_box(&result_bytes);
                let result_ns = materialize_ns + encode_ns;
                // Repeated name resolution runs after the primary result and outside
                // every recorded phase; only `resolve_name` itself is timed.
                let name_index = match &resolve {
                    None => None,
                    Some((names, rounds)) => {
                        store.names();
                        let live = heap_snapshot();
                        let mut lookup_ns: Vec<u64> =
                            Vec::with_capacity(names.len() * *rounds as usize);
                        let mut hasher = Sha256::new();
                        let mut ids_total = 0u64;
                        for round in 0..*rounds {
                            for name in names {
                                let start = std::time::Instant::now();
                                let ids = std::hint::black_box(store.resolve_name(name));
                                lookup_ns.push(start.elapsed().as_nanos() as u64);
                                ids_total += ids.len() as u64;
                                if round == 0 {
                                    let joined: Vec<String> =
                                        ids.iter().map(u64::to_string).collect();
                                    hasher.update(format!("[{}]\n", joined.join(",")));
                                }
                            }
                        }
                        Some((
                            json!({
                                "intern_ns": store.name_build_ns.get(),
                                "unique_strings": store.names().len(),
                                "lookups": lookup_ns.len(),
                                "lookup_ns": lookup_ns,
                                "lookup_digest": format!("sha256:{:x}", hasher.finalize()),
                                "lookup_ids_total": ids_total,
                            }),
                            live,
                        ))
                    }
                };
                let snapshot = name_index
                    .as_ref()
                    .map_or_else(heap_snapshot, |(_, snap)| *snap);
                let mut metadata = json!({
                    "profile_schema": "csl.eval.profile/v0.1",
                    "representation": "typed-hash-v1",
                    "phases_ns": {
                        "load": load_ns, "index": index_ns, "query": query_ns, "result": result_ns
                    },
                    "phase_detail_ns": {
                        "decode": decode_ns, "construct": construct_ns,
                        "materialize": materialize_ns, "encode": encode_ns
                    },
                    "phase_subdetail_ns": {
                        "read": read_ns, "parse": parse_ns,
                        "entities": entities_ns, "adjacency": adjacency_ns, "sort": sort_ns
                    }
                });
                if let Some((index, _)) = name_index {
                    metadata["name_index"] = index;
                }
                if stats {
                    metadata["heap"] = json!({
                        "model": "requested-bytes",
                        "allocations": snapshot[0],
                        "total_requested": snapshot[1],
                        "total_freed": snapshot[2],
                        "live": snapshot[3],
                        "peak_live": snapshot[4],
                        "live_after_load": live_after_load,
                        "live_after_index": live_after_index,
                        "live_after_query": live_after_query,
                        "retained": null,
                    });
                }
                let metadata = metadata.to_string();
                let result_str = String::from_utf8(result_bytes)?;
                Ok(format!(
                    "{},\"result\":{}}}",
                    &metadata[..metadata.len() - 1],
                    result_str
                ))
            } else {
                Ok(String::from_utf8(store.execute(query)?)?)
            }
        }
        _ => Err("unknown command".into()),
    }
}
fn main() {
    // Decided before anything else allocates in `main_run`; args_os temporaries are
    // allocated and freed while counting is still off, so they cancel out.
    if std::env::args_os().any(|a| a == "--stats")
        || std::env::args_os().nth(1).is_some_and(|a| a == "session")
    {
        STATS_ON.store(true, Relaxed);
    }
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("session") {
        let Some(repository) = args
            .windows(2)
            .find(|w| w[0] == "--repository")
            .map(|w| w[1].clone())
        else {
            eprintln!("missing --repository");
            std::process::exit(1);
        };
        let strategy = match args
            .windows(2)
            .find(|w| w[0] == "--strategy")
            .map(|w| w[1].as_str())
        {
            None | Some("full-rebuild") => session::Strategy::FullRebuild,
            Some("incremental") => session::Strategy::Incremental,
            Some(other) => {
                eprintln!("unknown --strategy {other}");
                std::process::exit(1);
            }
        };
        std::process::exit(session::run(std::path::Path::new(&repository), strategy));
    }
    match main_run() {
        Ok(v) => println!("{v}"),
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    }
}
