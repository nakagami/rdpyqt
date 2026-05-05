#
# RLGR1 entropy decoder for RFX Progressive tiles.
# This is the pure-Python fallback; rlgr1_decode.pyx is the Cython-accelerated
# version that shadows this module when compiled.
#

import numpy as np

# RLGR1 constants (matching FreeRDP/grdp)
_LSGR = 3
_KPMAX = 80
_UPGR = 4
_DNGR = 6
_UQGR = 3
_DQGR = 3


class _BitReader:
    __slots__ = ('_data', '_pos', '_total', '_accum', '_accum_bits')

    def __init__(self, data):
        self._data = bytes(data) if not isinstance(data, bytes) else data
        self._pos = 0
        self._total = len(data) * 8
        self._accum = 0       # bit accumulator (up to 64 bits)
        self._accum_bits = 0  # valid bits in accumulator

    def remaining(self):
        return self._total - self._pos + self._accum_bits

    def _refill(self, need):
        data = self._data
        byte_pos = self._pos >> 3
        data_len = len(data)
        accum = self._accum
        accum_bits = self._accum_bits
        pos = self._pos
        while accum_bits < need:
            if byte_pos < data_len:
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
        return (self._accum >> self._accum_bits) & ((1 << n) - 1)

    def count_leading_bits(self, target):
        count = 0
        while self.remaining() > 0:
            if self._accum_bits < 8:
                self._refill(16)
            while self._accum_bits >= 8:
                top8 = (self._accum >> (self._accum_bits - 8)) & 0xFF
                expected = 0xFF if target else 0x00
                if top8 == expected:
                    count += 8
                    self._accum_bits -= 8
                else:
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
        while self.remaining() > 0 and self._accum_bits > 0:
            self._accum_bits -= 1
            bit = (self._accum >> self._accum_bits) & 1
            if bit == target:
                count += 1
            else:
                return count
        return count


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

            if vk2 == 0:
                krp = max(0, krp - 2)
                kr = krp >> _LSGR
            elif vk2 != 1:
                krp = min(_KPMAX, krp + vk2)
                kr = krp >> _LSGR

            kp = max(0, kp - _DNGR)
            k = kp >> _LSGR

            mag = code + 1

            end = min(cnt + run, output_size)
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
                if code & 1:
                    mag = -((code + 1) >> 1)
                else:
                    mag = code >> 1
                if cnt < output_size:
                    output[cnt] = mag
                    cnt += 1

    return output
