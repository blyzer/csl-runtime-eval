#ifndef CSL_KERNEL_EXPERIMENTAL_H
#define CSL_KERNEL_EXPERIMENTAL_H
#include "csl_kernel.h"
#ifdef __cplusplus
extern "C" {
#endif
/* Experimental strategy B; NOT stable ABI v1. Inputs/output must not overlap.
 * result_len is required and reset to zero on invalid arguments/errors.
 * BUFFER_TOO_SMALL reports required size and does not write any result bytes.
 * NULL result is allowed only with zero capacity. Kernel retains no pointers.
 * Success writes exactly *result_len bytes; caller owns and releases storage.
 */
csl_status csl_kernel_execute_into(csl_kernel *kernel, const uint8_t *query,
    size_t query_len, uint8_t *result, size_t result_capacity, size_t *result_len);
#ifdef __cplusplus
}
#endif
#endif
