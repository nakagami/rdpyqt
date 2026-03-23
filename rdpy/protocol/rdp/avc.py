#
# Copyright (c) 2026 Hajime Nakagami
#
# This file is part of rdpy.
#
# rdpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#

"""
H.264/AVC decoder for RDPGFX AVC420/AVC444/AVC444v2 codecs.
Uses PyAV (FFmpeg) with optional hardware acceleration
(VideoToolbox on macOS, VAAPI on Linux).

Wire formats per MS-RDPEGFX:
  AVC420 (2.2.4.4): numRegionRects(4) + regionRects(8*N) +
      quantQualityVals(2*N) + avc420EncodedBitstream(remaining)
  AVC444 (2.2.4.5): cbAvc420EncodedBitstream1(4, LC in top 2 bits) +
      avc420EncodedBitstream1 + avc420EncodedBitstream2(optional)
  AVC444v2 (2.2.4.6): same structure as AVC444, different chroma handling
"""

import struct
import sys
import numpy as np
import rdpy.core.log as log

try:
    import av
    _HAS_AV = True
except ImportError:
    _HAS_AV = False
    log.warning("PyAV (av) not installed; H.264/AVC codec support disabled")


# H.264 Annex B start code for NAL unit framing
_ANNEX_B_START = b'\x00\x00\x00\x01'


def _try_open_hw_decoder():
    """Try to open a hardware-accelerated H.264 decoder.
    Returns (CodecContext, hw_name) or (None, None).
    """
    hw_configs = []
    if sys.platform == 'darwin':
        hw_configs.append('videotoolbox')
    elif sys.platform.startswith('linux'):
        hw_configs.append('vaapi')

    codec = av.codec.Codec('h264', 'r')
    for hw_name in hw_configs:
        try:
            ctx = av.codec.CodecContext.create(codec)
            for hw_config in ctx.codec.hardware_configs:
                if hw_config.device_type.name == hw_name:
                    device = av.codec.HWDeviceContext.create(hw_config.device_type)
                    ctx.hw_device_ctx = device
                    ctx.open()
                    log.info("AVC: opened hardware decoder: %s" % hw_name)
                    return ctx, hw_name
        except Exception as e:
            log.debug("AVC: hardware decoder '%s' unavailable: %s" % (hw_name, e))

    return None, None


def _open_sw_decoder():
    """Open a software H.264 decoder."""
    codec = av.codec.Codec('h264', 'r')
    ctx = av.codec.CodecContext.create(codec)
    ctx.open()
    log.info("AVC: using software H.264 decoder")
    return ctx


class AvcDecoder:
    """Decodes H.264/AVC bitstreams from RDPGFX AVC420/AVC444 codec data."""

    def __init__(self):
        if not _HAS_AV:
            raise RuntimeError("PyAV (av) is required for H.264/AVC support")

        self._ctx = None
        self._hw_name = None
        self._init_decoder()

    def _init_decoder(self):
        """Initialize H.264 decoder, trying hardware first."""
        ctx, hw_name = _try_open_hw_decoder()
        if ctx is not None:
            self._ctx = ctx
            self._hw_name = hw_name
        else:
            self._ctx = _open_sw_decoder()
            self._hw_name = None

    @property
    def is_hardware(self):
        return self._hw_name is not None

    def close(self):
        if self._ctx is not None:
            self._ctx.close()
            self._ctx = None

    # -----------------------------------------------------------------
    # AVC420 bitmap stream (MS-RDPEGFX 2.2.4.4)
    # -----------------------------------------------------------------

    def decode_avc420(self, data, dest_width, dest_height):
        """Decode AVC420 bitmap stream.

        Returns BGRA bytes (dest_width * dest_height * 4) or None.
        """
        _regions, h264_data = self._parse_avc420_stream(data)
        if h264_data is None or len(h264_data) == 0:
            return None

        frame = self._decode_h264(h264_data)
        if frame is None:
            return None

        return self._frame_to_bgra_bytes(frame, dest_width, dest_height)

    def _parse_avc420_stream(self, data):
        """Parse AVC420 bitmap stream per MS-RDPEGFX 2.2.4.4.

        Returns (regions, h264_data) where regions is a list of
        (left, top, right, bottom, qp, qualityMode) tuples.
        """
        if len(data) < 4:
            return [], None

        numRegionRects = struct.unpack_from('<I', data, 0)[0]
        off = 4

        # Region rects: each 8 bytes (left(2) + top(2) + right(2) + bottom(2))
        rects_size = numRegionRects * 8
        if len(data) < off + rects_size:
            log.warning("AVC420: insufficient data for %d region rects" % numRegionRects)
            return [], None

        rects = []
        for i in range(numRegionRects):
            left = struct.unpack_from('<H', data, off)[0]
            top = struct.unpack_from('<H', data, off + 2)[0]
            right = struct.unpack_from('<H', data, off + 4)[0]
            bottom = struct.unpack_from('<H', data, off + 6)[0]
            rects.append((left, top, right, bottom))
            off += 8

        # Quant quality vals: each 2 bytes (qpVal(1) + qualityMode flags(1))
        qvals_size = numRegionRects * 2
        if len(data) < off + qvals_size:
            log.warning("AVC420: insufficient data for quant quality values")
            return [], None

        regions = []
        for i in range(numRegionRects):
            qp = data[off]
            qualityMode = data[off + 1]
            left, top, right, bottom = rects[i]
            regions.append((left, top, right, bottom, qp, qualityMode))
            off += 2

        h264_data = data[off:]
        log.debug("AVC420: %d regions, %d bytes H.264 data" %
                  (numRegionRects, len(h264_data)))
        return regions, bytes(h264_data)

    # -----------------------------------------------------------------
    # AVC444 / AVC444v2 bitmap stream (MS-RDPEGFX 2.2.4.5 / 2.2.4.6)
    # -----------------------------------------------------------------

    def decode_avc444(self, data, dest_width, dest_height):
        """Decode AVC444/AVC444v2 bitmap stream.

        LC values:
          0 = both luma (YUV420) and chroma streams present
          1 = luma stream only
          2 = chroma stream only (skipped)

        Returns BGRA bytes (dest_width * dest_height * 4) or None.
        """
        if len(data) < 4:
            return None

        cbField = struct.unpack_from('<I', data, 0)[0]
        lc = (cbField >> 30) & 0x03
        cbAvc420Stream1 = cbField & 0x3FFFFFFF
        rest = data[4:]

        log.debug("AVC444: LC=%d cbStream1=%d restLen=%d" %
                  (lc, cbAvc420Stream1, len(rest)))

        if lc == 0:
            if cbAvc420Stream1 > len(rest):
                log.warning("AVC444: stream1 size %d exceeds data %d" %
                            (cbAvc420Stream1, len(rest)))
                return None
            return self.decode_avc420(rest[:cbAvc420Stream1], dest_width, dest_height)
        elif lc == 1:
            # Main stream only; use cbAvc420Stream1 if valid, otherwise all of rest
            stream_data = rest
            if cbAvc420Stream1 > 0 and cbAvc420Stream1 <= len(rest):
                stream_data = rest[:cbAvc420Stream1]
            return self.decode_avc420(stream_data, dest_width, dest_height)
        elif lc == 2:
            # Chroma-only refinement stream — skip
            log.debug("AVC444: LC=2 chroma-only, skipping")
            return None
        else:
            log.warning("AVC444: unexpected LC value %d" % lc)
            return None

    # -----------------------------------------------------------------
    # H.264 decoding core
    # -----------------------------------------------------------------

    def _decode_h264(self, h264_data):
        """Decode H.264 bitstream and return the decoded frame (av.VideoFrame) or None.

        The decoder may buffer frames internally (B-frame reordering etc.),
        so a single send_packet can produce multiple frames.  We drain them
        all and return the *last* (most recent) frame — matching grdp's
        ffmpegDecoder.Decode behaviour.
        """
        if self._ctx is None:
            return None

        try:
            packet = av.Packet(h264_data)
            result = None
            for frame in self._ctx.decode(packet):
                # If hardware decoded, transfer to system memory
                if frame.format.name in ('videotoolbox_vld', 'vaapi', 'cuda', 'dxva2_vld', 'd3d11'):
                    frame = frame.to_cpu()
                result = frame
            return result
        except av.error.InvalidDataError as e:
            log.debug("AVC: H.264 decode error (invalid data): %s" % e)
        except Exception as e:
            log.warning("AVC: H.264 decode error: %s" % e)
            # Try reinitializing decoder on unexpected errors
            try:
                self._init_decoder()
            except Exception:
                pass

        return None

    def _frame_to_bgra_bytes(self, frame, dest_width, dest_height):
        """Convert av.VideoFrame to BGRA bytes cropped/padded to dest_width x dest_height.

        Matches grdp's convertFrame + cropBGRA: convert directly to BGRA via
        sws_scale (PyAV reformat), then crop/pad to destination dimensions.
        """
        bgra_frame = frame.reformat(format='bgra')
        bgra = bgra_frame.to_ndarray()  # shape (H, W, 4), BGRA order
        src_h, src_w = bgra.shape[:2]

        if src_w == dest_width and src_h == dest_height:
            return bgra.tobytes()

        copy_w = min(src_w, dest_width)
        copy_h = min(src_h, dest_height)

        out = np.zeros((dest_height, dest_width, 4), dtype=np.uint8)
        out[:copy_h, :copy_w] = bgra[:copy_h, :copy_w]

        return out.tobytes()


def is_available():
    """Check if H.264/AVC decoding is available."""
    return _HAS_AV
