# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True

# https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/b6a3f5c2-0804-4c10-9d25-a321720fd23e

import sys

from libc.string cimport memcpy, memset


cdef inline (int, int, int) _parse_opcode(const unsigned char *inp, int pos) noexcept:
    cdef int code, opcode, count, offset
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
        if (opcode == 2) or (opcode == 7):
            if count == 0:
                count = inp[pos] + 1
                pos += 1
            else:
                count <<= 3
        else:
            if count == 0:
                count = inp[pos] + offset
                pos += 1

    return opcode, count, pos


cdef void _decompress1(unsigned char *output, int width, int height,
                        const unsigned char *inp, int n) noexcept:
    cdef int pos = 0
    cdef int prevline = 0
    cdef int line = 0
    cdef int x = width
    cdef int mix = 0xff
    cdef int colour1 = 0
    cdef int colour2 = 0
    cdef bint insertmix = False
    cdef bint bicolour = False
    cdef int lastopcode = -1
    cdef int fom_mask = 0
    cdef int mask = 0
    cdef int mixmask = 0
    cdef int opcode, count, n_pix, i

    while pos < n:
        fom_mask = 0
        opcode, count, pos = _parse_opcode(inp, pos)

        if opcode == 0:
            if (lastopcode == opcode) and not (x == width and prevline == 0):
                insertmix = True
        elif opcode == 8:
            colour1 = inp[pos]; pos += 1
            colour2 = inp[pos]; pos += 1
        elif opcode == 3:
            colour2 = inp[pos]; pos += 1
        elif opcode == 6 or opcode == 7:
            mix = inp[pos]; pos += 1
            opcode -= 5
        elif opcode == 9:
            mask = 0x03
            opcode = 0x02
            fom_mask = 3
        elif opcode == 0x0a:
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
                    if prevline == 0:
                        output[x + line] = <unsigned char>mix
                    else:
                        output[x + line] = <unsigned char>((output[prevline + x] ^ mix) & 0xff)
                    insertmix = False
                    count -= 1
                    x += 1
                    continue
                n_pix = min(count, width - x)
                if prevline == 0:
                    memset(&output[line + x], 0, n_pix)
                else:
                    memcpy(&output[line + x], &output[prevline + x], n_pix)
                count -= n_pix
                x += n_pix

            elif opcode == 1:  # Mix
                n_pix = min(count, width - x)
                if prevline == 0:
                    memset(&output[line + x], <unsigned char>mix, n_pix)
                else:
                    for i in range(n_pix):
                        output[line + x + i] = <unsigned char>((output[prevline + x + i] ^ mix) & 0xff)
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
                        if prevline == 0:
                            output[x + line] = <unsigned char>mix
                        else:
                            output[x + line] = <unsigned char>((output[prevline + x] ^ mix) & 0xff)
                    else:
                        if prevline == 0:
                            output[x + line] = 0
                        else:
                            output[x + line] = output[prevline + x]
                    count -= 1
                    x += 1

            elif opcode == 3:  # Colour
                n_pix = min(count, width - x)
                memset(&output[line + x], <unsigned char>colour2, n_pix)
                count -= n_pix
                x += n_pix

            elif opcode == 4:  # Copy
                n_pix = min(count, width - x)
                memcpy(&output[line + x], &inp[pos], n_pix)
                pos += n_pix
                count -= n_pix
                x += n_pix

            elif opcode == 8:  # Bicolour
                while count > 0 and x < width:
                    if bicolour:
                        output[x + line] = <unsigned char>colour2
                        bicolour = False
                    else:
                        output[x + line] = <unsigned char>colour1
                        bicolour = True
                        count += 1
                    count -= 1
                    x += 1

            elif opcode == 0xd:  # White
                n_pix = min(count, width - x)
                memset(&output[line + x], 0xff, n_pix)
                count -= n_pix
                x += n_pix

            elif opcode == 0xe:  # Black
                n_pix = min(count, width - x)
                memset(&output[line + x], 0, n_pix)
                count -= n_pix
                x += n_pix

            else:
                return


cdef void _decompress2(unsigned char *output, int width, int height,
                        const unsigned char *inp, int n) noexcept:
    cdef int pos = 0
    cdef int prevline = 0
    cdef int line = 0
    cdef int x = width
    cdef unsigned short mix = 0xffff
    cdef unsigned short colour1 = 0
    cdef unsigned short colour2 = 0
    cdef bint insertmix = False
    cdef bint bicolour = False
    cdef int lastopcode = -1
    cdef int fom_mask = 0
    cdef int mask = 0
    cdef int mixmask = 0
    cdef int opcode, count, n_pix, i
    cdef unsigned short v
    cdef int total_pixels = width * height

    cdef unsigned short *pixels = <unsigned short *>output

    while pos < n:
        fom_mask = 0
        opcode, count, pos = _parse_opcode(inp, pos)

        if opcode == 0:
            if (lastopcode == opcode) and not (x == width and prevline == 0):
                insertmix = True
        elif opcode == 8:
            colour1 = inp[pos] | (inp[pos + 1] << 8); pos += 2
            colour2 = inp[pos] | (inp[pos + 1] << 8); pos += 2
        elif opcode == 3:
            colour2 = inp[pos] | (inp[pos + 1] << 8); pos += 2
        elif opcode == 6 or opcode == 7:
            mix = inp[pos] | (inp[pos + 1] << 8); pos += 2
            opcode -= 5
        elif opcode == 9:
            mask = 0x03
            opcode = 0x02
            fom_mask = 3
        elif opcode == 0x0a:
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
                    if prevline == 0:
                        pixels[x + line] = mix
                    else:
                        pixels[x + line] = (pixels[prevline + x] ^ mix) & 0xffff
                    insertmix = False
                    count -= 1
                    x += 1
                    continue
                n_pix = min(count, width - x)
                if prevline == 0:
                    memset(&pixels[line + x], 0, n_pix * 2)
                else:
                    memcpy(&pixels[line + x], &pixels[prevline + x], n_pix * 2)
                count -= n_pix
                x += n_pix

            elif opcode == 1:  # Mix
                n_pix = min(count, width - x)
                if prevline == 0:
                    for i in range(n_pix):
                        pixels[line + x + i] = mix
                else:
                    for i in range(n_pix):
                        pixels[line + x + i] = (pixels[prevline + x + i] ^ mix) & 0xffff
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
                        if prevline == 0:
                            pixels[x + line] = mix
                        else:
                            pixels[x + line] = (pixels[prevline + x] ^ mix) & 0xffff
                    else:
                        if prevline == 0:
                            pixels[x + line] = 0
                        else:
                            pixels[x + line] = pixels[prevline + x]
                    count -= 1
                    x += 1

            elif opcode == 3:  # Colour
                n_pix = min(count, width - x)
                for i in range(n_pix):
                    pixels[line + x + i] = colour2
                count -= n_pix
                x += n_pix

            elif opcode == 4:  # Copy
                n_pix = min(count, width - x)
                memcpy(&pixels[line + x], &inp[pos], n_pix * 2)
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
                for i in range(n_pix):
                    pixels[line + x + i] = 0xffff
                count -= n_pix
                x += n_pix

            elif opcode == 0xe:  # Black
                n_pix = min(count, width - x)
                memset(&pixels[line + x], 0, n_pix * 2)
                count -= n_pix
                x += n_pix

            else:
                return

    # byteswap for big-endian
    if sys.byteorder == 'big':
        for i in range(total_pixels):
            v = pixels[i]
            pixels[i] = ((v >> 8) | (v << 8)) & 0xffff


cdef void _decompress3(unsigned char *output, int width, int height,
                        const unsigned char *inp, int n) noexcept:
    cdef int pos = 0
    cdef int prevline = 0
    cdef int line = 0
    cdef int x = width
    cdef unsigned char mix0 = 0xff, mix1 = 0xff, mix2 = 0xff
    cdef unsigned char c1_0 = 0, c1_1 = 0, c1_2 = 0
    cdef unsigned char c2_0 = 0, c2_1 = 0, c2_2 = 0
    cdef bint insertmix = False
    cdef bint bicolour = False
    cdef int lastopcode = -1
    cdef int fom_mask = 0
    cdef int mask = 0
    cdef int mixmask = 0
    cdef int opcode, count, n_pix, i, off, poff

    while pos < n:
        fom_mask = 0
        opcode, count, pos = _parse_opcode(inp, pos)

        if opcode == 0:
            if (lastopcode == opcode) and not (x == width and prevline == 0):
                insertmix = True
        elif opcode == 8:
            c1_0 = inp[pos]; c1_1 = inp[pos+1]; c1_2 = inp[pos+2]; pos += 3
            c2_0 = inp[pos]; c2_1 = inp[pos+1]; c2_2 = inp[pos+2]; pos += 3
        elif opcode == 3:
            c2_0 = inp[pos]; c2_1 = inp[pos+1]; c2_2 = inp[pos+2]; pos += 3
        elif opcode == 6 or opcode == 7:
            mix0 = inp[pos]; mix1 = inp[pos+1]; mix2 = inp[pos+2]; pos += 3
            opcode -= 5
        elif opcode == 9:
            mask = 0x03
            opcode = 0x02
            fom_mask = 3
        elif opcode == 0x0a:
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
                        output[off] = mix0
                        output[off+1] = mix1
                        output[off+2] = mix2
                    else:
                        poff = prevline + 3 * x
                        output[off]   = (output[poff]   ^ mix0) & 0xff
                        output[off+1] = (output[poff+1] ^ mix1) & 0xff
                        output[off+2] = (output[poff+2] ^ mix2) & 0xff
                    insertmix = False
                    count -= 1
                    x += 1
                    continue
                n_pix = min(count, width - x)
                off = line + 3 * x
                if prevline == 0:
                    memset(&output[off], 0, 3 * n_pix)
                else:
                    poff = prevline + 3 * x
                    memcpy(&output[off], &output[poff], 3 * n_pix)
                count -= n_pix
                x += n_pix

            elif opcode == 1:  # Mix
                n_pix = min(count, width - x)
                off = line + 3 * x
                if prevline == 0:
                    for i in range(n_pix):
                        output[off + i*3]     = mix0
                        output[off + i*3 + 1] = mix1
                        output[off + i*3 + 2] = mix2
                else:
                    poff = prevline + 3 * x
                    for i in range(n_pix):
                        output[off + i*3]     = (output[poff + i*3]     ^ mix0) & 0xff
                        output[off + i*3 + 1] = (output[poff + i*3 + 1] ^ mix1) & 0xff
                        output[off + i*3 + 2] = (output[poff + i*3 + 2] ^ mix2) & 0xff
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
                            output[off] = mix0
                            output[off+1] = mix1
                            output[off+2] = mix2
                        else:
                            poff = prevline + 3 * x
                            output[off]   = (output[poff]   ^ mix0) & 0xff
                            output[off+1] = (output[poff+1] ^ mix1) & 0xff
                            output[off+2] = (output[poff+2] ^ mix2) & 0xff
                    else:
                        if prevline == 0:
                            output[off] = 0
                            output[off+1] = 0
                            output[off+2] = 0
                        else:
                            poff = prevline + 3 * x
                            output[off]   = output[poff]
                            output[off+1] = output[poff+1]
                            output[off+2] = output[poff+2]
                    count -= 1
                    x += 1

            elif opcode == 3:  # Colour
                n_pix = min(count, width - x)
                off = line + 3 * x
                for i in range(n_pix):
                    output[off + i*3]     = c2_0
                    output[off + i*3 + 1] = c2_1
                    output[off + i*3 + 2] = c2_2
                count -= n_pix
                x += n_pix

            elif opcode == 4:  # Copy
                n_pix = min(count, width - x)
                off = line + 3 * x
                memcpy(&output[off], &inp[pos], 3 * n_pix)
                pos += 3 * n_pix
                count -= n_pix
                x += n_pix

            elif opcode == 8:  # Bicolour
                while count > 0 and x < width:
                    off = line + 3 * x
                    if bicolour:
                        output[off] = c2_0
                        output[off+1] = c2_1
                        output[off+2] = c2_2
                        bicolour = False
                    else:
                        output[off] = c1_0
                        output[off+1] = c1_1
                        output[off+2] = c1_2
                        bicolour = True
                        count += 1
                    count -= 1
                    x += 1

            elif opcode == 0xd:  # White
                n_pix = min(count, width - x)
                off = line + 3 * x
                memset(&output[off], 0xff, 3 * n_pix)
                count -= n_pix
                x += n_pix

            elif opcode == 0xe:  # Black
                n_pix = min(count, width - x)
                off = line + 3 * x
                memset(&output[off], 0, 3 * n_pix)
                count -= n_pix
                x += n_pix

            else:
                return


def bitmap_decompress(input_data, int width, int height, int bpp):
    """Decompress RDP Interleaved RLE compressed bitmap data.

    Args:
        input_data: compressed data bytes
        width: bitmap width in pixels
        height: bitmap height in pixels
        bpp: bytes per pixel (1 for 15bpp, 2 for 16bpp, 3 for 24bpp)

    Returns:
        bytes: decompressed bitmap data, width * height * bpp bytes,
               stored in bottom-up scan-line order (first row in buffer
               is the bottom row of the image).
    """
    if not input_data or width == 0 or height == 0:
        return bytes(width * height * bpp)

    cdef int size = width * height * bpp
    cdef bytearray output = bytearray(size)
    cdef const unsigned char[::1] inp_view
    cdef unsigned char *out_ptr = <unsigned char *><char *>output

    inp_bytes = bytes(input_data) if not isinstance(input_data, (bytes, bytearray)) else input_data
    inp_view = inp_bytes
    cdef const unsigned char *inp_ptr = &inp_view[0]
    cdef int inp_len = len(inp_bytes)

    if bpp == 1:
        _decompress1(out_ptr, width, height, inp_ptr, inp_len)
    elif bpp == 2:
        _decompress2(out_ptr, width, height, inp_ptr, inp_len)
    elif bpp == 3:
        _decompress3(out_ptr, width, height, inp_ptr, inp_len)

    return bytes(output)


cdef int _process_plane(const unsigned char *inp, int inp_len, int width, int height,
                         unsigned char *output, int j) noexcept:
    cdef int pos = 0
    cdef int lastline = 0
    cdef int indexh = 0
    cdef int indexw, i, code, replen, collen, revcode, color, x_val, val, k

    while indexh < height:
        i = j + (indexh * width * 4)
        color = 0
        indexw = 0

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
                    output[i] = <unsigned char>(color & 0xFF)
                    i += 4
                    indexw += 1
                    collen -= 1
                if replen > 0:
                    for k in range(replen):
                        output[i + k * 4] = <unsigned char>(color & 0xFF)
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
                    output[i] = <unsigned char>(val & 0xFF)
                    i += 4
                    indexw += 1
                    collen -= 1
                if replen > 0:
                    for k in range(replen):
                        val = output[(indexw + k) * 4 + lastline] + color
                        output[i + k * 4] = <unsigned char>(val & 0xFF)
                    i += replen * 4
                    indexw += replen
        lastline = j + (indexh * width * 4)
        indexh += 1

    return pos


def process_plane(input_data, int width, int height, output, int j):
    cdef const unsigned char[::1] inp_view
    cdef unsigned char[::1] out_view

    inp_bytes = bytes(input_data) if not isinstance(input_data, (bytes, bytearray)) else input_data
    inp_view = inp_bytes
    out_view = output

    cdef int pos = _process_plane(&inp_view[0], len(inp_bytes), width, height, &out_view[0], j)
    return pos, input_data[pos:]


def bitmap_decompress4(input_data, int width, int height):
    cdef int BPP = 4
    cdef int size = width * height * BPP
    cdef bytearray output = bytearray(size)
    cdef int code, total, process_ln, i
    cdef bint rle_flag, no_alpha

    if len(input_data) == 0:
        return bytes(output)

    code = input_data[0]
    input_data = input_data[1:]
    rle_flag = (code & 0x10) != 0
    no_alpha = (code & 0x20) != 0

    if not rle_flag:
        return bytes(output)

    total = 1

    if no_alpha:
        for i in range(width * height):
            output[3 + i * 4] = 0xff
    else:
        process_ln, input_data = process_plane(input_data, width, height, output, 3)
        total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 2)
    total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 1)
    total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 0)
    total += process_ln

    # Force alpha to 0xFF
    for i in range(width * height):
        output[3 + i * 4] = 0xff

    return bytes(output)
