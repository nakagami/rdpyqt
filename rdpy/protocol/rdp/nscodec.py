#
# NSCodec (MS-RDPNSC) decoder.
# Ported from grdp Go implementation.
#

import struct
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
            for _ in range(run_len):
                if out_pos < original_size:
                    output[out_pos] = value
                    out_pos += 1
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


def _clamp(v):
    if v < 0:
        return 0
    if v > 255:
        return 255
    return v


def decode_nscodec(data, width, height):
    """Decode NSCodec (MS-RDPNSC) bitmap data into BGRA pixels.
    Returns bytes of length width*height*4."""
    if len(data) < 20:
        log.warning("NSCodec data too short: %d" % len(data))
        return None

    luma_len = struct.unpack_from('<I', data, 0)[0]
    orange_len = struct.unpack_from('<I', data, 4)[0]
    green_len = struct.unpack_from('<I', data, 8)[0]
    alpha_len = struct.unpack_from('<I', data, 12)[0]
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

    # YCoCg to BGRA conversion (matching FreeRDP/grdp)
    pixels = bytearray(width * height * 4)

    y_row_width = temp_width if chroma_sub > 0 else width
    co_row_width = (temp_width >> 1) if chroma_sub > 0 else width

    for py in range(height):
        y_row_off = py * y_row_width
        if chroma_sub > 0:
            co_row_off = (py >> 1) * co_row_width
            cg_row_off = co_row_off
        else:
            co_row_off = py * co_row_width
            cg_row_off = co_row_off

        co_idx = co_row_off
        cg_idx = cg_row_off

        for px in range(width):
            out_idx = py * width + px
            y_idx = y_row_off + px

            y_val = y_plane[y_idx] if y_idx < len(y_plane) else 0

            # FreeRDP: co_val = (INT16)(INT8)(((INT16)*coplane) << shift)
            co_raw = co_plane[co_idx] if co_idx < len(co_plane) else 0
            cg_raw = cg_plane[cg_idx] if cg_idx < len(cg_plane) else 0

            # Shift and truncate to int8 (signed byte)
            co_val = ((co_raw << shift) & 0xFF)
            if co_val >= 128:
                co_val -= 256
            cg_val = ((cg_raw << shift) & 0xFF)
            if cg_val >= 128:
                cg_val -= 256

            rv = y_val + co_val - cg_val
            gv = y_val + cg_val
            bv = y_val - co_val - cg_val

            off4 = out_idx * 4
            pixels[off4] = _clamp(bv)
            pixels[off4 + 1] = _clamp(gv)
            pixels[off4 + 2] = _clamp(rv)
            if a_plane is not None and out_idx < len(a_plane):
                pixels[off4 + 3] = a_plane[out_idx]
            else:
                pixels[off4 + 3] = 0xFF

            # Advance chroma pointer
            if chroma_sub > 0:
                co_idx += px % 2
                cg_idx += px % 2
            else:
                co_idx += 1
                cg_idx += 1

    return bytes(pixels)
