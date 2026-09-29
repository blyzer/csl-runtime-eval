//! S0 v0 persistent session (oracle/SESSION-SEMANTICS.md, oracle/SESSION-BINDING-JSONL.md).
//!
//! `csl-eval-rust session --repository DIR` serves the JSONL binding on stdin/stdout. The
//! store keeps the *logical state* (entities with resolved names, relation and evidence
//! multisets) and derives the existing typed store from it. Mutation strategy is
//! `full-rebuild`: `mutate` applies the batch to a working copy of the logical state,
//! rebuilds every derived structure (entity map, adjacency indexes, name index) and only
//! then swaps, so its latency includes the rebuild and a failed batch changes nothing.
//!
//! Snapshot file layout (candidate-native, little endian, `<repository>/<snapshot_id>.snap`):
//!   magic `CSLSNAP1` | u32 format version (1) | str artifact | str snapshot label |
//!   u8 has_epoch | u64 epoch | u8 complete |
//!   u64 n_strings, n_strings x str (distinct names, ascending) |
//!   u64 n_entities, columns: ids u64[n], kinds u8[n], name_sid u32[n], containers u64[n] (0 = none) |
//!   u64 n_relations (rows expanded, canonical order), columns: subject u64[n], relation u8[n], object u64[n] |
//!   u64 n_evidence (rows expanded, canonical order), columns: proposition u64, subject u64, object u64,
//!   freshness_epoch u64, lineage u32, relation u8, polarity u8, quality u8 |
//!   32-byte SHA-256 of everything before it.
//! where `str` is u32 length + UTF-8 bytes. `snapshot_id` = `snap-` + first 16 hex digits of the
//! SHA-256 of the whole file (opaque to the host).
use super::{Store, heap_live, heap_snapshot};
use crate::model::{Edge, Entity, Evidence, Fixture, Kind, Polarity, Quality, Relation};
use serde::Serialize;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::cell::{Cell, OnceCell};
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::io::{BufRead, Write};
use std::path::{Path, PathBuf};

const BINDING: &str = "csl.eval.session.jsonl/v0";
const SEMANTICS: &str = "csl.eval.session/v0.1";
const ARTIFACT: &str = concat!("csl-eval-rust/s0-v0/", env!("CARGO_PKG_VERSION"));
const MIN_LINE: u64 = 65536;
const SNAP_MAGIC: &[u8; 8] = b"CSLSNAP1";
const SNAP_VERSION: u32 = 1;
const RELS: [&str; 5] = ["CALLS", "REFERENCES", "IMPLEMENTS", "OVERRIDES", "CONTAINS"];
const KINDS: [&str; 7] = [
    "TYPE", "METHOD", "FUNCTION", "FIELD", "MODULE", "FILE", "VARIABLE",
];
const POLARITIES: [&str; 2] = ["POSITIVE", "NEGATIVE"];
const QUALITIES: [&str; 5] = ["LEXICAL", "PROBABLE", "DERIVED", "EXACT", "VERIFIED"];

// ---------------------------------------------------------------- errors

struct Fail {
    code: &'static str,
    message: String,
}
fn fail(code: &'static str, message: impl Into<String>) -> Fail {
    Fail {
        code,
        message: message.into(),
    }
}
fn invalid_input(message: impl Into<String>) -> Fail {
    fail("INVALID_INPUT", message)
}
fn invalid_request(message: impl Into<String>) -> Fail {
    fail("INVALID_REQUEST", message)
}

// ------------------------------------------------------- enum <-> string

fn kind_of(s: &str) -> Option<Kind> {
    Some(match s {
        "TYPE" => Kind::Type,
        "METHOD" => Kind::Method,
        "FUNCTION" => Kind::Function,
        "FIELD" => Kind::Field,
        "MODULE" => Kind::Module,
        "FILE" => Kind::File,
        "VARIABLE" => Kind::Variable,
        _ => return None,
    })
}
fn relation_of(s: &str) -> Option<Relation> {
    Some(match s {
        "CALLS" => Relation::Calls,
        "REFERENCES" => Relation::References,
        "IMPLEMENTS" => Relation::Implements,
        "OVERRIDES" => Relation::Overrides,
        "CONTAINS" => Relation::Contains,
        _ => return None,
    })
}
fn polarity_of(s: &str) -> Option<Polarity> {
    Some(match s {
        "POSITIVE" => Polarity::Positive,
        "NEGATIVE" => Polarity::Negative,
        _ => return None,
    })
}
fn quality_of(s: &str) -> Option<Quality> {
    Some(match s {
        "LEXICAL" => Quality::Lexical,
        "PROBABLE" => Quality::Probable,
        "DERIVED" => Quality::Derived,
        "EXACT" => Quality::Exact,
        "VERIFIED" => Quality::Verified,
        _ => return None,
    })
}
fn index_of(list: &[&str], s: &str) -> u8 {
    list.iter().position(|x| *x == s).unwrap_or(0) as u8
}

// ------------------------------------------------------- logical state

type RelKey = (u64, &'static str, u64);
/// (subject, relation, object, proposition, lineage, polarity, quality, freshness_epoch):
/// the result sort key, with string comparison for the enum fields.
type EvKey = (
    u64,
    &'static str,
    u64,
    u64,
    u32,
    &'static str,
    &'static str,
    u64,
);

#[derive(Clone)]
struct Ent {
    kind: Kind,
    name: String,
    container: Option<u64>,
}

#[derive(Clone)]
struct Logical {
    snapshot: String,
    epoch: Option<u64>,
    complete: bool,
    entities: BTreeMap<u64, Ent>,
    relations: BTreeMap<RelKey, u64>,
    evidence: BTreeMap<EvKey, u64>,
}

fn ev_key(e: &Evidence) -> EvKey {
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
}
fn ev_of(k: &EvKey) -> Evidence {
    Evidence {
        freshness_epoch: k.7,
        lineage: k.4,
        object: k.2,
        polarity: polarity_of(k.5).expect("polarity"),
        proposition: k.3,
        quality: quality_of(k.6).expect("quality"),
        relation: relation_of(k.1).expect("relation"),
        subject: k.0,
    }
}

impl Logical {
    fn empty(snapshot: String, epoch: Option<u64>, complete: bool) -> Logical {
        Logical {
            snapshot,
            epoch,
            complete,
            entities: BTreeMap::new(),
            relations: BTreeMap::new(),
            evidence: BTreeMap::new(),
        }
    }
    fn from_fixture(fx: &Fixture) -> Logical {
        let mut l = Logical::empty(fx.snapshot.clone(), fx._epoch, fx.complete);
        for e in &fx.entities {
            l.entities.insert(
                e.id,
                Ent {
                    kind: e.kind,
                    name: fx.strings[e.name_sid as usize].clone(),
                    container: e.container,
                },
            );
        }
        for r in &fx.relations {
            *l.relations
                .entry((r.subject, r.relation.as_str(), r.object))
                .or_default() += 1;
        }
        for e in &fx.evidence {
            *l.evidence.entry(ev_key(e)).or_default() += 1;
        }
        l
    }
    /// A `Fixture` equal to this state (name table layout is arbitrary: one string per distinct name).
    fn to_fixture(&self) -> Fixture {
        let mut strings: Vec<String> = Vec::new();
        let mut sid: HashMap<&str, u32> = HashMap::new();
        let mut entities = Vec::with_capacity(self.entities.len());
        for (id, e) in &self.entities {
            let next = strings.len() as u32;
            let s = *sid.entry(e.name.as_str()).or_insert_with(|| {
                strings.push(e.name.clone());
                next
            });
            entities.push(Entity {
                id: *id,
                kind: e.kind,
                name_sid: s,
                container: e.container,
            });
        }
        let mut relations = Vec::new();
        for ((s, r, o), n) in &self.relations {
            for _ in 0..*n {
                relations.push(Edge {
                    subject: *s,
                    relation: relation_of(r).expect("relation"),
                    object: *o,
                });
            }
        }
        let mut evidence = Vec::new();
        for (k, n) in &self.evidence {
            for _ in 0..*n {
                evidence.push(ev_of(k));
            }
        }
        Fixture {
            schema: "csl.eval.fixture/v0.1".to_string(),
            snapshot: self.snapshot.clone(),
            _epoch: self.epoch,
            strings,
            entities,
            relations,
            evidence,
            complete: self.complete,
        }
    }
    fn counts(&self) -> [u64; 4] {
        let unique: HashSet<&str> = self.entities.values().map(|e| e.name.as_str()).collect();
        [
            self.entities.len() as u64,
            self.relations.values().sum(),
            self.evidence.values().sum(),
            unique.len() as u64,
        ]
    }
    /// SHA-256 and byte count of the canonical logical-state JSON (SESSION-SEMANTICS section 2).
    fn digest(&self) -> Result<(String, u64), Fail> {
        #[derive(Serialize)]
        struct SEntity<'a> {
            container: Option<u64>,
            id: u64,
            kind: &'a str,
            name: &'a str,
        }
        #[derive(Serialize)]
        struct SRelation<'a> {
            object: u64,
            relation: &'a str,
            subject: u64,
        }
        #[derive(Serialize)]
        struct SEvidence<'a> {
            freshness_epoch: u64,
            lineage: u32,
            object: u64,
            polarity: &'a str,
            proposition: u64,
            quality: &'a str,
            relation: &'a str,
            subject: u64,
        }
        #[derive(Serialize)]
        struct State<'a> {
            complete: bool,
            entities: Vec<SEntity<'a>>,
            epoch: Option<u64>,
            evidence: Vec<SEvidence<'a>>,
            relations: Vec<SRelation<'a>>,
            snapshot: &'a str,
        }
        struct Counting {
            hasher: Sha256,
            bytes: u64,
        }
        impl Write for Counting {
            fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
                self.hasher.update(buf);
                self.bytes += buf.len() as u64;
                Ok(buf.len())
            }
            fn flush(&mut self) -> std::io::Result<()> {
                Ok(())
            }
        }
        let state = State {
            complete: self.complete,
            entities: self
                .entities
                .iter()
                .map(|(id, e)| SEntity {
                    container: e.container,
                    id: *id,
                    kind: e.kind.as_str(),
                    name: &e.name,
                })
                .collect(),
            epoch: self.epoch,
            evidence: self
                .evidence
                .iter()
                .flat_map(|(k, n)| std::iter::repeat_n(k, *n as usize))
                .map(|k| SEvidence {
                    freshness_epoch: k.7,
                    lineage: k.4,
                    object: k.2,
                    polarity: k.5,
                    proposition: k.3,
                    quality: k.6,
                    relation: k.1,
                    subject: k.0,
                })
                .collect(),
            relations: self
                .relations
                .iter()
                .flat_map(|(k, n)| std::iter::repeat_n(k, *n as usize))
                .map(|k| SRelation {
                    object: k.2,
                    relation: k.1,
                    subject: k.0,
                })
                .collect(),
            snapshot: &self.snapshot,
        };
        let mut out = Counting {
            hasher: Sha256::new(),
            bytes: 0,
        };
        serde_json::to_writer(&mut out, &state).map_err(|e| fail("INTERNAL", e.to_string()))?;
        Ok((format!("sha256:{:x}", out.hasher.finalize()), out.bytes))
    }

    // ------------------------------------------------------------ mutation

    /// Apply one batch per ADR-0008 to a copy; mirrors `oracle/session_model.py::_applied`
    /// (same checks, same order, so error codes agree).
    fn applied(&self, batch: &Value) -> Result<Logical, Fail> {
        let ops = batch
            .as_array()
            .ok_or_else(|| invalid_request("batch must be a list"))?;
        if ops.is_empty() {
            return Err(invalid_input("empty batch"));
        }
        let mut entity_ops: Vec<(&Map<String, Value>, &str)> = Vec::new();
        let mut entity_ids: HashSet<String> = HashSet::new();
        let mut rel_add: BTreeMap<RelKey, u64> = BTreeMap::new();
        let mut rel_rem: BTreeMap<RelKey, u64> = BTreeMap::new();
        let mut ev_add: BTreeMap<EvKey, u64> = BTreeMap::new();
        let mut ev_rem: BTreeMap<EvKey, u64> = BTreeMap::new();
        for op in ops {
            let obj = op
                .as_object()
                .ok_or_else(|| invalid_request("unknown or malformed operation"))?;
            let kind = obj.get("op").and_then(Value::as_str).unwrap_or("");
            match kind {
                "ADD_ENTITY" | "REMOVE_ENTITY" | "UPDATE_ENTITY" => {
                    let allowed: &[&str] = match kind {
                        "ADD_ENTITY" => &["op", "id", "kind", "name", "container"],
                        "REMOVE_ENTITY" => &["op", "id"],
                        _ => &["op", "id", "set"],
                    };
                    if obj.keys().any(|k| !allowed.contains(&k.as_str())) || !obj.contains_key("id")
                    {
                        return Err(invalid_request(format!("{kind} fields")));
                    }
                    if kind == "ADD_ENTITY"
                        && !(obj.contains_key("kind") && obj.contains_key("name"))
                    {
                        return Err(invalid_request("ADD_ENTITY needs kind and name"));
                    }
                    if kind == "UPDATE_ENTITY" && !obj.get("set").is_some_and(Value::is_object) {
                        return Err(invalid_request("UPDATE_ENTITY needs set"));
                    }
                    if !entity_ids.insert(obj["id"].to_string()) {
                        return Err(invalid_input("two entity operations for one id"));
                    }
                    entity_ops.push((obj, kind));
                }
                "ADD_RELATION" | "REMOVE_RELATION" => {
                    if obj.len() != 4
                        || !["op", "subject", "relation", "object"]
                            .iter()
                            .all(|k| obj.contains_key(*k))
                    {
                        return Err(invalid_request(format!("{kind} fields")));
                    }
                    let key =
                        relation_row(obj).ok_or_else(|| invalid_input("invalid relation row"))?;
                    *(if kind == "ADD_RELATION" {
                        &mut rel_add
                    } else {
                        &mut rel_rem
                    })
                    .entry(key)
                    .or_default() += 1;
                }
                "ADD_EVIDENCE" | "REMOVE_EVIDENCE" => {
                    if obj.len() != 9
                        || ![
                            "op",
                            "proposition",
                            "subject",
                            "relation",
                            "object",
                            "polarity",
                            "quality",
                            "freshness_epoch",
                            "lineage",
                        ]
                        .iter()
                        .all(|k| obj.contains_key(*k))
                    {
                        return Err(invalid_request(format!("{kind} fields")));
                    }
                    let key =
                        evidence_row(obj).ok_or_else(|| invalid_input("invalid evidence row"))?;
                    *(if kind == "ADD_EVIDENCE" {
                        &mut ev_add
                    } else {
                        &mut ev_rem
                    })
                    .entry(key)
                    .or_default() += 1;
                }
                _ => return Err(invalid_request("unknown or malformed operation")),
            }
        }
        if rel_add.keys().any(|k| rel_rem.contains_key(k))
            || ev_add.keys().any(|k| ev_rem.contains_key(k))
        {
            return Err(invalid_input(
                "a value is both added and removed in one batch",
            ));
        }
        let mut entities = self.entities.clone();
        for (obj, kind) in entity_ops {
            let id = obj["id"].as_u64().filter(|n| *n >= 1);
            match kind {
                "ADD_ENTITY" => {
                    let bad = || invalid_input("invalid ADD_ENTITY");
                    let id = id.ok_or_else(bad)?;
                    let name = obj["name"].as_str().ok_or_else(bad)?;
                    let kind = obj["kind"].as_str().and_then(kind_of).ok_or_else(bad)?;
                    let container = optional_id(obj.get("container")).ok_or_else(bad)?;
                    if self.entities.contains_key(&id) {
                        return Err(bad());
                    }
                    entities.insert(
                        id,
                        Ent {
                            kind,
                            name: name.to_string(),
                            container,
                        },
                    );
                }
                "REMOVE_ENTITY" => {
                    let id = id
                        .filter(|i| self.entities.contains_key(i))
                        .ok_or_else(|| invalid_input("REMOVE_ENTITY of a missing id"))?;
                    entities.remove(&id);
                }
                _ => {
                    let changes = obj["set"].as_object().expect("checked object");
                    let id = id
                        .filter(|i| self.entities.contains_key(i))
                        .filter(|_| !changes.is_empty())
                        .filter(|_| {
                            changes
                                .keys()
                                .all(|k| ["kind", "name", "container"].contains(&k.as_str()))
                        })
                        .ok_or_else(|| invalid_input("invalid UPDATE_ENTITY"))?;
                    let bad = || invalid_input("invalid UPDATE_ENTITY values");
                    let current = entities.get(&id).expect("present").clone();
                    let merged = Ent {
                        kind: match changes.get("kind") {
                            Some(v) => v.as_str().and_then(kind_of).ok_or_else(bad)?,
                            None => current.kind,
                        },
                        name: match changes.get("name") {
                            Some(v) => v.as_str().ok_or_else(bad)?.to_string(),
                            None => current.name,
                        },
                        container: match changes.get("container") {
                            Some(v) => optional_id(Some(v)).ok_or_else(bad)?,
                            None => current.container,
                        },
                    };
                    entities.insert(id, merged);
                }
            }
        }
        let mut relations = self.relations.clone();
        let mut evidence = self.evidence.clone();
        if rel_rem
            .iter()
            .any(|(k, n)| relations.get(k).copied().unwrap_or(0) < *n)
            || ev_rem
                .iter()
                .any(|(k, n)| evidence.get(k).copied().unwrap_or(0) < *n)
        {
            return Err(invalid_input("removing more occurrences than exist"));
        }
        for (k, n) in rel_add {
            *relations.entry(k).or_default() += n;
        }
        for (k, n) in rel_rem {
            if let Some(count) = relations.get_mut(&k) {
                *count -= n;
            }
        }
        relations.retain(|_, n| *n > 0);
        for (k, n) in ev_add {
            *evidence.entry(k).or_default() += n;
        }
        for (k, n) in ev_rem {
            if let Some(count) = evidence.get_mut(&k) {
                *count -= n;
            }
        }
        evidence.retain(|_, n| *n > 0);
        if relations
            .keys()
            .any(|(s, _, o)| !entities.contains_key(s) || !entities.contains_key(o))
        {
            return Err(invalid_input("relation references a missing entity"));
        }
        if evidence
            .keys()
            .any(|k| !entities.contains_key(&k.0) || !entities.contains_key(&k.2))
        {
            return Err(invalid_input("evidence references a missing entity"));
        }
        if entities
            .values()
            .any(|e| e.container.is_some_and(|c| !entities.contains_key(&c)))
        {
            return Err(invalid_input("container references a missing entity"));
        }
        Ok(Logical {
            snapshot: self.snapshot.clone(),
            epoch: self.epoch,
            complete: self.complete,
            entities,
            relations,
            evidence,
        })
    }
}

/// `Some(None)` for absent/null, `Some(Some(id))` for a valid id, `None` for anything else.
fn optional_id(v: Option<&Value>) -> Option<Option<u64>> {
    match v {
        None | Some(Value::Null) => Some(None),
        Some(v) => v.as_u64().filter(|n| *n >= 1).map(Some),
    }
}
fn relation_row(obj: &Map<String, Value>) -> Option<RelKey> {
    let subject = obj["subject"].as_u64().filter(|n| *n >= 1)?;
    let object = obj["object"].as_u64().filter(|n| *n >= 1)?;
    let relation = relation_of(obj["relation"].as_str()?)?.as_str();
    Some((subject, relation, object))
}
fn evidence_row(obj: &Map<String, Value>) -> Option<EvKey> {
    let proposition = obj["proposition"].as_u64().filter(|n| *n >= 1)?;
    let subject = obj["subject"].as_u64().filter(|n| *n >= 1)?;
    let object = obj["object"].as_u64().filter(|n| *n >= 1)?;
    let relation = relation_of(obj["relation"].as_str()?)?.as_str();
    let polarity = polarity_of(obj["polarity"].as_str()?)?.as_str();
    let quality = quality_of(obj["quality"].as_str()?)?.as_str();
    let epoch = obj["freshness_epoch"].as_u64()?;
    let lineage = u32::try_from(obj["lineage"].as_u64()?).ok()?;
    Some((
        subject,
        relation,
        object,
        proposition,
        lineage,
        polarity,
        quality,
        epoch,
    ))
}

// ------------------------------------------------------- derived store

/// The typed `Fixture` plus the `Store` borrowing it. `Store<'a>` holds `&'a Fixture`, so the
/// fixture is boxed and the store is tied to it with a raw pointer; `Drop` releases the store
/// before the fixture, and the fields never leave this struct.
struct Derived {
    store: Option<Store<'static>>,
    fx: *mut Fixture,
}
impl Derived {
    fn build(fx: Fixture) -> Result<Derived, Fail> {
        let ptr = Box::into_raw(Box::new(fx));
        let mut derived = Derived {
            store: None,
            fx: ptr,
        };
        // SAFETY: `ptr` stays valid until `Drop`, which drops `store` first.
        let fxref: &'static Fixture = unsafe { &*ptr };
        let store = (|| -> super::Result<Store<'static>> {
            Store::check_schema(fxref)?;
            let entities = Store::build_entities(fxref)?;
            let (mut out, mut inc) = Store::build_adjacency(fxref, &entities)?;
            Store::sort_adjacency(&mut out, &mut inc);
            let store = Store {
                fx: fxref,
                entities,
                out,
                inc,
                names: OnceCell::new(),
                name_build_ns: Cell::new(0),
            };
            store.names(); // the name index is part of the derived state
            Ok(store)
        })()
        .map_err(|e| invalid_input(e.to_string()))?;
        derived.store = Some(store);
        Ok(derived)
    }
    fn store(&self) -> &Store<'static> {
        self.store.as_ref().expect("store built")
    }
}
impl Drop for Derived {
    fn drop(&mut self) {
        self.store = None;
        // SAFETY: allocated by `Box::into_raw` in `build`, and the store borrowing it is gone.
        drop(unsafe { Box::from_raw(self.fx) });
    }
}

// ---------------------------------------------------------- base64

const B64: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
fn b64_encode(data: &[u8], out: &mut Vec<u8>) {
    for chunk in data.chunks(3) {
        let n = (chunk[0] as u32) << 16
            | (*chunk.get(1).unwrap_or(&0) as u32) << 8
            | *chunk.get(2).unwrap_or(&0) as u32;
        out.push(B64[(n >> 18) as usize & 63]);
        out.push(B64[(n >> 12) as usize & 63]);
        out.push(if chunk.len() > 1 {
            B64[(n >> 6) as usize & 63]
        } else {
            b'='
        });
        out.push(if chunk.len() > 2 {
            B64[n as usize & 63]
        } else {
            b'='
        });
    }
}
fn b64_decode(text: &str) -> Option<Vec<u8>> {
    let bytes = text.as_bytes();
    if !bytes.len().is_multiple_of(4) {
        return None;
    }
    let value = |c: u8| B64.iter().position(|x| *x == c).map(|p| p as u32);
    let mut out = Vec::with_capacity(bytes.len() / 4 * 3);
    for (i, quad) in bytes.chunks(4).enumerate() {
        let last = i == bytes.len() / 4 - 1;
        let pad = quad.iter().rev().take_while(|c| **c == b'=').count();
        if pad > 2 || (pad > 0 && !last) {
            return None;
        }
        let mut n = 0u32;
        for (j, c) in quad.iter().enumerate() {
            n = n << 6 | if j >= 4 - pad { 0 } else { value(*c)? };
        }
        out.push((n >> 16) as u8);
        if pad < 2 {
            out.push((n >> 8) as u8);
        }
        if pad < 1 {
            out.push(n as u8);
        }
    }
    Some(out)
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

// ---------------------------------------------------------- snapshots

fn put_str(out: &mut Vec<u8>, s: &str) {
    out.extend_from_slice(&(s.len() as u32).to_le_bytes());
    out.extend_from_slice(s.as_bytes());
}
fn encode_snapshot(l: &Logical) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(SNAP_MAGIC);
    out.extend_from_slice(&SNAP_VERSION.to_le_bytes());
    put_str(&mut out, ARTIFACT);
    put_str(&mut out, &l.snapshot);
    out.push(l.epoch.is_some() as u8);
    out.extend_from_slice(&l.epoch.unwrap_or(0).to_le_bytes());
    out.push(l.complete as u8);
    let names: BTreeSet<&str> = l.entities.values().map(|e| e.name.as_str()).collect();
    let sid: HashMap<&str, u32> = names
        .iter()
        .enumerate()
        .map(|(i, n)| (*n, i as u32))
        .collect();
    out.extend_from_slice(&(names.len() as u64).to_le_bytes());
    for n in &names {
        put_str(&mut out, n);
    }
    let n = l.entities.len();
    out.extend_from_slice(&(n as u64).to_le_bytes());
    for id in l.entities.keys() {
        out.extend_from_slice(&id.to_le_bytes());
    }
    for e in l.entities.values() {
        out.push(index_of(&KINDS, e.kind.as_str()));
    }
    for e in l.entities.values() {
        out.extend_from_slice(&sid[e.name.as_str()].to_le_bytes());
    }
    for e in l.entities.values() {
        out.extend_from_slice(&e.container.unwrap_or(0).to_le_bytes());
    }
    let rows: Vec<&RelKey> = l
        .relations
        .iter()
        .flat_map(|(k, n)| std::iter::repeat_n(k, *n as usize))
        .collect();
    out.extend_from_slice(&(rows.len() as u64).to_le_bytes());
    for k in &rows {
        out.extend_from_slice(&k.0.to_le_bytes());
    }
    for k in &rows {
        out.push(index_of(&RELS, k.1));
    }
    for k in &rows {
        out.extend_from_slice(&k.2.to_le_bytes());
    }
    let rows: Vec<&EvKey> = l
        .evidence
        .iter()
        .flat_map(|(k, n)| std::iter::repeat_n(k, *n as usize))
        .collect();
    out.extend_from_slice(&(rows.len() as u64).to_le_bytes());
    for k in &rows {
        out.extend_from_slice(&k.3.to_le_bytes());
    }
    for k in &rows {
        out.extend_from_slice(&k.0.to_le_bytes());
    }
    for k in &rows {
        out.extend_from_slice(&k.2.to_le_bytes());
    }
    for k in &rows {
        out.extend_from_slice(&k.7.to_le_bytes());
    }
    for k in &rows {
        out.extend_from_slice(&k.4.to_le_bytes());
    }
    for k in &rows {
        out.push(index_of(&RELS, k.1));
    }
    for k in &rows {
        out.push(index_of(&POLARITIES, k.5));
    }
    for k in &rows {
        out.push(index_of(&QUALITIES, k.6));
    }
    let digest = Sha256::digest(&out);
    out.extend_from_slice(&digest);
    out
}

struct Cursor<'a> {
    data: &'a [u8],
    pos: usize,
}
impl<'a> Cursor<'a> {
    fn take(&mut self, n: usize) -> Option<&'a [u8]> {
        let end = self.pos.checked_add(n)?;
        let slice = self.data.get(self.pos..end)?;
        self.pos = end;
        Some(slice)
    }
    fn u8(&mut self) -> Option<u8> {
        self.take(1).map(|b| b[0])
    }
    fn u32(&mut self) -> Option<u32> {
        self.take(4)
            .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
    }
    fn u64(&mut self) -> Option<u64> {
        self.take(8)
            .map(|b| u64::from_le_bytes(b.try_into().unwrap()))
    }
    fn str(&mut self) -> Option<&'a str> {
        let n = self.u32()? as usize;
        std::str::from_utf8(self.take(n)?).ok()
    }
    fn count(&mut self, width: usize) -> Option<usize> {
        let n = usize::try_from(self.u64()?).ok()?;
        (n.checked_mul(width)? <= self.data.len() - self.pos.min(self.data.len())).then_some(n)
    }
}
fn decode_snapshot(data: &[u8]) -> Option<Logical> {
    if data.len() < SNAP_MAGIC.len() + 32 {
        return None;
    }
    let (body, tail) = data.split_at(data.len() - 32);
    if Sha256::digest(body).as_slice() != tail {
        return None;
    }
    let mut c = Cursor { data: body, pos: 0 };
    if c.take(8)? != SNAP_MAGIC || c.u32()? != SNAP_VERSION || c.str()? != ARTIFACT {
        return None;
    }
    let snapshot = c.str()?.to_string();
    let has_epoch = c.u8()? != 0;
    let epoch = c.u64()?;
    let complete = c.u8()? != 0;
    let mut l = Logical::empty(snapshot, has_epoch.then_some(epoch), complete);
    let mut names = Vec::new();
    for _ in 0..c.count(4)? {
        names.push(c.str()?);
    }
    let n = c.count(21)?;
    let ids: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    let kinds: Vec<u8> = (0..n).map(|_| c.u8()).collect::<Option<_>>()?;
    let sids: Vec<u32> = (0..n).map(|_| c.u32()).collect::<Option<_>>()?;
    let containers: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    for i in 0..n {
        l.entities.insert(
            ids[i],
            Ent {
                kind: kind_of(KINDS.get(kinds[i] as usize)?)?,
                name: (*names.get(sids[i] as usize)?).to_string(),
                container: (containers[i] != 0).then_some(containers[i]),
            },
        );
    }
    let n = c.count(17)?;
    let subjects: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    let rels: Vec<u8> = (0..n).map(|_| c.u8()).collect::<Option<_>>()?;
    let objects: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    for i in 0..n {
        let relation = relation_of(RELS.get(rels[i] as usize)?)?.as_str();
        *l.relations
            .entry((subjects[i], relation, objects[i]))
            .or_default() += 1;
    }
    let n = c.count(39)?;
    let propositions: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    let subjects: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    let objects: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    let epochs: Vec<u64> = (0..n).map(|_| c.u64()).collect::<Option<_>>()?;
    let lineages: Vec<u32> = (0..n).map(|_| c.u32()).collect::<Option<_>>()?;
    let rels: Vec<u8> = (0..n).map(|_| c.u8()).collect::<Option<_>>()?;
    let pols: Vec<u8> = (0..n).map(|_| c.u8()).collect::<Option<_>>()?;
    let quals: Vec<u8> = (0..n).map(|_| c.u8()).collect::<Option<_>>()?;
    for i in 0..n {
        let key: EvKey = (
            subjects[i],
            relation_of(RELS.get(rels[i] as usize)?)?.as_str(),
            objects[i],
            propositions[i],
            lineages[i],
            polarity_of(POLARITIES.get(pols[i] as usize)?)?.as_str(),
            quality_of(QUALITIES.get(quals[i] as usize)?)?.as_str(),
            epochs[i],
        );
        *l.evidence.entry(key).or_default() += 1;
    }
    (c.pos == body.len()).then_some(l)
}

// ------------------------------------------------------------ session

enum Reply {
    Value(Value),
    Query { generation: u64, result: Vec<u8> },
}

struct Live {
    logical: Logical,
    derived: Derived,
    generation: u64,
}

struct Session {
    repository: PathBuf,
    live: Option<Live>,
    max_line_bytes: u64,
    chunk_bytes: usize,
}

fn ok(id: u64, fields: Value) -> Value {
    let mut map = fields.as_object().cloned().unwrap_or_default();
    map.insert("id".into(), json!(id));
    map.insert("ok".into(), json!(true));
    Value::Object(map)
}
fn error_value(id: Option<u64>, f: &Fail) -> Value {
    json!({"id": id, "ok": false, "code": f.code, "message": f.message})
}
fn check_fields(req: &Map<String, Value>, allowed: &[&str]) -> Result<(), Fail> {
    if let Some(k) = req.keys().find(|k| !allowed.contains(&k.as_str())) {
        return Err(invalid_request(format!("unexpected field {k}")));
    }
    if let Some(k) = allowed.iter().find(|k| !req.contains_key(**k)) {
        return Err(invalid_request(format!("missing field {k}")));
    }
    Ok(())
}

impl Session {
    fn live(&mut self) -> Result<&mut Live, Fail> {
        self.live
            .as_mut()
            .ok_or_else(|| fail("INVALID_STATE", "session is not open"))
    }

    fn handle(&mut self, id: u64, op: &str, req: &Map<String, Value>) -> Result<Reply, Fail> {
        match op {
            "open" => {
                check_fields(req, &["id", "op", "binding", "max_line_bytes", "source"])?;
                if req["binding"] != BINDING {
                    return Err(invalid_request("unsupported binding"));
                }
                let max = req["max_line_bytes"]
                    .as_u64()
                    .filter(|n| *n >= MIN_LINE)
                    .ok_or_else(|| invalid_request("max_line_bytes must be an integer >= 65536"))?;
                let source = req["source"]
                    .as_object()
                    .ok_or_else(|| invalid_request("source must be an object"))?;
                let logical = match source.get("kind").and_then(Value::as_str) {
                    Some("fixture") => {
                        check_fields(source, &["kind", "path"])?;
                        let path = source["path"]
                            .as_str()
                            .ok_or_else(|| invalid_request("path must be a string"))?;
                        if self.live.is_some() {
                            return Err(fail("INVALID_STATE", "session already open"));
                        }
                        let bytes = std::fs::read(path)
                            .map_err(|e| invalid_input(format!("{path}: {e}")))?;
                        let fx: Fixture = serde_json::from_slice(&bytes)
                            .map_err(|e| invalid_input(format!("fixture: {e}")))?;
                        let derived = Derived::build(fx)?;
                        let logical = Logical::from_fixture(derived.store().fx);
                        return self.finish_open(id, max, logical, Some(derived));
                    }
                    Some("empty") => {
                        check_fields(source, &["kind", "context"])?;
                        let ctx = source["context"]
                            .as_object()
                            .ok_or_else(|| invalid_request("context must be an object"))?;
                        check_fields(ctx, &["snapshot", "epoch", "complete"])?;
                        let snapshot = ctx["snapshot"]
                            .as_str()
                            .ok_or_else(|| invalid_request("snapshot must be a string"))?;
                        let epoch = match &ctx["epoch"] {
                            Value::Null => None,
                            v => Some(v.as_u64().ok_or_else(|| {
                                invalid_request("epoch must be an integer or null")
                            })?),
                        };
                        let complete = ctx["complete"]
                            .as_bool()
                            .ok_or_else(|| invalid_request("complete must be a boolean"))?;
                        Logical::empty(snapshot.to_string(), epoch, complete)
                    }
                    _ => return Err(invalid_request("unknown source kind")),
                };
                if self.live.is_some() {
                    return Err(fail("INVALID_STATE", "session already open"));
                }
                self.finish_open(id, max, logical, None)
            }
            "query" => {
                check_fields(req, &["id", "op", "query"])?;
                let live = self.live()?;
                let bytes = live
                    .derived
                    .store()
                    .execute(&req["query"])
                    .map_err(|e| invalid_input(e.to_string()))?;
                Ok(Reply::Query {
                    generation: live.generation,
                    result: bytes,
                })
            }
            "mutate" => {
                check_fields(req, &["id", "op", "batch"])?;
                let live = self.live()?;
                let next = live.logical.applied(&req["batch"])?;
                let derived = Derived::build(next.to_fixture())?;
                live.logical = next;
                live.derived = derived;
                live.generation += 1;
                Ok(Reply::Value(ok(id, json!({"generation": live.generation}))))
            }
            "state_digest" => {
                check_fields(req, &["id", "op"])?;
                let live = self.live()?;
                let start = std::time::Instant::now();
                let (digest, bytes) = live.logical.digest()?;
                let ms = start.elapsed().as_secs_f64() * 1000.0;
                Ok(Reply::Value(ok(
                    id,
                    json!({"generation": live.generation, "state_digest": digest,
                           "state_digest_ms": ms, "bytes_processed": bytes}),
                )))
            }
            "snapshot" => {
                check_fields(req, &["id", "op"])?;
                let repository = self.repository.clone();
                let live = self.live()?;
                let data = encode_snapshot(&live.logical);
                let snapshot_id = format!("snap-{}", &hex(&Sha256::digest(&data))[..16]);
                std::fs::create_dir_all(&repository)
                    .map_err(|e| fail("INTERNAL", e.to_string()))?;
                let target = repository.join(format!("{snapshot_id}.snap"));
                let temp = repository.join(format!("{snapshot_id}.tmp{}", std::process::id()));
                std::fs::write(&temp, &data)
                    .and_then(|_| std::fs::rename(&temp, &target))
                    .map_err(|e| fail("INTERNAL", e.to_string()))?;
                Ok(Reply::Value(ok(
                    id,
                    json!({"generation": live.generation, "snapshot_id": snapshot_id,
                           "captured_generation": live.generation}),
                )))
            }
            "restore" => {
                check_fields(req, &["id", "op", "snapshot_id"])?;
                let repository = self.repository.clone();
                let live = self.live()?;
                let snapshot_id = req["snapshot_id"]
                    .as_str()
                    .filter(|s| {
                        !s.is_empty()
                            && s.bytes()
                                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
                    })
                    .ok_or_else(|| invalid_input("unknown snapshot_id"))?;
                let data = std::fs::read(repository.join(format!("{snapshot_id}.snap")))
                    .map_err(|_| invalid_input("unknown snapshot_id"))?;
                let restored = decode_snapshot(&data).ok_or_else(|| {
                    invalid_input("snapshot is corrupt or from an incompatible artifact")
                })?;
                if restored.snapshot != live.logical.snapshot
                    || restored.epoch != live.logical.epoch
                    || restored.complete != live.logical.complete
                {
                    return Err(invalid_input("context mismatch"));
                }
                let derived = Derived::build(restored.to_fixture())?;
                live.logical = restored;
                live.derived = derived;
                live.generation += 1;
                Ok(Reply::Value(ok(id, json!({"generation": live.generation}))))
            }
            "stats" => {
                check_fields(req, &["id", "op"])?;
                let live = self.live()?;
                let [entities, relations, evidence, unique] = live.logical.counts();
                let snap = heap_snapshot();
                Ok(Reply::Value(ok(
                    id,
                    json!({"generation": live.generation, "stats": {
                        "schema": "csl.eval.session.stats/v0.1", "generation": live.generation,
                        "entities": entities, "relations": relations, "evidence": evidence,
                        "unique_strings": unique, "live_heap_bytes": heap_live(),
                        "peak_heap_bytes": snap[4], "heap_breakdown": null,
                        "allocations_total": snap[0]}}),
                )))
            }
            "cancel" => {
                check_fields(req, &["id", "op", "target"])?;
                self.live()?;
                Err(fail("UNSUPPORTED", "cancellation is not advertised"))
            }
            "close" => {
                check_fields(req, &["id", "op"])?;
                Ok(Reply::Value(ok(id, json!({}))))
            }
            _ => Err(invalid_request(format!("unknown operation {op}"))),
        }
    }

    fn finish_open(
        &mut self,
        id: u64,
        max: u64,
        logical: Logical,
        derived: Option<Derived>,
    ) -> Result<Reply, Fail> {
        let derived = match derived {
            Some(d) => d,
            None => Derived::build(logical.to_fixture())?,
        };
        self.max_line_bytes = max;
        // A chunk line is `{"data":"<base64>","frame":"chunk","id":N,"seq":K}` plus a newline:
        // reserve 256 bytes for the envelope and take 3 raw bytes per 4 base64 characters.
        self.chunk_bytes = ((max as usize - 256) / 4) * 3;
        self.live = Some(Live {
            logical,
            derived,
            generation: 0,
        });
        Ok(Reply::Value(ok(
            id,
            json!({"generation": 0, "semantics": SEMANTICS, "binding": BINDING, "max_line_bytes": max,
                   "chunk_bytes": self.chunk_bytes, "capabilities": [],
                   "strategy": {"mutation": "full-rebuild"}, "artifact": ARTIFACT}),
        )))
    }
}

// ----------------------------------------------------------- transport

struct Assembly {
    id: u64,
    op: String,
    parts: Vec<u8>,
    chunks: u64,
    skipping: bool,
}

fn write_line(out: &mut impl Write, value: &Value) {
    let mut line = serde_json::to_vec(value).expect("serializable");
    line.push(b'\n');
    out.write_all(&line).expect("stdout");
    out.flush().expect("stdout");
}

fn send_query(out: &mut impl Write, session: &Session, id: u64, generation: u64, result: &[u8]) {
    let mut line =
        format!("{{\"generation\":{generation},\"id\":{id},\"ok\":true,\"result\":").into_bytes();
    line.extend_from_slice(result);
    line.extend_from_slice(b"}\n");
    if (line.len() as u64) <= session.max_line_bytes {
        out.write_all(&line).expect("stdout");
        out.flush().expect("stdout");
        return;
    }
    write_line(out, &json!({"id": id, "frame": "begin"}));
    let mut hasher = Sha256::new();
    let mut chunks = 0u64;
    for (seq, chunk) in result.chunks(session.chunk_bytes).enumerate() {
        hasher.update(chunk);
        let mut data = Vec::with_capacity(chunk.len() / 3 * 4 + 4);
        b64_encode(chunk, &mut data);
        let mut line = format!(
            "{{\"data\":\"{}\",",
            String::from_utf8(data).expect("ascii")
        )
        .into_bytes();
        line.extend_from_slice(
            format!("\"frame\":\"chunk\",\"id\":{id},\"seq\":{seq}}}\n").as_bytes(),
        );
        out.write_all(&line).expect("stdout");
        chunks += 1;
    }
    out.flush().expect("stdout");
    write_line(
        out,
        &json!({"id": id, "frame": "end", "ok": true, "generation": generation, "chunks": chunks,
                "bytes": result.len(), "sha256": hex(&hasher.finalize())}),
    );
}

fn dispatch(session: &mut Session, out: &mut impl Write, req: Value) -> bool {
    let Some(map) = req.as_object() else {
        write_line(
            out,
            &error_value(None, &invalid_request("request must be an object")),
        );
        return false;
    };
    let Some(id) = map.get("id").and_then(Value::as_u64) else {
        write_line(
            out,
            &error_value(None, &invalid_request("id must be a non-negative integer")),
        );
        return false;
    };
    let Some(op) = map.get("op").and_then(Value::as_str) else {
        write_line(
            out,
            &error_value(Some(id), &invalid_request("op must be a string")),
        );
        return false;
    };
    let closing = op == "close";
    match session.handle(id, op, map) {
        Ok(Reply::Value(value)) => {
            write_line(out, &value);
            closing
        }
        Ok(Reply::Query { generation, result }) => {
            send_query(out, session, id, generation, &result);
            false
        }
        Err(f) => {
            write_line(out, &error_value(Some(id), &f));
            false
        }
    }
}

/// Serve the session on stdin/stdout until `close` or end of input; returns the exit code.
pub fn run(repository: &Path) -> i32 {
    let mut session = Session {
        repository: repository.to_path_buf(),
        live: None,
        max_line_bytes: MIN_LINE,
        chunk_bytes: 0,
    };
    let stdin = std::io::stdin();
    let mut input = stdin.lock();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let mut assembly: Option<Assembly> = None;
    let mut line = Vec::new();
    loop {
        line.clear();
        match input.read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => return 0, // end of input while open is an implicit close
            Ok(_) => {}
        }
        let text = line.strip_suffix(b"\n").unwrap_or(&line);
        let value: Value = match serde_json::from_slice(text) {
            Ok(v) => v,
            Err(e) => {
                write_line(
                    &mut out,
                    &error_value(None, &invalid_request(format!("malformed JSON: {e}"))),
                );
                continue;
            }
        };
        let frame = value
            .get("frame")
            .and_then(Value::as_str)
            .map(str::to_string);
        let Some(frame) = frame else {
            if dispatch(&mut session, &mut out, value) {
                return 0;
            }
            continue;
        };
        let id = value.get("id").and_then(Value::as_u64);
        let reject = |out: &mut std::io::StdoutLock, a: &mut Option<Assembly>, message: &str| {
            write_line(out, &error_value(id, &invalid_request(message)));
            if let Some(a) = a {
                a.skipping = true;
            }
        };
        match frame.as_str() {
            "begin" => {
                let (Some(id), Some(op)) = (id, value.get("op").and_then(Value::as_str)) else {
                    write_line(
                        &mut out,
                        &error_value(id, &invalid_request("begin needs id and op")),
                    );
                    continue;
                };
                assembly = Some(Assembly {
                    id,
                    op: op.to_string(),
                    parts: Vec::new(),
                    chunks: 0,
                    skipping: false,
                });
            }
            "chunk" => match assembly.as_mut() {
                Some(a) if a.skipping && Some(a.id) == id => {}
                Some(a) if Some(a.id) == id => {
                    let data = value
                        .get("data")
                        .and_then(Value::as_str)
                        .and_then(b64_decode);
                    let seq = value.get("seq").and_then(Value::as_u64);
                    match (data, seq) {
                        (Some(data), Some(seq)) if seq == a.chunks => {
                            a.parts.extend_from_slice(&data);
                            a.chunks += 1;
                        }
                        _ => reject(&mut out, &mut assembly, "bad chunk frame"),
                    }
                }
                _ => write_line(
                    &mut out,
                    &error_value(id, &invalid_request("chunk without begin")),
                ),
            },
            "end" => match assembly.take() {
                Some(a) if a.skipping && Some(a.id) == id => {}
                Some(a) if Some(a.id) == id => {
                    let hash = hex(&Sha256::digest(&a.parts));
                    let consistent = value.get("chunks").and_then(Value::as_u64) == Some(a.chunks)
                        && value.get("bytes").and_then(Value::as_u64) == Some(a.parts.len() as u64)
                        && value.get("sha256").and_then(Value::as_str) == Some(hash.as_str());
                    let body =
                        serde_json::from_slice::<Value>(&a.parts)
                            .ok()
                            .and_then(|v| match v {
                                Value::Object(m)
                                    if !m.contains_key("id") && !m.contains_key("op") =>
                                {
                                    Some(m)
                                }
                                _ => None,
                            });
                    match (consistent, body) {
                        (true, Some(mut m)) => {
                            m.insert("id".into(), json!(a.id));
                            m.insert("op".into(), json!(a.op));
                            if dispatch(&mut session, &mut out, Value::Object(m)) {
                                return 0;
                            }
                        }
                        _ => write_line(
                            &mut out,
                            &error_value(
                                Some(a.id),
                                &invalid_request("chunked request failed verification"),
                            ),
                        ),
                    }
                }
                other => {
                    assembly = other;
                    write_line(
                        &mut out,
                        &error_value(id, &invalid_request("end without begin")),
                    );
                }
            },
            _ => write_line(
                &mut out,
                &error_value(id, &invalid_request("unknown frame")),
            ),
        }
    }
}
