/* Smoke test for the EXPERIMENTAL persistent session ABI (csl_session_experimental.h).
 * Built and run by run_session_smoke.sh under ASan/UBSan (the Zig library is not instrumented). */
#include "csl_session_experimental.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef __cplusplus
static_assert(sizeof(csl_session_reply) == 2 * sizeof(csl_buffer) + 3 * sizeof(uint64_t) + 2 * sizeof(uint32_t), "reply layout");
#else
_Static_assert(sizeof(csl_session_reply) == 2 * sizeof(csl_buffer) + 3 * sizeof(uint64_t) + 2 * sizeof(uint32_t), "reply layout");
#endif

static int contains(const csl_buffer *b, const char *needle) {
    if (b->len == 0) return 0;
    char *copy = (char *)malloc(b->len + 1);
    memcpy(copy, b->ptr, b->len);
    copy[b->len] = 0;
    int found = strstr(copy, needle) != NULL;
    free(copy);
    return found;
}
static void release(csl_session_reply *r) {
    csl_buffer_release(r->line);
    csl_buffer_release(r->payload);
    memset(r, 0, sizeof(*r));
}
#define CALL(fn, s, text, reply) fn((s), (const uint8_t *)(text), strlen(text), &(reply))

int main(int argc, char **argv) {
    assert(argc == 2); /* argv[1]: a writable snapshot repository directory */
    const char *repository = argv[1];
    assert(csl_session_abi_version() == CSL_SESSION_ABI_VERSION);
    csl_session *s = NULL;
    const char strategy[] = "incremental";
    assert(csl_session_create(NULL, 0, (const uint8_t *)strategy, strlen(strategy), &s) == CSL_STATUS_INVALID_ARGUMENT && s == NULL);
    assert(csl_session_create((const uint8_t *)repository, strlen(repository), (const uint8_t *)"nope", 4, &s) == CSL_STATUS_INVALID_QUERY && s == NULL);
    assert(csl_session_create((const uint8_t *)repository, strlen(repository), (const uint8_t *)strategy, strlen(strategy), &s) == CSL_STATUS_OK);

    csl_session_reply r;
    memset(&r, 7, sizeof(r));
    assert(csl_session_open(NULL, NULL, 0, &r) == CSL_STATUS_INVALID_ARGUMENT);
    assert(csl_session_open(s, NULL, 3, &r) == CSL_STATUS_INVALID_ARGUMENT);
    assert(r.line.ptr == NULL && r.line.len == 0 && r.flags == 0); /* reset on failure */
    assert(csl_session_open(s, (const uint8_t *)"x", 1, NULL) == CSL_STATUS_INVALID_ARGUMENT);

    /* an S0 error is an ordinary reply, not a boundary fault */
    assert(CALL(csl_session_stats, s, "{\"id\":1,\"op\":\"stats\"}", r) == CSL_STATUS_OK);
    assert((r.flags & CSL_SESSION_FLAG_OK) == 0 && contains(&r.line, "INVALID_STATE"));
    release(&r);

    assert(CALL(csl_session_open, s,
                "{\"binding\":\"csl.eval.session.jsonl/v0\",\"id\":2,\"max_line_bytes\":65536,\"op\":\"open\","
                "\"source\":{\"context\":{\"complete\":false,\"epoch\":1,\"snapshot\":\"S0\"},\"kind\":\"empty\"}}", r) == CSL_STATUS_OK);
    assert((r.flags & CSL_SESSION_FLAG_OK) && (r.flags & CSL_SESSION_FLAG_HAS_GENERATION) && r.generation == 0);
    assert(contains(&r.line, "csl-eval-hybrid/s0-v0") && contains(&r.line, "\"mutation\":\"incremental\""));
    assert(r.payload.len == 0 && r.service_ns > 0);
    release(&r);

    assert(CALL(csl_session_mutate, s,
                "{\"batch\":[{\"container\":null,\"id\":5,\"kind\":\"TYPE\",\"name\":\"n\",\"op\":\"ADD_ENTITY\"}],\"id\":3,\"op\":\"mutate\"}", r) == CSL_STATUS_OK);
    assert((r.flags & CSL_SESSION_FLAG_OK) && r.generation == 1);
    release(&r);
    assert(CALL(csl_session_mutate, s, "{\"batch\":[],\"id\":4,\"op\":\"mutate\"}", r) == CSL_STATUS_OK);
    assert((r.flags & CSL_SESSION_FLAG_OK) == 0 && contains(&r.line, "INVALID_INPUT"));
    release(&r);

    for (int i = 0; i < 50; ++i) {
        assert(CALL(csl_session_query, s,
                    "{\"id\":5,\"op\":\"query\",\"query\":{\"op\":\"FILTER\",\"query_id\":\"q\",\"schema\":\"csl.eval.query/v0.1\"}}", r) == CSL_STATUS_OK);
        assert((r.flags & CSL_SESSION_FLAG_OK) && !(r.flags & CSL_SESSION_FLAG_CHUNKED) && contains(&r.line, "\"entities\":[5]"));
        release(&r);
    }
    assert(CALL(csl_session_state_digest, s, "{\"id\":6,\"op\":\"state_digest\"}", r) == CSL_STATUS_OK);
    assert(contains(&r.line, "sha256:") && contains(&r.line, "state_digest_ms"));
    release(&r);
    assert(CALL(csl_session_cancel, s, "{\"id\":7,\"op\":\"cancel\",\"target\":1}", r) == CSL_STATUS_OK);
    assert(contains(&r.line, "UNSUPPORTED"));
    release(&r);
    assert(CALL(csl_session_request, s, "{not json", r) == CSL_STATUS_OK);
    assert(contains(&r.line, "INVALID_REQUEST"));
    release(&r);
    assert(CALL(csl_session_close, s, "{\"id\":8,\"op\":\"close\"}", r) == CSL_STATUS_OK);
    assert(r.flags & CSL_SESSION_FLAG_CLOSE);
    /* buffers outlive the session */
    csl_session_destroy(s);
    assert(contains(&r.line, "\"ok\":true"));
    release(&r);
    csl_session_destroy(NULL);
    return 0;
}
