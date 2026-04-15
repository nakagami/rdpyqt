#
# NSCodec (MS-RDPNSC) decoder.
# Ported from grdp Go implementation.
#

import struct
import numpy as np
import rdpy.core.log as log


def _nrle_decode(data, original_size):
    """Decompress NRLE (NSCodec Run-Length Encoding) data.
    Matches FreeRDP's nsc_rle_decode exactly."""
    output = bytearray(original_size)
    left = original_size
    in_pos = 0
    out_pos = 0
    in_len = len(data)

    while left > 4 and in_pos < in_len:
        value = data[in_pos]
        in_pos += 1

        if left == 5:
            output[out_pos] = value
            out_pos += 1
            left -= 1
        elif in_pos < in_len and value == data[in_pos]:
            # Run detected
            in_pos += 1  # skip second occurrence
            run_len = 0
            if in_pos < in_len:
                if data[in_pos] < 0xFF:
                    run_len = data[in_pos] + 2
                    in_pos += 1
                else:
                    # Long run: skip 0xFF marker, read uint32 LE
                    in_pos += 1
                    if in_pos + 4 <= in_len:
                        run_len = struct.unpack_from('<I', data, in_pos)[0]
                        in_pos += 4
            if run_len > left:
                run_len = left
            end = min(out_pos + run_len, original_size)
            fill_len = end - out_pos
            if fill_len > 0:
                output[out_pos:end] = bytes((value,)) * fill_len
            out_pos = end
            left -= run_len
        else:
            # Single byte
            output[out_pos] = value
            out_pos += 1
            left -= 1

    # Copy last 4 bytes raw
    if left >= 4 and in_pos + 4 <= in_len:
        output[out_pos:out_pos + 4] = data[in_pos:in_pos + 4]

    return bytes(output)


def _decompress_plane(data, plane_size, original_size):
    """Decompress a single NSCodec plane."""
    if plane_size == 0:
        return bytes([0xFF] * original_size)
    if plane_size >= original_size:
        return bytes(data[:original_size])
    return _nrle_decode(data[:plane_size], original_size)


def decode_nscodec(data, width, height):
    """Decode NSCodec (MS-RDPNSC) bitmap data into BGRA pixels.
    Returns bytes of length width*height*4."""
    if len(data) < 20:
        log.warning("NSCodec data too short: %d" % len(data))
        return None

    luma_len, orange_len, green_len, alpha_len = struct.unpack_from('<IIII', data, 0)
    color_loss_level = data[16]
    chroma_sub = data[17]
    # data[18:20] reserved

    if color_loss_level < 1:
        color_loss_level = 1
    shift = color_loss_level - 1

    remaining = data[20:]

    total_plane_len = luma_len + orange_len + green_len + alpha_len
    if total_plane_len > len(remaining):
        log.warning("NSCodec plane lengths exceed data: %d > %d" %
                    (total_plane_len, len(remaining)))
        return None

    # Compute plane original sizes (matching FreeRDP)
    temp_width = (width + 7) & ~7    # ROUND_UP_TO(width, 8)
    temp_height = (height + 1) & ~1  # ROUND_UP_TO(height, 2)

    if chroma_sub > 0:
        y_orig_size = temp_width * height
        co_orig_size = (temp_width >> 1) * (temp_height >> 1)
        cg_orig_size = co_orig_size
    else:
        y_orig_size = width * height
        co_orig_size = y_orig_size
        cg_orig_size = y_orig_size
    a_orig_size = width * height

    # Decompress planes
    off = 0
    y_plane = _decompress_plane(remaining[off:off + luma_len], luma_len, y_orig_size)
    off += luma_len
    co_plane = _decompress_plane(remaining[off:off + orange_len], orange_len, co_orig_size)
    off += orange_len
    cg_plane = _decompress_plane(remaining[off:off + green_len], green_len, cg_orig_size)
    off += green_len

    a_plane = None
    if alpha_len > 0:
        a_plane = _decompress_plane(remaining[off:off + alpha_len], alpha_len, a_orig_size)

    # YCoCg to BGRA conversion (matching FreeRDP/grdp) — vectorized with numpy
    n_pixels = width * height

    y_np = np.frombuffer(y_plane, dtype=np.uint8)
    co_np = np.frombuffer(co_plane, dtype=np.uint8)
    cg_np = np.frombuffer(cg_plane, dtype=np.uint8)

    y_row_width = temp_width if chroma_sub > 0 else width
    co_row_width = (temp_width >> 1) if chroma_sub > 0 else width

    if chroma_sub > 0:
        # Pre-compute >>1 arrays once, avoid redundant per-pixel recomputation
        py_half = np.arange(height, dtype=np.int32) >> 1
        px_half = np.arange(width, dtype=np.int32) >> 1
        py_arr = np.arange(height, dtype=np.int32)
        px_arr = np.arange(width, dtype=np.int32)

        y_flat_idx = (py_arr[:, np.newaxis] * y_row_width + px_arr[np.newaxis, :]).ravel()
        co_flat_idx = (py_half[:, np.newaxis] * co_row_width + px_half[np.newaxis, :]).ravel()

        np.clip(y_flat_idx, 0, len(y_plane) - 1, out=y_flat_idx)
        np.clip(co_flat_idx, 0, len(co_plane) - 1, out=co_flat_idx)

        y_vals = y_np[y_flat_idx].astype(np.int32)
        co_raw = co_np[co_flat_idx].astype(np.int32)
        cg_raw = cg_np[co_flat_idx].astype(np.int32)
    else:
        y_vals = y_np[:n_pixels].astype(np.int32)
        co_raw = co_np[:n_pixels].astype(np.int32)
        cg_raw = cg_np[:n_pixels].astype(np.int32)

    # YCoCg → RGB conversion in a single vectorized pass.
    # Use int16 to avoid large int32 intermediates while handling the ±128 range.
    co_val = ((co_raw << shift) & 0xFF).astype(np.int16)
    co_val[co_val >= 128] -= 256
    cg_val = ((cg_raw << shift) & 0xFF).astype(np.int16)
    cg_val[cg_val >= 128] -= 256

    y16 = y_vals.astype(np.int16)
    bv = y16 - co_val - cg_val
    gv = y16 + cg_val
    rv = y16 + co_val - cg_val

    # Assemble BGRA output — clip and assign in one pass per channel
    pixels = np.empty((n_pixels, 4), dtype=np.uint8)
    np.clip(bv, 0, 255, out=bv)
    np.clip(gv, 0, 255, out=gv)
    np.clip(rv, 0, 255, out=rv)
    pixels[:, 0] = bv
    pixels[:, 1] = gv
    pixels[:, 2] = rv
    if a_plane is not None:
        a_np = np.frombuffer(a_plane, dtype=np.uint8)
        pixels[:, 3] = a_np[:n_pixels]
    else:
        pixels[:, 3] = 0xFF

    return pixels.tobytes()
