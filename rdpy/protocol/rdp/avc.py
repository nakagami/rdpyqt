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

        Returns list of (left, top, width, height, bgra_bytes) tuples,
        one per region rectangle.
        """
        regions, h264_data = self._parse_avc420_stream(data)
        if h264_data is None or len(h264_data) == 0:
            return []

        frame = self._decode_h264(h264_data)
        if frame is None:
            return []

        # Convert full frame to BGRA
        bgra = self._frame_to_bgra(frame)
        frame_h, frame_w = bgra.shape[:2]

        results = []
        for (left, top, right, bottom, _qp, _qualityMode) in regions:
            rw = right - left
            rh = bottom - top
            if rw <= 0 or rh <= 0:
                continue
            # Clamp to frame boundaries
            r = min(right, frame_w)
            b = min(bottom, frame_h)
            rw = r - left
            rh = b - top
            if rw <= 0 or rh <= 0:
                continue
            region_bgra = np.ascontiguousarray(bgra[top:b, left:r]).tobytes()
            results.append((left, top, rw, rh, region_bgra))

        return results

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

        The wire format has:
          cbAvc420EncodedBitstream1(4) - top 2 bits are LC field
          avc420EncodedBitstream1 (cbAvc420... & 0x3FFFFFFF bytes)
          avc420EncodedBitstream2 (remaining, optional)

        LC values:
          0 = both luma (YUV420) and chroma streams present
          1 = luma stream only
          2 = chroma stream only

        Returns list of (left, top, width, height, bgra_bytes) tuples.
        """
        if len(data) < 4:
            return []

        cbField = struct.unpack_from('<I', data, 0)[0]
        lc = (cbField >> 30) & 0x03
        cbAvc420Stream1 = cbField & 0x3FFFFFFF
        off = 4

        stream1 = data[off:off + cbAvc420Stream1] if cbAvc420Stream1 > 0 else b''
        off += cbAvc420Stream1
        stream2 = data[off:] if off < len(data) else b''

        log.debug("AVC444: LC=%d stream1=%d bytes stream2=%d bytes" %
                  (lc, len(stream1), len(stream2)))

        if lc == 0:
            # Both luma and chroma: decode luma (main picture)
            return self.decode_avc420(stream1, dest_width, dest_height)
        elif lc == 1:
            # Luma only
            return self.decode_avc420(stream1, dest_width, dest_height)
        elif lc == 2:
            # Chroma only (refinement); decode as standalone
            if len(stream2) > 0:
                return self.decode_avc420(stream2, dest_width, dest_height)
            return []
        else:
            log.warning("AVC444: unexpected LC value %d" % lc)
            return []

    # -----------------------------------------------------------------
    # H.264 decoding core
    # -----------------------------------------------------------------

    def _decode_h264(self, h264_data):
        """Decode H.264 bitstream and return the decoded frame (av.VideoFrame) or None."""
        if self._ctx is None:
            return None

        try:
            packet = av.Packet(h264_data)
            frames = self._ctx.decode(packet)
            for frame in frames:
                # If hardware decoded, transfer to system memory
                if frame.format.name in ('videotoolbox_vld', 'vaapi', 'cuda', 'dxva2_vld', 'd3d11'):
                    frame = frame.to_ndarray()
                    # Already numpy, handle below
                    return frame
                return frame
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

    def _frame_to_bgra(self, frame):
        """Convert av.VideoFrame or numpy array to BGRA numpy array (H, W, 4)."""
        if isinstance(frame, np.ndarray):
            # Already a numpy array from hardware decoder transfer
            # Assume YUV420p layout; use av to convert properly
            h, w = frame.shape[:2]
            bgra = np.empty((h, w, 4), dtype=np.uint8)
            bgra[:, :, 0] = frame[:, :, 0] if frame.ndim == 3 else frame
            bgra[:, :, 1] = bgra[:, :, 0]
            bgra[:, :, 2] = bgra[:, :, 0]
            bgra[:, :, 3] = 0xFF
            return bgra

        # av.VideoFrame -> convert to BGR24 then add alpha
        bgr_frame = frame.reformat(format='bgr24')
        bgr = bgr_frame.to_ndarray()
        h, w = bgr.shape[:2]
        bgra = np.empty((h, w, 4), dtype=np.uint8)
        bgra[:, :, :3] = bgr
        bgra[:, :, 3] = 0xFF
        return bgra


def is_available():
    """Check if H.264/AVC decoding is available."""
    return _HAS_AV
