# S0 reference transport binding: JSONL v0 (preliminary)

Status: PRELIMINARY DESIGN. Not implemented. This binding maps
[SESSION-SEMANTICS.md](SESSION-SEMANTICS.md) to bytes. It **defines no semantics**:
every rule of state, ordering, atomicity, cancellation and error meaning lives in the
semantics document, and a different binding (length-prefixed, in-process, ...) must
preserve every outcome defined there. If this file and the semantics ever disagree,
the semantics win and this file is wrong.

## Framing

* Transport: the candidate's standard input (requests) and standard output
  (responses). Standard error is diagnostic only and is never compared.
* One message per line: UTF-8 canonical JSON (sorted keys, compact separators, no
  ASCII escaping), terminated by a single `\n`. No blank lines, no comments.
* End of standard input while `OPEN` is treated as an implicit `close`.

## Request

```
{"id": <integer>, "op": "<operation>", ... operation fields ...}
```

`id` is chosen by the host and unique within the session. Operation names and
fields follow the semantics document (provisional). Unknown top-level fields are
`INVALID_REQUEST`.

## Response

Exactly one line per request, in request order in the sequential baseline:

```
{"id": <integer>, "ok": true,  "generation": <integer>, ...operation result...}
{"id": <integer>, "ok": false, "code": "<ERROR_CODE>", "message": "<diagnostic>"}
```

`code` is one of the closed set in the semantics document; `message` is free text,
never compared. A query response embeds the canonical result object **unmodified**
under `result`, so the result bytes stay identical to the one-shot/oracle canonical
form and can be compared byte-for-byte.

## Known limitations (to be settled at review)

* **Large results.** At M scale a single result exceeds 100 MB; one JSON line of
  that size is workable for a first binding but is not ideal for streaming. An
  alternative binding may frame results as length-prefixed bytes. Deferred, along
  with open question 5 of the semantics document.
* Line-based framing makes reading the next request while a long one is executing
  a candidate obligation; see open question 3 (cancellation) in the semantics
  document.
* No versioning of the binding beyond `csl.eval.session.jsonl/v0`, carried in the
  `open` request and response.
