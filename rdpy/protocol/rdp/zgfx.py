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
            # Uncompressed: copy to output and history
            for b in payload:
                self._history[self._hist_idx] = b
                self._hist_idx = (self._hist_idx + 1) % ZGFX_HISTORY_SIZE
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
        input_pos = [0]
        bits_current = [0]
        n_bits_current = [0]
        bits_remaining_ref = [bits_remaining]
        out = bytearray()

        def get_bits(nbits):
            while n_bits_current[0] < nbits:
                bits_current[0] <<= 8
                if input_pos[0] < num_data_bytes:
                    bits_current[0] += input_data[input_pos[0]]
                    input_pos[0] += 1
                n_bits_current[0] += 8
            bits_remaining_ref[0] -= nbits
            n_bits_current[0] -= nbits
            result = (bits_current[0] >> n_bits_current[0]) & ((1 << nbits) - 1)
            bits_current[0] &= (1 << n_bits_current[0]) - 1
            return result

        def output_literal(b):
            self._history[self._hist_idx] = b
            self._hist_idx = (self._hist_idx + 1) % ZGFX_HISTORY_SIZE
            out.append(b)

        def output_match(distance, count):
            src_idx = (self._hist_idx + ZGFX_HISTORY_SIZE - distance) % ZGFX_HISTORY_SIZE
            for _ in range(count):
                b = self._history[src_idx % ZGFX_HISTORY_SIZE]
                self._history[self._hist_idx] = b
                self._hist_idx = (self._hist_idx + 1) % ZGFX_HISTORY_SIZE
                out.append(b)
                src_idx += 1

        while bits_remaining_ref[0] > 0:
            # Decode Huffman token by reading bits one at a time
            # (matching FreeRDP's token table scanning approach)
            have_bits = 0
            in_prefix = 0
            matched = False

            for entry in _TOKEN_TABLE:
                plen, pcode, vbits, ttype, vbase = entry
                while have_bits < plen:
                    in_prefix = (in_prefix << 1) + get_bits(1)
                    have_bits += 1
                if in_prefix == pcode:
                    if ttype == 0:
                        # Literal
                        value = (vbase + get_bits(vbits)) & 0xFF if vbits > 0 else vbase
                        output_literal(value)
                    else:
                        # Match or Unencoded
                        distance = vbase + get_bits(vbits)
                        if distance != 0:
                            # Match: decode length (FreeRDP scheme)
                            if get_bits(1) == 0:
                                count = 3
                            else:
                                count = 4
                                extra = 2
                                while get_bits(1) == 1:
                                    count *= 2
                                    extra += 1
                                count += get_bits(extra)
                            output_match(distance, count)
                        else:
                            # Unencoded: 15-bit count + flush bits + raw bytes
                            count = get_bits(15)
                            bits_remaining_ref[0] -= n_bits_current[0]
                            n_bits_current[0] = 0
                            bits_current[0] = 0
                            for _ in range(count):
                                if input_pos[0] < num_data_bytes:
                                    output_literal(input_data[input_pos[0]])
                                    input_pos[0] += 1
                                    bits_remaining_ref[0] -= 8
                    matched = True
                    break

            if not matched:
                log.warning("ZGFX: no token matched at input_pos=%d have_bits=%d prefix=0x%x outLen=%d" %
                            (input_pos[0], have_bits, in_prefix, len(out)))
                break

        return bytes(out)
