
# https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/b6a3f5c2-0804-4c10-9d25-a321720fd23e

import array
import sys

# Cache for mix XOR translation tables (keyed by mix value).
# _mix_table_cache: for 8bpp, maps uint8 mix -> 256-byte translate table
_mix_table_cache: dict = {}


def _get_mix_table(mix: int) -> bytes:
    """Return a 256-byte translate table that XORs each byte with mix."""
    if mix not in _mix_table_cache:
        _mix_table_cache[mix] = bytes(i ^ mix for i in range(256))
    return _mix_table_cache[mix]


def CVAL(p):
    return p[0], p[1:]


def _parse_opcode(inp, pos):
    """Parse RLE opcode from input stream, returning (opcode, count, new_pos)."""
    code = inp[pos]
    pos += 1
    opcode = code >> 4

    if opcode in (0xc, 0xd, 0xe):
        opcode -= 6
        count = code & 0xf
        offset = 16
    elif opcode == 0xf:
        opcode = code & 0xf
        if opcode < 9:
            count = inp[pos]
            pos += 1
            count |= inp[pos] << 8
            pos += 1
        else:
            count = 8 if opcode < 0xb else 1
        offset = 0
    else:
        opcode >>= 1
        count = code & 0x1f
        offset = 32

    if offset != 0:
        isfillormix = (opcode == 2) or (opcode == 7)
        if count == 0:
            if isfillormix:
                count = inp[pos] + 1
                pos += 1
            else:
                count = inp[pos] + offset
                pos += 1
        elif isfillormix:
            count <<= 3

    return opcode, count, pos


def _decompress1(output, width, height, input_data):
    """Decompress 1-byte-per-pixel RLE (used for 15bpp)."""
    inp = input_data if isinstance(input_data, (bytes, bytearray)) else bytes(input_data)
    pos = 0
    n = len(inp)

    prevline = 0
    line = 0
    x = width  # start past end triggers first row transition

    mix = 0xff
    colour1 = 0
    colour2 = 0
    insertmix = False
    bicolour = False
    lastopcode = -1
    fom_mask = 0
    mask = 0
    mixmask = 0
    mix_table = _get_mix_table(mix)
    # Pre-allocated zero/fill buffers to avoid per-op bytes() allocation
    _zero_buf = b'\x00' * width
    _white_buf = b'\xff' * width
    _mix_buf = bytes([mix]) * width

    while pos < n:
        fom_mask = 0
        opcode, count, pos = _parse_opcode(inp, pos)

        # Read preliminary data
        if opcode == 0:  # Fill
            if (lastopcode == opcode) and not (x == width and prevline == 0):
                insertmix = True
        elif opcode == 8:  # Bicolour
            colour1 = inp[pos]; pos += 1
            colour2 = inp[pos]; pos += 1
        elif opcode == 3:  # Colour
            colour2 = inp[pos]; pos += 1
        elif opcode == 6 or opcode == 7:  # SetMix/Mix or SetMix/FillOrMix
            mix = inp[pos]; pos += 1
            mix_table = _get_mix_table(mix)
            _mix_buf = bytes([mix]) * width
            opcode -= 5
        elif opcode == 9:  # FillOrMix_1
            mask = 0x03
            opcode = 0x02
            fom_mask = 3
        elif opcode == 0x0a:  # FillOrMix_2
            mask = 0x05
            opcode = 0x02
            fom_mask = 5

        lastopcode = opcode
        mixmask = 0

        # Output body
        while count > 0:
            if x >= width:
                if height <= 0:
                    return
                x = 0
                height -= 1
                prevline = line
                line = height * width

            if opcode == 0:  # Fill
                if insertmix:
                    output[x + line] = mix if prevline == 0 else (output[prevline + x] ^ mix) & 0xff
                    insertmix = False
                    count -= 1
                    x += 1
                    continue
                n_pix = min(count, width - x)
                if prevline == 0:
                    output[line + x : line + x + n_pix] = _zero_buf[:n_pix]
                else:
                    output[line + x : line + x + n_pix] = output[prevline + x : prevline + x + n_pix]
                count -= n_pix
                x += n_pix

            elif opcode == 1:  # Mix
                n_pix = min(count, width - x)
                if prevline == 0:
                    output[line + x : line + x + n_pix] = _mix_buf[:n_pix]
                else:
                    src = bytes(output[prevline + x : prevline + x + n_pix])
                    output[line + x : line + x + n_pix] = src.translate(mix_table)
                count -= n_pix
                x += n_pix

            elif opcode == 2:  # Fill or Mix
                while count > 0 and x < width:
                    mixmask <<= 1
                    if mixmask == 0:
                        if fom_mask != 0:
                            mask = fom_mask
                        else:
                            mask = inp[pos]; pos += 1
                        mixmask = 1
                    if mask & mixmask:
                        output[x + line] = mix if prevline == 0 else (output[prevline + x] ^ mix) & 0xff
                    else:
                        output[x + line] = 0 if prevline == 0 else output[prevline + x]
                    count -= 1
                    x += 1

            elif opcode == 3:  # Colour
                n_pix = min(count, width - x)
                # Reuse single-byte repeat via slice of pre-built buffer
                _colour_buf = bytes([colour2]) * width
                output[line + x : line + x + n_pix] = _colour_buf[:n_pix]
                count -= n_pix
                x += n_pix

            elif opcode == 4:  # Copy
                n_pix = min(count, width - x)
                output[line + x : line + x + n_pix] = inp[pos : pos + n_pix]
                pos += n_pix
                count -= n_pix
                x += n_pix

            elif opcode == 8:  # Bicolour
                while count > 0 and x < width:
                    if bicolour:
                        output[x + line] = colour2
                        bicolour = False
                    else:
                        output[x + line] = colour1
                        bicolour = True
                        count += 1
                    count -= 1
                    x += 1

            elif opcode == 0xd:  # White
                n_pix = min(count, width - x)
                output[line + x : line + x + n_pix] = _white_buf[:n_pix]
                count -= n_pix
                x += n_pix

            elif opcode == 0xe:  # Black
                n_pix = min(count, width - x)
                output[line + x : line + x + n_pix] = _zero_buf[:n_pix]
                count -= n_pix
                x += n_pix

            else:
                return


def _decompress2(output, width, height, input_data):
    """Decompress 2-bytes-per-pixel RLE (used for 16bpp).

    Pixels are stored as little-endian uint16 values to match Qt Format_RGB16.
    """
    inp = input_data if isinstance(input_data, (bytes, bytearray)) else bytes(input_data)
    pos = 0
    n = len(inp)

    prevline = 0
    line = 0
    x = width

    mix = 0xffff
    colour1 = 0
    colour2 = 0
    insertmix = False
    bicolour = False
    lastopcode = -1
    fom_mask = 0
    mask = 0
    mixmask = 0

    # Internal buffer of uint16 pixel values (pixel-indexed)
    pixels = array.array('H', b'\x00' * (width * height * 2))
    # Pre-allocated zero row for Fill/Black ops
    _zero_row = array.array('H', b'\x00' * (width * 2))
    # Pre-allocated single-element arrays for repeated fill patterns
    _mix_arr = array.array('H', [mix])
    _white_arr = array.array('H', [0xffff])

    def read_pixel():
        nonlocal pos
        v = inp[pos] | (inp[pos + 1] << 8)
        pos += 2
        return v

    while pos < n:
        fom_mask = 0
        opcode, count, pos = _parse_opcode(inp, pos)

        if opcode == 0:  # Fill
            if (lastopcode == opcode) and not (x == width and prevline == 0):
                insertmix = True
        elif opcode == 8:  # Bicolour
            colour1 = read_pixel()
            colour2 = read_pixel()
        elif opcode == 3:  # Colour
            colour2 = read_pixel()
        elif opcode == 6 or opcode == 7:  # SetMix/Mix or SetMix/FillOrMix
            mix = read_pixel()
            _mix_arr = array.array('H', [mix])
            opcode -= 5
        elif opcode == 9:  # FillOrMix_1
            mask = 0x03
            opcode = 0x02
            fom_mask = 3
        elif opcode == 0x0a:  # FillOrMix_2
            mask = 0x05
            opcode = 0x02
            fom_mask = 5

        lastopcode = opcode
        mixmask = 0

        while count > 0:
            if x >= width:
                if height <= 0:
                    return
                x = 0
                height -= 1
                prevline = line
                line = height * width

            if opcode == 0:  # Fill
                if insertmix:
                    pixels[x + line] = mix if prevline == 0 else (pixels[prevline + x] ^ mix) & 0xffff
                    insertmix = False
                    count -= 1
                    x += 1
                    continue
                n_pix = min(count, width - x)
                if prevline == 0:
                    pixels[line + x : line + x + n_pix] = _zero_row[:n_pix]
                else:
                    pixels[line + x : line + x + n_pix] = pixels[prevline + x : prevline + x + n_pix]
                count -= n_pix
                x += n_pix

            elif opcode == 1:  # Mix
                n_pix = min(count, width - x)
                if prevline == 0:
                    pixels[line + x : line + x + n_pix] = _mix_arr * n_pix
                else:
                    dst = line + x
                    src_off = prevline + x
                    # Batch XOR using array slicing + list comprehension
                    src_slice = pixels[src_off:src_off + n_pix]
                    pixels[dst:dst + n_pix] = array.array('H',
                        ((v ^ mix) & 0xffff for v in src_slice))
                count -= n_pix
                x += n_pix

            elif opcode == 2:  # Fill or Mix
                while count > 0 and x < width:
                    mixmask <<= 1
                    if mixmask == 0:
                        if fom_mask != 0:
                            mask = fom_mask
                        else:
                            mask = inp[pos]; pos += 1
                        mixmask = 1
                    if mask & mixmask:
                        pixels[x + line] = mix if prevline == 0 else (pixels[prevline + x] ^ mix) & 0xffff
                    else:
                        pixels[x + line] = 0 if prevline == 0 else pixels[prevline + x]
                    count -= 1
                    x += 1

            elif opcode == 3:  # Colour
                n_pix = min(count, width - x)
                pixels[line + x : line + x + n_pix] = array.array('H', [colour2]) * n_pix
                count -= n_pix
                x += n_pix

            elif opcode == 4:  # Copy
                n_pix = min(count, width - x)
                chunk = array.array('H')
                chunk.frombytes(inp[pos : pos + n_pix * 2])
                pixels[line + x : line + x + n_pix] = chunk
                pos += n_pix * 2
                count -= n_pix
                x += n_pix

            elif opcode == 8:  # Bicolour
                while count > 0 and x < width:
                    if bicolour:
                        pixels[x + line] = colour2
                        bicolour = False
                    else:
                        pixels[x + line] = colour1
                        bicolour = True
                        count += 1
                    count -= 1
                    x += 1

            elif opcode == 0xd:  # White
                n_pix = min(count, width - x)
                pixels[line + x : line + x + n_pix] = _white_arr * n_pix
                count -= n_pix
                x += n_pix

            elif opcode == 0xe:  # Black
                n_pix = min(count, width - x)
                pixels[line + x : line + x + n_pix] = _zero_row[:n_pix]
                count -= n_pix
                x += n_pix

            else:
                return

    # Serialize uint16 pixels to output bytearray as little-endian
    if sys.byteorder == 'big':
        pixels.byteswap()
    output[:] = pixels.tobytes()


def _decompress3(output, width, height, input_data):
    """Decompress 3-bytes-per-pixel RLE (used for 24bpp)."""
    inp = input_data if isinstance(input_data, (bytes, bytearray)) else bytes(input_data)
    pos = 0
    n = len(inp)

    prevline = 0
    line = 0
    x = width

    mix = [0xff, 0xff, 0xff]
    colour1 = [0, 0, 0]
    colour2 = [0, 0, 0]
    insertmix = False
    bicolour = False
    lastopcode = -1
    fom_mask = 0
    mask = 0
    mixmask = 0
    # Pre-allocated fill buffers
    _zero_buf3 = b'\x00' * (width * 3)
    _white_buf3 = b'\xff' * (width * 3)
    # Per-channel XOR translate tables for 3bpp Mix
    _mix_tables = [_get_mix_table(mix[0]), _get_mix_table(mix[1]), _get_mix_table(mix[2])]

    def read_pixel():
        nonlocal pos
        v = [inp[pos], inp[pos + 1], inp[pos + 2]]
        pos += 3
        return v

    while pos < n:
        fom_mask = 0
        opcode, count, pos = _parse_opcode(inp, pos)

        if opcode == 0:  # Fill
            if (lastopcode == opcode) and not (x == width and prevline == 0):
                insertmix = True
        elif opcode == 8:  # Bicolour
            colour1 = read_pixel()
            colour2 = read_pixel()
        elif opcode == 3:  # Colour
            colour2 = read_pixel()
        elif opcode == 6 or opcode == 7:  # SetMix/Mix or SetMix/FillOrMix
            mix = read_pixel()
            _mix_tables = [_get_mix_table(mix[0]), _get_mix_table(mix[1]), _get_mix_table(mix[2])]
            opcode -= 5
        elif opcode == 9:  # FillOrMix_1
            mask = 0x03
            opcode = 0x02
            fom_mask = 3
        elif opcode == 0x0a:  # FillOrMix_2
            mask = 0x05
            opcode = 0x02
            fom_mask = 5

        lastopcode = opcode
        mixmask = 0

        while count > 0:
            if x >= width:
                if height <= 0:
                    return
                x = 0
                height -= 1
                prevline = line
                line = height * width * 3

            if opcode == 0:  # Fill
                if insertmix:
                    off = line + 3 * x
                    if prevline == 0:
                        output[off : off + 3] = mix
                    else:
                        poff = prevline + 3 * x
                        output[off] = (output[poff] ^ mix[0]) & 0xff
                        output[off + 1] = (output[poff + 1] ^ mix[1]) & 0xff
                        output[off + 2] = (output[poff + 2] ^ mix[2]) & 0xff
                    insertmix = False
                    count -= 1
                    x += 1
                    continue
                n_pix = min(count, width - x)
                off = line + 3 * x
                if prevline == 0:
                    output[off : off + 3 * n_pix] = _zero_buf3[:3 * n_pix]
                else:
                    poff = prevline + 3 * x
                    output[off : off + 3 * n_pix] = output[poff : poff + 3 * n_pix]
                count -= n_pix
                x += n_pix

            elif opcode == 1:  # Mix
                n_pix = min(count, width - x)
                off = line + 3 * x
                mix_bytes = bytes(mix)
                if prevline == 0:
                    output[off : off + 3 * n_pix] = mix_bytes * n_pix
                else:
                    poff = prevline + 3 * x
                    nbytes = 3 * n_pix
                    src = bytes(output[poff : poff + nbytes])
                    # Per-channel translate via cached XOR tables
                    t0, t1, t2 = _mix_tables
                    ch0 = src[0::3]
                    ch1 = src[1::3]
                    ch2 = src[2::3]
                    r0 = ch0.translate(t0)
                    r1 = ch1.translate(t1)
                    r2 = ch2.translate(t2)
                    # Interleave channels back
                    result = bytearray(nbytes)
                    result[0::3] = r0
                    result[1::3] = r1
                    result[2::3] = r2
                    output[off : off + nbytes] = result
                count -= n_pix
                x += n_pix

            elif opcode == 2:  # Fill or Mix
                while count > 0 and x < width:
                    mixmask <<= 1
                    if mixmask == 0:
                        if fom_mask != 0:
                            mask = fom_mask
                        else:
                            mask = inp[pos]; pos += 1
                        mixmask = 1
                    off = line + 3 * x
                    if mask & mixmask:
                        if prevline == 0:
                            output[off : off + 3] = mix
                        else:
                            poff = prevline + 3 * x
                            output[off]     = (output[poff]     ^ mix[0]) & 0xff
                            output[off + 1] = (output[poff + 1] ^ mix[1]) & 0xff
                            output[off + 2] = (output[poff + 2] ^ mix[2]) & 0xff
                    else:
                        if prevline == 0:
                            output[off : off + 3] = b'\x00\x00\x00'
                        else:
                            poff = prevline + 3 * x
                            output[off : off + 3] = output[poff : poff + 3]
                    count -= 1
                    x += 1

            elif opcode == 3:  # Colour
                n_pix = min(count, width - x)
                off = line + 3 * x
                output[off : off + 3 * n_pix] = bytes(colour2) * n_pix
                count -= n_pix
                x += n_pix

            elif opcode == 4:  # Copy
                n_pix = min(count, width - x)
                off = line + 3 * x
                output[off : off + 3 * n_pix] = inp[pos : pos + 3 * n_pix]
                pos += 3 * n_pix
                count -= n_pix
                x += n_pix

            elif opcode == 8:  # Bicolour
                while count > 0 and x < width:
                    off = line + 3 * x
                    if bicolour:
                        output[off : off + 3] = colour2
                        bicolour = False
                    else:
                        output[off : off + 3] = colour1
                        bicolour = True
                        count += 1
                    count -= 1
                    x += 1

            elif opcode == 0xd:  # White
                n_pix = min(count, width - x)
                off = line + 3 * x
                output[off : off + 3 * n_pix] = _white_buf3[:3 * n_pix]
                count -= n_pix
                x += n_pix

            elif opcode == 0xe:  # Black
                n_pix = min(count, width - x)
                off = line + 3 * x
                output[off : off + 3 * n_pix] = _zero_buf3[:3 * n_pix]
                count -= n_pix
                x += n_pix

            else:
                return


def bitmap_decompress(input_data, width, height, bpp):
    """Decompress RDP Interleaved RLE compressed bitmap data.

    Args:
        input_data: compressed data bytes
        width: bitmap width in pixels
        height: bitmap height in pixels
        bpp: bytes per pixel (1 for 15bpp, 2 for 16bpp, 3 for 24bpp)

    Returns:
        bytes: decompressed bitmap data, width * height * bpp bytes,
               stored in top-down scan-line order (first row in buffer
               is the top row of the image).
    """
    if not input_data or width == 0 or height == 0:
        return bytes(width * height * bpp)
    size = width * height * bpp
    output = bytearray(size)
    if bpp == 1:
        _decompress1(output, width, height, input_data)
    elif bpp == 2:
        _decompress2(output, width, height, input_data)
    elif bpp == 3:
        _decompress3(output, width, height, input_data)
    return bytes(output)


def process_plane(input_data, width, height, output, j):
    inp = input_data if isinstance(input_data, (bytes, bytearray)) else bytes(input_data)
    pos = 0

    lastline = 0
    indexh = 0
    while indexh < height:
        thisline = j + (indexh * width * 4)
        color = 0
        indexw = 0
        i = thisline

        if indexh == 0:
            while indexw < width:
                code = inp[pos]; pos += 1
                replen = code & 0x0F
                collen = (code >> 4) & 0xF
                revcode = (replen << 4) | collen
                if 16 <= revcode <= 47:
                    replen = revcode
                    collen = 0
                while collen > 0:
                    color = inp[pos]; pos += 1
                    output[i] = color & 0xFF
                    i += 4
                    indexw += 1
                    collen -= 1
                if replen > 0:
                    c = color & 0xFF
                    output[i : i + replen * 4 : 4] = bytes([c]) * replen
                    i += replen * 4
                    indexw += replen
        else:
            while indexw < width:
                code = inp[pos]; pos += 1
                replen = code & 0x0F
                collen = (code >> 4) & 0x0F
                revcode = (replen << 4) | collen
                if 16 <= revcode <= 47:
                    replen = revcode
                    collen = 0
                while collen > 0:
                    x_val = inp[pos]; pos += 1
                    if x_val & 1:
                        color = -(x_val >> 1) - 1
                    else:
                        color = x_val >> 1
                    val = output[indexw * 4 + lastline] + color
                    output[i] = val & 0xFF
                    i += 4
                    indexw += 1
                    collen -= 1
                if replen > 0:
                    # Batch read previous-line values and apply delta
                    base = indexw * 4 + lastline
                    step4 = 4
                    vals = bytearray(replen)
                    for k in range(replen):
                        vals[k] = (output[base + k * step4] + color) & 0xFF
                    output[i : i + replen * 4 : 4] = vals
                    i += replen * 4
                    indexw += replen
        indexh += 1
        lastline = thisline

    return pos, input_data[pos:]


def bitmap_decompress4(input_data, width, height):
    BPP = 4
    size = width * height * BPP
    output = bytearray(size)

    code, input_data = CVAL(input_data)
    rle = (code & 0x10) != 0
    no_alpha = (code & 0x20) != 0

    if not rle:
        return bytes(output)

    total = 1

    if no_alpha:
        # No alpha plane in the stream; fill alpha channel with 0xFF.
        output[3::4] = b'\xff' * (width * height)
    else:
        process_ln, input_data = process_plane(input_data, width, height, output, 3)
        total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 2)
    total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 1)
    total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 0)
    total += process_ln

    # Force alpha channel to 0xFF for Qt Format_RGB32 compatibility.
    output[3::4] = b'\xff' * (width * height)

    return bytes(output)
