# S0 reference transport binding: JSONL v0

Status: **FINAL v0** (approved 2026-09-29; implementation authorized). JSONL remains the
**reference control binding**. It maps [SESSION-SEMANTICS.md](SESSION-SEMANTICS.md) to bytes and
**defines no semantics**: state, ordering, atomicity, cancellation and error meaning
live in the semantics document. Any other binding (length-prefixed, in-process, ...)
must preserve every outcome defined there. If this file and the semantics disagree,
the semantics win and this file is wrong.

Binding identifier: `csl.eval.session.jsonl/v0` (independent of the semantics
version).

## 1. Framing

* Transport: the candidate's standard input (requests) and standard output
  (responses). Standard error is diagnostic only and never compared.
* One message per physical line: UTF-8 canonical JSON (sorted keys, compact
  separators, no ASCII escaping), terminated by one `\n`. No blank lines, no
  comments.
* End of standard input while `OPEN` is an implicit `close`.
* Every physical line, in either direction, is at most `max_line_bytes`
  (negotiated in section 2). A logical message that would exceed it is **chunked**
  (section 4). Chunking is purely a transport device.

## 2. Process start and handshake

The candidate is started as `<candidate> session --repository DIR`. `DIR` is the
**snapshot repository** of the semantics document (a directory the candidate may create
files in); it is process-level configuration, not part of any request. Process start is
part of the cold-restore measurement (semantics 4.3).

The `open` request carries `binding` (the identifier above) and the host's
`max_line_bytes` (minimum accepted value: 65536). The `open` response echoes
`binding` and returns the candidate's `max_line_bytes` and the agreed `chunk_bytes`:
the maximum number of *raw* payload bytes per chunk, chosen so that a chunk line,
including base64 expansion and envelope, is at most the smaller of the two
`max_line_bytes`. Chunk parameters are transport configuration and never appear in
comparisons of semantic results.

## 3. Request and single-line response

```
request:  {"id": <integer>, "op": "<operation>", ...operation fields...}
response: {"id": <integer>, "ok": true,  "generation": <integer>, ...operation result...}
          {"id": <integer>, "ok": false, "code": "<ERROR_CODE>", "message": "<diagnostic>"}
```

`id` is chosen by the host and is unique in the session. `code` is one of the closed
set in the semantics document; `message` is free text, never compared. A query
response that fits within `max_line_bytes` embeds the canonical result object
**unmodified** under `result`.

## 4. Chunked messages

The **payload** of a chunked message is the canonical JSON bytes of the logical value
being transferred: the `result` object of a query response, or the operation fields
of a large request (for example a big `mutate` batch). Chunking splits those exact
bytes; it never re-encodes or reorders them.

Frames (all lines carry `id`):

```
{"id": N, "frame": "begin", ...envelope fields (op or ok/generation)...}
{"id": N, "frame": "chunk", "seq": 0, "data": "<base64 of raw bytes>"}
{"id": N, "frame": "chunk", "seq": 1, "data": "<base64 of raw bytes>"}
...
{"id": N, "frame": "end", "chunks": C, "bytes": T, "sha256": "<hex>", ...}
```

* `data` is RFC 4648 base64 (no line breaks) of a contiguous slice of the payload of at
  most `chunk_bytes` raw bytes. Slice boundaries are arbitrary and may fall inside a
  token or a UTF-8 sequence, which is why the payload travels as bytes, not text.
* `seq` starts at 0 and increases by exactly 1. `begin` announces nothing about size
  (a streaming producer does not know it yet); `end` carries the chunk count `C`, the
  total payload length `T` in bytes, and the SHA-256 of the **canonical payload
  bytes**.
* **Reassembly rule.** Concatenating the decoded `data` of chunks `0..C-1` in order
  must equal the canonical payload bytes exactly, with `T` bytes and the given
  SHA-256. The reassembled bytes *are* the logical value: byte-identical to what a
  single-line message would have carried under `result` (or the request fields).
* A response's `end` frame is its **terminal response**. For a chunked query it
  carries `ok`, `generation` and the fields above; the payload it completes is the
  `result`.
* **Requests** use the same frames host-to-candidate. The candidate acts on the
  request only once `end` is received and verified, so chunking cannot affect the
  atomicity or ordering defined by the semantics. A count, length or hash mismatch
  yields an ordinary terminal error `INVALID_REQUEST` for that `id` and no state
  change.

### 4.1 Verification

A verifier either reassembles the payload or, preferably, compares the decoded bytes
against the reference file (or its known SHA-256) in a streaming fashion, holding only
one chunk in memory. Both must give the same verdict. A mismatch in count, length or
hash is a transport-level fault reported as `INVALID_REQUEST` (candidate-side, for
requests) or as a harness failure (for responses); it is never interpreted as a
semantic result.

### 4.2 Ordering, exactly-one-terminal, and errors mid-stream

* **Ordering.** In the sequential baseline all frames of the response to request `N`
  are contiguous and precede any frame of the response to request `N+1`. Frames of
  different `id`s are never interleaved. Interleaving would be a separate advertised
  capability, tied to the concurrency capability, and is not part of v0.
* **Exactly one terminal response per `id`.** `begin` and `chunk` frames are
  non-terminal. The terminal message is either an ordinary single-line response, a
  chunked response's `end` frame, or an `abort` frame. No `id` ever has two terminal
  messages.
* **Error mid-stream.** If a candidate cannot complete a chunked response it emits
  `{"id": N, "frame": "abort", "ok": false, "code": "<ERROR_CODE>"}` as the terminal
  message. The host **discards every chunk of that `id`** and must not treat any
  fragment as a partial result, consistent with the semantics rule that a failed or
  cancelled request yields no partial semantic result. `abort` follows the same
  error-code set and the same state rules as any failed request.
* **Framing violations** (a gap or duplicate in `seq`, a `chunk` without `begin`, an
  `end` count/hash mismatch, an oversize line) are binding faults, not semantic
  outcomes. The host treats the session as failed at the transport level and closes it.

## 5. What this binding may not do

Change the canonical logical result or request bytes; add or alter state, ordering,
atomicity or error semantics; make chunk sizes observable in comparisons; or define
behavior the semantics document does not.

## 6. Known limitations

* Base64 expands chunk payload by about one third. Acceptable for a *reference
  control* binding, where correctness and byte-exactness matter more than throughput.
  A length-prefixed binary binding may follow if transport overhead becomes a
  measured concern.
* Reading the next request while a long one runs is not required by the baseline
  (semantics section 6); it is needed only with the cancellation or concurrency
  capabilities.

## Appendix A. v0 wire messages

All requests carry `id` and `op`; all responses carry `id` and `ok`. Unknown fields are
`INVALID_REQUEST`. Fields not listed for an operation are not allowed. `generation` is
present in every successful response except `open` failures and `close`.

**open**
```
{"id":1,"op":"open","binding":"csl.eval.session.jsonl/v0","max_line_bytes":1048576,
 "source":{"kind":"fixture","path":"<fixture file>"}}
{"id":1,"op":"open","binding":"...","max_line_bytes":1048576,
 "source":{"kind":"empty","context":{"snapshot":"S0","epoch":1,"complete":false}}}
-> {"id":1,"ok":true,"generation":0,"semantics":"csl.eval.session/v0.1",
    "binding":"csl.eval.session.jsonl/v0","max_line_bytes":<n>,"chunk_bytes":<n>,
    "capabilities":[],"strategy":{"mutation":"full-rebuild"|"incremental"},
    "artifact":"<candidate build identifier>"}
```
`epoch` is an integer or `null`; `complete` a boolean. `artifact` identifies the candidate
build for snapshot compatibility.

**query**
```
{"id":2,"op":"query","query":<query IR>}
-> {"id":2,"ok":true,"generation":<g>,"result":<canonical result>}    (or chunked, section 4)
```

**mutate** (operations as defined in ADR-0008)
```
{"id":3,"op":"mutate","batch":[
  {"op":"ADD_ENTITY","id":9,"kind":"METHOD","name":"n","container":null},
  {"op":"REMOVE_ENTITY","id":9},
  {"op":"UPDATE_ENTITY","id":9,"set":{"kind":"TYPE","name":"m","container":null}},
  {"op":"ADD_RELATION","subject":1,"relation":"CALLS","object":2},
  {"op":"REMOVE_RELATION","subject":1,"relation":"CALLS","object":2},
  {"op":"ADD_EVIDENCE","proposition":7,"subject":1,"relation":"CALLS","object":2,
   "polarity":"POSITIVE","quality":"EXACT","freshness_epoch":1,"lineage":0},
  {"op":"REMOVE_EVIDENCE", ...the same eight fields...}]}
-> {"id":3,"ok":true,"generation":<g+1>}
```

**state_digest** (explicit checkpoint)
```
{"id":4,"op":"state_digest"}
-> {"id":4,"ok":true,"generation":<g>,"state_digest":"sha256:<hex>","state_digest_ms":<number>,
    "bytes_processed":<integer|null>}
```

**snapshot / restore**
```
{"id":5,"op":"snapshot"}
-> {"id":5,"ok":true,"generation":<g>,"snapshot_id":"<opaque>","captured_generation":<g>}
{"id":6,"op":"restore","snapshot_id":"<opaque>"}
-> {"id":6,"ok":true,"generation":<g+1>}
```

**stats / cancel / close**
```
{"id":7,"op":"stats"}   -> {"id":7,"ok":true,"generation":<g>,"stats":{ csl.eval.session.stats/v0.1 }}
{"id":8,"op":"cancel","target":<id>} -> {"id":8,"ok":false,"code":"UNSUPPORTED","message":"..."}
{"id":9,"op":"close"}   -> {"id":9,"ok":true}   (the process then exits)
```

**errors**: `{"id":N,"ok":false,"code":"<code>","message":"<free text>"}`, code from the closed
set in the semantics document. A failed request leaves state and `generation` unchanged.

**Chunked frames, exactly (v0)**

* Chunked *response* to a `query`: `{"id":N,"frame":"begin"}`, then
  `{"id":N,"frame":"chunk","seq":k,"data":"<base64>"}` for `k = 0..C-1`, then the terminal
  `{"id":N,"frame":"end","ok":true,"generation":g,"chunks":C,"bytes":T,"sha256":"<hex>"}`. The
  concatenated decoded chunk bytes are the canonical `result` object and nothing else.
* Chunked *request*: `{"id":N,"op":"<op>","frame":"begin"}`, chunks as above, then
  `{"id":N,"frame":"end","chunks":C,"bytes":T,"sha256":"<hex>"}`. The concatenated bytes are the
  canonical JSON object of the operation's remaining fields (for `mutate`: `{"batch":[...]}`);
  the candidate merges it with `id` and `op` and acts only after `end` verifies.
* A candidate chunks a response when the single-line response would exceed the negotiated
  `max_line_bytes`; it must accept chunked requests whatever their size.
* Abort: `{"id":N,"frame":"abort","ok":false,"code":"<code>","message":"..."}`.
* Float values (only `state_digest_ms`) may print in any valid JSON number form; every other
  value, and every result payload, is canonical.
