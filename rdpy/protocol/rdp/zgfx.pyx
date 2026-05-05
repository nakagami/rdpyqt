# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""
ZGFX (RDP 8.0 Bulk Compression) decompressor — Cython accelerated.
Same public API as zgfx.py; shadows it when compiled.
"""

import rdpy.core.log as log

ZGFX_HISTORY_SIZE = 2500000
PACKET_COMPRESSED = 0x20

# ---------------------------------------------------------------------------
# Huffman token table — same data as zgfx.py
# ---------------------------------------------------------------------------
_TOKEN_TABLE = [
    (1,   0,    8,  0, 0),
    (5,   17,   5,  1, 0),
    (5,   18,   7,  1, 32),
    (5,   19,   9,  1, 160),
    (5,   20,   10, 1, 672),
    (5,   21,   12, 1, 1696),
    (5,   24,   0,  0, 0x00),
    (5,   25,   0,  0, 0x01),
    (6,   44,   14, 1, 5792),
    (6,   45,   15, 1, 22176),
    (6,   52,   0,  0, 0x02),
    (6,   53,   0,  0, 0x03),
    (6,   54,   0,  0, 0xFF),
    (7,   92,   18, 1, 54944),
    (7,   93,   20, 1, 317088),
    (7,   110,  0,  0, 0x04),
    (7,   111,  0,  0, 0x05),
    (7,   112,  0,  0, 0x06),
    (7,   113,  0,  0, 0x07),
    (7,   114,  0,  0, 0x08),
    (7,   115,  0,  0, 0x09),
    (7,   116,  0,  0, 0x0A),
    (7,   117,  0,  0, 0x0B),
    (7,   118,  0,  0, 0x3A),
    (7,   119,  0,  0, 0x3B),
    (7,   120,  0,  0, 0x3C),
    (7,   121,  0,  0, 0x3D),
    (7,   122,  0,  0, 0x3E),
    (7,   123,  0,  0, 0x3F),
    (7,   124,  0,  0, 0x40),
    (7,   125,  0,  0, 0x80),
    (8,   188,  20, 1, 1365664),
    (8,   189,  21, 1, 2414240),
    (8,   252,  0,  0, 0x0C),
    (8,   253,  0,  0, 0x38),
    (8,   254,  0,  0, 0x39),
    (8,   255,  0,  0, 0x66),
    (9,   380,  22, 1, 4511392),
    (9,   381,  23, 1, 8705696),
    (9,   382,  24, 1, 17094304),
]

# ---------------------------------------------------------------------------
# C-level lookup table (512 entries, 9-bit prefix)
# ---------------------------------------------------------------------------
cdef struct _Entry:
    int valid   # 0 = no match
    int plen    # prefix length
    int vbits   # value bits
    int ttype   # 0=literal, 1=match
    int vbase   # value base

cdef _Entry _HUFF_LOOKUP[512]

# Initialise at module load (Python-level loop, runs once)
for _i in range(512):
    _HUFF_LOOKUP[_i].valid = 0
for _plen, _pcode, _vbits, _ttype, _vbase in _TOKEN_TABLE:
    _pad_bits = 9 - _plen
    _base_idx = _pcode << _pad_bits
    for _pad in range(1 << _pad_bits):
        _HUFF_LOOKUP[_base_idx | _pad].valid = 1
        _HUFF_LOOKUP[_base_idx | _pad].plen  = _plen
        _HUFF_LOOKUP[_base_idx | _pad].vbits = _vbits
        _HUFF_LOOKUP[_base_idx | _pad].ttype = _ttype
        _HUFF_LOOKUP[_base_idx | _pad].vbase = _vbase


# ---------------------------------------------------------------------------
# Decompressor class
# ---------------------------------------------------------------------------
cdef class ZgfxDecompressor:
    """Stateful ZGFX decompressor with persistent history buffer."""

    cdef bytearray _history
    cdef int _hist_idx

    def __init__(self):
        self._history = bytearray(ZGFX_HISTORY_SIZE)
        self._hist_idx = 0

    def reset(self):
        """Reset history (for testing / benchmarking)."""
        self._history = bytearray(ZGFX_HISTORY_SIZE)
        self._hist_idx = 0

    def decompress_segment(self, data):
        """Decompress a single ZGFX segment (after descriptor byte)."""
        cdef int n, idx, space, rest
        if len(data) < 1:
            return b''

        flags = data[0]
        payload = data[1:]

        if flags & PACKET_COMPRESSED:
            return self._decompress_compressed(payload)
        else:
            n = len(payload)
            if n > 0:
                hist = self._history
                idx = self._hist_idx
                space = ZGFX_HISTORY_SIZE - idx
                if n <= space:
                    hist[idx:idx + n] = payload
                    self._hist_idx = idx + n
                else:
                    hist[idx:idx + space] = payload[:space]
                    rest = n - space
                    hist[0:rest] = payload[space:]
                    self._hist_idx = rest
                if self._hist_idx >= ZGFX_HISTORY_SIZE:
                    self._hist_idx %= ZGFX_HISTORY_SIZE
            return bytes(payload)

    cpdef bytes _decompress_compressed(self, data):
        """Hot-path: decompress one RDP8 compressed segment."""
        cdef:
            # Bit accumulator — never needs more than 9+24 = 33 bits; ull is safe
            unsigned long long bits_current = 0
            int n_bits_current = 0
            int bits_remaining
            int num_data_bytes
            int padding_bits
            int input_pos = 0
            int hist_idx
            int hist_size = ZGFX_HISTORY_SIZE
            # Per-token
            int plen, vbits, ttype, vbase, peek
            int value, distance, count, bit, extra
            int src_idx, space, actual, b, i
            # Memoryviews for zero-overhead array access
            const unsigned char[:] in_view
            unsigned char[:] hist_view
            _Entry entry

        if len(data) < 1:
            return b''

        num_data_bytes = len(data) - 1
        padding_bits = data[num_data_bytes]   # last byte = padding count
        if num_data_bytes <= 0:
            return b''
        bits_remaining = num_data_bytes * 8 - padding_bits
        if bits_remaining <= 0:
            return b''

        in_view   = data
        hist_view = self._history
        hist_idx  = self._hist_idx

        out = bytearray()

        while bits_remaining > 0:
            # Refill accumulator to at least 9 bits for Huffman lookup
            while n_bits_current < 9:
                bits_current = (bits_current << 8)
                if input_pos < num_data_bytes:
                    bits_current |= in_view[input_pos]
                    input_pos += 1
                n_bits_current += 8

            # O(1) lookup
            peek = (bits_current >> (n_bits_current - 9)) & 0x1FF
            entry = _HUFF_LOOKUP[peek]
            if not entry.valid:
                log.warning("ZGFX: no token at input_pos=%d outLen=%d" %
                            (input_pos, len(out)))
                break

            plen  = entry.plen
            vbits = entry.vbits
            ttype = entry.ttype
            vbase = entry.vbase

            bits_remaining  -= plen
            n_bits_current  -= plen
            if n_bits_current > 0:
                bits_current &= (1 << n_bits_current) - 1
            else:
                bits_current = 0

            if ttype == 0:
                # ---- Literal ----
                if vbits > 0:
                    while n_bits_current < vbits:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= in_view[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= vbits
                    n_bits_current -= vbits
                    value = (vbase + ((bits_current >> n_bits_current) & ((1 << vbits) - 1))) & 0xFF
                    if n_bits_current > 0:
                        bits_current &= (1 << n_bits_current) - 1
                    else:
                        bits_current = 0
                else:
                    value = vbase & 0xFF

                hist_view[hist_idx] = value
                hist_idx += 1
                if hist_idx >= hist_size:
                    hist_idx = 0
                out.append(value)

            else:
                # ---- Match or Unencoded ----
                if vbits > 0:
                    while n_bits_current < vbits:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= in_view[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= vbits
                    n_bits_current -= vbits
                    distance = vbase + ((bits_current >> n_bits_current) & ((1 << vbits) - 1))
                    if n_bits_current > 0:
                        bits_current &= (1 << n_bits_current) - 1
                    else:
                        bits_current = 0
                else:
                    distance = vbase

                if distance != 0:
                    # Decode match length
                    while n_bits_current < 1:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= in_view[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= 1
                    n_bits_current -= 1
                    bit = (bits_current >> n_bits_current) & 1
                    if n_bits_current > 0:
                        bits_current &= (1 << n_bits_current) - 1
                    else:
                        bits_current = 0

                    if bit == 0:
                        count = 3
                    else:
                        count = 4
                        extra = 2
                        while True:
                            while n_bits_current < 1:
                                bits_current = (bits_current << 8)
                                if input_pos < num_data_bytes:
                                    bits_current |= in_view[input_pos]
                                    input_pos += 1
                                n_bits_current += 8
                            bits_remaining -= 1
                            n_bits_current -= 1
                            bit = (bits_current >> n_bits_current) & 1
                            if n_bits_current > 0:
                                bits_current &= (1 << n_bits_current) - 1
                            else:
                                bits_current = 0
                            if bit != 1:
                                break
                            count *= 2
                            extra += 1
                        while n_bits_current < extra:
                            bits_current = (bits_current << 8)
                            if input_pos < num_data_bytes:
                                bits_current |= in_view[input_pos]
                                input_pos += 1
                            n_bits_current += 8
                        bits_remaining -= extra
                        n_bits_current -= extra
                        count += (bits_current >> n_bits_current) & ((1 << extra) - 1)
                        if n_bits_current > 0:
                            bits_current &= (1 << n_bits_current) - 1
                        else:
                            bits_current = 0

                    # Copy from history
                    src_idx = (hist_idx + hist_size - distance) % hist_size
                    if distance >= count and src_idx + count <= hist_size and hist_idx + count <= hist_size:
                        # Non-overlapping: bulk copy (avoid bytes intermediate — causes
                        # "an integer is required" when assigning bytes to unsigned char[:])
                        out.extend(hist_view[src_idx:src_idx + count])
                        hist_view[hist_idx:hist_idx + count] = hist_view[src_idx:src_idx + count]
                        hist_idx += count
                        if hist_idx >= hist_size:
                            hist_idx %= hist_size
                    else:
                        # Overlapping or wrap-around: byte by byte
                        for i in range(count):
                            b = hist_view[src_idx % hist_size]
                            hist_view[hist_idx] = b
                            hist_idx += 1
                            if hist_idx >= hist_size:
                                hist_idx = 0
                            out.append(b)
                            src_idx += 1

                else:
                    # Unencoded: 15-bit count + flush + raw bytes
                    while n_bits_current < 15:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= in_view[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= 15
                    n_bits_current -= 15
                    count = (bits_current >> n_bits_current) & ((1 << 15) - 1)
                    if n_bits_current > 0:
                        bits_current &= (1 << n_bits_current) - 1
                    else:
                        bits_current = 0
                    bits_remaining -= n_bits_current
                    n_bits_current = 0
                    bits_current = 0
                    # Bulk copy raw bytes (avoid bytes intermediate — causes
                    # "an integer is required" when assigning bytes to unsigned char[:])
                    actual = min(count, num_data_bytes - input_pos)
                    out.extend(in_view[input_pos:input_pos + actual])
                    space = hist_size - hist_idx
                    if actual <= space:
                        hist_view[hist_idx:hist_idx + actual] = in_view[input_pos:input_pos + actual]
                        hist_idx += actual
                    else:
                        hist_view[hist_idx:hist_idx + space] = in_view[input_pos:input_pos + space]
                        rest = actual - space
                        hist_view[0:rest] = in_view[input_pos + space:input_pos + actual]
                        hist_idx = rest
                    if hist_idx >= hist_size:
                        hist_idx %= hist_size
                    input_pos += actual
                    bits_remaining -= actual * 8

        self._hist_idx = hist_idx
        return bytes(out)
