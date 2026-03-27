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

    Hardware decoders (VideoToolbox on macOS, VAAPI on Linux) do not
    perform B-frame reorder buffering, so every packet produces
    immediate output — matching grdp's behaviour.
    """
    hw_configs = []
    if sys.platform == 'darwin':
        hw_configs.append('videotoolbox')
    elif sys.platform.startswith('linux'):
        hw_configs.append('vaapi')

    codec = av.codec.Codec('h264', 'r')
    for hw_name in hw_configs:
        try:
            from av.codec.hwaccel import HWAccel
            accel = HWAccel(hw_name)
            ctx = av.codec.CodecContext.create(codec, hwaccel=accel)
            ctx.open()
            log.debug("AVC: opened hardware decoder: %s" % hw_name)
            return ctx, hw_name
        except Exception as e:
            log.debug("AVC: hardware decoder '%s' unavailable: %s" % (hw_name, e))

    return None, None


def _open_sw_decoder():
    """Open a software H.264 decoder.

    Use FFmpeg's built-in h264 decoder with LOW_DELAY and FAST flags.
    LOW_DELAY minimises frame reordering, and FAST skips some post-
    processing.  thread_count=1 avoids multi-threaded reorder buffering.

    libopenh264 was tried as an alternative (no reorder buffering at
    all) but it returns AVERROR_UNKNOWN on certain High-profile streams
    that RDP servers commonly send, making it unsuitable.
    """
    codec = av.codec.Codec('h264', 'r')
    ctx = av.codec.CodecContext.create(codec)
    ctx.thread_count = 1
    ctx.options = {'flags': '+low_delay', 'flags2': '+fast'}
    ctx.open()
    log.debug("AVC: using software H.264 decoder (thread_count=1, low_delay)")
    return ctx


class AvcDecoder:
    """Decodes H.264/AVC bitstreams from RDPGFX AVC420/AVC444 codec data."""

    def __init__(self):
        if not _HAS_AV:
            raise RuntimeError("PyAV (av) is required for H.264/AVC support")

        self._ctx = None
        self._hw_name = None
        self._decode_count = 0
        self._null_count = 0
        self._init_decoder()

    def _init_decoder(self):
        """Initialize H.264 decoder, trying hardware first."""
        self._decode_count = 0
        self._null_count = 0
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

    _NAL_NAMES = {1: 'P', 5: 'IDR', 6: 'SEI', 7: 'SPS', 8: 'PPS', 9: 'AUD'}

    @staticmethod
    def _parse_nal_types(h264_data):
        """Extract NAL unit type numbers from an Annex-B bitstream."""
        nal_types = []
        i = 0
        end = len(h264_data) - 4
        while i < end:
            if h264_data[i:i+3] == b'\x00\x00\x01':
                nal_types.append(h264_data[i+3] & 0x1F)
                i += 4
            elif h264_data[i:i+4] == b'\x00\x00\x00\x01':
                nal_types.append(h264_data[i+4] & 0x1F)
                i += 5
            else:
                i += 1
        return nal_types

    def _decode_h264(self, h264_data):
        """Decode H.264 bitstream and return the decoded frame (av.VideoFrame) or None.

        The decoder may buffer frames internally (B-frame reordering etc.),
        so a single send_packet can produce multiple frames.  We drain them
        all and return the *last* (most recent) frame — matching grdp's
        ffmpegDecoder.Decode behaviour.

        When the decoder returns no frame for a packet that contains an IDR
        slice (NAL type 5), we flush the decoder's internal buffers and
        retry.  This handles the case where a new SPS triggers internal
        reinitialization that causes the decoder to buffer the IDR instead
        of outputting it immediately — even with LOW_DELAY set.  Flushing
        discards reference frames, but IDR slices are self-contained so the
        retry always succeeds.
        """
        if self._ctx is None:
            return None

        self._decode_count += 1

        nal_types = self._parse_nal_types(h264_data)
        has_idr = 5 in nal_types

        # Log NAL types for early frames and every IDR
        if self._decode_count <= 5 or has_idr:
            names = [self._NAL_NAMES.get(t, str(t)) for t in nal_types]
            log.debug("AVC: decode #%d NAL types: %s (h264Len=%d)" %
                     (self._decode_count, ','.join(names), len(h264_data)))

        try:
            packet = av.Packet(h264_data)
            result = self._receive_frame(packet)

            if result is None and has_idr:
                # New SPS caused the decoder to buffer instead of output.
                # Flush internal state and retry — IDR is self-contained.
                log.debug("AVC: decode #%d null on IDR, flushing and retrying" %
                         self._decode_count)
                self._ctx.flush_buffers()
                result = self._receive_frame(packet)
                if result is not None:
                    log.debug("AVC: decode #%d retry OK %dx%d" %
                             (self._decode_count, result.width, result.height))

            if result is None:
                self._null_count += 1
                log.debug("AVC: decode #%d returned no frame (null_count=%d, h264Len=%d)" %
                         (self._decode_count, self._null_count, len(h264_data)))
            elif self._decode_count <= 3:
                log.debug("AVC: decode #%d OK frame=%dx%d fmt=%s h264Len=%d" %
                         (self._decode_count, result.width, result.height,
                          result.format.name, len(h264_data)))
            return result
        except av.error.InvalidDataError as e:
            log.debug("AVC: H.264 decode error (invalid data): %s" % e)
        except Exception as e:
            # Log but do NOT recreate the decoder or flush buffers.
            # The context (SPS/PPS, reference frames) must survive so
            # subsequent P-frames can still be decoded.  Recreating or
            # flushing wipes all state, causing every following P-frame
            # to fail until the next IDR (which may never come).
            # grdp also keeps the decoder alive after errors.
            log.warning("AVC: H.264 decode error: %s" % e)

        return None

    def _receive_frame(self, packet):
        """Send *packet* to the decoder and return the last decoded frame, or None."""
        result = None
        for frame in self._ctx.decode(packet):
            result = frame
        return result

    def _frame_to_bgra_bytes(self, frame, dest_width, dest_height):
        """Convert av.VideoFrame to BGRA bytes cropped/padded to dest_width x dest_height.

        Matches grdp's convertFrame + cropBGRA: convert directly to BGRA via
        sws_scale (PyAV reformat), then crop/pad to destination dimensions.
        """
        bgra_frame = frame.reformat(format='bgra')
        bgra = bgra_frame.to_ndarray()  # shape (H, W, 4), BGRA order
        src_h, src_w = bgra.shape[:2]

        # Log stride info for first few frames to detect padding issues
        if self._decode_count <= 3:
            plane = bgra_frame.planes[0]
            log.debug("AVC: frame_to_bgra #%d src=%dx%d dest=%dx%d linesize=%d expected=%d" %
                     (self._decode_count, src_w, src_h, dest_width, dest_height,
                      plane.line_size, dest_width * 4))

        if src_w == dest_width and src_h == dest_height:
            raw = bgra.tobytes()
            # Save first frame as PNG for visual diagnostic
            if self._decode_count <= 1:
                self._save_debug_frame(bgra, dest_width, dest_height)
            return raw

        copy_w = min(src_w, dest_width)
        copy_h = min(src_h, dest_height)

        out = np.zeros((dest_height, dest_width, 4), dtype=np.uint8)
        out[:copy_h, :copy_w] = bgra[:copy_h, :copy_w]

        # Save first frame as PNG for visual diagnostic
        if self._decode_count <= 1:
            self._save_debug_frame(out, dest_width, dest_height)
        return out.tobytes()

    def _save_debug_frame(self, bgra_array, width, height):
        """Save a BGRA numpy array as PNG for diagnostic inspection."""
        try:
            from PIL import Image
            # BGRA → RGBA for PIL
            rgba = bgra_array.copy()
            rgba[:, :, 0], rgba[:, :, 2] = bgra_array[:, :, 2].copy(), bgra_array[:, :, 0].copy()
            img = Image.fromarray(rgba[:height, :width], 'RGBA')
            path = '/tmp/rdpyqt_frame_%d.png' % self._decode_count
            img.save(path)
            log.debug("AVC: saved diagnostic frame to %s" % path)
        except Exception as e:
            log.debug("AVC: could not save diagnostic frame: %s" % e)


def is_available():
    """Check if H.264/AVC decoding is available."""
    return _HAS_AV
