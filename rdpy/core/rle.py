
# https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpbcgr/b6a3f5c2-0804-4c10-9d25-a321720fd23e


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
                while count > 0 and x < width:
                    output[x + line] = 0 if prevline == 0 else output[prevline + x]
                    count -= 1
                    x += 1

            elif opcode == 1:  # Mix
                while count > 0 and x < width:
                    output[x + line] = mix if prevline == 0 else (output[prevline + x] ^ mix) & 0xff
                    count -= 1
                    x += 1

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
                while count > 0 and x < width:
                    output[x + line] = colour2
                    count -= 1
                    x += 1

            elif opcode == 4:  # Copy
                while count > 0 and x < width:
                    output[x + line] = inp[pos]; pos += 1
                    count -= 1
                    x += 1

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
                while count > 0 and x < width:
                    output[x + line] = 0xff
                    count -= 1
                    x += 1

            elif opcode == 0xe:  # Black
                while count > 0 and x < width:
                    output[x + line] = 0
                    count -= 1
                    x += 1

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
    pixels = [0] * (width * height)

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
                while count > 0 and x < width:
                    pixels[x + line] = 0 if prevline == 0 else pixels[prevline + x]
                    count -= 1
                    x += 1

            elif opcode == 1:  # Mix
                while count > 0 and x < width:
                    pixels[x + line] = mix if prevline == 0 else (pixels[prevline + x] ^ mix) & 0xffff
                    count -= 1
                    x += 1

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
                while count > 0 and x < width:
                    pixels[x + line] = colour2
                    count -= 1
                    x += 1

            elif opcode == 4:  # Copy
                while count > 0 and x < width:
                    pixels[x + line] = read_pixel()
                    count -= 1
                    x += 1

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
                while count > 0 and x < width:
                    pixels[x + line] = 0xffff
                    count -= 1
                    x += 1

            elif opcode == 0xe:  # Black
                while count > 0 and x < width:
                    pixels[x + line] = 0
                    count -= 1
                    x += 1

            else:
                return

    # Write uint16 pixels to output bytearray as little-endian
    j = 0
    for v in pixels:
        output[j] = v & 0xff
        output[j + 1] = (v >> 8) & 0xff
        j += 2


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
                    if prevline == 0:
                        output[3 * x + line] = mix[0]
                        output[3 * x + line + 1] = mix[1]
                        output[3 * x + line + 2] = mix[2]
                    else:
                        output[3 * x + line] = (output[prevline + 3 * x] ^ mix[0]) & 0xff
                        output[3 * x + line + 1] = (output[prevline + 3 * x + 1] ^ mix[1]) & 0xff
                        output[3 * x + line + 2] = (output[prevline + 3 * x + 2] ^ mix[2]) & 0xff
                    insertmix = False
                    count -= 1
                    x += 1
                    continue
                while count > 0 and x < width:
                    if prevline == 0:
                        output[3 * x + line] = 0
                        output[3 * x + line + 1] = 0
                        output[3 * x + line + 2] = 0
                    else:
                        output[3 * x + line] = output[prevline + 3 * x]
                        output[3 * x + line + 1] = output[prevline + 3 * x + 1]
                        output[3 * x + line + 2] = output[prevline + 3 * x + 2]
                    count -= 1
                    x += 1

            elif opcode == 1:  # Mix
                while count > 0 and x < width:
                    if prevline == 0:
                        output[3 * x + line] = mix[0]
                        output[3 * x + line + 1] = mix[1]
                        output[3 * x + line + 2] = mix[2]
                    else:
                        output[3 * x + line] = (output[prevline + 3 * x] ^ mix[0]) & 0xff
                        output[3 * x + line + 1] = (output[prevline + 3 * x + 1] ^ mix[1]) & 0xff
                        output[3 * x + line + 2] = (output[prevline + 3 * x + 2] ^ mix[2]) & 0xff
                    count -= 1
                    x += 1

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
                            output[3 * x + line] = mix[0]
                            output[3 * x + line + 1] = mix[1]
                            output[3 * x + line + 2] = mix[2]
                        else:
                            output[3 * x + line] = (output[prevline + 3 * x] ^ mix[0]) & 0xff
                            output[3 * x + line + 1] = (output[prevline + 3 * x + 1] ^ mix[1]) & 0xff
                            output[3 * x + line + 2] = (output[prevline + 3 * x + 2] ^ mix[2]) & 0xff
                    else:
                        if prevline == 0:
                            output[3 * x + line] = 0
                            output[3 * x + line + 1] = 0
                            output[3 * x + line + 2] = 0
                        else:
                            output[3 * x + line] = output[prevline + 3 * x]
                            output[3 * x + line + 1] = output[prevline + 3 * x + 1]
                            output[3 * x + line + 2] = output[prevline + 3 * x + 2]
                    count -= 1
                    x += 1

            elif opcode == 3:  # Colour
                while count > 0 and x < width:
                    output[3 * x + line] = colour2[0]
                    output[3 * x + line + 1] = colour2[1]
                    output[3 * x + line + 2] = colour2[2]
                    count -= 1
                    x += 1

            elif opcode == 4:  # Copy
                while count > 0 and x < width:
                    output[3 * x + line] = inp[pos]; pos += 1
                    output[3 * x + line + 1] = inp[pos]; pos += 1
                    output[3 * x + line + 2] = inp[pos]; pos += 1
                    count -= 1
                    x += 1

            elif opcode == 8:  # Bicolour
                while count > 0 and x < width:
                    if bicolour:
                        output[3 * x + line] = colour2[0]
                        output[3 * x + line + 1] = colour2[1]
                        output[3 * x + line + 2] = colour2[2]
                        bicolour = False
                    else:
                        output[3 * x + line] = colour1[0]
                        output[3 * x + line + 1] = colour1[1]
                        output[3 * x + line + 2] = colour1[2]
                        bicolour = True
                        count += 1
                    count -= 1
                    x += 1

            elif opcode == 0xd:  # White
                while count > 0 and x < width:
                    output[3 * x + line] = 0xff
                    output[3 * x + line + 1] = 0xff
                    output[3 * x + line + 2] = 0xff
                    count -= 1
                    x += 1

            elif opcode == 0xe:  # Black
                while count > 0 and x < width:
                    output[3 * x + line] = 0
                    output[3 * x + line + 1] = 0
                    output[3 * x + line + 2] = 0
                    count -= 1
                    x += 1

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
               stored in bottom-up scan-line order (first row in buffer
               is the bottom row of the image).
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
    ln = len(input_data)

    lastline = 0
    indexh = 0
    i = 0
    while indexh < height:
        thisline = j + (indexh * width * 4)
        color = 0
        indexw = 0
        i = thisline

        if indexh == 0:
            while indexw < width:
                code, input_data = CVAL(input_data)
                replen = code & 0x0F
                collen = (code >> 4) & 0xF
                revcode = (replen << 4) | collen
                if revcode <= 47 and revcode >= 16:
                    replen = revcode
                    collen = 0
                while collen > 0:
                    color, input_data = CVAL(input_data)
                    output[i] = color & 0xFF
                    i += 4
                    indexw += 1
                    collen -= 1
                while replen > 0:
                    output[i] = color & 0xFF
                    i += 4
                    indexw += 1
                    replen -= 1
        else:
            while indexw < width:
                code, input_data = CVAL(input_data)
                replen = code & 0x0F
                collen = (code >> 4) & 0x0F
                revcode = (replen << 4) | collen
                if revcode <= 47 and revcode >=16:
                    replen = revcode
                    collen = 0
                while collen >0:
                    x, input_data = CVAL(input_data)
                    if x & 1 != 0:
                        x = x >> 1
                        x = x + 1
                        color = -x
                    else:
                        x = x >> 1
                        color = x
                    x = output[indexw * 4 + lastline] + color
                    output[i] = x & 0xFF
                    i += 4
                    indexw += 1
                    collen -= 1
                while replen > 0:
                    x = output[indexw * 4 + lastline] + color
                    output[i] = x & 0xFF
                    i += 4
                    indexw += 1
                    replen -= 1
        indexh += 1
        lastline = thisline

    return ln - len(input_data), input_data


def bitmap_decompress4(input_data, width, height):
    BPP = 4
    size = width * height * BPP
    output = bytearray(size)

    code, input_data = CVAL(input_data)
    # code should be 0x10 for RDP 32bpp compressed bitmap

    total = 1

    process_ln, input_data = process_plane(input_data, width, height, output, 3)
    total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 2)
    total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 1)
    total += process_ln

    process_ln, input_data = process_plane(input_data, width, height, output, 0)
    total += process_ln

    # Force alpha channel (byte[3] of each pixel) to 0xFF for Qt Format_RGB32 compatibility.
    # RDP 32bpp compressed bitmaps use the alpha plane for internal encoding,
    # but Qt Format_RGB32 requires byte[3] = 0xFF for opaque pixels.
    output[3::4] = b'\xff' * (width * height)

    return bytes(output)
