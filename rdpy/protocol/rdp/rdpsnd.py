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
@summary: RDPSND - Audio Output Virtual Channel Extension (MS-RDPEA).
Handles the "rdpsnd" static virtual channel for server-to-client audio
redirection. Negotiates audio formats with the server and plays back
PCM audio using PyQt6 QAudioSink.

Protocol reference: [MS-RDPEA]
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpea/
"""

import struct
import numpy as np
import time
import threading
from rdpy.core.layer import LayerAutomata
import rdpy.core.log as log


# ---------------------------------------------------------------------------
# Audio ring buffer (QIODevice subclass for QAudioSink pull mode)
# ---------------------------------------------------------------------------

# Crossfade length in sample frames.  At 44100 Hz stereo 16-bit
# (frame = 4 bytes), 128 frames ≈ 2.9 ms — short enough to be
# inaudible as a fade but long enough to eliminate the "click" that
# occurs when Core Audio transitions between audio data and silence.
_FADE_FRAMES = 128


def _build_fade_table(n):
    """Return a contiguous float32 numpy array of n values from 0.0 to 1.0."""
    if n <= 1:
        return np.ones(1, dtype=np.float32)
    return np.linspace(0.0, 1.0, n, dtype=np.float32)


# Per-frame fade curves (contiguous float32)
_FADE_IN = _build_fade_table(_FADE_FRAMES)            # 0→1
_FADE_OUT = np.ascontiguousarray(_FADE_IN[::-1])      # 1→0, contiguous copy

# Pre-interleaved stereo gains (L0,R0,L1,R1,...).
# Computed once here so _apply_fade never calls np.repeat per invocation.
_FADE_IN_STEREO = np.ascontiguousarray(np.repeat(_FADE_IN, 2))
_FADE_OUT_STEREO = np.ascontiguousarray(np.repeat(_FADE_OUT, 2))


def _apply_fade(data, stereo_gains):
    """Apply a pre-interleaved stereo gain array to the leading samples of
    *data* (bytearray or writable memoryview), modified in-place.

    *stereo_gains* must be a contiguous float32 array with shape (2*n_frames,)
    where gains are interleaved as [g_L0, g_R0, g_L1, g_R1, ...].
    Assumes 16-bit signed LE stereo (4 bytes per frame).
    """
    n_samples = min(len(stereo_gains), len(data) // 2)
    # np.frombuffer with count= avoids creating a slice copy of data
    arr = np.frombuffer(data, dtype='<i2', count=n_samples)
    work = arr.astype(np.float32)       # int16 → float32 working copy
    work *= stereo_gains[:n_samples]    # in-place gain application
    np.clip(work, -32768, 32767, out=work)
    if arr.flags.writeable:
        # arr is a writable view into data (bytearray / writable memoryview):
        # write back without tobytes() – no extra allocation or copy.
        arr[:] = work
    else:
        data[:n_samples * 2] = work.astype('<i2').tobytes()


def _amplify_pcm16_stereo(data, gain):
    """Amplify 16-bit signed LE stereo PCM *data* by *gain* (float).

    Returns a new bytes object.  Samples are clamped to [-32768, 32767].
    """
    if gain == 1.0:
        return data
    samples = np.frombuffer(data, dtype='<i2').astype(np.float32)
    samples *= gain                             # in-place multiply
    np.clip(samples, -32768, 32767, out=samples)
    return samples.astype('<i2').tobytes()


class _AudioRingBuffer:
    """Thread-safe ring buffer that acts as a QIODevice for QAudioSink pull mode.

    Includes crossfade logic: when the buffer empties the last chunk is
    faded out, and when audio resumes after an underrun the first chunk
    is faded in.  This prevents the audible "click" that Core Audio
    produces at abrupt silence↔audio transitions.

    Lazily imports PyQt6 so the module can be loaded without Qt installed.
    """

    _cls = None  # will hold the real QIODevice subclass

    def __new__(cls):
        if cls._cls is None:
            from PyQt6.QtCore import QIODevice

            class _Impl(QIODevice):
                _MAX_BUF = 1 << 20  # 1 MB ≈ 6s PCM 44100/stereo/16-bit

                def __init__(self):
                    super().__init__()
                    self._lock = threading.Lock()
                    self._buf = bytearray()
                    self._read_pos = 0      # amortised-O(1) front-deletion
                    self._was_underrun = False

                def _available(self):
                    return len(self._buf) - self._read_pos

                def append(self, data):
                    with self._lock:
                        self._buf.extend(data)
                        # Compact when the dead prefix exceeds half of MAX_BUF,
                        # amortising the cost of memmove across many reads.
                        if self._read_pos >= (self._MAX_BUF >> 1):
                            del self._buf[:self._read_pos]
                            self._read_pos = 0
                        if self._available() > self._MAX_BUF:
                            excess = self._available() - self._MAX_BUF
                            self._read_pos += excess

                def readData(self, maxSize):
                    with self._lock:
                        n = min(maxSize, self._available())
                        if n > 0:
                            end = self._read_pos + n
                            chunk = bytearray(self._buf[self._read_pos:end])
                            self._read_pos = end    # O(1) – no memmove

                            if self._was_underrun:
                                _apply_fade(chunk, _FADE_IN_STEREO)
                                self._was_underrun = False

                            if self._available() == 0:
                                _apply_fade(
                                    # fade applies to the START of data,
                                    # so pass a view of the TAIL
                                    memoryview(chunk)[-_FADE_FRAMES * 4:],
                                    _FADE_OUT_STEREO,
                                )

                            return bytes(chunk)

                        self._was_underrun = True

                    return b''

                def writeData(self, data):
                    return -1  # read-only

                def bytesAvailable(self):
                    with self._lock:
                        return self._available() + super().bytesAvailable()

                def isSequential(self):
                    return True

            cls._cls = _Impl
        return cls._cls()

# Virtual Channel PDU flags (same as drdynvc.py)
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002

# RDPSND PDU types (MS-RDPEA 2.2)
SNDC_CLOSE = 0x01
SNDC_WAVE = 0x02
SNDC_SETVOLUME = 0x03
SNDC_SETPITCH = 0x04
SNDC_WAVECONFIRM = 0x05
SNDC_TRAINING = 0x06
SNDC_FORMATS = 0x07
SNDC_CRYPTKEY = 0x08
SNDC_WAVEENCRYPT = 0x09
SNDC_UDPWAVE = 0x0A
SNDC_UDPWAVELAST = 0x0B
SNDC_QUALITYMODE = 0x0C
SNDC_WAVE2 = 0x0D

# RDPSND version
# gnome-remote-desktop (grd-rdp-dvc-audio-playback.c) requires clientVersion >= 8
# (CHANNEL_VERSION_WIN_8). FreeRDP defines WIN_7=6, WIN_8=8, WIN_MAX=8.
RDPSND_VERSION_MAJOR = 0x08
RDPSND_VERSION_MINOR = 0x00

# Audio format tags (subset of WAVE format tags)
WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_ADPCM = 0x0002
WAVE_FORMAT_ALAW = 0x0006
WAVE_FORMAT_MULAW = 0x0007
WAVE_FORMAT_OPUS = 0x704F

# RDPSND flags
TSSNDCAPS_ALIVE = 0x00000001
TSSNDCAPS_VOLUME = 0x00000002
TSSNDCAPS_PITCH = 0x00000004

# Quality modes
HIGH_QUALITY = 0x0001
MEDIUM_QUALITY = 0x0002
DYNAMIC_QUALITY = 0x0000


class AudioFormat:
    """Represents a WAVE audio format (WAVEFORMATEX)."""

    def __init__(self, tag=0, channels=0, samples_per_sec=0,
                 avg_bytes_per_sec=0, block_align=0, bits_per_sample=0,
                 extra_data=b''):
        self.tag = tag
        self.channels = channels
        self.samples_per_sec = samples_per_sec
        self.avg_bytes_per_sec = avg_bytes_per_sec
        self.block_align = block_align
        self.bits_per_sample = bits_per_sample
        self.extra_data = extra_data

    def pack(self):
        """Pack into WAVEFORMATEX binary + cbSize prefix."""
        hdr = struct.pack('<HHIIH',
                          self.tag,
                          self.channels,
                          self.samples_per_sec,
                          self.avg_bytes_per_sec,
                          self.block_align)
        hdr += struct.pack('<H', self.bits_per_sample)
        hdr += struct.pack('<H', len(self.extra_data))
        hdr += self.extra_data
        return hdr

    @staticmethod
    def unpack(data, offset):
        """Unpack WAVEFORMATEX from data at offset. Returns (AudioFormat, new_offset)."""
        if len(data) - offset < 18:
            return None, offset
        tag, channels, sps, abps, ba = struct.unpack_from('<HHIIH', data, offset)
        bps = struct.unpack_from('<H', data, offset + 14)[0]
        cb_size = struct.unpack_from('<H', data, offset + 16)[0]
        extra = data[offset + 18:offset + 18 + cb_size]
        fmt = AudioFormat(tag, channels, sps, abps, ba, bps, extra)
        return fmt, offset + 18 + cb_size

    def is_pcm(self):
        return self.tag == WAVE_FORMAT_PCM

    def __repr__(self):
        names = {WAVE_FORMAT_PCM: "PCM", WAVE_FORMAT_ADPCM: "ADPCM",
                 WAVE_FORMAT_ALAW: "A-Law", WAVE_FORMAT_MULAW: "μ-Law"}
        name = names.get(self.tag, "0x%04x" % self.tag)
        return "AudioFormat(%s %dHz %dch %dbit)" % (
            name, self.samples_per_sec, self.channels, self.bits_per_sample)


class RdpsndLayer(LayerAutomata):
    """
    RDPSND static virtual channel layer.

    Handles the MS-RDPEA protocol: format negotiation, training, and
    wave data reception. PCM audio is played back via QAudioSink.
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        # VChannel reassembly
        self._vchanBuf = b''
        # Server and client formats
        self._serverFormats = []
        self._clientFormatIndices = []  # indices into _serverFormats that we support
        self._activeFormatIndex = -1
        # Training
        self._trainingTimestamp = 0
        # Wave state
        self._waveTimestamp = 0
        self._pendingWaveData = b''
        self._expectingWaveData = False
        # Audio playback
        self._audioSink = None
        self._audioPushDevice = None      # push-mode write device (Qt main thread)
        self._audioPendingBuf = bytearray()  # pre-buffer (Twisted thread, plain bytes)
        self._audioInitialized = False
        self._audioSinkStarted = False  # True after QAudioSink.start()
        self._audioPrebufBytes = 0      # bytes to accumulate before starting
        # Debug WAV dump (set to a path to write raw PCM to a WAV file)
        self._wavDumpFile = None
        self._wavDumpFmt = None
        self._wavDumpPath = None
        self._audioGain = 1.0
        self._lastPlayTime = 0.0
        # Auto-enable WAV dump from environment variable
        import os
        wav_path = os.environ.get('RDPSND_WAV_DUMP')
        if wav_path:
            self._wavDumpPath = wav_path
        # DVC send callback (set when audio arrives via DVC)
        self._dvcSendCallback = None
        # Qt main-thread invoker: set by RDPClientQt via setQtInvoker().
        # Default calls fn() directly (safe when qreactor is used, or in tests).
        self._qt_invoke = lambda fn: fn()

    def setQtInvoker(self, invoke_fn):
        """Set the function used to post callables to the Qt main thread.

        Must be called before any audio data arrives.  When the Twisted
        reactor runs on a background thread (not via qreactor), pass a
        thread-safe dispatcher here so that QAudioSink operations are
        always executed on the Qt main thread.

        @param invoke_fn: callable(fn) — schedules fn() on the Qt main thread.
        """
        self._qt_invoke = invoke_fn

    def connect(self):
        log.debug("RdpsndLayer.connect()")

    def recv(self, s):
        """Receive data on the rdpsnd static virtual channel with VChannel reassembly."""
        data = s.read()
        log.debug("RDPSND: recv raw %d bytes" % len(data))
        if len(data) < 8:
            log.warning("RDPSND: recv data too short (%d bytes), skipping" % len(data))
            return
        totalLen = struct.unpack_from('<I', data, 0)[0]
        flags = struct.unpack_from('<I', data, 4)[0]
        payload = data[8:]

        if flags & CHANNEL_FLAG_FIRST:
            self._vchanBuf = payload
        else:
            self._vchanBuf += payload

        if flags & CHANNEL_FLAG_LAST:
            if len(self._vchanBuf) >= 1:
                self._processStaticData(bytes(self._vchanBuf))
            self._vchanBuf = b''

    def _processStaticData(self, data):
        """Process data from the static rdpsnd virtual channel.

        When a DVC audio channel (AUDIO_PLAYBACK_DVC) is active, the server
        sends audio on both the DVC channel and the static rdpsnd channel for
        backward compatibility.  Playing both causes audio to play twice, which
        sounds like the video is repeating.  When the DVC channel is active
        (indicated by _dvcSendCallback being set), skip audio playback messages
        on the static channel so only the DVC path produces sound.

        Capability-negotiation messages (FORMATS, TRAINING, QUALITYMODE) are
        always processed regardless of DVC state.
        """
        if self._dvcSendCallback is not None:
            if self._expectingWaveData:
                # Skip the Wave body continuation when DVC is active
                self._expectingWaveData = False
                return
            if len(data) >= 1 and data[0] in (SNDC_WAVE, SNDC_WAVE2, SNDC_CLOSE):
                log.debug("RDPSND: static channel audio/close suppressed (DVC active)")
                return
        self._processData(data)

    def _processData(self, data):
        """Dispatch a reassembled RDPSND PDU."""
        if self._expectingWaveData:
            self._processWaveBody(data)
            return

        if len(data) < 4:
            return

        msgType = data[0]
        # data[1] is bPad
        bodySize = struct.unpack_from('<H', data, 2)[0]
        body = data[4:4 + bodySize]

        _names = {SNDC_FORMATS: "FORMATS", SNDC_TRAINING: "TRAINING",
                  SNDC_WAVE: "WAVE", SNDC_WAVE2: "WAVE2", SNDC_CLOSE: "CLOSE",
                  SNDC_SETVOLUME: "SETVOLUME", SNDC_QUALITYMODE: "QUALITYMODE"}
        log.debug("RDPSND: recv msgType=%s(0x%02x) bodySize=%d" %
                 (_names.get(msgType, "?"), msgType, bodySize))

        if msgType == SNDC_FORMATS:
            self._processServerFormats(body)
        elif msgType == SNDC_TRAINING:
            self._processTraining(body)
        elif msgType == SNDC_WAVE:
            self._processWaveInfo(body)
        elif msgType == SNDC_WAVE2:
            self._processWave2(body)
        elif msgType == SNDC_CLOSE:
            self._processClose()
        elif msgType == SNDC_SETVOLUME:
            pass
        elif msgType == SNDC_QUALITYMODE:
            pass
        else:
            log.debug("RDPSND: unknown msgType=0x%02x len=%d" % (msgType, len(data)))

    # ---------------------------------------------------------------
    # Server Audio Formats and Version (MS-RDPEA 2.2.2.1)
    # ---------------------------------------------------------------

    def _processServerFormats(self, body):
        """Parse Server Audio Formats PDU and respond with client formats."""
        if len(body) < 20:
            log.warning("RDPSND: Server Formats PDU too short")
            return

        dwFlags, dwVolume, dwPitch, wDGramPort = struct.unpack_from('<IIIH', body, 0)
        wNumberOfFormats = struct.unpack_from('<H', body, 14)[0]
        # body[16] = cLastBlockConfirmed (1 byte)
        # body[17] = wVersion (2 bytes)
        wVersion = struct.unpack_from('<H', body, 17)[0]
        # body[19] = bPad (1 byte)

        log.debug("RDPSND: Server Formats version=%d numFormats=%d flags=0x%x" %
                 (wVersion, wNumberOfFormats, dwFlags))

        offset = 20
        self._serverFormats = []
        for i in range(wNumberOfFormats):
            fmt, offset = AudioFormat.unpack(body, offset)
            if fmt is None:
                break
            self._serverFormats.append(fmt)
            log.debug("RDPSND:   server format[%d] = %s" % (i, fmt))

        # Select PCM formats we can play
        self._clientFormatIndices = []
        for i, fmt in enumerate(self._serverFormats):
            if fmt.is_pcm() and fmt.bits_per_sample in (8, 16) and fmt.channels in (1, 2):
                self._clientFormatIndices.append(i)

        if not self._clientFormatIndices:
            log.warning("RDPSND: no supported PCM format found among server formats")
            # Still respond with empty format list so the channel doesn't hang
            self._clientFormatIndices = []

        self._sendClientFormats(wVersion)

    def _sendClientFormats(self, serverVersion):
        """Send Client Audio Formats and Version PDU (MS-RDPEA 2.2.2.2)."""
        version = min(serverVersion, RDPSND_VERSION_MAJOR)

        # Build the list of formats we support
        formatData = b''
        for idx in self._clientFormatIndices:
            formatData += self._serverFormats[idx].pack()

        # Header: dwFlags(4) + dwVolume(4) + dwPitch(4) + wDGramPort(2)
        #        + wNumberOfFormats(2) + cLastBlockConfirmed(1) + wVersion(2) + bPad(1)
        hdr = struct.pack('<IIIH', TSSNDCAPS_ALIVE, 0, 0, 0)
        hdr += struct.pack('<H', len(self._clientFormatIndices))
        hdr += struct.pack('<B', 0)  # cLastBlockConfirmed
        hdr += struct.pack('<H', version)
        hdr += struct.pack('<B', 0)  # bPad
        body = hdr + formatData

        pdu = struct.pack('<BBH', SNDC_FORMATS, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent Client Formats version=%d numFormats=%d" %
                 (version, len(self._clientFormatIndices)))

        # FreeRDP sends Quality Mode PDU right after Client Formats
        # when server version >= 6 (CHANNEL_VERSION_WIN_7).
        if version >= RDPSND_VERSION_MAJOR:
            self._sendQualityMode()

    # ---------------------------------------------------------------
    # Training (MS-RDPEA 2.2.2.3)
    # ---------------------------------------------------------------

    def _processTraining(self, body):
        """Process Training PDU and send Training Confirm."""
        if len(body) < 4:
            return
        wTimeStamp, wPackSize = struct.unpack_from('<HH', body, 0)
        log.debug("RDPSND: Training timestamp=%d packSize=%d" % (wTimeStamp, wPackSize))
        self._sendTrainingConfirm(wTimeStamp, wPackSize)

    def _sendTrainingConfirm(self, timestamp, packSize):
        """Send Training Confirm PDU (MS-RDPEA 2.2.2.4)."""
        body = struct.pack('<HH', timestamp, packSize)
        pdu = struct.pack('<BBH', SNDC_TRAINING, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent Training Confirm")

    # ---------------------------------------------------------------
    # Wave Info / Wave Data (MS-RDPEA 2.2.2.5 / 2.2.2.6)
    # ---------------------------------------------------------------

    def _processWaveInfo(self, body):
        """Process WaveInfo PDU (first part of audio data)."""
        if len(body) < 12:
            log.warning("RDPSND: WaveInfo body too short (%d)" % len(body))
            return

        wTimeStamp = struct.unpack_from('<H', body, 0)[0]
        wFormatNo = struct.unpack_from('<H', body, 2)[0]
        # body[4] = cBlockNo (1 byte)
        cBlockNo = body[4]
        # body[5:8] = bPad (3 bytes)
        # body[8:12] = first 4 bytes of audio data
        initialData = body[8:12]

        self._waveTimestamp = wTimeStamp
        self._waveBlockNo = cBlockNo
        self._pendingWaveData = initialData

        # wFormatNo is an index into the CLIENT's format list
        if wFormatNo < len(self._clientFormatIndices):
            serverIdx = self._clientFormatIndices[wFormatNo]
            if self._activeFormatIndex != serverIdx:
                self._activeFormatIndex = serverIdx
                self._setupAudio(self._serverFormats[serverIdx])
        else:
            log.warning("RDPSND: WaveInfo format index %d out of range (client has %d formats)" %
                        (wFormatNo, len(self._clientFormatIndices)))

        # Next PDU body will be the Wave data body
        self._expectingWaveData = True
        log.debug("RDPSND: WaveInfo ts=%d fmt=%d block=%d initialBytes=%d" %
                  (wTimeStamp, wFormatNo, cBlockNo, len(initialData)))

    def _processWaveBody(self, data):
        """Process Wave PDU body (continuation of WaveInfo)."""
        self._expectingWaveData = False
        # Wave body: first 4 bytes are padding (duplicate of WaveInfo header),
        # then the remaining audio data
        audioData = self._pendingWaveData + data[4:]
        self._pendingWaveData = b''

        log.debug("RDPSND: Wave data %d bytes" % len(audioData))
        self._playAudio(audioData)
        self._sendWaveConfirm(self._waveTimestamp, self._waveBlockNo)

    # ---------------------------------------------------------------
    # Wave2 (MS-RDPEA 2.2.2.7) - SNDC_WAVE2 (version >= 6)
    # ---------------------------------------------------------------

    def _processWave2(self, body):
        """Process Wave2 PDU (single PDU with all audio data)."""
        if len(body) < 12:
            log.warning("RDPSND: Wave2 body too short")
            return

        wTimeStamp = struct.unpack_from('<H', body, 0)[0]
        wFormatNo = struct.unpack_from('<H', body, 2)[0]
        cBlockNo = body[4]
        # body[5:8] = bPad (3 bytes)
        # body[8:12] = dwAudioTimeStamp (4 bytes, not used for playback)
        audioData = body[12:]

        # wFormatNo is an index into the CLIENT's format list
        if wFormatNo < len(self._clientFormatIndices):
            serverIdx = self._clientFormatIndices[wFormatNo]
            if self._activeFormatIndex != serverIdx:
                self._activeFormatIndex = serverIdx
                self._setupAudio(self._serverFormats[serverIdx])
        else:
            log.warning("RDPSND: Wave2 format index %d out of range (client has %d formats)" %
                        (wFormatNo, len(self._clientFormatIndices)))

        log.debug("RDPSND: Wave2 ts=%d fmt=%d block=%d dataLen=%d" %
                  (wTimeStamp, wFormatNo, cBlockNo, len(audioData)))
        self._playAudio(audioData)
        self._sendWaveConfirm(wTimeStamp, cBlockNo)

    # ---------------------------------------------------------------
    # Wave Confirm (MS-RDPEA 2.2.2.8)
    # ---------------------------------------------------------------

    def _sendWaveConfirm(self, timestamp, blockNo):
        """Send Wave Confirm PDU."""
        body = struct.pack('<HBB', timestamp, blockNo, 0)  # wTimeStamp, cConfBlockNo, bPad
        pdu = struct.pack('<BBH', SNDC_WAVECONFIRM, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent WaveConfirm ts=%d block=%d" % (timestamp, blockNo))

    # ---------------------------------------------------------------
    # Close (MS-RDPEA 2.2.2.9)
    # ---------------------------------------------------------------

    def _processClose(self):
        """Handle Close PDU from server."""
        log.debug("RDPSND: server closed audio channel")
        self._stopAudio()
        # Reset format index so the next Wave2 always re-initialises the sink,
        # even if the server uses the same format number for the new stream.
        self._activeFormatIndex = -1

    # ---------------------------------------------------------------
    # Quality Mode (MS-RDPEA 2.2.2.13)
    # ---------------------------------------------------------------

    def _sendQualityMode(self):
        """Send Quality Mode PDU (MS-RDPEA 2.2.2.13).
        FreeRDP sends this after Client Formats when version >= 6.
        DYNAMIC_QUALITY lets the server choose the best mode."""
        body = struct.pack('<HH', DYNAMIC_QUALITY, 0)  # wQualityMode + Reserved
        pdu = struct.pack('<BBH', SNDC_QUALITYMODE, 0, len(body)) + body
        self._send(pdu)
        log.debug("RDPSND: sent Quality Mode (DYNAMIC)")

    # ---------------------------------------------------------------
    # Audio playback via QAudioSink (push mode)
    # ---------------------------------------------------------------

    def _setupAudio(self, fmt):
        """Configure audio for the given AudioFormat.

        Safe to call from the Twisted background thread.  All Qt objects
        are created on the Qt main thread via _qt_invoke.
        """
        if not fmt.is_pcm():
            log.warning("RDPSND: non-PCM format, audio playback not supported: %s" % fmt)
            return

        self._stopAudio()

        self._audioPrebufBytes = fmt.avg_bytes_per_sec // 20  # 50 ms pre-buffer
        self._audioSinkStarted = False
        self._audioInitialized = True
        self._audioGain = 1.0
        self._wavDumpFmt = fmt
        if hasattr(self, '_wavDumpPath') and self._wavDumpPath:
            self.enableWavDump(self._wavDumpPath)
            self._wavDumpPath = None
        log.debug("RDPSND: audio ready (rate=%dHz prebuf=%d bytes): %s" % (
            fmt.samples_per_sec, self._audioPrebufBytes, fmt))

    def _qtStartSink(self, fmt, initial_data):
        """Create QAudioSink in push mode and write initial buffered data.

        Must be called on the Qt main thread.
        """
        log.debug("RDPSND: _qtStartSink called with %d initial bytes" % len(initial_data))
        try:
            from PyQt6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices
        except ImportError:
            log.warning("RDPSND: PyQt6.QtMultimedia not available, audio disabled")
            return

        audioFmt = QAudioFormat()
        audioFmt.setSampleRate(fmt.samples_per_sec)
        audioFmt.setChannelCount(fmt.channels)
        if fmt.bits_per_sample == 8:
            audioFmt.setSampleFormat(QAudioFormat.SampleFormat.UInt8)
        elif fmt.bits_per_sample == 16:
            audioFmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        elif fmt.bits_per_sample == 32:
            audioFmt.setSampleFormat(QAudioFormat.SampleFormat.Int32)
        else:
            log.warning("RDPSND: unsupported bits_per_sample=%d" % fmt.bits_per_sample)
            return

        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            log.warning("RDPSND: no audio output device available")
            return

        sink = QAudioSink(device, audioFmt)
        # Use a 1-second hardware buffer so brief Twisted delivery gaps
        # don't cause underruns.
        sink.setBufferSize(fmt.avg_bytes_per_sec)
        push_dev = sink.start()   # push mode — returns a writable QIODevice
        self._audioSink = sink
        self._audioPushDevice = push_dev
        if push_dev is not None:
            written = push_dev.write(initial_data)
            log.debug("RDPSND: QAudioSink (push) started, wrote %d/%d initial bytes" % (
                written, len(initial_data)))
        else:
            log.warning("RDPSND: QAudioSink.start() returned None push device")

    def _qtPushAudio(self, data):
        """Write PCM data to the push device.  Must be called on Qt main thread."""
        if self._audioPushDevice is not None:
            written = self._audioPushDevice.write(data)
            if written < len(data):
                log.debug("RDPSND: push write short %d/%d bytes" % (written, len(data)))

    def _playAudio(self, data):
        """Receive PCM data from the Twisted thread and forward to QAudioSink.

        Pre-buffers data until enough is accumulated, then starts the sink
        on the Qt main thread.  Subsequent chunks are dispatched via _qt_invoke.
        """
        if not self._audioInitialized:
            return
        now = time.monotonic()
        if self._lastPlayTime > 0:
            gap = now - self._lastPlayTime
            if gap > 0.050:
                bufsize = len(self._audioPendingBuf) if not self._audioSinkStarted else -1
                log.debug("RDPSND: audio gap %.0fms" % (gap * 1000,))
        self._lastPlayTime = now
        if self._audioGain != 1.0:
            data = _amplify_pcm16_stereo(data, self._audioGain)
        if self._wavDumpFile is not None:
            try:
                self._wavDumpFile.writeframesraw(data)
            except Exception:
                pass
        if not self._audioSinkStarted:
            self._audioPendingBuf.extend(data)
            if len(self._audioPendingBuf) >= self._audioPrebufBytes:
                self._audioSinkStarted = True
                initial = bytes(self._audioPendingBuf)
                self._audioPendingBuf = bytearray()
                fmt_snap = self._wavDumpFmt
                self._qt_invoke(lambda: self._qtStartSink(fmt_snap, initial))
                log.debug("RDPSND: posted _qtStartSink with %d bytes" % len(initial))
        else:
            chunk = bytes(data)
            self._qt_invoke(lambda: self._qtPushAudio(chunk))

    def _stopAudio(self):
        """Stop and clean up audio output.

        Qt objects (QAudioSink) are stopped on the Qt main thread.
        All bookkeeping flags are cleared immediately so subsequent
        calls see a clean state.
        """
        self._closeWavDump()
        self._audioInitialized = False
        self._audioSinkStarted = False
        self._audioPendingBuf = bytearray()

        sink = self._audioSink
        self._audioSink      = None
        self._audioPushDevice = None

        def _qt_cleanup():
            if sink is not None:
                try:
                    sink.stop()
                except Exception:
                    pass

        self._qt_invoke(_qt_cleanup)

    def enableWavDump(self, path):
        """Start dumping raw PCM audio to *path* (WAV format).

        Call before connecting, or at any time — the file is opened when
        the first audio format is negotiated.  The dump captures the
        exact bytes received from the server, BEFORE any crossfade or
        buffering.  Play the resulting file locally to verify whether
        scratchiness originates from the server or from client playback.
        """
        import wave
        if self._wavDumpFmt is None:
            # Format not yet known; store path and open later
            self._wavDumpPath = path
            return
        self._closeWavDump()
        fmt = self._wavDumpFmt
        wf = wave.open(path, 'wb')
        wf.setnchannels(fmt.channels)
        wf.setsampwidth(fmt.bits_per_sample // 8)
        wf.setframerate(fmt.samples_per_sec)
        self._wavDumpFile = wf
        log.info("RDPSND: WAV dump started: %s" % path)

    def _closeWavDump(self):
        if self._wavDumpFile is not None:
            try:
                self._wavDumpFile.close()
                log.info("RDPSND: WAV dump closed")
            except Exception:
                pass
            self._wavDumpFile = None

    # ---------------------------------------------------------------
    # Send helper
    # ---------------------------------------------------------------

    def _send(self, data):
        """Send data back to server via static VChannel or DVC."""
        if self._dvcSendCallback is not None:
            # DVC path: send raw rdpsnd PDU (no VChannel header)
            log.debug("RDPSND: _send via DVC callback, len=%d" % len(data))
            self._dvcSendCallback(data)
        elif self._transport is not None:
            # Static VChannel path: wrap with VChannel header
            log.debug("RDPSND: _send via static VChannel, len=%d" % len(data))
            from rdpy.core.type import String
            flags = CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST
            header = struct.pack('<II', len(data), flags)
            self._transport.send(String(header + data))
        else:
            log.warning("RDPSND: _send failed: no transport or DVC callback")
