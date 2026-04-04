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
        # SPS/PPS recovery state
        self._sps_nal = b''          # cached SPS NAL (Annex B, with start code)
        self._pps_nal = b''          # cached PPS NAL (Annex B, with start code)
        self._hw_error_count = 0     # consecutive non-IDR decode errors
        self._hw_reset_count = 0     # total hard resets (for software fallback)
        self._prepend_sps_next_idr = False  # inject SPS+PPS before next IDR
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

    def flush(self):
        """Flush decoder buffers while preserving the codec context.

        VideoToolbox (hardware H.264 decoder on macOS) silently returns null
        frames when it is waiting for an IDR to restart the reference chain.
        This is *correct behaviour*, not an error state.  Calling
        flush_buffers() while VideoToolbox is in this wait state has been
        confirmed to push it into a hard AVERROR_UNKNOWN error from which
        only full decoder recreation recovers.

        Therefore we intentionally do NOT call flush_buffers() here.  We
        just reset the null-frame counters so _onAvcNoOutput does not keep
        firing every 1.5 s.  The decoder stays alive with its full codec
        context (SPS, PPS, reference frames) intact, and it will resume
        naturally as soon as the server sends the next IDR — exactly the
        behaviour VideoToolbox expects.

        For software (libavcodec) decoders an IDR also naturally restarts
        the reference chain without needing an explicit flush, so this
        approach is correct for both paths.

        Explicit flush_buffers() is still used in _flush_and_retry_idr(),
        which is called only when an IDR packet fails to decode — a
        fundamentally different situation where the codec state is already
        known to be corrupt.
        """
        self._decode_count = 0
        self._null_count = 0

    def _cache_sps_pps(self, h264_data):
        """Extract and cache SPS/PPS NAL units from an Annex B bitstream.

        Called whenever the incoming bitstream contains NAL types 7 (SPS) or
        8 (PPS).  The cached bytes are used by _hard_reset() to seed a freshly
        created decoder so it can decode the server's subsequent bare IDRs.
        """
        data = bytes(h264_data) if not isinstance(h264_data, bytes) else h264_data
        n = len(data)
        i = 0
        while i < n:
            if i + 4 <= n and data[i:i+4] == b'\x00\x00\x00\x01':
                sc_len = 4
            elif i + 3 <= n and data[i:i+3] == b'\x00\x00\x01':
                sc_len = 3
            else:
                i += 1
                continue
            if i + sc_len >= n:
                break
            nal_type = data[i + sc_len] & 0x1F
            if nal_type not in (7, 8):
                i += sc_len + 1
                continue
            # Find end: next start code or end of data
            j = i + sc_len + 1
            while j < n:
                if (j + 4 <= n and data[j:j+4] == b'\x00\x00\x00\x01') or \
                   (j + 3 <= n and data[j:j+3] == b'\x00\x00\x01'):
                    break
                j += 1
            nal_bytes = data[i:j]
            if nal_type == 7:
                self._sps_nal = nal_bytes
                log.debug("AVC: cached SPS NAL (%d bytes)" % len(nal_bytes))
            else:
                self._pps_nal = nal_bytes
                log.debug("AVC: cached PPS NAL (%d bytes)" % len(nal_bytes))
            i = j

    def _hard_reset(self):
        """Destroy and recreate the decoder after a persistent hardware error.

        VideoToolbox (macOS hardware decoder) can enter an unrecoverable error
        state where every avcodec_send_packet() returns AVERROR_UNKNOWN.
        flush_buffers() has no effect on this state; the only fix is to destroy
        and recreate the codec context.

        After recreation the decoder has no SPS/PPS context.  If we have
        cached SPS/PPS from the original session we set _prepend_sps_next_idr
        so those headers are prepended to the next IDR, giving the fresh
        decoder the codec context it needs to decode the server's bare IDRs.

        After 3 hard resets we permanently fall back to the software decoder
        to avoid an infinite reset loop if hardware acceleration is broken.
        """
        self._hw_reset_count += 1
        log.warning("AVC: hard reset #%d (persistent hardware error, recreating decoder)"
                    % self._hw_reset_count)
        self._ctx = None
        self._hw_error_count = 0
        self._decode_count = 0
        self._null_count = 0

        if self._hw_reset_count >= 3:
            log.warning("AVC: too many hardware resets, falling back to software decoder")
            self._ctx = _open_sw_decoder()
            self._hw_name = None
        else:
            ctx, hw_name = _try_open_hw_decoder()
            if ctx is not None:
                self._ctx = ctx
                self._hw_name = hw_name
            else:
                self._ctx = _open_sw_decoder()
                self._hw_name = None

        if self._sps_nal and self._pps_nal:
            self._prepend_sps_next_idr = True
            log.debug("AVC: will inject cached SPS+PPS (%d+%d bytes) before next IDR"
                      % (len(self._sps_nal), len(self._pps_nal)))

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

        When avcodec_send_packet() raises a generic error (e.g. VideoToolbox
        AVERROR_UNKNOWN on macOS), flush_buffers() has no effect.  After
        _HW_ERROR_THRESHOLD consecutive non-IDR failures we call _hard_reset()
        to destroy and recreate the codec context.  Cached SPS/PPS are then
        injected before the next IDR so the fresh decoder can decode the
        server's bare IDRs without needing a new SPS from the server.
        """
        if self._ctx is None:
            return None

        self._decode_count += 1

        nal_types = self._parse_nal_types(h264_data)
        has_idr = 5 in nal_types

        # Cache SPS/PPS whenever the server includes them (session start + key events)
        if 7 in nal_types or 8 in nal_types:
            self._cache_sps_pps(h264_data)

        # Log NAL types for early frames and every IDR
        if self._decode_count <= 5 or has_idr:
            names = [self._NAL_NAMES.get(t, str(t)) for t in nal_types]
            log.debug("AVC: decode #%d NAL types: %s (h264Len=%d)" %
                     (self._decode_count, ','.join(names), len(h264_data)))

        # After a hard reset the fresh decoder has no SPS context.  Inject our
        # cached SPS+PPS before the next IDR so it can decode the server's bare IDR.
        decode_data = h264_data
        if has_idr and self._prepend_sps_next_idr and self._sps_nal and self._pps_nal:
            decode_data = self._sps_nal + self._pps_nal + bytes(h264_data)
            log.debug("AVC: decode #%d injecting cached SPS+PPS (%d+%d bytes) before IDR"
                      % (self._decode_count, len(self._sps_nal), len(self._pps_nal)))

        packet = None
        try:
            packet = av.Packet(decode_data)
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
            else:
                self._null_count = 0
                self._hw_error_count = 0
                self._prepend_sps_next_idr = False
                if self._decode_count <= 3:
                    log.debug("AVC: decode #%d OK frame=%dx%d fmt=%s h264Len=%d" %
                             (self._decode_count, result.width, result.height,
                              result.format.name, len(h264_data)))
            return result
        except av.error.InvalidDataError as e:
            log.debug("AVC: H.264 decode error (invalid data): %s" % e)
            if has_idr and packet is not None:
                # The decoder state was corrupted by earlier bad frames.
                # An IDR is self-contained, so flush and retry immediately
                # rather than waiting for a future IDR to succeed on its own.
                return self._flush_and_retry_idr(packet)
        except Exception as e:
            log.warning("AVC: H.264 decode error: %s" % e)
            if has_idr and packet is not None:
                # IDR is self-contained; flush and retry may recover.
                return self._flush_and_retry_idr(packet)
            # Non-IDR hardware error: count consecutive failures.  After
            # _HW_ERROR_THRESHOLD failures the hardware decoder is stuck in
            # an unrecoverable error state; recreate it via _hard_reset().
            self._hw_error_count += 1
            if self._hw_error_count >= self._HW_ERROR_THRESHOLD:
                self._hard_reset()

        return None

    _HW_ERROR_THRESHOLD = 5  # consecutive non-IDR errors before hard reset

    def _flush_and_retry_idr(self, packet):
        """Flush decoder buffers and retry an IDR packet after an error.

        Called when decoding an IDR frame raises an exception, which means
        the decoder state is corrupt.  Since IDR frames are self-contained
        (no reference frames needed), flushing and retrying always recovers
        — without waiting for the next server-sent IDR.

        If the retry also fails (e.g. VideoToolbox in hard error state) we
        count that toward _hw_error_count so a subsequent _hard_reset() is
        triggered if the threshold is reached.
        """
        try:
            log.debug("AVC: decode #%d flushing corrupted decoder and retrying IDR" %
                      self._decode_count)
            self._ctx.flush_buffers()
            result = self._receive_frame(packet)
            if result is not None:
                self._null_count = 0
                self._hw_error_count = 0
                self._prepend_sps_next_idr = False
                log.debug("AVC: decode #%d IDR recovery after flush OK %dx%d" %
                          (self._decode_count, result.width, result.height))
            return result
        except Exception as e:
            log.debug("AVC: decode #%d IDR retry also failed: %s" %
                      (self._decode_count, e))
            self._hw_error_count += 1
            if self._hw_error_count >= self._HW_ERROR_THRESHOLD:
                self._hard_reset()
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
