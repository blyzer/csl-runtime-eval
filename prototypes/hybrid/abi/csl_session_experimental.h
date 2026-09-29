#ifndef CSL_SESSION_EXPERIMENTAL_H
#define CSL_SESSION_EXPERIMENTAL_H
/*
 * EXPERIMENTAL persistent session boundary (W10, Gate #1). NOT the stable ABI v1 and NOT a
 * production API: it exists to falsify (or not) the Hybrid hypothesis under the approved S0
 * v0 semantics, and it is deliberately minimal and unoptimized.
 *
 * Coarse-grained: exactly one call per S0 request. No per-entity, per-edge or per-evidence
 * calls, and no shared memory. The Zig side owns the store (an S0 `Engine`); the Rust side owns
 * the process, the JSON Lines transport and the chunk framing.
 *
 * Every operation takes the COMPLETE canonical request line (no trailing newline), valid only for
 * the duration of the call, and returns the engine's response through `csl_session_reply`.
 * S0 errors (INVALID_REQUEST, INVALID_INPUT, ...) travel inside the response line as ordinary S0
 * error responses; the int32 status reports only boundary faults.
 *
 * Ownership: `line` and `payload` are Zig-allocated copies owned by the caller from the moment the
 * call returns until each non-empty buffer is released, exactly once, by value, with
 * csl_buffer_release() from csl_kernel.h (same private-header allocation as ABI v1). Copies of a
 * descriptor are aliases, not owners. Buffers survive csl_session_destroy(). Calls on a session
 * are serialized; no concurrency guarantee.
 */
#include "csl_kernel.h"
#ifdef __cplusplus
extern "C" {
#endif

#define CSL_SESSION_ABI_VERSION 1u

typedef struct csl_session csl_session;

#define CSL_SESSION_FLAG_OK             0x01u /* the S0 request succeeded */
#define CSL_SESSION_FLAG_CHUNKED        0x02u /* query result too large for one line: `line` is empty,
                                                 `payload` holds the canonical result bytes */
#define CSL_SESSION_FLAG_CLOSE          0x04u /* the front end should exit after writing the reply */
#define CSL_SESSION_FLAG_HAS_ID         0x08u /* `id` is valid */
#define CSL_SESSION_FLAG_HAS_GENERATION 0x10u /* `generation` is valid */

typedef struct csl_session_reply {
    csl_buffer line;      /* complete canonical single-line response, no '\n'; {NULL,0} when chunked */
    csl_buffer payload;   /* canonical result bytes when CHUNKED, else {NULL,0} */
    uint64_t service_ns;  /* the engine's own wall time for this request */
    uint64_t generation;
    uint64_t id;
    uint32_t flags;
    uint32_t reserved;    /* zero */
} csl_session_reply;

/* Version of this experimental interface (not of ABI v1). */
uint32_t csl_session_abi_version(void);

/* Create a session bound to a snapshot repository directory and a mutation strategy
 * ("full-rebuild" or "incremental"). repository/strategy are copied. On failure *out is NULL:
 * INVALID_ARGUMENT for null/empty repository or a non-null-with-zero-length mismatch,
 * INVALID_QUERY for an unknown strategy, OUT_OF_MEMORY. */
csl_status csl_session_create(const uint8_t *repository, size_t repository_len,
                              const uint8_t *strategy, size_t strategy_len, csl_session **out);

/* Destroy a session (NULL is a no-op). Reply buffers already returned stay valid. */
void csl_session_destroy(csl_session *session);

/* One function per S0 operation; each handles one complete request line. `reply` is reset to zero
 * first and fully written on CSL_STATUS_OK. INVALID_ARGUMENT for NULL session/reply or a null line
 * with non-zero length. */
csl_status csl_session_open(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_query(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_mutate(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_state_digest(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_snapshot(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_restore(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_stats(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_cancel(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);
csl_status csl_session_close(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);

/* Fallback for a line the front end could not attribute to a known operation (malformed JSON,
 * unknown op): the engine produces the S0 error itself, so error semantics are never
 * re-implemented on the Rust side. */
csl_status csl_session_request(csl_session *s, const uint8_t *line, size_t len, csl_session_reply *reply);

#ifdef __cplusplus
}
#endif
#endif
