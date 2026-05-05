# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""RLGR1 decoder — Cython accelerated.
Public API: rlgr1_decode(data, output_size) -> np.ndarray(int16)

Same semantics as rlgr1_decode.py; this .so shadows the .py when compiled.
"""

import numpy as np
cimport numpy as np
from numpy cimport int16_t

np.import_array()

# RLGR1 constants
DEF _LSGR = 3
DEF _KPMAX = 80
DEF _UPGR = 4
DEF _DNGR = 6
DEF _UQGR = 3
DEF _DQGR = 3


# ---------------------------------------------------------------------------
# Inline bit reader — all state kept as C locals, no Python objects in the
# hot loop.  _pos mirrors Python _BitReader._pos (bits "loaded into accum");
# remaining = total - _pos + _accum_bits.
# ---------------------------------------------------------------------------

cdef inline void _br_refill(unsigned long long *acc, int *ab, int *pos,
                              const unsigned char *buf, int *bp, int dlen,
                              int need) noexcept nogil:
    """Fill accumulator until ab >= need.  Phantom zero bytes after EOF."""
    cdef int load, n, i
    while ab[0] < need:
        if bp[0] < dlen:
            n = (64 - ab[0]) >> 3      # free slots in accumulator
            load = dlen - bp[0]
            if load > n:  load = n
            if load > 8:  load = 8
            if load <= 0: load = 1
            for i in range(load):
                acc[0] = (acc[0] << 8) | buf[bp[0]]
                bp[0] += 1
            ab[0]  += load * 8
            pos[0] += load * 8
        else:
            acc[0] <<= 8
            ab[0]  += 8
            pos[0] += 8


cdef inline int _br_read(unsigned long long *acc, int *ab, int *pos,
                          const unsigned char *buf, int *bp, int dlen,
                          int n) nogil:
    """Read n bits (0 <= n <= 30)."""
    if n == 0:
        return 0
    if ab[0] < n:
        _br_refill(acc, ab, pos, buf, bp, dlen, n)
    ab[0] -= n
    return <int>((acc[0] >> ab[0]) & ((1ULL << n) - 1))


cdef inline int _br_count0(unsigned long long *acc, int *ab, int *pos,
                             const unsigned char *buf, int *bp, int dlen,
                             int total) nogil:
    """Count consecutive 0-bits; consumes them from the stream."""
    cdef int count = 0, i, bit
    cdef unsigned long long top8
    while True:
        if (total - pos[0] + ab[0]) <= 0:
            break
        if ab[0] < 8:
            _br_refill(acc, ab, pos, buf, bp, dlen, 16)
        while ab[0] >= 8:
            top8 = (acc[0] >> (ab[0] - 8)) & 0xFF
            if top8 == 0x00:
                count += 8
                ab[0] -= 8
            else:
                for i in range(8):
                    ab[0] -= 1
                    bit = (acc[0] >> ab[0]) & 1
                    if bit == 0:
                        count += 1
                    else:
                        return count
                return count          # found a 1-bit
        # ab < 8: outer loop will refill
    return count


cdef inline int _br_count1(unsigned long long *acc, int *ab, int *pos,
                             const unsigned char *buf, int *bp, int dlen,
                             int total) nogil:
    """Count consecutive 1-bits; consumes them from the stream."""
    cdef int count = 0, i, bit
    cdef unsigned long long top8
    while True:
        if (total - pos[0] + ab[0]) <= 0:
            break
        if ab[0] < 8:
            _br_refill(acc, ab, pos, buf, bp, dlen, 16)
        while ab[0] >= 8:
            top8 = (acc[0] >> (ab[0] - 8)) & 0xFF
            if top8 == 0xFF:
                count += 8
                ab[0] -= 8
            else:
                for i in range(8):
                    ab[0] -= 1
                    bit = (acc[0] >> ab[0]) & 1
                    if bit == 1:
                        count += 1
                    else:
                        return count
                return count          # found a 0-bit
        # ab < 8: outer loop will refill
    return count


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rlgr1_decode(data, int output_size):
    """Decode RLGR1 encoded data into a signed int16 numpy array."""
    cdef:
        const unsigned char[:] buf_mv
        const unsigned char *buf
        unsigned long long acc = 0
        int ab = 0    # accum_bits (unconsumed bits in acc)
        int pos = 0   # bits "loaded" into acc (mirrors _BitReader._pos)
        int bp = 0    # next byte index to load from buf
        int dlen, total
        int cnt = 0
        int k = 1, kp = 8, kr = 1, krp = 8
        int vk, vk2, run, sign_bit, code, mag, end, i
        int16_t[:] out_view

    if data is None or len(data) == 0:
        return np.zeros(output_size, dtype=np.int16)

    data = bytes(data) if not isinstance(data, bytes) else data
    buf_mv = <const unsigned char[:len(data)]>(<const unsigned char *><object>data)
    buf = &buf_mv[0]
    dlen = len(data)
    total = dlen * 8

    output = np.zeros(output_size, dtype=np.int16)
    out_view = output

    while (total - pos + ab) > 0 and cnt < output_size:

        if k > 0:
            # RL (Run-Length) mode
            vk = _br_count0(&acc, &ab, &pos, buf, &bp, dlen, total)

            run = 0
            for i in range(vk):
                run += 1 << k
                kp += _UPGR
                if kp > _KPMAX: kp = _KPMAX
                k = kp >> _LSGR

            if (total - pos + ab) < k:
                break
            if k > 0:
                run += _br_read(&acc, &ab, &pos, buf, &bp, dlen, k)

            if (total - pos + ab) < 1:
                break
            sign_bit = _br_read(&acc, &ab, &pos, buf, &bp, dlen, 1)

            vk2 = _br_count1(&acc, &ab, &pos, buf, &bp, dlen, total)

            if (total - pos + ab) < kr:
                break
            code = _br_read(&acc, &ab, &pos, buf, &bp, dlen, kr)
            code |= vk2 << kr

            if vk2 == 0:
                krp -= 2
                if krp < 0: krp = 0
                kr = krp >> _LSGR
            elif vk2 != 1:
                krp += vk2
                if krp > _KPMAX: krp = _KPMAX
                kr = krp >> _LSGR

            kp -= _DNGR
            if kp < 0: kp = 0
            k = kp >> _LSGR

            mag = code + 1
            end = cnt + run
            if end > output_size: end = output_size
            cnt = end
            if cnt < output_size:
                if sign_bit:
                    out_view[cnt] = <int16_t>(-mag)
                else:
                    out_view[cnt] = <int16_t>mag
                cnt += 1

        else:
            # GR (Golomb-Rice) mode
            vk = _br_count1(&acc, &ab, &pos, buf, &bp, dlen, total)

            if (total - pos + ab) < kr:
                break
            code = _br_read(&acc, &ab, &pos, buf, &bp, dlen, kr)
            code |= vk << kr

            if vk == 0:
                krp -= 2
                if krp < 0: krp = 0
                kr = krp >> _LSGR
            elif vk != 1:
                krp += vk
                if krp > _KPMAX: krp = _KPMAX
                kr = krp >> _LSGR

            if code == 0:
                kp += _UQGR
                if kp > _KPMAX: kp = _KPMAX
                k = kp >> _LSGR
                if cnt < output_size:
                    out_view[cnt] = 0
                    cnt += 1
            else:
                kp -= _DQGR
                if kp < 0: kp = 0
                k = kp >> _LSGR
                if code & 1:
                    mag = -((code + 1) >> 1)
                else:
                    mag = code >> 1
                if cnt < output_size:
                    out_view[cnt] = <int16_t>mag
                    cnt += 1

    return output
