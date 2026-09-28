#ifndef CSL_KERNEL_H
#define CSL_KERNEL_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define CSL_KERNEL_ABI_VERSION 1u

/*
 * Opaque kernel instance.
 *
 * Its representation belongs entirely to the Zig implementation.
 */
typedef struct csl_kernel csl_kernel;


/*
 * Stable ABI status type.
 *
 * Use an explicitly sized integer across the ABI rather than relying
 * on the implementation-defined size of a C enum.
 */
typedef int32_t csl_status;

#define CSL_STATUS_OK                ((csl_status)0)
#define CSL_STATUS_INVALID_ARGUMENT  ((csl_status)1)
#define CSL_STATUS_OUT_OF_MEMORY     ((csl_status)2)
#define CSL_STATUS_INVALID_QUERY     ((csl_status)3)
#define CSL_STATUS_BUFFER_TOO_SMALL  ((csl_status)4)
#define CSL_STATUS_INTERNAL_ERROR    ((csl_status)255)


/*
 * Kernel-owned result buffer.
 *
 * ptr may be NULL when len == 0.
 *
 * The caller must not free ptr directly.
 * Release the buffer with csl_buffer_release().
 */
typedef struct csl_buffer {
    uint8_t *ptr;
    size_t len;
} csl_buffer;


/*
 * Runtime ABI version.
 *
 * Allows a host to verify compatibility before using the kernel.
 */
uint32_t csl_kernel_abi_version(void);


/*
 * Create a kernel instance.
 *
 * On success:
 *   - returns CSL_STATUS_OK
 *   - *out points to a valid kernel
 *
 * On failure:
 *   - returns an error status
 *   - *out is NULL
 */
csl_status csl_kernel_open(
    csl_kernel **out
);


/*
 * Destroy a kernel instance.
 *
 * Passing NULL is allowed and is a no-op.
 */
void csl_kernel_close(
    csl_kernel *kernel
);


/*
 * Execute one coarse-grained query payload.
 *
 * query/query_len are caller-owned and valid only for the duration
 * of this call.
 *
 * On success, out receives a kernel-owned buffer, valid even after close.
 * On failure, a non-NULL out is reset to {NULL, 0}. Never pass an out
 * containing an unreleased result. NULL query is valid only for length zero.
 *
 * The caller releases that buffer using csl_buffer_release().
 */
csl_status csl_kernel_execute(
    csl_kernel *kernel,
    const uint8_t *query,
    size_t query_len,
    csl_buffer *out
);


/*
 * Release a buffer returned by the kernel.
 *
 * The buffer must have originated from this ABI and remain unmodified.
 * Release each nonempty buffer exactly once; copies are not new owners.
 * Released bytes must never be read. Calls on a kernel are serialized.
 *
 * Passing { NULL, 0 } is valid.
 */
void csl_buffer_release(
    csl_buffer buffer
);


#ifdef __cplusplus
}
#endif

#endif /* CSL_KERNEL_H */