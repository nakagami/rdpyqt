#
# RFX Progressive Codec decoder (MS-RDPRFX / MS-RDPEGFX 2.2.4).
# Handles RDPGFX_CODECID_CAPROGRESSIVE (0x0009) in WIRE_TO_SURFACE_PDU_2.
# Ported from grdp Go implementation (commit 6d4735c).
#

import struct
import functools
import numpy as np
import rdpy.core.log as log
from rdpy.protocol.rdp.rlgr1_decode import rlgr1_decode

# Progressive block types
PROG_WBT_SYNC = 0xCCC0
PROG_WBT_FRAME_BEGIN = 0xCCC1
PROG_WBT_FRAME_END = 0xCCC2
PROG_WBT_CONTEXT = 0xCCC3
PROG_WBT_REGION = 0xCCC4
PROG_WBT_TILE_SIMPLE = 0xCCC5
PROG_WBT_TILE_FIRST = 0xCCC6
PROG_WBT_TILE_UPGRADE = 0xCCC7

RFX_TILE_SIZE = 64

# Pre-built index array for full 64×64 tiles — avoids np.arange() per tile call.
_FULL_TILE_COEFF_IDX = np.arange(RFX_TILE_SIZE * RFX_TILE_SIZE, dtype=np.int32)

# Sub-band sizes in RLGR decode order:
# HL1(1024) LH1(1024) HH1(1024) HL2(256) LH2(256) HH2(256) HL3(64) LH3(64) HH3(64) LL3(64)
_SUBBAND_SIZES = [1024, 1024, 1024, 256, 256, 256, 64, 64, 64, 64]


# ---------------------------------------------------------------
# RLGR1 Decoder
# ---------------------------------------------------------------

# _BitReader is kept here for use by _upgrade_component.
# The same class also lives in rlgr1_decode.py for use by the Python rlgr1_decode.
class _BitReader:
    __slots__ = ('_data', '_pos', '_total', '_accum', '_accum_bits')

    def __init__(self, data):
        self._data = bytes(data) if not isinstance(data, bytes) else data
        self._pos = 0
        self._total = len(data) * 8
        self._accum = 0       # bit accumulator (up to 64 bits)
        self._accum_bits = 0  # valid bits in accumulator

    def remaining(self):
        # _pos tracks bits loaded into accumulator; _accum_bits are loaded but unconsumed
        return self._total - self._pos + self._accum_bits

    def _refill(self, need):
        """Ensure at least `need` bits in the accumulator.
        Loads up to 8 bytes per call to minimize Python loop overhead."""
        data = self._data
        byte_pos = self._pos >> 3
        data_len = len(data)
        accum = self._accum
        accum_bits = self._accum_bits
        pos = self._pos
        # Load multiple bytes in a tight loop
        while accum_bits < need:
            if byte_pos < data_len:
                # Load up to 8 bytes at once (limited by available data)
                load = min(8, data_len - byte_pos, (64 - accum_bits) >> 3)
                if load <= 0:
                    load = 1
                for i in range(load):
                    accum = (accum << 8) | data[byte_pos]
                    byte_pos += 1
                accum_bits += load * 8
                pos += load * 8
            else:
                accum <<= 8
                accum_bits += 8
                pos += 8
        self._accum = accum
        self._accum_bits = accum_bits
        self._pos = pos

    def read_bit(self):
        if self._accum_bits < 1:
            self._refill(8)
        self._accum_bits -= 1
        return (self._accum >> self._accum_bits) & 1

    def read_bits(self, n):
        if n == 0:
            return 0
        if self._accum_bits < n:
            self._refill(n)
        self._accum_bits -= n
        val = (self._accum >> self._accum_bits) & ((1 << n) - 1)
        return val

    def count_leading_bits(self, target):
        """Count consecutive bits matching `target`. Scans multiple bits at
        a time within the accumulator to avoid per-bit Python overhead."""
        count = 0
        while self.remaining() > 0:
            if self._accum_bits < 8:
                self._refill(16)
            # Scan available bits in chunks — check 8 bits at a time
            while self._accum_bits >= 8:
                top8 = (self._accum >> (self._accum_bits - 8)) & 0xFF
                expected = 0xFF if target else 0x00
                if top8 == expected:
                    count += 8
                    self._accum_bits -= 8
                else:
                    # Scan remaining bits in this byte individually
                    for _ in range(8):
                        self._accum_bits -= 1
                        bit = (self._accum >> self._accum_bits) & 1
                        if bit == target:
                            count += 1
                        else:
                            return count
                    break
            else:
                continue
            break
        # Handle leftover bits (< 8) at end of stream
        while self.remaining() > 0 and self._accum_bits > 0:
            self._accum_bits -= 1
            bit = (self._accum >> self._accum_bits) & 1
            if bit == target:
                count += 1
            else:
                return count
        return count


# ---------------------------------------------------------------
# Quantization
# ---------------------------------------------------------------

class RfxQuant:
    """TS_RFX_CODEC_QUANT: 5-byte packed quantization values per MS-RDPRFX 2.2.2.1.5."""
    __slots__ = ('LL3', 'LH3', 'HL3', 'HH3', 'LH2', 'HL2', 'HH2', 'LH1', 'HL1', 'HH1')

    def __init__(self, data):
        # byte 0: LL3 (low), LH3 (high)
        self.LL3 = data[0] & 0x0F
        self.LH3 = data[0] >> 4
        # byte 1: HL3 (low), HH3 (high)
        self.HL3 = data[1] & 0x0F
        self.HH3 = data[1] >> 4
        # byte 2: LH2 (low), HL2 (high)
        self.LH2 = data[2] & 0x0F
        self.HL2 = data[2] >> 4
        # byte 3: HH2 (low), LH1 (high)
        self.HH2 = data[3] & 0x0F
        self.LH1 = data[3] >> 4
        # byte 4: HL1 (low), HH1 (high)
        self.HL1 = data[4] & 0x0F
        self.HH1 = data[4] >> 4

    @staticmethod
    def default():
        q = object.__new__(RfxQuant)
        q.LL3 = q.LH3 = q.HL3 = q.HH3 = 6
        q.LH2 = q.HL2 = q.HH2 = 6
        q.LH1 = q.HL1 = q.HH1 = 6
        return q

    def subband_shifts(self):
        """Return shift values per subband in RLGR decode order."""
        return [self.HL1, self.LH1, self.HH1,
                self.HL2, self.LH2, self.HH2,
                self.HL3, self.LH3, self.HH3, self.LL3]


class ProgQuant:
    """RFX_PROGRESSIVE_CODEC_QUANT: 16-byte progressive quant entry."""
    __slots__ = ('y', 'cb', 'cr', 'quality')

    def __init__(self, data):
        self.y = RfxQuant(data[0:5])
        self.cb = RfxQuant(data[5:10])
        self.cr = RfxQuant(data[10:15])
        self.quality = data[15]


@functools.lru_cache(maxsize=128)
def _make_shift_arr(hl1, lh1, hh1, hl2, lh2, hh2, hl3, lh3, hh3, ll3):
    """Build and cache the per-coefficient shift array for dequantization.

    Called once per unique set of quantization values (typically constant within
    a session) and then reused from the cache on every subsequent tile decode.
    """
    factors = np.array([hl1, lh1, hh1, hl2, lh2, hh2, hl3, lh3, hh3, ll3],
                       dtype=np.int16)
    shifts = np.maximum(factors - 1, 0)
    if not np.any(shifts > 0):
        return None
    return np.repeat(shifts, _SUBBAND_SIZES)


def _dequantize(coeffs, q):
    """Apply dequantization (left-shift by factor-1) per subband — vectorized."""
    shift_arr = _make_shift_arr(q.HL1, q.LH1, q.HH1, q.HL2, q.LH2, q.HH2,
                                q.HL3, q.LH3, q.HH3, q.LL3)
    if shift_arr is not None:
        coeffs <<= shift_arr


# ---------------------------------------------------------------
# Inverse 2D DWT (3 levels)
# ---------------------------------------------------------------

def _idwt_2d_level(buf, n):
    """One level of inverse 2D DWT.
    buf contains [HL(n²)|LH(n²)|HH(n²)|LL(n²)] → (2n)×(2n) result.
    Uses MS-RDPRFX lifting scheme matching FreeRDP/grdp."""
    nn = n * n
    size = 2 * n

    # Extract subbands as int32 2D arrays
    hl = buf[0:nn].astype(np.int32).reshape(n, n)
    lh = buf[nn:2*nn].astype(np.int32).reshape(n, n)
    hh = buf[2*nn:3*nn].astype(np.int32).reshape(n, n)
    ll = buf[3*nn:4*nn].astype(np.int32).reshape(n, n)

    # --- Step 1: Horizontal IDWT (all rows at once) ---
    # Stack L-part and H-part: rows 0..n-1 use (ll,hl), rows n..2n-1 use (lh,hh)
    lo = np.vstack([ll, lh])   # (2n, n)
    hi = np.vstack([hl, hh])   # (2n, n)

    # Even positions (undo update step); compute in int32, then truncate to int16 (matching grdp)
    even_h = np.empty((2 * n, n), dtype=np.int32)
    even_h[:, 0] = lo[:, 0] - ((hi[:, 0] * 2 + 1) >> 1)
    even_h[:, 1:] = lo[:, 1:] - ((hi[:, :-1] + hi[:, 1:] + 1) >> 1)
    even_h_i16 = even_h.astype(np.int16)
    even_h_i32 = even_h_i16.astype(np.int32)  # sign-extended truncated values

    # Odd positions — both neighbours are read from int16 tmp (both truncated, matching grdp)
    odd_h = np.empty((2 * n, n), dtype=np.int32)
    odd_h[:, :-1] = (hi[:, :-1] << 1) + ((even_h_i32[:, :-1] + even_h_i32[:, 1:]) >> 1)
    odd_h[:, -1] = (hi[:, -1] << 1) + even_h_i32[:, -1]

    # Interleave even/odd into tmp as int16 (matching grdp's int16 tmp buffer)
    tmp = np.empty((2 * n, size), dtype=np.int16)
    tmp[:, 0::2] = even_h_i16
    tmp[:, 1::2] = odd_h.astype(np.int16)

    # --- Step 2: Vertical IDWT (all columns at once) ---
    lo_v = tmp[:n, :].astype(np.int32)    # L rows (sign-extended from int16)
    hi_v = tmp[n:, :].astype(np.int32)    # H rows (sign-extended from int16)

    # Even rows; compute in int32, then truncate to int16 (matching grdp)
    even_v = np.empty((n, size), dtype=np.int32)
    even_v[0, :] = lo_v[0, :] - ((hi_v[0, :] * 2 + 1) >> 1)
    even_v[1:, :] = lo_v[1:, :] - ((hi_v[:-1, :] + hi_v[1:, :] + 1) >> 1)
    even_v_i16 = even_v.astype(np.int16)
    even_v_i32 = even_v_i16.astype(np.int32)  # sign-extended truncated values

    # Odd rows — prevEven (even[row-1]) is truncated; current even[row] is not yet truncated
    # (matches grdp: prevEven = int32(buf[prev]) is truncated, even = local int32 is not)
    odd_v = np.empty((n, size), dtype=np.int32)
    odd_v[:-1, :] = (hi_v[:-1, :] << 1) + ((even_v_i32[:-1, :] + even_v[1:, :]) >> 1)
    odd_v[-1, :] = (hi_v[-1, :] << 1) + even_v_i32[-1, :]

    # Interleave even/odd into output as int16
    out = np.empty((size, size), dtype=np.int16)
    out[0::2, :] = even_v_i16
    out[1::2, :] = odd_v.astype(np.int16)

    buf[:size * size] = out.reshape(-1)


def _inverse_dwt_2d(coeffs):
    """3-level inverse 2D DWT in-place."""
    _idwt_2d_level(coeffs[3840:], 8)   # Level 3: 8×8 → 16×16
    _idwt_2d_level(coeffs[3072:], 16)  # Level 2: 16×16 → 32×32
    _idwt_2d_level(coeffs[0:], 32)     # Level 1: 32×32 → 64×64


# ---------------------------------------------------------------
# Component decoding
# ---------------------------------------------------------------

def _decode_component(data, quant, store=False):
    """Decode one color component (Y/Cb/Cr) for a 64×64 tile.

    If store=True, also returns (raw_coeffs, sign_context) needed for
    progressive upgrade (pre-dequantization values).
    """
    TILE_PIXELS = RFX_TILE_SIZE * RFX_TILE_SIZE  # 4096

    # 1. RLGR1 entropy decode
    coeffs = rlgr1_decode(data, TILE_PIXELS)

    # 2. Differential decode LL3 (positions 4032..4095) using numpy cumsum
    coeffs[4032:4096] = np.cumsum(coeffs[4032:4096], dtype=np.int16)

    if store:
        raw = coeffs.copy()
        sign = np.sign(raw).astype(np.int8)

    # 3. Dequantize
    _dequantize(coeffs, quant)

    # 4. Inverse DWT (3 levels)
    _inverse_dwt_2d(coeffs)

    if store:
        return coeffs, raw, sign
    return coeffs


# ---------------------------------------------------------------
# YCbCr → BGRA color conversion and tile placement
# ---------------------------------------------------------------

def _place_tile_abs(y_coeffs, cb_coeffs, cr_coeffs, tile_x, tile_y, output, out_w, out_h):
    """Convert YCbCr tile to BGRA at absolute pixel coordinates (tile_x, tile_y)."""
    tile_w = min(RFX_TILE_SIZE, out_w - tile_x)
    tile_h = min(RFX_TILE_SIZE, out_h - tile_y)
    if tile_w <= 0 or tile_h <= 0:
        return

    # Build flat index into 64-wide coefficient arrays
    if tile_w == RFX_TILE_SIZE and tile_h == RFX_TILE_SIZE:
        # Full tile — direct contiguous astype() is faster than fancy indexing
        y_arr = y_coeffs.astype(np.int32)
        cb_arr = cb_coeffs.astype(np.int32)
        cr_arr = cr_coeffs.astype(np.int32)
    else:
        rows = np.arange(tile_h, dtype=np.int32)
        cols = np.arange(tile_w, dtype=np.int32)
        coeff_idx = (rows[:, np.newaxis] * RFX_TILE_SIZE + cols[np.newaxis, :]).ravel()
        y_arr = y_coeffs[coeff_idx].astype(np.int32)
        cb_arr = cb_coeffs[coeff_idx].astype(np.int32)
        cr_arr = cr_coeffs[coeff_idx].astype(np.int32)

    # ICT (BT.601) inverse colour transform
    y_scaled = (y_arr + 4096) << 16
    r = (cr_arr * 91916 + y_scaled) >> 21
    g = (y_scaled - cb_arr * 22527 - cr_arr * 46819) >> 21
    b = (cb_arr * 115992 + y_scaled) >> 21

    np.clip(r, 0, 255, out=r)
    np.clip(g, 0, 255, out=g)
    np.clip(b, 0, 255, out=b)

    # Assemble BGRA tile
    bgra = np.empty((tile_h, tile_w, 4), dtype=np.uint8)
    bgra_flat = bgra.reshape(-1, 4)
    bgra_flat[:, 0] = b
    bgra_flat[:, 1] = g
    bgra_flat[:, 2] = r
    bgra_flat[:, 3] = 0xFF

    # Write into output using numpy 2D view (avoids Python row loop)
    try:
        out_arr = np.frombuffer(output, dtype=np.uint8).reshape(out_h, out_w * 4)
        out_arr[tile_y:tile_y + tile_h, tile_x * 4:(tile_x + tile_w) * 4] = \
            bgra.reshape(tile_h, tile_w * 4)
    except (ValueError, IndexError):
        stride = tile_w * 4
        out_stride = out_w * 4
        out_mv = memoryview(output)
        bgra_bytes = bgra.tobytes()
        for row in range(tile_h):
            out_start = ((tile_y + row) * out_w + tile_x) * 4
            if out_start + stride <= len(output):
                out_mv[out_start:out_start + stride] = bgra_bytes[row * stride:(row + 1) * stride]


def _place_tile(y_coeffs, cb_coeffs, cr_coeffs, x_idx, y_idx, output, out_w, out_h):
    """Convert YCbCr tile to BGRA using tile grid indices."""
    _place_tile_abs(y_coeffs, cb_coeffs, cr_coeffs,
                    x_idx * RFX_TILE_SIZE, y_idx * RFX_TILE_SIZE,
                    output, out_w, out_h)


# ---------------------------------------------------------------
# SRL + RAW bit-level reader for progressive upgrades
# ---------------------------------------------------------------

def _upgrade_component(srl_data, raw_data, current, sign, quant, prog_quant):
    """Apply progressive upgrade delta to stored coefficients.
    Uses SRL+RAW bit-level decoding per MS-RDPEGFX 3.3.8.4.

    current: stored raw (pre-dequant) coefficients (int16, modified in-place)
    sign: stored sign context (int8, modified in-place)
    quant: regular quant values for dequantization
    prog_quant: progressive quant values (bit positions)
    """
    srl = _BitReader(srl_data)
    raw = _BitReader(raw_data)

    shifts = quant.subband_shifts()
    bit_positions = prog_quant.subband_shifts()

    offset = 0
    for band_idx, band_size in enumerate(_SUBBAND_SIZES):
        shift = shifts[band_idx]
        bit_pos = bit_positions[band_idx]
        n_bits = max(0, shift - bit_pos)

        # Process band: separate known-sign and unknown-sign elements
        for i in range(offset, offset + band_size):
            if sign[i] != 0:
                mag = raw.read_bits(n_bits) if n_bits > 0 else 0
                if sign[i] > 0:
                    current[i] += mag
                else:
                    current[i] -= mag
            else:
                if srl.read_bit():
                    mag = raw.read_bits(n_bits) if n_bits > 0 else 0
                    if srl.read_bit():
                        current[i] = -mag
                        sign[i] = -1
                    else:
                        current[i] = mag
                        sign[i] = 1

        offset += band_size


def _reconstruct_from_raw(raw_coeffs, quant):
    """Dequantize + inverse DWT from raw (pre-dequant) coefficients."""
    coeffs = raw_coeffs.astype(np.int16).copy()
    _dequantize(coeffs, quant)
    _inverse_dwt_2d(coeffs)
    return coeffs


# ---------------------------------------------------------------
# Progressive stream parser
# ---------------------------------------------------------------

class RfxProgressiveDecoder:
    """Decodes RFX Progressive codec data from RDPGFX WIRE_TO_SURFACE_2."""

    def __init__(self):
        self._debug_tile_count = 0
        # Per-tile state: (x_idx, y_idx) -> dict with y/cb/cr raw coefficients and sign
        self._tileState = {}

    def reset(self):
        """Reset tile state (called on DELETEENCODINGCONTEXT)."""
        self._tileState.clear()
        self._debug_tile_count = 0

    def decode(self, data, surf_data, width, height):
        """Decode progressive bitmap stream, render tiles onto surf_data (BGRA buffer).
        Returns list of (x, y, w, h) bounding rectangles."""
        rects = []
        offset = 0

        while offset + 6 <= len(data):
            block_type, block_len = struct.unpack_from('<HI', data, offset)

            if block_len < 6 or offset + block_len > len(data):
                break

            block_data = data[offset + 6:offset + block_len]

            if block_type == PROG_WBT_REGION:
                rects.extend(self._decode_region(block_data, surf_data, width, height))
            elif block_type in (PROG_WBT_SYNC, PROG_WBT_FRAME_BEGIN,
                                PROG_WBT_FRAME_END, PROG_WBT_CONTEXT):
                pass  # handled implicitly

            offset += block_len

        return rects

    def _decode_region(self, data, output, width, height):
        if len(data) < 12:
            return []

        num_rects = struct.unpack_from('<H', data, 1)[0]
        num_quant = data[3]
        num_prog_quant = data[4]
        flags = data[5]
        num_tiles = struct.unpack_from('<H', data, 6)[0]

        offset = 12

        # Parse rects
        rects = []
        for _ in range(num_rects):
            if offset + 8 > len(data):
                return []
            rx, ry, rw, rh = struct.unpack_from('<HHHH', data, offset)
            rects.append((rx, ry, rw, rh))
            offset += 8

        # Parse quant values (5 bytes each)
        quants = []
        for _ in range(num_quant):
            if offset + 5 > len(data):
                return []
            quants.append(RfxQuant(data[offset:offset + 5]))
            offset += 5

        # Parse progressive quant values (16 bytes each)
        prog_quants = []
        for _ in range(num_prog_quant):
            if offset + 16 > len(data):
                return []
            prog_quants.append(ProgQuant(data[offset:offset + 16]))
            offset += 16

        log.debug("RFX: region %d tiles, %d quants, %d rects, flags=0x%02X" %
                  (num_tiles, num_quant, num_rects, flags))
        for i, (rx, ry, rw, rh) in enumerate(rects):
            log.debug("RFX:   rect[%d]=(%d,%d,%d,%d)" % (i, rx, ry, rw, rh))
        for i, q in enumerate(quants):
            log.debug("RFX:   quant[%d] LL3=%d LH3=%d HL3=%d HH3=%d LH2=%d HL2=%d HH2=%d LH1=%d HL1=%d HH1=%d" %
                      (i, q.LL3, q.LH3, q.HL3, q.HH3, q.LH2, q.HL2, q.HH2, q.LH1, q.HL1, q.HH1))

        tile_positions = []
        for _ in range(num_tiles):
            if offset + 6 > len(data):
                break
            tile_type, tile_len = struct.unpack_from('<HI', data, offset)

            if tile_len < 6 or offset + tile_len > len(data):
                break

            tile_data = data[offset + 6:offset + tile_len]

            # Log tile position from header (xIdx at offset 3, yIdx at offset 5)
            if len(tile_data) >= 7:
                tx, ty = struct.unpack_from('<HH', tile_data, 3)
                tile_positions.append((tx, ty))

            if tile_type == PROG_WBT_TILE_SIMPLE:
                self._decode_tile_simple(tile_data, quants, output, width, height)
            elif tile_type == PROG_WBT_TILE_FIRST:
                self._decode_tile_first(tile_data, quants, output, width, height)
            elif tile_type == PROG_WBT_TILE_UPGRADE:
                self._decode_tile_upgrade(tile_data, quants, prog_quants,
                                          output, width, height)

            offset += tile_len

        if tile_positions:
            log.debug("RFX: tile positions (idx): %s" %
                      ", ".join("(%d,%d)" % (tx, ty) for tx, ty in tile_positions[:10]))
            if len(tile_positions) > 10:
                log.debug("RFX:   ... and %d more tiles" % (len(tile_positions) - 10))

        # MS-RDPEGFX: numRects==0 means the tile data represents a full-surface
        # refresh — compute dirty bounds from decoded tile positions.
        if num_rects == 0 and tile_positions:
            min_x = min(tx * 64 for (tx, ty) in tile_positions)
            min_y = min(ty * 64 for (tx, ty) in tile_positions)
            max_x = min(max(tx * 64 + 64 for (tx, ty) in tile_positions), width)
            max_y = min(max(ty * 64 + 64 for (tx, ty) in tile_positions), height)
            rects = [(min_x, min_y, max_x - min_x, max_y - min_y)]
        elif num_rects == 0:
            rects = [(0, 0, width, height)]

        return rects

    def _get_quant(self, quants, idx):
        if idx < len(quants):
            return quants[idx]
        return RfxQuant.default()

    def _decode_tile_simple(self, data, quants, output, out_w, out_h):
        if len(data) < 16:
            return
        quant_idx_y = data[0]
        quant_idx_cb = data[1]
        quant_idx_cr = data[2]
        x_idx, y_idx = struct.unpack_from('<HH', data, 3)
        y_len, cb_len, cr_len = struct.unpack_from('<HHH', data, 8)

        off = 16
        y_data = data[off:off + y_len] if y_len > 0 else None
        off += y_len
        cb_data = data[off:off + cb_len] if cb_len > 0 else None
        off += cb_len
        cr_data = data[off:off + cr_len] if cr_len > 0 else None

        q_y = self._get_quant(quants, quant_idx_y)
        q_cb = self._get_quant(quants, quant_idx_cb)
        q_cr = self._get_quant(quants, quant_idx_cr)

        if self._debug_tile_count < 3:
            log.debug("RFX: TILE_SIMPLE (%d,%d) yLen=%d cbLen=%d crLen=%d qY=%d qCb=%d qCr=%d" %
                      (x_idx, y_idx, y_len, cb_len, cr_len,
                       quant_idx_y, quant_idx_cb, quant_idx_cr))
        self._debug_tile_count += 1

        y_pixels, y_raw, y_sign = _decode_component(y_data, q_y, store=True)
        cb_pixels, cb_raw, cb_sign = _decode_component(cb_data, q_cb, store=True)
        cr_pixels, cr_raw, cr_sign = _decode_component(cr_data, q_cr, store=True)

        if self._debug_tile_count <= 3:
            # Sample center pixel (32,32) of the tile for debugging
            idx = 32 * 64 + 32
            log.debug("RFX:   tile(%d,%d) center Y=%d Cb=%d Cr=%d" %
                      (x_idx, y_idx, int(y_pixels[idx]), int(cb_pixels[idx]), int(cr_pixels[idx])))

        # Store tile state for potential future upgrades
        self._tileState[(x_idx, y_idx)] = {
            'y_raw': y_raw, 'cb_raw': cb_raw, 'cr_raw': cr_raw,
            'y_sign': y_sign, 'cb_sign': cb_sign, 'cr_sign': cr_sign,
        }

        _place_tile(y_pixels, cb_pixels, cr_pixels, x_idx, y_idx, output, out_w, out_h)

    def _decode_tile_first(self, data, quants, output, out_w, out_h):
        if len(data) < 17:
            return
        quant_idx_y = data[0]
        quant_idx_cb = data[1]
        quant_idx_cr = data[2]
        x_idx, y_idx = struct.unpack_from('<HH', data, 3)
        y_len, cb_len, cr_len = struct.unpack_from('<HHH', data, 9)

        off = 17
        y_data = data[off:off + y_len] if y_len > 0 else None
        off += y_len
        cb_data = data[off:off + cb_len] if cb_len > 0 else None
        off += cb_len
        cr_data = data[off:off + cr_len] if cr_len > 0 else None

        q_y = self._get_quant(quants, quant_idx_y)
        q_cb = self._get_quant(quants, quant_idx_cb)
        q_cr = self._get_quant(quants, quant_idx_cr)

        if self._debug_tile_count < 3:
            log.debug("RFX: TILE_FIRST (%d,%d) yLen=%d cbLen=%d crLen=%d qY=%d qCb=%d qCr=%d" %
                      (x_idx, y_idx, y_len, cb_len, cr_len,
                       quant_idx_y, quant_idx_cb, quant_idx_cr))
        self._debug_tile_count += 1

        y_pixels, y_raw, y_sign = _decode_component(y_data, q_y, store=True)
        cb_pixels, cb_raw, cb_sign = _decode_component(cb_data, q_cb, store=True)
        cr_pixels, cr_raw, cr_sign = _decode_component(cr_data, q_cr, store=True)

        if self._debug_tile_count <= 3:
            idx = 32 * 64 + 32
            log.debug("RFX:   tile(%d,%d) center Y=%d Cb=%d Cr=%d" %
                      (x_idx, y_idx, int(y_pixels[idx]), int(cb_pixels[idx]), int(cr_pixels[idx])))

        # Store tile state for progressive upgrades
        self._tileState[(x_idx, y_idx)] = {
            'y_raw': y_raw, 'cb_raw': cb_raw, 'cr_raw': cr_raw,
            'y_sign': y_sign, 'cb_sign': cb_sign, 'cr_sign': cr_sign,
        }

        _place_tile(y_pixels, cb_pixels, cr_pixels, x_idx, y_idx, output, out_w, out_h)

    def _decode_tile_upgrade(self, data, quants, prog_quants, output, out_w, out_h):
        """Decode a TILE_UPGRADE: apply progressive delta to stored tile state."""
        if len(data) < 20:
            return
        quant_idx_y = data[0]
        quant_idx_cb = data[1]
        quant_idx_cr = data[2]
        x_idx, y_idx = struct.unpack_from('<HH', data, 3)
        quality = data[7]
        y_srl_len, y_raw_len, cb_srl_len, cb_raw_len, cr_srl_len, cr_raw_len = struct.unpack_from('<HHHHHH', data, 8)

        off = 20
        y_srl = data[off:off + y_srl_len]; off += y_srl_len
        y_raw = data[off:off + y_raw_len]; off += y_raw_len
        cb_srl = data[off:off + cb_srl_len]; off += cb_srl_len
        cb_raw = data[off:off + cb_raw_len]; off += cb_raw_len
        cr_srl = data[off:off + cr_srl_len]; off += cr_srl_len
        cr_raw = data[off:off + cr_raw_len]; off += cr_raw_len

        key = (x_idx, y_idx)
        state = self._tileState.get(key)
        if state is None:
            return  # No previous state to upgrade

        q_y = self._get_quant(quants, quant_idx_y)
        q_cb = self._get_quant(quants, quant_idx_cb)
        q_cr = self._get_quant(quants, quant_idx_cr)

        # Get progressive quant by quality index
        pq = self._get_prog_quant(prog_quants, quality)

        # Apply upgrade deltas to stored coefficients
        _upgrade_component(y_srl, y_raw, state['y_raw'], state['y_sign'],
                           q_y, pq.y if pq else q_y)
        _upgrade_component(cb_srl, cb_raw, state['cb_raw'], state['cb_sign'],
                           q_cb, pq.cb if pq else q_cb)
        _upgrade_component(cr_srl, cr_raw, state['cr_raw'], state['cr_sign'],
                           q_cr, pq.cr if pq else q_cr)

        # Reconstruct from updated raw coefficients
        y_pixels = _reconstruct_from_raw(state['y_raw'], q_y)
        cb_pixels = _reconstruct_from_raw(state['cb_raw'], q_cb)
        cr_pixels = _reconstruct_from_raw(state['cr_raw'], q_cr)

        _place_tile(y_pixels, cb_pixels, cr_pixels, x_idx, y_idx, output, out_w, out_h)

    def _get_prog_quant(self, prog_quants, quality):
        """Get progressive quant entry by quality index."""
        if quality < len(prog_quants):
            return prog_quants[quality]
        if prog_quants:
            return prog_quants[-1]
        return None
