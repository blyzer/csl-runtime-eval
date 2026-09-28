#include "../prototypes/hybrid/abi/csl_kernel.h"
#include "../prototypes/hybrid/abi/csl_kernel_experimental.h"
#include <assert.h>
#include <string.h>
#include <stdlib.h>
#ifdef __cplusplus
static_assert(sizeof(csl_status) == 4, "status width");
static_assert(sizeof(csl_buffer) == sizeof(void*) + sizeof(size_t), "buffer layout");
#else
_Static_assert(sizeof(csl_status) == 4, "status width");
_Static_assert(sizeof(csl_buffer) == sizeof(void*) + sizeof(size_t), "buffer layout");
#endif
int main(void) {
    csl_kernel *k = NULL;
    csl_buffer b = {NULL, 0};
    assert(csl_kernel_abi_version() == 1);
    assert(csl_kernel_open(&k) == CSL_STATUS_OK);
    assert(csl_kernel_execute(k, NULL, 0, &b) == CSL_STATUS_OK);
    csl_buffer_release(b);
    const uint8_t query[] = {0, 'C', 'S', 'L'};
    for (int i = 0; i < 100; ++i) {
        assert(csl_kernel_execute(k, query, sizeof(query), &b) == CSL_STATUS_OK);
        assert(b.len == 3 && memcmp(b.ptr, "CSL", 3) == 0);
        csl_buffer_release(b);
        uint8_t caller[3] = {99, 99, 99};
        size_t written = 0;
        assert(csl_kernel_execute_into(k, query, sizeof(query), caller, 2, &written) == CSL_STATUS_BUFFER_TOO_SMALL);
        assert(written == 3 && caller[0] == 99);
        assert(csl_kernel_execute_into(k, query, sizeof(query), caller, 3, &written) == CSL_STATUS_OK);
        assert(memcmp(caller, "CSL", 3) == 0);
    }
    const size_t size = 16 * 1024 * 1024;
    uint8_t *large = (uint8_t *)malloc(size + 1);
    assert(large != NULL);
    memset(large, 42, size + 1);
    large[0] = 0;
    assert(csl_kernel_execute(k, large, size + 1, &b) == CSL_STATUS_OK);
    free(large);
    csl_kernel_close(k);
    assert(b.len == size && b.ptr[0] == 42 && b.ptr[size - 1] == 42);
    csl_buffer_release(b);
    csl_kernel_close(NULL);
    return 0;
}
