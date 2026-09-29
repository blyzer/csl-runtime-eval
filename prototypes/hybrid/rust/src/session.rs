//! Hybrid S0 front end: the Rust process owns stdin/stdout, the JSONL transport and the chunk framing;
//! the Zig side owns the store through the EXPERIMENTAL persistent boundary
//! (`abi/csl_session_experimental.h`, W10; not the stable ABI v1, not a production API).
//!
//! The semantics are not reimplemented here. Per request the front end
//!   1. reads a complete line (or reassembles chunked-request frames),
//!   2. scans it with `RawValue` slices (no `serde_json::Value` tree is ever built for a request body:
//!      a large `mutate` batch is validated as JSON syntax and handed to Zig as bytes),
//!   3. makes ONE coarse call into Zig for the operation,
//!   4. forwards the canonical response, chunking a large query result itself.
//!
//! Instrumentation (`stats.boundary`, added to the engine's `stats` response):
//! * BoundaryTax of one call = wall time of the FFI call measured here MINUS the `service_ns` the Zig
//!   engine reports for it, i.e. crossing, dispatch, the engine-side reply copies and the arena setup and
//!   teardown around the engine. It is summed per op.
//! * bytes_in / bytes_out: request bytes and response (line + payload) bytes that crossed.
//! * copies / copy_bytes: memcpys of payload bytes performed because of the boundary: the Zig side copies
//!   each non-empty reply buffer out of its per-request arena into a caller-owned allocation. The request
//!   crosses by pointer (zero copies) and Rust reads replies in place.
//! * allocations / ownership_transitions: Zig-allocated buffers that crossed to Rust; releases: buffers
//!   handed back through `csl_buffer_release`.
//! * wrapper_ns_total: Rust-side protocol time (envelope scan, service_ns splice, stats rewrite, frame
//!   building) between the complete request and the response being ready, excluding the FFI call. It is
//!   reported separately: it is not boundary tax.
//!
//! The counters in a `stats` response cover every call completed before it plus the FFI crossing of that
//! `stats` call itself (its wrapper time and its reply buffer release are not yet counted).
use serde_json::{Value, json, value::RawValue};
use sha2::{Digest, Sha256};
use std::alloc::{GlobalAlloc, Layout, System};
use std::collections::BTreeMap;
use std::ffi::c_void;
use std::io::{BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicU64, Ordering::Relaxed};
use std::time::Instant;

use crate::{CslBuffer, csl_buffer_release};

// ---------------------------------------------------------------- wrapper-side heap counters
static STATS_ON: AtomicBool = AtomicBool::new(false);
static ALLOCATIONS: AtomicU64 = AtomicU64::new(0);
static LIVE: AtomicI64 = AtomicI64::new(0);
static PEAK_LIVE: AtomicI64 = AtomicI64::new(0);
struct CountingAlloc;
fn note_alloc(size: usize) {
    ALLOCATIONS.fetch_add(1, Relaxed);
    let live = LIVE.fetch_add(size as i64, Relaxed) + size as i64;
    PEAK_LIVE.fetch_max(live, Relaxed);
}
// SAFETY: every method forwards to `System` with the caller's layout/pointer and only updates atomic
// counters afterwards, so the `GlobalAlloc` contract is inherited as-is. Zig allocates through its own
// C allocator and is not counted here.
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
            LIVE.fetch_sub(layout.size() as i64, Relaxed);
        }
    }
    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        let new = unsafe { System.realloc(ptr, layout, new_size) };
        if STATS_ON.load(Relaxed) && !new.is_null() {
            ALLOCATIONS.fetch_add(1, Relaxed);
            let delta = new_size as i64 - layout.size() as i64;
            let live = LIVE.fetch_add(delta, Relaxed) + delta;
            PEAK_LIVE.fetch_max(live, Relaxed);
        }
        new
    }
}
#[global_allocator]
static GLOBAL: CountingAlloc = CountingAlloc;

// ---------------------------------------------------------------- experimental boundary (FFI)
#[repr(C)]
#[derive(Default)]
struct CslReply {
    line: CslBuffer,
    payload: CslBuffer,
    service_ns: u64,
    generation: u64,
    id: u64,
    flags: u32,
    reserved: u32,
}
const FLAG_OK: u32 = 0x01;
const FLAG_CHUNKED: u32 = 0x02;
const FLAG_CLOSE: u32 = 0x04;
type OpFn = unsafe extern "C" fn(*mut c_void, *const u8, usize, *mut CslReply) -> i32;
unsafe extern "C" {
    fn csl_session_abi_version() -> u32;
    fn csl_session_create(
        repository: *const u8,
        repository_len: usize,
        strategy: *const u8,
        strategy_len: usize,
        out: *mut *mut c_void,
    ) -> i32;
    fn csl_session_destroy(session: *mut c_void);
    fn csl_session_open(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_query(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_mutate(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_state_digest(
        s: *mut c_void,
        line: *const u8,
        len: usize,
        r: *mut CslReply,
    ) -> i32;
    fn csl_session_snapshot(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_restore(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_stats(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_cancel(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_close(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
    fn csl_session_request(s: *mut c_void, line: *const u8, len: usize, r: *mut CslReply) -> i32;
}
const OPS: [(&str, OpFn); 9] = [
    ("open", csl_session_open),
    ("query", csl_session_query),
    ("mutate", csl_session_mutate),
    ("state_digest", csl_session_state_digest),
    ("snapshot", csl_session_snapshot),
    ("restore", csl_session_restore),
    ("stats", csl_session_stats),
    ("cancel", csl_session_cancel),
    ("close", csl_session_close),
];

/// Zig-owned reply buffers, handed back to Zig exactly once when this guard drops.
struct Owned {
    reply: CslReply,
}
static RELEASES: AtomicU64 = AtomicU64::new(0);
impl Owned {
    fn line(&self) -> &[u8] {
        buffer_bytes(&self.reply.line)
    }
    fn payload(&self) -> &[u8] {
        buffer_bytes(&self.reply.payload)
    }
}
fn buffer_bytes(b: &CslBuffer) -> &[u8] {
    if b.len == 0 {
        &[]
    } else {
        // SAFETY: a successful call returned a live allocation of `len` bytes, valid until released.
        unsafe { std::slice::from_raw_parts(b.ptr, b.len) }
    }
}
impl Drop for Owned {
    fn drop(&mut self) {
        for buffer in [
            std::mem::take(&mut self.reply.line),
            std::mem::take(&mut self.reply.payload),
        ] {
            if buffer.len > 0 {
                RELEASES.fetch_add(1, Relaxed);
                // SAFETY: this guard is the only owner; release hands the allocation back to Zig.
                unsafe { csl_buffer_release(buffer) };
            }
        }
    }
}

// ---------------------------------------------------------------- boundary accounting
#[derive(Default, Clone)]
struct OpStats {
    calls: u64,
    bytes_in: u64,
    bytes_out: u64,
    boundary_ns: u128,
    kernel_ns: u128,
}
#[derive(Default)]
struct Boundary {
    by_op: BTreeMap<&'static str, OpStats>,
    copies: u64,
    copy_bytes: u64,
    allocations: u64,
    ownership_transitions: u64,
    wrapper_ns_total: u128,
    wrapper_copy_bytes: u64,
}
impl Boundary {
    fn record(&mut self, op: &'static str, bytes_in: usize, owned: &Owned, ffi_ns: u128) {
        let kernel = owned.reply.service_ns as u128;
        let entry = self.by_op.entry(op).or_default();
        entry.calls += 1;
        entry.bytes_in += bytes_in as u64;
        entry.bytes_out += (owned.line().len() + owned.payload().len()) as u64;
        entry.boundary_ns += ffi_ns.saturating_sub(kernel);
        entry.kernel_ns += kernel;
        for len in [owned.line().len(), owned.payload().len()] {
            if len > 0 {
                self.copies += 1;
                self.copy_bytes += len as u64;
                self.allocations += 1;
                self.ownership_transitions += 1;
            }
        }
    }
    fn to_json(&self) -> Value {
        let sum = |f: fn(&OpStats) -> u128| self.by_op.values().map(f).sum::<u128>();
        let by_op: serde_json::Map<String, Value> = self
            .by_op
            .iter()
            .map(|(k, v)| {
                (
                    (*k).to_string(),
                    json!({"calls": v.calls, "bytes_in": v.bytes_in, "bytes_out": v.bytes_out,
                           "boundary_ns": v.boundary_ns as u64, "kernel_ns": v.kernel_ns as u64}),
                )
            })
            .collect();
        json!({
            "model": "coarse-persistent-v0",
            "calls": sum(|v| v.calls as u128) as u64,
            "bytes_in": sum(|v| v.bytes_in as u128) as u64,
            "bytes_out": sum(|v| v.bytes_out as u128) as u64,
            "copies": self.copies, "copy_bytes": self.copy_bytes,
            "allocations": self.allocations, "releases": RELEASES.load(Relaxed),
            "ownership_transitions": self.ownership_transitions,
            "boundary_ns_total": sum(|v| v.boundary_ns) as u64,
            "kernel_ns_total": sum(|v| v.kernel_ns) as u64,
            "wrapper_ns_total": self.wrapper_ns_total as u64,
            "wrapper_copy_bytes": self.wrapper_copy_bytes,
            "wrapper_heap": {"live_bytes": LIVE.load(Relaxed).max(0), "peak_bytes": PEAK_LIVE.load(Relaxed).max(0),
                             "allocations": ALLOCATIONS.load(Relaxed)},
            "by_op": by_op,
        })
    }
}

struct Session {
    handle: *mut c_void,
    boundary: Boundary,
    max_line_bytes: u64,
    chunk_bytes: usize,
}

// ---------------------------------------------------------------- small helpers
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
        for (k, c) in quad.iter().enumerate() {
            n = n << 6 | if k >= 4 - pad { 0 } else { value(*c)? };
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
fn write_line(out: &mut impl Write, value: &Value) {
    let mut line = serde_json::to_vec(value).expect("json");
    line.push(b'\n');
    out.write_all(&line).expect("stdout");
    out.flush().expect("stdout");
}
/// Replace the value of the top-level `"service_ns":<digits>` field. The unescaped `"service_ns":`
/// cannot occur inside a canonical JSON string, and no result object has such a key.
fn splice_service_ns(line: &[u8], ns: u64) -> Vec<u8> {
    const KEY: &[u8] = b"\"service_ns\":";
    let Some(at) = line.windows(KEY.len()).position(|w| w == KEY) else {
        return line.to_vec();
    };
    let start = at + KEY.len();
    let end = start
        + line[start..]
            .iter()
            .take_while(|c| c.is_ascii_digit())
            .count();
    let mut out = Vec::with_capacity(line.len() + 8);
    out.extend_from_slice(&line[..start]);
    out.extend_from_slice(ns.to_string().as_bytes());
    out.extend_from_slice(&line[end..]);
    out
}
fn error_line(id: Option<u64>, message: &str, ns: u64) -> Value {
    json!({"code": "INVALID_REQUEST", "id": id, "message": message, "ok": false, "service_ns": ns})
}
fn ns_since(t: Instant) -> u64 {
    t.elapsed().as_nanos() as u64
}

/// What the front end can tell about a line without building a value tree.
enum Scan<'a> {
    Malformed,
    Frame(BTreeMap<&'a str, &'a RawValue>),
    Request { op: Option<&'a str> },
}
fn scan(text: &[u8]) -> Scan<'_> {
    let Ok(map) = serde_json::from_slice::<BTreeMap<&str, &RawValue>>(text) else {
        return Scan::Malformed;
    };
    if map.contains_key("frame") {
        return Scan::Frame(map);
    }
    let op = map
        .get("op")
        .and_then(|raw| serde_json::from_str::<&str>(raw.get()).ok());
    Scan::Request { op }
}
fn raw_u64(map: &BTreeMap<&str, &RawValue>, key: &str) -> Option<u64> {
    map.get(key)
        .and_then(|raw| serde_json::from_str::<u64>(raw.get()).ok())
}
fn raw_str<'a>(map: &BTreeMap<&'a str, &'a RawValue>, key: &str) -> Option<&'a str> {
    map.get(key)
        .and_then(|raw| serde_json::from_str::<&str>(raw.get()).ok())
}

// ---------------------------------------------------------------- one request
impl Session {
    /// One coarse FFI call; returns the Zig-owned reply or a boundary status.
    fn call(&mut self, name: &'static str, f: OpFn, line: &[u8]) -> Result<(Owned, u128), i32> {
        let mut owned = Owned {
            reply: CslReply::default(),
        };
        let t = Instant::now();
        // SAFETY: live session handle; `line` and the reply outlive the call; Zig retains no pointer.
        let status = unsafe { f(self.handle, line.as_ptr(), line.len(), &mut owned.reply) };
        let ffi_ns = t.elapsed().as_nanos();
        if status != 0 {
            return Err(status);
        }
        self.boundary.record(name, line.len(), &owned, ffi_ns);
        Ok((owned, ffi_ns))
    }

    /// Handle one complete request line. `started` is the moment the complete request was in hand.
    /// Returns true when the session is over (close acknowledged or a boundary fault).
    fn handle(
        &mut self,
        out: &mut impl Write,
        text: &[u8],
        op: Option<&str>,
        started: Instant,
    ) -> bool {
        let (name, f): (&'static str, OpFn) =
            match op.and_then(|o| OPS.iter().find(|(n, _)| *n == o)) {
                Some((n, f)) => (n, *f),
                None => ("request", csl_session_request as OpFn),
            };
        let scan_ns = started.elapsed(); // envelope scan before the call: wrapper time
        let (owned, ffi_ns) = match self.call(name, f, text) {
            Ok(o) => o,
            Err(status) => {
                write_line(
                    out,
                    &json!({"code": "INTERNAL", "id": null, "message": format!("boundary status {status}"),
                            "ok": false, "service_ns": ns_since(started)}),
                );
                return true;
            }
        };
        let flags = owned.reply.flags;
        if flags & FLAG_CHUNKED != 0 {
            self.send_chunked(out, &owned, started, scan_ns);
            return flags & FLAG_CLOSE != 0;
        }
        let mut line = owned.line().to_vec();
        self.boundary.wrapper_copy_bytes += line.len() as u64;
        if name == "stats" && flags & FLAG_OK != 0 {
            line = self.rewrite_stats(&line);
        }
        if name == "open" && flags & FLAG_OK != 0 {
            self.learn_open(&line);
        }
        let total = ns_since(started);
        self.boundary.wrapper_ns_total += (total as u128).saturating_sub(ffi_ns);
        let mut patched = splice_service_ns(&line, total);
        patched.push(b'\n');
        out.write_all(&patched).expect("stdout");
        out.flush().expect("stdout");
        flags & FLAG_CLOSE != 0
    }

    fn learn_open(&mut self, line: &[u8]) {
        if let Ok(v) = serde_json::from_slice::<Value>(line) {
            self.max_line_bytes = v["max_line_bytes"].as_u64().unwrap_or(self.max_line_bytes);
            self.chunk_bytes =
                v["chunk_bytes"].as_u64().unwrap_or(self.chunk_bytes as u64) as usize;
        }
    }

    /// Add heap accounting for both sides and the boundary block to the engine's stats response. The
    /// response is small, so it is parsed into a value here; canonical form is preserved by the
    /// BTreeMap-backed serializer.
    fn rewrite_stats(&self, line: &[u8]) -> Vec<u8> {
        let Ok(mut value) = serde_json::from_slice::<Value>(line) else {
            return line.to_vec();
        };
        let live = value["stats"]["live_heap_bytes"].clone();
        if let Some(stats) = value.get_mut("stats").and_then(Value::as_object_mut) {
            stats.insert(
                "heap_breakdown".into(),
                json!({"kernel_zig": live, "wrapper_rust": LIVE.load(Relaxed).max(0)}),
            );
            stats.insert("boundary".into(), self.boundary.to_json());
        }
        serde_json::to_vec(&value).unwrap_or_else(|_| line.to_vec())
    }

    fn send_chunked(
        &mut self,
        out: &mut impl Write,
        owned: &Owned,
        started: Instant,
        scan_ns: std::time::Duration,
    ) {
        let (id, generation) = (owned.reply.id, owned.reply.generation);
        let result = owned.payload();
        write_line(out, &json!({"id": id, "frame": "begin"}));
        let mut hasher = Sha256::new();
        let mut chunks = 0u64;
        // Service time counts the kernel work and producing the frames, not the pipe writes.
        let mut spent = started.elapsed();
        let mut wrapper = scan_ns;
        for (seq, chunk) in result.chunks(self.chunk_bytes.max(1)).enumerate() {
            let produced = Instant::now();
            hasher.update(chunk);
            let mut data = Vec::with_capacity(chunk.len() / 3 * 4 + 4);
            b64_encode(chunk, &mut data);
            let mut line = b"{\"data\":\"".to_vec();
            line.extend_from_slice(&data);
            line.extend_from_slice(
                format!("\",\"frame\":\"chunk\",\"id\":{id},\"seq\":{seq}}}\n").as_bytes(),
            );
            let took = produced.elapsed();
            spent += took;
            wrapper += took;
            out.write_all(&line).expect("stdout");
            chunks += 1;
        }
        out.flush().expect("stdout");
        let produced = Instant::now();
        let digest = hex(&hasher.finalize());
        let took = produced.elapsed();
        spent += took;
        wrapper += took;
        self.boundary.wrapper_ns_total += wrapper.as_nanos();
        write_line(
            out,
            &json!({"id": id, "frame": "end", "ok": true, "generation": generation, "chunks": chunks,
                    "bytes": result.len(), "sha256": digest, "service_ns": spent.as_nanos() as u64}),
        );
    }
}
// ---------------------------------------------------------------- the process
struct Assembly {
    id: u64,
    op: String,
    parts: Vec<u8>,
    chunks: u64,
    skipping: bool,
}

pub fn run() -> i32 {
    STATS_ON.store(true, Relaxed);
    let args: Vec<String> = std::env::args().collect();
    let arg = |key: &str| args.windows(2).find(|w| w[0] == key).map(|w| w[1].clone());
    let Some(repository) = arg("--repository") else {
        eprintln!("session needs --repository DIR");
        return 2;
    };
    let strategy = arg("--strategy").unwrap_or_else(|| "full-rebuild".into());
    // SAFETY: plain version query.
    if unsafe { csl_session_abi_version() } != 1 {
        eprintln!("session ABI mismatch");
        return 2;
    }
    let mut handle: *mut c_void = std::ptr::null_mut();
    let started = Instant::now();
    // SAFETY: both byte strings outlive the call (they are copied by Zig); valid out pointer.
    let status = unsafe {
        csl_session_create(
            repository.as_ptr(),
            repository.len(),
            strategy.as_ptr(),
            strategy.len(),
            &mut handle,
        )
    };
    if status != 0 {
        eprintln!("session create status {status}");
        return 2;
    }
    let mut session = Session {
        handle,
        boundary: Boundary::default(),
        max_line_bytes: 65536,
        chunk_bytes: 49_000,
    };
    let create_ns = started.elapsed().as_nanos();
    let create = session.boundary.by_op.entry("create").or_default();
    create.calls = 1;
    create.bytes_in = (repository.len() + strategy.len()) as u64;
    create.boundary_ns = create_ns;
    let code = serve(&mut session);
    // SAFETY: the unique owner destroys the session once; returned reply buffers were already released.
    unsafe { csl_session_destroy(session.handle) };
    code
}

fn serve(session: &mut Session) -> i32 {
    let stdin = std::io::stdin();
    let mut input = std::io::BufReader::with_capacity(1 << 20, stdin.lock());
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    let mut line: Vec<u8> = Vec::new();
    let mut assembly: Option<Assembly> = None;
    loop {
        line.clear();
        match input.read_until(b'\n', &mut line) {
            Ok(0) | Err(_) => return 0,
            Ok(_) => {}
        }
        let started = Instant::now(); // the complete request line is in hand
        let text = line.strip_suffix(b"\n").unwrap_or(&line);
        match scan(text) {
            Scan::Malformed => {
                if session.handle(&mut out, text, None, started) {
                    return 0;
                }
            }
            Scan::Request { op } => {
                if session.handle(&mut out, text, op, started) {
                    return 0;
                }
            }
            Scan::Frame(map) => {
                let id = raw_u64(&map, "id");
                let frame = raw_str(&map, "frame").unwrap_or("");
                let reject =
                    |out: &mut std::io::StdoutLock, a: &mut Option<Assembly>, message: &str| {
                        write_line(out, &error_line(id, message, ns_since(started)));
                        if let Some(a) = a {
                            a.skipping = true;
                        }
                    };
                match frame {
                    "begin" => match (id, raw_str(&map, "op")) {
                        (Some(id), Some(op)) => {
                            assembly = Some(Assembly {
                                id,
                                op: op.to_string(),
                                parts: Vec::new(),
                                chunks: 0,
                                skipping: false,
                            });
                        }
                        _ => write_line(
                            &mut out,
                            &error_line(id, "begin needs id and op", ns_since(started)),
                        ),
                    },
                    "chunk" => match assembly.as_mut() {
                        Some(a) if a.skipping && Some(a.id) == id => {}
                        Some(a) if Some(a.id) == id => {
                            let data = raw_str(&map, "data").and_then(b64_decode);
                            match (data, raw_u64(&map, "seq")) {
                                (Some(data), Some(seq)) if seq == a.chunks => {
                                    a.parts.extend_from_slice(&data);
                                    a.chunks += 1;
                                }
                                _ => reject(&mut out, &mut assembly, "bad chunk frame"),
                            }
                        }
                        _ => write_line(
                            &mut out,
                            &error_line(id, "chunk without begin", ns_since(started)),
                        ),
                    },
                    "end" => match assembly.take() {
                        Some(a) if a.skipping && Some(a.id) == id => {}
                        Some(a) if Some(a.id) == id => {
                            let hash = hex(&Sha256::digest(&a.parts));
                            let consistent = raw_u64(&map, "chunks") == Some(a.chunks)
                                && raw_u64(&map, "bytes") == Some(a.parts.len() as u64)
                                && raw_str(&map, "sha256") == Some(hash.as_str());
                            let merged = merge_request(a.id, &a.op, &a.parts);
                            match (consistent, merged) {
                                (true, Some(merged)) => {
                                    let op = a.op.clone();
                                    if session.handle(&mut out, &merged, Some(&op), Instant::now())
                                    {
                                        return 0;
                                    }
                                }
                                _ => write_line(
                                    &mut out,
                                    &error_line(
                                        Some(a.id),
                                        "chunked request failed verification",
                                        ns_since(started),
                                    ),
                                ),
                            }
                        }
                        other => {
                            assembly = other;
                            write_line(
                                &mut out,
                                &error_line(id, "end without begin", ns_since(started)),
                            );
                        }
                    },
                    _ => write_line(
                        &mut out,
                        &error_line(id, "unknown frame", ns_since(started)),
                    ),
                }
            }
        }
    }
}

/// `{"id":N,"op":"OP",` + the reassembled canonical object without its opening brace. The object must
/// be a JSON object without `id` or `op` (checked with a RawValue scan, no value tree).
fn merge_request(id: u64, op: &str, parts: &[u8]) -> Option<Vec<u8>> {
    let map = serde_json::from_slice::<BTreeMap<&str, &RawValue>>(parts).ok()?;
    if map.contains_key("id") || map.contains_key("op") {
        return None;
    }
    let mut merged =
        format!("{{\"id\":{id},\"op\":{}", serde_json::to_string(op).ok()?).into_bytes();
    if map.is_empty() {
        merged.push(b'}');
    } else {
        merged.push(b',');
        merged.extend_from_slice(parts.strip_prefix(b"{")?);
    }
    Some(merged)
}
