"""
ZGFX (RDP 8.0 Bulk Compression) decompressor.
Based on FreeRDP's zgfx.c implementation (MS-RDPEGFX 3.3.8.2).
"""

import rdpy.core.log as log

ZGFX_HISTORY_SIZE = 2500000
PACKET_COMPRESSED = 0x20

# Full Huffman token table from FreeRDP zgfx.c / MS-RDPEGFX 3.3.8.2
# (prefixLength, prefixCode, valueBits, tokenType, valueBase)
# tokenType: 0 = literal, 1 = match
_TOKEN_TABLE = [
    (1,   0,    8,  0, 0),          # 0           -> literal (any byte)
    (5,   17,   5,  1, 0),          # 10001       -> match
    (5,   18,   7,  1, 32),         # 10010       -> match
    (5,   19,   9,  1, 160),        # 10011       -> match
    (5,   20,   10, 1, 672),        # 10100       -> match
    (5,   21,   12, 1, 1696),       # 10101       -> match
    (5,   24,   0,  0, 0x00),       # 11000       -> literal 0x00
    (5,   25,   0,  0, 0x01),       # 11001       -> literal 0x01
    (6,   44,   14, 1, 5792),       # 101100      -> match
    (6,   45,   15, 1, 22176),      # 101101      -> match
    (6,   52,   0,  0, 0x02),       # 110100      -> literal 0x02
    (6,   53,   0,  0, 0x03),       # 110101      -> literal 0x03
    (6,   54,   0,  0, 0xFF),       # 110110      -> literal 0xFF
    (7,   92,   18, 1, 54944),      # 1011100     -> match
    (7,   93,   20, 1, 317088),     # 1011101     -> match
    (7,   110,  0,  0, 0x04),       # 1101110     -> literal 0x04
    (7,   111,  0,  0, 0x05),       # 1101111     -> literal 0x05
    (7,   112,  0,  0, 0x06),       # 1110000     -> literal 0x06
    (7,   113,  0,  0, 0x07),       # 1110001     -> literal 0x07
    (7,   114,  0,  0, 0x08),       # 1110010     -> literal 0x08
    (7,   115,  0,  0, 0x09),       # 1110011     -> literal 0x09
    (7,   116,  0,  0, 0x0A),       # 1110100     -> literal 0x0A
    (7,   117,  0,  0, 0x0B),       # 1110101     -> literal 0x0B
    (7,   118,  0,  0, 0x3A),       # 1110110     -> literal 0x3A
    (7,   119,  0,  0, 0x3B),       # 1110111     -> literal 0x3B
    (7,   120,  0,  0, 0x3C),       # 1111000     -> literal 0x3C
    (7,   121,  0,  0, 0x3D),       # 1111001     -> literal 0x3D
    (7,   122,  0,  0, 0x3E),       # 1111010     -> literal 0x3E
    (7,   123,  0,  0, 0x3F),       # 1111011     -> literal 0x3F
    (7,   124,  0,  0, 0x40),       # 1111100     -> literal 0x40
    (7,   125,  0,  0, 0x80),       # 1111101     -> literal 0x80
    (8,   188,  20, 1, 1365664),    # 10111100    -> match
    (8,   189,  21, 1, 2414240),    # 10111101    -> match
    (8,   252,  0,  0, 0x0C),       # 11111100    -> literal 0x0C
    (8,   253,  0,  0, 0x38),       # 11111101    -> literal 0x38
    (8,   254,  0,  0, 0x39),       # 11111110    -> literal 0x39
    (8,   255,  0,  0, 0x66),       # 11111111    -> literal 0x66
    (9,   380,  22, 1, 4511392),    # 101111100   -> match
    (9,   381,  23, 1, 8705696),    # 101111101   -> match
    (9,   382,  24, 1, 17094304),   # 101111110   -> match
]

# Pre-built lookup tables for O(1) Huffman decode (max prefix = 9 bits).
# _HUFF_LOOKUP[bits9] = (prefixLength, valueBits, tokenType, valueBase) or None.
# _HUFF_FLAT groups entries by prefix length for fast fallback.
_MAX_PREFIX_BITS = 9
_HUFF_LOOKUP = [None] * (1 << _MAX_PREFIX_BITS)  # 512 entries
for _plen, _pcode, _vbits, _ttype, _vbase in _TOKEN_TABLE:
    _pad_bits = _MAX_PREFIX_BITS - _plen
    _base_idx = _pcode << _pad_bits
    for _pad in range(1 << _pad_bits):
        _HUFF_LOOKUP[_base_idx | _pad] = (_plen, _vbits, _ttype, _vbase)
# Convert to tuple for slightly faster indexing
_HUFF_LOOKUP = tuple(_HUFF_LOOKUP)


class ZgfxDecompressor:
    """Stateful ZGFX decompressor with persistent history buffer.
    Based on FreeRDP's zgfx.c implementation."""

    def __init__(self):
        self._history = bytearray(ZGFX_HISTORY_SIZE)
        self._hist_idx = 0

    def decompress_segment(self, data):
        """Decompress a single ZGFX segment (after descriptor byte).
        data starts with the flags/header byte."""
        if len(data) < 1:
            return b''

        flags = data[0]
        payload = data[1:]

        if flags & PACKET_COMPRESSED:
            return self._decompress_compressed(payload)
        else:
            # Uncompressed: bulk-copy to history and return
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

    def _decompress_compressed(self, data):
        """Decompress RDP8 compressed data (FreeRDP zgfx_decompress_segment).
        The last byte is the padding bit count, not compressed data."""
        if len(data) < 1:
            return b''

        # Last byte = number of padding bits to subtract
        # Total valid bits = (len-1)*8 - lastByte  (MS-RDPEGFX 3.3.8.1)
        num_data_bytes = len(data) - 1
        padding_bits = data[-1]
        if num_data_bytes <= 0:
            return b''
        bits_remaining = num_data_bytes * 8 - padding_bits
        if bits_remaining <= 0:
            return b''

        # Input data (excluding last byte which is padding count)
        input_data = data
        input_pos = 0
        # Use a wide accumulator to minimize per-bit refills
        bits_current = 0
        n_bits_current = 0
        out = bytearray()
        hist = self._history
        hist_idx = self._hist_idx
        hist_size = ZGFX_HISTORY_SIZE
        huff_lookup = _HUFF_LOOKUP
        max_prefix = _MAX_PREFIX_BITS

        while bits_remaining > 0:
            # Ensure we have at least 9 bits in the accumulator for Huffman lookup
            while n_bits_current < max_prefix:
                bits_current = (bits_current << 8)
                if input_pos < num_data_bytes:
                    bits_current |= input_data[input_pos]
                    input_pos += 1
                n_bits_current += 8

            # O(1) Huffman lookup via 9-bit prefix table
            peek = (bits_current >> (n_bits_current - max_prefix)) & 0x1FF
            entry = huff_lookup[peek]
            if entry is None:
                log.warning("ZGFX: no token matched at input_pos=%d outLen=%d" %
                            (input_pos, len(out)))
                break

            plen, vbits, ttype, vbase = entry
            bits_remaining -= plen
            n_bits_current -= plen
            bits_current &= (1 << n_bits_current) - 1 if n_bits_current > 0 else 0

            if ttype == 0:
                # Literal
                if vbits > 0:
                    while n_bits_current < vbits:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= input_data[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= vbits
                    n_bits_current -= vbits
                    value = (vbase + ((bits_current >> n_bits_current) & ((1 << vbits) - 1))) & 0xFF
                    bits_current &= (1 << n_bits_current) - 1 if n_bits_current > 0 else 0
                else:
                    value = vbase
                hist[hist_idx] = value
                hist_idx += 1
                if hist_idx >= hist_size:
                    hist_idx = 0
                out.append(value)
            else:
                # Match or Unencoded
                if vbits > 0:
                    while n_bits_current < vbits:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= input_data[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= vbits
                    n_bits_current -= vbits
                    distance = vbase + ((bits_current >> n_bits_current) & ((1 << vbits) - 1))
                    bits_current &= (1 << n_bits_current) - 1 if n_bits_current > 0 else 0
                else:
                    distance = vbase
                if distance != 0:
                    # Decode match length
                    while n_bits_current < 1:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= input_data[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= 1
                    n_bits_current -= 1
                    bit = (bits_current >> n_bits_current) & 1
                    bits_current &= (1 << n_bits_current) - 1 if n_bits_current > 0 else 0
                    if bit == 0:
                        count = 3
                    else:
                        count = 4
                        extra = 2
                        while True:
                            while n_bits_current < 1:
                                bits_current = (bits_current << 8)
                                if input_pos < num_data_bytes:
                                    bits_current |= input_data[input_pos]
                                    input_pos += 1
                                n_bits_current += 8
                            bits_remaining -= 1
                            n_bits_current -= 1
                            bit = (bits_current >> n_bits_current) & 1
                            bits_current &= (1 << n_bits_current) - 1 if n_bits_current > 0 else 0
                            if bit != 1:
                                break
                            count *= 2
                            extra += 1
                        while n_bits_current < extra:
                            bits_current = (bits_current << 8)
                            if input_pos < num_data_bytes:
                                bits_current |= input_data[input_pos]
                                input_pos += 1
                            n_bits_current += 8
                        bits_remaining -= extra
                        n_bits_current -= extra
                        count += (bits_current >> n_bits_current) & ((1 << extra) - 1)
                        bits_current &= (1 << n_bits_current) - 1 if n_bits_current > 0 else 0

                    # output_match — batch copy from history
                    src_idx = (hist_idx + hist_size - distance) % hist_size
                    if distance >= count and src_idx + count <= hist_size and hist_idx + count <= hist_size:
                        # Non-overlapping: safe to batch
                        chunk = hist[src_idx:src_idx + count]
                        out.extend(chunk)
                        hist[hist_idx:hist_idx + count] = chunk
                        hist_idx += count
                        if hist_idx >= hist_size:
                            hist_idx %= hist_size
                    else:
                        for _ in range(count):
                            b = hist[src_idx % hist_size]
                            hist[hist_idx] = b
                            hist_idx += 1
                            if hist_idx >= hist_size:
                                hist_idx = 0
                            out.append(b)
                            src_idx += 1
                else:
                    # Unencoded: 15-bit count + flush bits + raw bytes
                    while n_bits_current < 15:
                        bits_current = (bits_current << 8)
                        if input_pos < num_data_bytes:
                            bits_current |= input_data[input_pos]
                            input_pos += 1
                        n_bits_current += 8
                    bits_remaining -= 15
                    n_bits_current -= 15
                    count = (bits_current >> n_bits_current) & ((1 << 15) - 1)
                    bits_current &= (1 << n_bits_current) - 1 if n_bits_current > 0 else 0
                    bits_remaining -= n_bits_current
                    n_bits_current = 0
                    bits_current = 0
                    # Bulk copy raw bytes
                    end_pos = min(input_pos + count, num_data_bytes)
                    chunk = input_data[input_pos:end_pos]
                    actual = len(chunk)
                    out.extend(chunk)
                    # Copy to history in bulk
                    space = hist_size - hist_idx
                    if actual <= space:
                        hist[hist_idx:hist_idx + actual] = chunk
                        hist_idx += actual
                    else:
                        hist[hist_idx:hist_idx + space] = chunk[:space]
                        rest = actual - space
                        hist[0:rest] = chunk[space:]
                        hist_idx = rest
                    if hist_idx >= hist_size:
                        hist_idx %= hist_size
                    input_pos = end_pos
                    bits_remaining -= actual * 8

        self._hist_idx = hist_idx
        return bytes(out)
