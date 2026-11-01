/*
 * memceiling compiled kernels.
 *
 * This file builds into a plain shared library, not a CPython extension, so
 * it needs no Python headers and can be built with any C compiler. memceiling
 * loads it with ctypes, which releases the GIL for the duration of each
 * foreign call; that is what lets the Python-side thread harness get real
 * parallelism out of these loops.
 *
 * Each kernel takes a pointer, an element count and nothing else that could
 * vary between calls, so the timed region in Python contains exactly one
 * call and no per-element interpreter work.
 *
 * Build it with the CLI:
 *     memceiling build-c
 * or by hand:
 *     cc -O3 -fPIC -shared -o memceiling_ckernels.so _ckernels.c       (Unix)
 *     cl /O2 /LD /Fe:memceiling_ckernels.dll _ckernels.c               (MSVC)
 *
 * Deliberately NOT used here: non-temporal stores, hand-written SIMD, and
 * -ffast-math. Those raise the number on some machines and change what is
 * being measured. The provenance record names the compiler and flags so a
 * number from a differently-built library is never silently compared with
 * one from this build.
 *
 * Copyright (c) 2026 Manu Nicholas Jacob
 * SPDX-License-Identifier: MIT
 */

#include <stddef.h>

#ifdef _MSC_VER
#define MEMCEILING_EXPORT __declspec(dllexport)
#else
#define MEMCEILING_EXPORT __attribute__((visibility("default")))
#endif

/* Bumped whenever a kernel's meaning or signature changes. memceiling refuses to
 * use a library whose ABI version it does not recognise. */
#define MEMCEILING_ABI_VERSION 1

MEMCEILING_EXPORT int memceiling_abi_version(void)
{
    return MEMCEILING_ABI_VERSION;
}

/*
 * Sequential read of n doubles.
 *
 * Four independent accumulators, so the loop is bounded by memory and not
 * by floating-point add latency. The sum is returned so that no compiler
 * can prove the loads dead and delete them, which is the classic way a
 * naive read benchmark reports an impossible number.
 */
MEMCEILING_EXPORT double memceiling_read(const double *a, size_t n)
{
    double s0 = 0.0, s1 = 0.0, s2 = 0.0, s3 = 0.0;
    size_t i = 0;
    size_t limit = n - (n % 4);

    for (; i < limit; i += 4) {
        s0 += a[i];
        s1 += a[i + 1];
        s2 += a[i + 2];
        s3 += a[i + 3];
    }
    for (; i < n; ++i) {
        s0 += a[i];
    }
    return s0 + s1 + s2 + s3;
}

/*
 * Strided read: touch one double every `stride` elements.
 *
 * With stride set to one cache line's worth of doubles this measures the
 * rate at which whole cache lines can be fetched while only one word of
 * each is consumed. That is a different quantity from the sequential read
 * above, and memceiling labels it differently in its output for exactly that
 * reason.
 */
MEMCEILING_EXPORT double memceiling_read_strided(const double *a, size_t n, size_t stride)
{
    double s0 = 0.0;
    size_t i;

    if (stride == 0) {
        stride = 1;
    }
    for (i = 0; i < n; i += stride) {
        s0 += a[i];
    }
    return s0;
}

/* STREAM Copy: c[i] = a[i]. */
MEMCEILING_EXPORT void memceiling_copy(double *dst, const double *src, size_t n)
{
    size_t i;
    for (i = 0; i < n; ++i) {
        dst[i] = src[i];
    }
}

/* STREAM Scale: b[i] = q * c[i]. */
MEMCEILING_EXPORT void memceiling_scale(double *dst, const double *src, double q, size_t n)
{
    size_t i;
    for (i = 0; i < n; ++i) {
        dst[i] = q * src[i];
    }
}

/* STREAM Add: c[i] = a[i] + b[i]. */
MEMCEILING_EXPORT void memceiling_add(double *dst, const double *a, const double *b, size_t n)
{
    size_t i;
    for (i = 0; i < n; ++i) {
        dst[i] = a[i] + b[i];
    }
}

/*
 * STREAM Triad: a[i] = b[i] + q * c[i].
 *
 * One fused pass over three arrays. The numpy path cannot express this
 * without a temporary, which is why memceiling records which path produced a
 * number instead of treating the two as interchangeable.
 */
MEMCEILING_EXPORT void memceiling_triad(double *a, const double *b, const double *c,
                              double q, size_t n)
{
    size_t i;
    for (i = 0; i < n; ++i) {
        a[i] = b[i] + q * c[i];
    }
}

/* Fill, used to commit pages and to warm a buffer before timing. */
MEMCEILING_EXPORT void memceiling_fill(double *a, double value, size_t n)
{
    size_t i;
    for (i = 0; i < n; ++i) {
        a[i] = value;
    }
}
