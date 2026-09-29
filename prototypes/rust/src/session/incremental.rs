//! True incremental mutation for the S0 session (`--strategy incremental`, ADR-0008 section 14).
//!
//! `IncStore` owns every derived structure and updates it in place, at a cost proportional to the
//! batch (plus local sizes such as one node's degree):
//!   * entities: id -> {kind, name, container, refs}. `refs` counts the rows that reference the
//!     entity (relation rows and evidence rows once per endpoint, plus entities naming it as their
//!     container), so "removal of a still-referenced entity" is decided from counters, not a scan;
//!   * outgoing / incoming adjacency: id -> edges kept sorted by (subject, relation, object)
//!     (duplicates retained), inserted by binary search, removed one occurrence at a time;
//!   * evidence: a slot store (rows + live flags + free list) with a row -> slots index, so removing
//!     one occurrence is O(1) and slots are reused;
//!   * name index: name -> ascending entity ids, updated on ADD/UPDATE/REMOVE entity.
//!
//! A batch is validated first against an overlay sized O(|batch|); only a valid batch is applied,
//! so a rejected batch leaves every structure untouched (no undo needed). Queries run the same
//! algorithm as the one-shot `Store`, so results are byte-identical to it and to the oracle.
use super::super::model::{Edge, Evidence, Kind};
use super::super::{FxBuild, QUALITY, REL, Set, id, member, valid_query};
use super::{Ent, Fail, Logical, Parsed, ev_of, invalid_input, kind_of, optional_id, relation_of};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::HashMap;

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
type EvNum = (u64, u64, u64, u64, u32, u8, u8, u8);

fn ev_num(e: &Evidence) -> EvNum {
    (
        e.subject,
        e.object,
        e.proposition,
        e.freshness_epoch,
        e.lineage,
        e.relation as u8,
        e.polarity as u8,
        e.quality as u8,
    )
}
fn edge_key(e: &Edge) -> (u64, &'static str, u64) {
    (e.subject, e.relation.as_str(), e.object)
}
/// Insert keeping the list sorted by (subject, relation, object); a duplicate goes after its twins.
fn insert_edge(list: &mut Vec<Edge>, edge: Edge) {
    let key = edge_key(&edge);
    let at = list.partition_point(|x| edge_key(x) <= key);
    list.insert(at, edge);
}
/// Number of occurrences of `edge` and the index of the first one.
fn edge_span(list: &[Edge], edge: &Edge) -> (usize, usize) {
    let key = edge_key(edge);
    let lo = list.partition_point(|x| edge_key(x) < key);
    let hi = list.partition_point(|x| edge_key(x) <= key);
    (lo, hi - lo)
}
fn remove_edge(map: &mut HashMap<u64, Vec<Edge>, FxBuild>, node: u64, edge: &Edge) {
    if let Some(list) = map.get_mut(&node) {
        let (at, n) = edge_span(list, edge);
        if n > 0 {
            list.remove(at);
        }
        if list.is_empty() {
            map.remove(&node);
        }
    }
}

struct IncEnt {
    kind: Kind,
    name: String,
    container: Option<u64>,
    refs: u32,
}

/// What a batch does to one entity id.
enum Final {
    Gone,
    Set(Kind, String, Option<u64>),
}

pub(super) struct IncStore {
    snapshot: String,
    epoch: Option<u64>,
    complete: bool,
    entities: HashMap<u64, IncEnt, FxBuild>,
    out: HashMap<u64, Vec<Edge>, FxBuild>,
    inc: HashMap<u64, Vec<Edge>, FxBuild>,
    names: HashMap<String, Vec<u64>>,
    ev: Vec<Evidence>,
    live: Vec<bool>,
    pos: HashMap<EvNum, Vec<u32>, FxBuild>,
    free: Vec<u32>,
    ev_count: u64,
    n_relations: u64,
}

impl IncStore {
    /// Bulk build (open, restore): a fresh store from a valid logical state.
    pub(super) fn from_logical(l: &Logical) -> IncStore {
        let mut s = IncStore {
            snapshot: l.snapshot.clone(),
            epoch: l.epoch,
            complete: l.complete,
            entities: HashMap::default(),
            out: HashMap::default(),
            inc: HashMap::default(),
            names: HashMap::new(),
            ev: Vec::new(),
            live: Vec::new(),
            pos: HashMap::default(),
            free: Vec::new(),
            ev_count: 0,
            n_relations: 0,
        };
        for (i, e) in &l.entities {
            s.entities.insert(
                *i,
                IncEnt {
                    kind: e.kind,
                    name: e.name.clone(),
                    container: e.container,
                    refs: 0,
                },
            );
            s.names.entry(e.name.clone()).or_default().push(*i); // ascending: BTreeMap order
        }
        for e in l.entities.values() {
            if let Some(c) = e.container {
                s.entities.get_mut(&c).expect("container").refs += 1;
            }
        }
        for ((sub, rel, obj), n) in &l.relations {
            let edge = Edge {
                subject: *sub,
                relation: relation_of(rel).expect("relation"),
                object: *obj,
            };
            for _ in 0..*n {
                s.out.entry(*sub).or_default().push(edge); // key order keeps each list sorted
                s.inc.entry(*obj).or_default().push(edge);
            }
            s.n_relations += n;
            s.entities.get_mut(sub).expect("subject").refs += *n as u32;
            s.entities.get_mut(obj).expect("object").refs += *n as u32;
        }
        for (k, n) in &l.evidence {
            let e = ev_of(k);
            for _ in 0..*n {
                s.push_evidence(e);
            }
            s.entities.get_mut(&e.subject).expect("subject").refs += *n as u32;
            s.entities.get_mut(&e.object).expect("object").refs += *n as u32;
        }
        s
    }

    fn push_evidence(&mut self, e: Evidence) {
        let slot = match self.free.pop() {
            Some(slot) => {
                self.ev[slot as usize] = e;
                self.live[slot as usize] = true;
                slot
            }
            None => {
                self.ev.push(e);
                self.live.push(true);
                (self.ev.len() - 1) as u32
            }
        };
        self.pos.entry(ev_num(&e)).or_default().push(slot);
        self.ev_count += 1;
    }
    fn pop_evidence(&mut self, e: &Evidence) {
        let key = ev_num(e);
        if let Some(slots) = self.pos.get_mut(&key)
            && let Some(slot) = slots.pop()
        {
            self.live[slot as usize] = false;
            self.free.push(slot);
            self.ev_count -= 1;
            if slots.is_empty() {
                self.pos.remove(&key);
            }
        }
    }
    fn name_insert(&mut self, name: &str, id: u64) {
        let list = self.names.entry(name.to_string()).or_default();
        let at = list.partition_point(|x| *x < id);
        list.insert(at, id);
    }
    fn name_remove(&mut self, name: &str, id: u64) {
        if let Some(list) = self.names.get_mut(name) {
            if let Ok(at) = list.binary_search(&id) {
                list.remove(at);
            }
            if list.is_empty() {
                self.names.remove(name);
            }
        }
    }

    pub(super) fn context(&self) -> (String, Option<u64>, bool) {
        (self.snapshot.clone(), self.epoch, self.complete)
    }

    pub(super) fn counts(&self) -> [u64; 4] {
        [
            self.entities.len() as u64,
            self.n_relations,
            self.ev_count,
            self.names.len() as u64,
        ]
    }

    /// The logical state (for `state_digest` and `snapshot`, whose cost is not mutation cost).
    pub(super) fn to_logical(&self) -> Logical {
        let mut l = Logical::empty(self.snapshot.clone(), self.epoch, self.complete);
        for (i, e) in &self.entities {
            l.entities.insert(
                *i,
                Ent {
                    kind: e.kind,
                    name: e.name.clone(),
                    container: e.container,
                },
            );
        }
        for list in self.out.values() {
            for e in list {
                *l.relations
                    .entry((e.subject, e.relation.as_str(), e.object))
                    .or_default() += 1;
            }
        }
        for (e, live) in self.ev.iter().zip(&self.live) {
            if *live {
                *l.evidence.entry(super::ev_key(e)).or_default() += 1;
            }
        }
        l
    }

    // ------------------------------------------------------------ mutation

    /// Validate `batch` against an overlay of the touched ids, then apply it in place.
    /// Errors and their codes match `oracle/session_model.py::_applied`.
    pub(super) fn apply(&mut self, p: &Parsed) -> std::result::Result<(), Fail> {
        let mut fin: HashMap<u64, Final> = HashMap::new();
        let mut delta: HashMap<u64, i64> = HashMap::new();
        for (obj, kind) in &p.entity_ops {
            let id = obj["id"].as_u64().filter(|n| *n >= 1);
            match *kind {
                "ADD_ENTITY" => {
                    let bad = || invalid_input("invalid ADD_ENTITY");
                    let id = id.ok_or_else(bad)?;
                    let name = obj["name"].as_str().ok_or_else(bad)?;
                    let k = obj["kind"].as_str().and_then(kind_of).ok_or_else(bad)?;
                    let container = optional_id(obj.get("container")).ok_or_else(bad)?;
                    if self.entities.contains_key(&id) {
                        return Err(bad());
                    }
                    if let Some(c) = container {
                        *delta.entry(c).or_default() += 1;
                    }
                    fin.insert(id, Final::Set(k, name.to_string(), container));
                }
                "REMOVE_ENTITY" => {
                    let id = id
                        .filter(|i| self.entities.contains_key(i))
                        .ok_or_else(|| invalid_input("REMOVE_ENTITY of a missing id"))?;
                    if let Some(c) = self.entities[&id].container {
                        *delta.entry(c).or_default() -= 1;
                    }
                    fin.insert(id, Final::Gone);
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
                    let current = &self.entities[&id];
                    let k = match changes.get("kind") {
                        Some(v) => v.as_str().and_then(kind_of).ok_or_else(bad)?,
                        None => current.kind,
                    };
                    let name = match changes.get("name") {
                        Some(v) => v.as_str().ok_or_else(bad)?.to_string(),
                        None => current.name.clone(),
                    };
                    let container = match changes.get("container") {
                        Some(v) => optional_id(Some(v)).ok_or_else(bad)?,
                        None => current.container,
                    };
                    if container != current.container {
                        if let Some(c) = current.container {
                            *delta.entry(c).or_default() -= 1;
                        }
                        if let Some(c) = container {
                            *delta.entry(c).or_default() += 1;
                        }
                    }
                    fin.insert(id, Final::Set(k, name, container));
                }
            }
        }
        // Multiplicities are checked against the pre-batch store.
        for (k, n) in &p.rel_rem {
            let edge = Edge {
                subject: k.0,
                relation: relation_of(k.1).expect("relation"),
                object: k.2,
            };
            let have = self.out.get(&k.0).map_or(0, |l| edge_span(l, &edge).1);
            if (have as u64) < *n {
                return Err(invalid_input("removing more occurrences than exist"));
            }
        }
        for (k, n) in &p.ev_rem {
            let have = self.pos.get(&ev_num(&ev_of(k))).map_or(0, Vec::len);
            if (have as u64) < *n {
                return Err(invalid_input("removing more occurrences than exist"));
            }
        }
        let exists = |id: u64| match fin.get(&id) {
            Some(Final::Gone) => false,
            Some(Final::Set(..)) => true,
            None => self.entities.contains_key(&id),
        };
        for (k, n) in &p.rel_add {
            if !exists(k.0) || !exists(k.2) {
                return Err(invalid_input("relation references a missing entity"));
            }
            *delta.entry(k.0).or_default() += *n as i64;
            *delta.entry(k.2).or_default() += *n as i64;
        }
        for (k, n) in &p.rel_rem {
            *delta.entry(k.0).or_default() -= *n as i64;
            *delta.entry(k.2).or_default() -= *n as i64;
        }
        for (k, n) in &p.ev_add {
            if !exists(k.0) || !exists(k.2) {
                return Err(invalid_input("evidence references a missing entity"));
            }
            *delta.entry(k.0).or_default() += *n as i64;
            *delta.entry(k.2).or_default() += *n as i64;
        }
        for (k, n) in &p.ev_rem {
            *delta.entry(k.0).or_default() -= *n as i64;
            *delta.entry(k.2).or_default() -= *n as i64;
        }
        for f in fin.values() {
            if let Final::Set(_, _, Some(c)) = f
                && !exists(*c)
            {
                return Err(invalid_input("container references a missing entity"));
            }
        }
        for (id, f) in &fin {
            if matches!(f, Final::Gone) {
                let refs = self.entities[id].refs as i64 + delta.get(id).copied().unwrap_or(0);
                if refs != 0 {
                    return Err(invalid_input(
                        "entity is still referenced (no implicit cascade)",
                    ));
                }
            }
        }

        // The batch is valid: apply in place.
        for (id, f) in fin {
            match f {
                Final::Gone => {
                    let e = self.entities.remove(&id).expect("present");
                    self.name_remove(&e.name, id);
                }
                Final::Set(k, name, container) => {
                    if let Some(e) = self.entities.get_mut(&id) {
                        let old = std::mem::replace(&mut e.name, name.clone());
                        e.kind = k;
                        e.container = container;
                        if old != name {
                            self.name_remove(&old, id);
                            self.name_insert(&name, id);
                        }
                    } else {
                        self.name_insert(&name, id);
                        self.entities.insert(
                            id,
                            IncEnt {
                                kind: k,
                                name,
                                container,
                                refs: 0,
                            },
                        );
                    }
                }
            }
        }
        for (k, n) in &p.rel_rem {
            let edge = Edge {
                subject: k.0,
                relation: relation_of(k.1).expect("relation"),
                object: k.2,
            };
            for _ in 0..*n {
                remove_edge(&mut self.out, k.0, &edge);
                remove_edge(&mut self.inc, k.2, &edge);
            }
            self.n_relations -= n;
        }
        for (k, n) in &p.rel_add {
            let edge = Edge {
                subject: k.0,
                relation: relation_of(k.1).expect("relation"),
                object: k.2,
            };
            for _ in 0..*n {
                insert_edge(self.out.entry(k.0).or_default(), edge);
                insert_edge(self.inc.entry(k.2).or_default(), edge);
            }
            self.n_relations += n;
        }
        for (k, n) in &p.ev_rem {
            let e = ev_of(k);
            for _ in 0..*n {
                self.pop_evidence(&e);
            }
        }
        for (k, n) in &p.ev_add {
            let e = ev_of(k);
            for _ in 0..*n {
                self.push_evidence(e);
            }
        }
        for (id, d) in delta {
            if let Some(e) = self.entities.get_mut(&id) {
                e.refs = (e.refs as i64 + d) as u32;
            }
        }
        Ok(())
    }

    // -------------------------------------------------------------- queries

    fn eval(&self, q: &Value, truncated: &mut bool) -> Result<Set> {
        let op = q["op"].as_str().ok_or("missing op")?;
        if op == "RESOLVE" {
            if let Some(name) = q["name"].as_str() {
                return Ok(self
                    .names
                    .get(name)
                    .map_or(&[][..], Vec::as_slice)
                    .iter()
                    .copied()
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
        let base: Set = if op == "FILTER" && q.get("input").is_none() {
            self.entities.keys().copied().collect()
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

    pub(super) fn execute(&self, q: &Value) -> Result<Vec<u8>> {
        if q["schema"] != "csl.eval.query/v0.1" || !q["query_id"].is_string() {
            return Err("invalid query schema".into());
        }
        valid_query(q)?;
        let mut truncated = false;
        let selected = self.eval(q, &mut truncated)?;
        let mut ids: Vec<_> = selected.iter().copied().collect();
        ids.sort_unstable();
        let minimum = q["evidence"]["min_quality"]
            .as_str()
            .and_then(|v| QUALITY.iter().position(|x| *x == v));
        let epoch = q["evidence"]["freshness_epoch"].as_u64();
        let mut props: Vec<Evidence> = self
            .ev
            .iter()
            .zip(&self.live)
            .filter(|(_, live)| **live)
            .map(|(e, _)| e)
            .filter(|e| selected.contains(&e.subject) || selected.contains(&e.object))
            .filter(|e| minimum.is_none_or(|m| e.quality as usize >= m))
            .filter(|e| epoch.is_none_or(|x| e.freshness_epoch == x))
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
        let entity_set = if truncated {
            "TRUNCATED"
        } else if self.complete {
            "COMPLETE"
        } else {
            "OBSERVED"
        };
        let payload = Payload {
            completeness: Completeness { entity_set },
            entities: &ids,
            knowledge: Knowledge {
                model: "open-world",
            },
            propositions: &props,
        };
        let digest = format!("sha256:{:x}", Sha256::digest(serde_json::to_vec(&payload)?));
        let envelope = Envelope {
            completeness: Completeness { entity_set },
            digest,
            entities: &ids,
            knowledge: Knowledge {
                model: "open-world",
            },
            propositions: &props,
            query_id: q["query_id"].as_str().ok_or("missing query_id")?,
            schema: "csl.eval.result/v0.1",
            snapshot: &self.snapshot,
        };
        Ok(serde_json::to_vec(&envelope)?)
    }
}
