#
# RFX Progressive Codec decoder (MS-RDPRFX / MS-RDPEGFX 2.2.4).
# Handles RDPGFX_CODECID_CAPROGRESSIVE (0x0009) in WIRE_TO_SURFACE_PDU_2.
# Ported from grdp Go implementation (commit 6d4735c).
#

import struct
import numpy as np
import rdpy.core.log as log

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

# Sub-band sizes in RLGR decode order:
# HL1(1024) LH1(1024) HH1(1024) HL2(256) LH2(256) HH2(256) HL3(64) LH3(64) HH3(64) LL3(64)
_SUBBAND_SIZES = [1024, 1024, 1024, 256, 256, 256, 64, 64, 64, 64]


# ---------------------------------------------------------------
# RLGR1 Decoder
# ---------------------------------------------------------------

class _BitReader:
    __slots__ = ('_data', '_pos', '_total')

    def __init__(self, data):
        self._data = data
        self._pos = 0
        self._total = len(data) * 8

    def remaining(self):
        return self._total - self._pos

    def read_bit(self):
        if self._pos >= self._total:
            return 0
        byte_idx = self._pos >> 3
        bit_idx = 7 - (self._pos & 7)
        self._pos += 1
        return (self._data[byte_idx] >> bit_idx) & 1

    def read_bits(self, n):
        val = 0
        for _ in range(n):
            val = (val << 1) | self.read_bit()
        return val

    def count_leading_bits(self, target):
        count = 0
        while self.remaining() > 0:
            bit = self.read_bit()
            if bit == target:
                count += 1
            else:
                return count
        return count


# RLGR1 constants (matching FreeRDP/grdp)
_LSGR = 3
_KPMAX = 80
_UPGR = 4
_DNGR = 6
_UQGR = 3
_DQGR = 3


def rlgr1_decode(data, output_size):
    """Decode RLGR1 encoded data into signed 16-bit DWT coefficients."""
    if data is None or len(data) == 0:
        return np.zeros(output_size, dtype=np.int16)

    br = _BitReader(data)
    output = np.zeros(output_size, dtype=np.int16)
    cnt = 0

    k = 1
    kp = 1 << _LSGR  # 8
    kr = 1
    krp = 1 << _LSGR  # 8

    while br.remaining() > 0 and cnt < output_size:
        if k > 0:
            # RL (Run-Length) Mode
            vk = br.count_leading_bits(0)
            if br.remaining() < 0:
                break

            run = 0
            for _ in range(vk):
                run += 1 << k
                kp += _UPGR
                if kp > _KPMAX:
                    kp = _KPMAX
                k = kp >> _LSGR

            if br.remaining() < k:
                break
            if k > 0:
                run += br.read_bits(k)

            if br.remaining() < 1:
                break
            sign = br.read_bits(1)

            vk2 = br.count_leading_bits(1)
            if br.remaining() < 0:
                break

            if br.remaining() < kr:
                break
            code = br.read_bits(kr) if kr > 0 else 0
            code |= vk2 << kr

            # Update kr/krp
            if vk2 == 0:
                krp = max(0, krp - 2)
                kr = krp >> _LSGR
            elif vk2 != 1:
                krp = min(_KPMAX, krp + vk2)
                kr = krp >> _LSGR

            # Update k/kp (decrease after non-zero)
            kp = max(0, kp - _DNGR)
            k = kp >> _LSGR

            mag = code + 1

            # Output run zeros then the non-zero value
            end = min(cnt + run, output_size)
            # output[cnt:end] already 0
            cnt = end
            if cnt < output_size:
                output[cnt] = -mag if sign else mag
                cnt += 1

        else:
            # GR (Golomb-Rice) Mode
            vk = br.count_leading_bits(1)
            if br.remaining() < 0:
                break

            if br.remaining() < kr:
                break
            code = br.read_bits(kr) if kr > 0 else 0
            code |= vk << kr

            # Update kr/krp
            if vk == 0:
                krp = max(0, krp - 2)
                kr = krp >> _LSGR
            elif vk != 1:
                krp = min(_KPMAX, krp + vk)
                kr = krp >> _LSGR

            if code == 0:
                kp = min(_KPMAX, kp + _UQGR)
                k = kp >> _LSGR
                if cnt < output_size:
                    output[cnt] = 0
                    cnt += 1
            else:
                kp = max(0, kp - _DQGR)
                k = kp >> _LSGR
                # RLGR1: code = 2*magnitude - sign
                if code & 1:
                    mag = -((code + 1) >> 1)
                else:
                    mag = code >> 1
                if cnt < output_size:
                    output[cnt] = mag
                    cnt += 1

    return output


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


def _dequantize(coeffs, q):
    """Apply dequantization (left-shift by factor-1) per subband."""
    # Sub-band order: HL1 LH1 HH1 HL2 LH2 HH2 HL3 LH3 HH3 LL3
    factors = [q.HL1, q.LH1, q.HH1, q.HL2, q.LH2, q.HH2,
               q.HL3, q.LH3, q.HH3, q.LL3]
    offset = 0
    for i, size in enumerate(_SUBBAND_SIZES):
        f = factors[i]
        if f > 1:
            coeffs[offset:offset + size] <<= (f - 1)
        offset += size


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

    # Even positions (undo update step)
    even_h = np.empty((2 * n, n), dtype=np.int32)
    even_h[:, 0] = lo[:, 0] - ((hi[:, 0] * 2 + 1) >> 1)
    even_h[:, 1:] = lo[:, 1:] - ((hi[:, :-1] + hi[:, 1:] + 1) >> 1)

    # Odd positions (undo predict step, H<<1 scaling)
    odd_h = np.empty((2 * n, n), dtype=np.int32)
    odd_h[:, :-1] = (hi[:, :-1] << 1) + ((even_h[:, :-1] + even_h[:, 1:]) >> 1)
    odd_h[:, -1] = (hi[:, -1] << 1) + even_h[:, -1]

    # Interleave even/odd into tmp columns
    tmp = np.empty((2 * n, size), dtype=np.int32)
    tmp[:, 0::2] = even_h
    tmp[:, 1::2] = odd_h

    # --- Step 2: Vertical IDWT (all columns at once) ---
    lo_v = tmp[:n, :]    # L rows
    hi_v = tmp[n:, :]    # H rows

    # Even rows
    even_v = np.empty((n, size), dtype=np.int32)
    even_v[0, :] = lo_v[0, :] - ((hi_v[0, :] * 2 + 1) >> 1)
    even_v[1:, :] = lo_v[1:, :] - ((hi_v[:-1, :] + hi_v[1:, :] + 1) >> 1)

    # Odd rows
    odd_v = np.empty((n, size), dtype=np.int32)
    odd_v[:-1, :] = (hi_v[:-1, :] << 1) + ((even_v[:-1, :] + even_v[1:, :]) >> 1)
    odd_v[-1, :] = (hi_v[-1, :] << 1) + even_v[-1, :]

    # Interleave even/odd into output rows
    out = np.empty((size, size), dtype=np.int32)
    out[0::2, :] = even_v
    out[1::2, :] = odd_v

    buf[:size * size] = out.reshape(-1).astype(np.int16)


def _inverse_dwt_2d(coeffs):
    """3-level inverse 2D DWT in-place."""
    _idwt_2d_level(coeffs[3840:], 8)   # Level 3: 8×8 → 16×16
    _idwt_2d_level(coeffs[3072:], 16)  # Level 2: 16×16 → 32×32
    _idwt_2d_level(coeffs[0:], 32)     # Level 1: 32×32 → 64×64


# ---------------------------------------------------------------
# Component decoding
# ---------------------------------------------------------------

def _decode_component(data, quant):
    """Decode one color component (Y/Cb/Cr) for a 64×64 tile."""
    TILE_PIXELS = RFX_TILE_SIZE * RFX_TILE_SIZE  # 4096

    # 1. RLGR1 entropy decode
    coeffs = rlgr1_decode(data, TILE_PIXELS)

    # 2. Differential decode LL3 (positions 4032..4095)
    for i in range(4033, 4096):
        coeffs[i] += coeffs[i - 1]

    # 3. Dequantize
    _dequantize(coeffs, quant)

    # 4. Inverse DWT (3 levels)
    _inverse_dwt_2d(coeffs)

    return coeffs


def _decode_component_store(data, quant):
    """Decode one component and return (spatial_coeffs, raw_coeffs, sign_context).
    raw_coeffs stores pre-dequant values for progressive upgrade."""
    TILE_PIXELS = RFX_TILE_SIZE * RFX_TILE_SIZE

    coeffs = rlgr1_decode(data, TILE_PIXELS)
    for i in range(4033, 4096):
        coeffs[i] += coeffs[i - 1]

    # Store raw coefficients and sign context before dequantization
    raw = coeffs.copy()
    sign = np.zeros(TILE_PIXELS, dtype=np.int8)
    sign[raw > 0] = 1
    sign[raw < 0] = -1

    _dequantize(coeffs, quant)
    _inverse_dwt_2d(coeffs)
    return coeffs, raw, sign


# ---------------------------------------------------------------
# YCbCr → BGRA color conversion and tile placement
# ---------------------------------------------------------------

def _place_tile(y_coeffs, cb_coeffs, cr_coeffs, x_idx, y_idx, output, out_w, out_h):
    """Convert YCbCr tile to BGRA and write into output buffer."""
    tile_x = x_idx * RFX_TILE_SIZE
    tile_y = y_idx * RFX_TILE_SIZE
    tile_w = min(RFX_TILE_SIZE, out_w - tile_x)
    tile_h = min(RFX_TILE_SIZE, out_h - tile_y)
    if tile_w <= 0 or tile_h <= 0:
        return

    # Build indices into coefficient arrays (RFX_TILE_SIZE-wide rows)
    rows = np.arange(tile_h)
    cols = np.arange(tile_w)
    row_grid, col_grid = np.meshgrid(rows, cols, indexing='ij')
    coeff_idx = (row_grid * RFX_TILE_SIZE + col_grid).ravel()

    y_arr = y_coeffs[coeff_idx].astype(np.int32)
    cb_arr = cb_coeffs[coeff_idx].astype(np.int32)
    cr_arr = cr_coeffs[coeff_idx].astype(np.int32)

    # ICT (YCbCr → RGB) with fixed-point arithmetic matching FreeRDP/grdp
    y_scaled = (y_arr + 4096) << 16
    r = (cr_arr * 91916 + y_scaled) >> 21
    g = (y_scaled - cb_arr * 22527 - cr_arr * 46819) >> 21
    b = (cb_arr * 115992 + y_scaled) >> 21

    np.clip(r, 0, 255, out=r)
    np.clip(g, 0, 255, out=g)
    np.clip(b, 0, 255, out=b)

    # Assemble BGRA tile
    bgra = np.empty((tile_h * tile_w, 4), dtype=np.uint8)
    bgra[:, 0] = b
    bgra[:, 1] = g
    bgra[:, 2] = r
    bgra[:, 3] = 0xFF
    bgra_bytes = bgra.tobytes()

    # Write into output row by row
    stride = tile_w * 4
    for row in range(tile_h):
        out_start = ((tile_y + row) * out_w + tile_x) * 4
        if out_start + stride <= len(output):
            output[out_start:out_start + stride] = bgra_bytes[row * stride:(row + 1) * stride]


# ---------------------------------------------------------------
# SRL + RAW bit-level reader for progressive upgrades
# ---------------------------------------------------------------

class _RawBitReader:
    """Read bits from a byte buffer for SRL/RAW streams."""
    __slots__ = ('_data', '_pos', '_total')

    def __init__(self, data):
        self._data = bytes(data) if not isinstance(data, bytes) else data
        self._pos = 0
        self._total = len(data) * 8

    def remaining(self):
        return self._total - self._pos

    def read_bits(self, n):
        if n <= 0:
            return 0
        val = 0
        for _ in range(n):
            if self._pos < self._total:
                byte_idx = self._pos >> 3
                bit_idx = 7 - (self._pos & 7)
                val = (val << 1) | ((self._data[byte_idx] >> bit_idx) & 1)
                self._pos += 1
            else:
                val <<= 1
        return val

    def read_bit(self):
        if self._pos < self._total:
            byte_idx = self._pos >> 3
            bit_idx = 7 - (self._pos & 7)
            self._pos += 1
            return (self._data[byte_idx] >> bit_idx) & 1
        return 0


def _upgrade_component(srl_data, raw_data, current, sign, quant, prog_quant):
    """Apply progressive upgrade delta to stored coefficients.
    Uses SRL+RAW bit-level decoding per MS-RDPEGFX 3.3.8.4.

    current: stored raw (pre-dequant) coefficients (int16, modified in-place)
    sign: stored sign context (int8, modified in-place)
    quant: regular quant values for dequantization
    prog_quant: progressive quant values (bit positions)
    """
    srl = _RawBitReader(srl_data)
    raw = _RawBitReader(raw_data)

    shifts = quant.subband_shifts()
    bit_positions = prog_quant.subband_shifts()

    offset = 0
    for band_idx, band_size in enumerate(_SUBBAND_SIZES):
        shift = shifts[band_idx]
        bit_pos = bit_positions[band_idx]
        n_bits = max(0, shift - bit_pos)

        for i in range(offset, offset + band_size):
            if sign[i] != 0:
                # Known sign: read magnitude refinement from RAW
                mag = raw.read_bits(n_bits) if n_bits > 0 else 0
                if sign[i] > 0:
                    current[i] += mag
                else:
                    current[i] -= mag
            else:
                # Unknown sign: read significance from SRL
                if srl.read_bit():
                    # Becomes significant: read magnitude from RAW
                    mag = raw.read_bits(n_bits) if n_bits > 0 else 0
                    # Read sign bit from SRL (0=positive, 1=negative)
                    if srl.read_bit():
                        current[i] = -mag
                        sign[i] = -1
                    else:
                        current[i] = mag
                        sign[i] = 1
                # else: stays zero

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
            block_type = struct.unpack_from('<H', data, offset)[0]
            block_len = struct.unpack_from('<I', data, offset + 2)[0]

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
            tile_type = struct.unpack_from('<H', data, offset)[0]
            tile_len = struct.unpack_from('<I', data, offset + 2)[0]

            if tile_len < 6 or offset + tile_len > len(data):
                break

            tile_data = data[offset + 6:offset + tile_len]

            # Log tile position from header (xIdx at offset 3, yIdx at offset 5)
            if len(tile_data) >= 7:
                tx = struct.unpack_from('<H', tile_data, 3)[0]
                ty = struct.unpack_from('<H', tile_data, 5)[0]
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
        x_idx = struct.unpack_from('<H', data, 3)[0]
        y_idx = struct.unpack_from('<H', data, 5)[0]
        y_len = struct.unpack_from('<H', data, 8)[0]
        cb_len = struct.unpack_from('<H', data, 10)[0]
        cr_len = struct.unpack_from('<H', data, 12)[0]

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

        y_pixels, y_raw, y_sign = _decode_component_store(y_data, q_y)
        cb_pixels, cb_raw, cb_sign = _decode_component_store(cb_data, q_cb)
        cr_pixels, cr_raw, cr_sign = _decode_component_store(cr_data, q_cr)

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
        x_idx = struct.unpack_from('<H', data, 3)[0]
        y_idx = struct.unpack_from('<H', data, 5)[0]
        y_len = struct.unpack_from('<H', data, 9)[0]
        cb_len = struct.unpack_from('<H', data, 11)[0]
        cr_len = struct.unpack_from('<H', data, 13)[0]

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

        y_pixels, y_raw, y_sign = _decode_component_store(y_data, q_y)
        cb_pixels, cb_raw, cb_sign = _decode_component_store(cb_data, q_cb)
        cr_pixels, cr_raw, cr_sign = _decode_component_store(cr_data, q_cr)

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
        x_idx = struct.unpack_from('<H', data, 3)[0]
        y_idx = struct.unpack_from('<H', data, 5)[0]
        quality = data[7]
        y_srl_len = struct.unpack_from('<H', data, 8)[0]
        y_raw_len = struct.unpack_from('<H', data, 10)[0]
        cb_srl_len = struct.unpack_from('<H', data, 12)[0]
        cb_raw_len = struct.unpack_from('<H', data, 14)[0]
        cr_srl_len = struct.unpack_from('<H', data, 16)[0]
        cr_raw_len = struct.unpack_from('<H', data, 18)[0]

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
