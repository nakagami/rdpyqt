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
@summary: Dynamic Virtual Channel Extension (MS-RDPEDYC) with RDPGFX support.
Handles DRDYNVC protocol over the "drdynvc" static virtual channel, and
implements the Graphics Pipeline Extension (MS-RDPEGFX) for GNOME Remote
Desktop and other modern RDP servers.
"""

import struct
import time
import numpy as np
from rdpy.core.layer import LayerAutomata
import rdpy.core.log as log
from rdpy.protocol.rdp.rfx_progressive import RfxProgressiveDecoder
from rdpy.protocol.rdp.rfx import RfxDecoder
from rdpy.protocol.rdp.zgfx import ZgfxDecompressor
from rdpy.protocol.rdp import avc as avc_module


RDPGFX_CHANNEL_NAME = "Microsoft::Windows::RDS::Graphics"

# RDPGFX command IDs (MS-RDPEGFX 2.2, FreeRDP rdpgfx.h)
RDPGFX_CMDID_WIRETOSURFACE_1 = 0x0001
RDPGFX_CMDID_WIRETOSURFACE_2 = 0x0002
RDPGFX_CMDID_DELETEENCODINGCONTEXT = 0x0003
RDPGFX_CMDID_SOLIDFILL = 0x0004
RDPGFX_CMDID_SURFACETOSURFACE = 0x0005
RDPGFX_CMDID_SURFACETOCACHE = 0x0006
RDPGFX_CMDID_CACHETOSURFACE = 0x0007
RDPGFX_CMDID_EVICTCACHEENTRY = 0x0008
RDPGFX_CMDID_CREATESURFACE = 0x0009
RDPGFX_CMDID_DELETESURFACE = 0x000A
RDPGFX_CMDID_STARTFRAME = 0x000B
RDPGFX_CMDID_ENDFRAME = 0x000C
RDPGFX_CMDID_FRAMEACKNOWLEDGE = 0x000D
RDPGFX_CMDID_RESETGRAPHICS = 0x000E
RDPGFX_CMDID_MAPSURFACETOOUTPUT = 0x000F
RDPGFX_CMDID_CACHEIMPORTOFFER = 0x0010
RDPGFX_CMDID_CACHEIMPORTREPLY = 0x0011
RDPGFX_CMDID_CAPSADVERTISE = 0x0012
RDPGFX_CMDID_CAPSCONFIRM = 0x0013
RDPGFX_CMDID_MAPSURFACETOWINDOW = 0x0015
RDPGFX_CMDID_MAPSURFACETOSCALEDOUTPUT = 0x0017

_RDPGFX_CMDID_NAMES = {
    0x0001: "WIRETOSURFACE_1", 0x0002: "WIRETOSURFACE_2",
    0x0003: "DELETEENCODINGCTX", 0x0004: "SOLIDFILL",
    0x0005: "SURFACETOSURFACE", 0x0006: "SURFACETOCACHE",
    0x0007: "CACHETOSURFACE", 0x0008: "EVICTCACHEENTRY",
    0x0009: "CREATESURFACE", 0x000A: "DELETESURFACE",
    0x000B: "STARTFRAME", 0x000C: "ENDFRAME",
    0x000D: "FRAMEACKNOWLEDGE", 0x000E: "RESETGRAPHICS",
    0x000F: "MAPSURFACETOOUTPUT", 0x0010: "CACHEIMPORTOFFER",
    0x0011: "CACHEIMPORTREPLY", 0x0012: "CAPSADVERTISE",
    0x0013: "CAPSCONFIRM", 0x0015: "MAPSURFACETOWINDOW",
    0x0017: "MAPSURFACETOSCALEDOUTPUT",
}

# RDPGFX codec IDs (MS-RDPEGFX, FreeRDP rdpgfx.h)
RDPGFX_CODECID_UNCOMPRESSED = 0x0000
RDPGFX_CODECID_CAVIDEO = 0x0003
RDPGFX_CODECID_CLEARCODEC = 0x0008
RDPGFX_CODECID_CAPROGRESSIVE = 0x0009
RDPGFX_CODECID_PLANAR = 0x000A
RDPGFX_CODECID_AVC420 = 0x000B
RDPGFX_CODECID_ALPHA = 0x000C
RDPGFX_CODECID_AVC444 = 0x000E
RDPGFX_CODECID_AVC444v2 = 0x000F

# Pixel formats
GFX_PIXEL_FORMAT_XRGB_8888 = 0x20
GFX_PIXEL_FORMAT_ARGB_8888 = 0x21

# RDPGFX capability versions (MS-RDPEGFX 2.2.3)
RDPGFX_CAPVERSION_8 = 0x00080004
RDPGFX_CAPVERSION_81 = 0x00080105
RDPGFX_CAPVERSION_10 = 0x000A0002
RDPGFX_CAPVERSION_101 = 0x000A0100
RDPGFX_CAPVERSION_102 = 0x000A0200
RDPGFX_CAPVERSION_104 = 0x000A0400

# RDPGFX capability flags
RDPGFX_CAPS_FLAG_THINCLIENT = 0x00000001
RDPGFX_CAPS_FLAG_SMALL_CACHE = 0x00000002
RDPGFX_CAPS_FLAG_AVC_DISABLED = 0x00000020

# Frame drop: skip heavy codec decode when frames arrive faster than decode.
# If time since last END_FRAME exceeds this interval (seconds), skip heavy
# CaVideo/Progressive decode but still process EndFrame for ACKs.


# VChannel flags
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002
CHANNEL_FLAG_SHOW_PROTOCOL = 0x00000010


class DrdynvcCmd(object):
    CREATE = 0x01
    DATA_FIRST = 0x02
    DATA = 0x03
    CLOSE = 0x04
    CAPABILITY = 0x05
    DATA_FIRST_COMPRESSED = 0x06
    DATA_COMPRESSED = 0x07
    SOFT_SYNC_REQUEST = 0x08
    SOFT_SYNC_RESPONSE = 0x09


class DrdynvcLayer(LayerAutomata):
    """
    @summary: Dynamic Virtual Channel Extension layer with RDPGFX support.
    Handles DRDYNVC protocol over the "drdynvc" static virtual channel,
    including full RDPGFX Graphics Pipeline for rendering.
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        self._version = 2
        self._dynamicChannels = {}     # channelId -> channelName
        self._channelCbId = {}         # channelId -> cbId (for sending data)
        # VChannel reassembly buffer
        self._vchanBuf = b''
        # DVC DATA_FIRST reassembly: channelId -> (totalLen, buffer)
        self._dvcReassembly = {}
        # RDPGFX state
        self._gfxChannelId = None      # DVC channelId for Graphics pipeline
        self._gfxConfirmed = False      # True after CAPS_CONFIRM received
        self._surfaces = {}            # surfaceId -> (width, height, pixelFormat)
        self._surfaceData = {}         # surfaceId -> bytearray (BGRA pixel buffer)
        self._surfaceOutputMap = {}    # surfaceId -> (outputOriginX, outputOriginY)
        self._currentFrameId = 0
        self._totalFramesDecoded = 0
        self._lastFrameTime = 0.0      # monotonic time of last END_FRAME (for stats)
        self._resetWidth = 0
        self._resetHeight = 0
        # RFX Progressive decoder
        self._rfxDecoder = RfxProgressiveDecoder()
        # Non-progressive RFX decoder (for CaVideo codec 0x0003)
        self._rfxTileDecoder = RfxDecoder()
        # ZGFX decompressor (stateful, persistent history across segments)
        self._zgfx = ZgfxDecompressor()
        # H.264/AVC decoder (lazy init)
        self._avcDecoder = None
        # Bitmap cache: cacheSlot -> (width, height, bytearray BGRA data)
        self._gfxCache = {}
        # ClearCodec VBAR caches (persistent across decode calls, matching grdp)
        self._ccVBarStorage = [None] * 32768       # VBAR cache: list of (pixels_bytes, count)
        self._ccShortVBarStorage = [None] * 16384  # Short VBAR cache
        self._ccVBarCursor = 0
        self._ccShortVBarCursor = 0
        # Negotiated GFX version (set by CAPS_CONFIRM)
        self._gfxVersion = 0
        # Callbacks
        self._gfxCallback = None       # bitmap delivery callback
        self._capsConfirmCallback = None  # called when CAPS_CONFIRM received
        # RDPSND DVC routing
        self._rdpsndLayer = None       # set via setRdpsndLayer() for DVC audio routing
        self._rdpsndDvcChannelIds = set()  # DVC channelIds for rdpsnd audio
        self._rdpsndDvcPrimaryId = None    # primary channel for sending responses

    def setGfxCallback(self, callback):
        """Set callback for RDPGFX bitmap delivery: callback(x, y, w, h, bpp, data)"""
        self._gfxCallback = callback

    def setCapsConfirmCallback(self, callback):
        """Set callback for RDPGFX CAPS_CONFIRM notification."""
        self._capsConfirmCallback = callback

    def setRdpsndLayer(self, rdpsndLayer):
        """Set the RDPSND layer for DVC audio routing."""
        self._rdpsndLayer = rdpsndLayer

    def connect(self):
        log.debug("DrdynvcLayer.connect()")

    def recv(self, s):
        """Receive data on the drdynvc static virtual channel with VChannel reassembly."""
        data = s.read()
        if len(data) < 9:
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
                self._processData(self._vchanBuf)
            self._vchanBuf = b''

    def _processData(self, data):
        header = data[0]
        cmd = (header >> 4) & 0x0f
        sp = (header >> 2) & 0x03
        cbId = header & 0x03

        if cmd == DrdynvcCmd.CAPABILITY:
            self._processCapabilities(data)
        elif cmd == DrdynvcCmd.CREATE:
            self._processCreateRequest(data, cbId)
        elif cmd == DrdynvcCmd.DATA_FIRST:
            self._processDataFirst(data, cbId, sp)
        elif cmd == DrdynvcCmd.DATA:
            self._processDataPdu(data, cbId)
        elif cmd == DrdynvcCmd.CLOSE:
            self._processClose(data, cbId)
        elif cmd == DrdynvcCmd.SOFT_SYNC_REQUEST:
            self._processSoftSyncRequest(data)
        else:
            log.debug("DrdynvcLayer: unknown cmd=%d" % cmd)

    def _readChannelId(self, data, offset, cbId):
        if cbId == 0:
            return data[offset], offset + 1
        elif cbId == 1:
            return struct.unpack_from('<H', data, offset)[0], offset + 2
        elif cbId == 2:
            return struct.unpack_from('<I', data, offset)[0], offset + 4
        return 0, offset

    def _readLength(self, data, offset, sp):
        """Read variable-length field based on Sp bits (for DATA_FIRST totalLength)."""
        if sp == 0:
            return data[offset], offset + 1
        elif sp == 1:
            return struct.unpack_from('<H', data, offset)[0], offset + 2
        elif sp == 2:
            return struct.unpack_from('<I', data, offset)[0], offset + 4
        return 0, offset

    def _processCapabilities(self, data):
        if len(data) < 4:
            log.warning("DrdynvcLayer: capabilities PDU too short")
            return
        version = struct.unpack_from('<H', data, 2)[0]
        log.debug("DrdynvcLayer: server DRDYNVC version=%d" % version)

        # Clear all DVC state from previous connection (reconnection case)
        if self._dynamicChannels:
            log.debug("DrdynvcLayer: clearing stale DVC state (%d channels)" % len(self._dynamicChannels))
        self._dynamicChannels.clear()
        self._channelCbId.clear()
        self._dvcReassembly.clear()
        self._gfxChannelId = None
        self._gfxConfirmed = False
        self._rdpsndDvcChannelIds.clear()
        self._rdpsndDvcPrimaryId = None
        # Reset ZGFX decompressor (history is connection-scoped)
        self._zgfx = ZgfxDecompressor()

        # Accept up to version 3 (Soft-Sync)
        self._version = min(version, 3)

        response = struct.pack('<BBH', 0x50, 0x00, self._version)
        self._send(response)
        log.debug("DrdynvcLayer: sent capabilities response version=%d" % self._version)

    def _processSoftSyncRequest(self, data):
        """Handle DVC Soft-Sync Request (version 3). Parse and respond."""
        # data[0] = header (already parsed), data[1] = pad
        if len(data) < 10:
            log.warning("DrdynvcLayer: Soft-Sync Request too short (%d bytes)" % len(data))
            return
        offset = 1  # skip header byte
        pad = data[offset]; offset += 1
        length = struct.unpack_from('<I', data, offset)[0]; offset += 4
        flags = struct.unpack_from('<H', data, offset)[0]; offset += 2
        numTunnels = struct.unpack_from('<H', data, offset)[0]; offset += 2
        log.debug("DrdynvcLayer: Soft-Sync Request flags=0x%04x numTunnels=%d" % (flags, numTunnels))

        tunnelTypes = []
        for i in range(numTunnels):
            if offset + 4 > len(data):
                break
            tt = struct.unpack_from('<I', data, offset)[0]; offset += 4
            tunnelTypes.append(tt)
            log.debug("DrdynvcLayer: Soft-Sync tunnel[%d] type=0x%08x" % (i, tt))

        SOFT_SYNC_CHANNEL_LIST_PRESENT = 0x0002
        if flags & SOFT_SYNC_CHANNEL_LIST_PRESENT:
            if offset + 2 <= len(data):
                numChannels = struct.unpack_from('<H', data, offset)[0]; offset += 2
                for i in range(numChannels):
                    if offset + 8 > len(data):
                        break
                    dvcChId = struct.unpack_from('<I', data, offset)[0]; offset += 4
                    chTunnelType = struct.unpack_from('<I', data, offset)[0]; offset += 4
                    log.debug("DrdynvcLayer: Soft-Sync channel[%d] dvcId=%d tunnelType=0x%08x" %
                             (i, dvcChId, chTunnelType))

        # Send Soft-Sync Response
        SOFT_SYNC_TCP_FLUSHED = 0x0001
        resp = bytearray()
        resp.append(0x90)  # header: cmd=SOFT_SYNC_RESPONSE(0x09) << 4
        resp.append(0x00)  # pad
        respPayload = struct.pack('<H', SOFT_SYNC_TCP_FLUSHED)  # flags
        respPayload += struct.pack('<H', numTunnels)  # numberOfTunnels
        for tt in tunnelTypes:
            respPayload += struct.pack('<I', tt)
        resp += struct.pack('<I', len(respPayload))  # length
        resp += respPayload
        self._send(bytes(resp))
        log.debug("DrdynvcLayer: sent Soft-Sync Response")

    def _processCreateRequest(self, data, cbId):
        channelId, offset = self._readChannelId(data, 1, cbId)
        channelName = data[offset:].split(b'\x00')[0].decode('utf-8', errors='replace')
        log.debug("DrdynvcLayer: CREATE channelId=%d name=%s" % (channelId, channelName))

        # Only accept channels we actually implement; reject others so the
        # server doesn't expect functionality we cannot provide (matching grdp).
        _SUPPORTED_CHANNELS = {
            RDPGFX_CHANNEL_NAME,
            "rdpsnd", "AUDIO_PLAYBACK_DVC",
            # AUDIO_PLAYBACK_LOSSY_DVC (AAC/Opus) is not supported; reject it so
            # gnome-remote-desktop falls back to lossless AUDIO_PLAYBACK_DVC (PCM).
        }
        accepted = channelName in _SUPPORTED_CHANNELS

        self._dynamicChannels[channelId] = channelName
        self._channelCbId[channelId] = cbId

        header = (DrdynvcCmd.CREATE << 4) | cbId
        response = bytearray([header])
        if cbId == 0:
            response += struct.pack('<B', channelId)
        elif cbId == 1:
            response += struct.pack('<H', channelId)
        elif cbId == 2:
            response += struct.pack('<I', channelId)

        if accepted:
            response += struct.pack('<I', 0)  # CHANNEL_RC_OK
            self._send(bytes(response))
            log.debug("DrdynvcLayer: sent create response (OK) for channelId=%d" % channelId)
        else:
            response += struct.pack('<i', -1)  # reject
            self._send(bytes(response))
            log.debug("DrdynvcLayer: rejected channel %s (id=%d) — no handler" % (channelName, channelId))
            return

        if channelName == RDPGFX_CHANNEL_NAME:
            self._gfxChannelId = channelId
            if not self._gfxConfirmed:
                self._sendRdpgfxCapsAdvertise(channelId, cbId)
            else:
                log.debug("RDPGFX: channel re-created without close, skipping duplicate CAPS_ADVERTISE")
        elif channelName in ("rdpsnd", "AUDIO_PLAYBACK_DVC"):
            self._rdpsndDvcChannelIds.add(channelId)
            log.debug("DrdynvcLayer: RDPSND DVC channel mapped to channelId=%d (%s)" % (channelId, channelName))
            if self._rdpsndLayer is not None and self._rdpsndDvcPrimaryId is None:
                # Use the first audio channel as the primary for sending responses
                self._rdpsndDvcPrimaryId = channelId
                def dvcSendRdpsnd(data, _chId=channelId, _cbId=cbId):
                    log.debug("DrdynvcLayer: dvcSendRdpsnd channelId=%d cbId=%d dataLen=%d" %
                              (_chId, _cbId, len(data)))
                    header = (DrdynvcCmd.DATA << 4) | _cbId
                    pdu = bytearray([header])
                    if _cbId == 0:
                        pdu += struct.pack('<B', _chId)
                    elif _cbId == 1:
                        pdu += struct.pack('<H', _chId)
                    elif _cbId == 2:
                        pdu += struct.pack('<I', _chId)
                    pdu += data
                    self._send(bytes(pdu))
                self._rdpsndLayer._dvcSendCallback = dvcSendRdpsnd

    def _processDataFirst(self, data, cbId, sp):
        """Handle DVC DATA_FIRST: first fragment of a large DVC message."""
        channelId, offset = self._readChannelId(data, 1, cbId)
        totalLen, offset = self._readLength(data, offset, sp)
        fragment = data[offset:]
        channelName = self._dynamicChannels.get(channelId, "unknown")
        log.debug("DrdynvcLayer: DATA_FIRST channelId=%d (%s) totalLen=%d fragLen=%d" %
                  (channelId, channelName, totalLen, len(fragment)))
        if len(fragment) >= totalLen:
            # Complete message in first fragment — dispatch immediately
            # (matching grdp behaviour; avoids losing the message if a
            # subsequent DATA_FIRST overwrites the reassembly entry and
            # desynchronising the ZGFX history buffer).
            self._dispatchDvcData(channelId, bytes(fragment[:totalLen]))
        else:
            self._dvcReassembly[channelId] = (totalLen, bytearray(fragment))

    def _processDataPdu(self, data, cbId):
        """Handle DVC DATA: either a complete message or continuation of DATA_FIRST."""
        channelId, offset = self._readChannelId(data, 1, cbId)
        fragment = data[offset:]

        # Check if this is a continuation of a DATA_FIRST sequence
        if channelId in self._dvcReassembly:
            totalLen, buf = self._dvcReassembly[channelId]
            buf += fragment
            if len(buf) >= totalLen:
                del self._dvcReassembly[channelId]
                self._dispatchDvcData(channelId, bytes(buf[:totalLen]))
            else:
                self._dvcReassembly[channelId] = (totalLen, buf)
            return

        # Complete single-segment message
        self._dispatchDvcData(channelId, fragment)

    def _dispatchDvcData(self, channelId, payload):
        """Dispatch reassembled DVC channel data to the appropriate handler."""
        channelName = self._dynamicChannels.get(channelId, "unknown")
        if channelId == self._gfxChannelId and len(payload) >= 8:
            self._processRdpgfxStream(payload)
        elif channelId in self._rdpsndDvcChannelIds and self._rdpsndLayer is not None:
            log.debug("DrdynvcLayer: routing DVC rdpsnd data len=%d channelId=%d to RdpsndLayer" %
                     (len(payload), channelId))
            self._rdpsndLayer._processData(payload)
        else:
            log.debug("DrdynvcLayer: data on channelId=%d (%s) len=%d" %
                      (channelId, channelName, len(payload)))

    def _processClose(self, data, cbId):
        channelId, offset = self._readChannelId(data, 1, cbId)
        channelName = self._dynamicChannels.pop(channelId, "unknown")
        self._channelCbId.pop(channelId, None)
        self._dvcReassembly.pop(channelId, None)
        if channelId == self._gfxChannelId:
            self._gfxChannelId = None
            self._gfxConfirmed = False
        if channelId in self._rdpsndDvcChannelIds:
            self._rdpsndDvcChannelIds.discard(channelId)
            if self._rdpsndDvcPrimaryId == channelId:
                self._rdpsndDvcPrimaryId = None
                if self._rdpsndLayer is not None:
                    self._rdpsndLayer._dvcSendCallback = None
        log.debug("DrdynvcLayer: CLOSE channelId=%d (%s)" % (channelId, channelName))

    # ---------------------------------------------------------------
    # RDP_SEGMENTED_DATA unwrapping (MS-RDPEGFX 2.2.5)
    # ---------------------------------------------------------------

    ZGFX_SEGMENTED_SINGLE = 0xE0
    ZGFX_SEGMENTED_MULTIPART = 0xE1
    ZGFX_PACKET_COMPRESSED = 0x20

    def _unwrapSegmentedData(self, data):
        """Unwrap RDP_SEGMENTED_DATA envelope to get raw RDPGFX PDU stream.
        Returns decompressed/raw data, or None on error."""
        if len(data) < 2:
            return None
        descriptor = data[0]
        if descriptor == self.ZGFX_SEGMENTED_SINGLE:
            # Single segment: decompress using ZGFX
            try:
                return self._zgfx.decompress_segment(data[1:])
            except Exception as e:
                log.warning("RDPGFX: ZGFX decompress error (single): %s" % e)
                return None
        elif descriptor == self.ZGFX_SEGMENTED_MULTIPART:
            if len(data) < 7:
                return None
            segmentCount = struct.unpack_from('<H', data, 1)[0]
            uncompressedSize = struct.unpack_from('<I', data, 3)[0]
            offset = 7
            result = bytearray()
            for i in range(segmentCount):
                if offset + 4 > len(data):
                    break
                segSize = struct.unpack_from('<I', data, offset)[0]
                offset += 4
                if offset + segSize > len(data):
                    break
                segData = data[offset:offset + segSize]
                offset += segSize
                try:
                    result += self._zgfx.decompress_segment(segData)
                except Exception as e:
                    log.warning("RDPGFX: ZGFX decompress error (multi seg %d): %s" % (i, e))
                    return None
            return bytes(result)
        else:
            log.warning("RDPGFX: unknown segmented data descriptor 0x%02x" % descriptor)
            return None

    # ---------------------------------------------------------------
    # RDPGFX (MS-RDPEGFX) handling
    # ---------------------------------------------------------------

    def _processRdpgfxStream(self, data):
        """Unwrap RDP_SEGMENTED_DATA and parse concatenated RDPGFX PDUs."""
        self._processRdpgfxStreamSync(data)

    def _processRdpgfxStreamSync(self, data):
        """Synchronous GFX processing (decompress + parse + dispatch inline)."""
        raw = self._unwrapSegmentedData(data)
        if raw is None:
            return
        offset = 0
        while offset + 8 <= len(raw):
            cmdId = struct.unpack_from('<H', raw, offset)[0]
            flags = struct.unpack_from('<H', raw, offset + 2)[0]
            pduLen = struct.unpack_from('<I', raw, offset + 4)[0]
            if pduLen < 8 or offset + pduLen > len(raw):
                break
            payload = bytes(raw[offset + 8:offset + pduLen])
            self._processRdpgfxPdu(cmdId, flags, payload)
            offset += pduLen

    def _processRdpgfxPdu(self, cmdId, flags, payload):
        """Dispatch a single RDPGFX PDU."""
        name = _RDPGFX_CMDID_NAMES.get(cmdId, "UNKNOWN(0x%04x)" % cmdId)

        if cmdId == RDPGFX_CMDID_CAPSCONFIRM:
            self._onCapsConfirm(payload)
        elif cmdId == RDPGFX_CMDID_RESETGRAPHICS:
            self._onResetGraphics(payload)
        elif cmdId == RDPGFX_CMDID_CREATESURFACE:
            self._onCreateSurface(payload)
        elif cmdId == RDPGFX_CMDID_DELETESURFACE:
            self._onDeleteSurface(payload)
        elif cmdId == RDPGFX_CMDID_MAPSURFACETOOUTPUT:
            self._onMapSurfaceToOutput(payload)
        elif cmdId == RDPGFX_CMDID_STARTFRAME:
            self._onStartFrame(payload)
        elif cmdId == RDPGFX_CMDID_ENDFRAME:
            self._onEndFrame(payload)
        elif cmdId == RDPGFX_CMDID_WIRETOSURFACE_1:
            self._onWireToSurface1(payload)
        elif cmdId == RDPGFX_CMDID_WIRETOSURFACE_2:
            self._onWireToSurface2(payload)
        elif cmdId == RDPGFX_CMDID_SOLIDFILL:
            self._onSolidFill(payload)
        elif cmdId == RDPGFX_CMDID_SURFACETOCACHE:
            self._onSurfaceToCache(payload)
        elif cmdId == RDPGFX_CMDID_CACHETOSURFACE:
            self._onCacheToSurface(payload)
        elif cmdId == RDPGFX_CMDID_EVICTCACHEENTRY:
            self._onEvictCacheEntry(payload)
        elif cmdId == RDPGFX_CMDID_DELETEENCODINGCONTEXT:
            self._onDeleteEncodingContext(payload)
        elif cmdId == RDPGFX_CMDID_CACHEIMPORTREPLY:
            log.debug("RDPGFX: CACHEIMPORTREPLY len=%d" % len(payload))
        elif cmdId == RDPGFX_CMDID_MAPSURFACETOWINDOW:
            log.debug("RDPGFX: MAPSURFACETOWINDOW len=%d" % len(payload))
        elif cmdId == RDPGFX_CMDID_MAPSURFACETOSCALEDOUTPUT:
            log.debug("RDPGFX: MAPSURFACETOSCALEDOUTPUT len=%d" % len(payload))
        else:
            log.debug("RDPGFX: %s len=%d" % (name, len(payload)))

    def _onCapsConfirm(self, payload):
        if len(payload) >= 12:
            version = struct.unpack_from('<I', payload, 0)[0]
            dataLen = struct.unpack_from('<I', payload, 4)[0]
            flags = struct.unpack_from('<I', payload, 8)[0] if dataLen >= 4 else 0
            log.debug("RDPGFX: CAPS_CONFIRM version=0x%08x flags=0x%08x" % (version, flags))
            self._gfxVersion = version
        else:
            log.debug("RDPGFX: CAPS_CONFIRM (short payload)")
        self._gfxConfirmed = True
        if self._capsConfirmCallback:
            self._capsConfirmCallback()

    def _onResetGraphics(self, payload):
        """RESET_GRAPHICS: server tells client the desktop dimensions."""
        if len(payload) < 12:
            return
        width = struct.unpack_from('<I', payload, 0)[0]
        height = struct.unpack_from('<I', payload, 4)[0]
        monitorCount = struct.unpack_from('<I', payload, 8)[0]
        self._resetWidth = width
        self._resetHeight = height
        self._surfaces.clear()
        self._surfaceData.clear()
        self._surfaceOutputMap.clear()
        # Reset ClearCodec caches (matches grdp's onResetGraphics)
        self._ccVBarStorage = [None] * 32768
        self._ccShortVBarStorage = [None] * 16384
        self._ccVBarCursor = 0
        self._ccShortVBarCursor = 0
        # Reset H.264 decoder state (matches grdp's onResetGraphics)
        if self._avcDecoder is not None:
            try:
                self._avcDecoder.close()
            except Exception:
                pass
            self._avcDecoder = None
        # Reset frame counter (matches grdp's framesDecoded.Store(0))
        self._totalFramesDecoded = 0
        log.debug("RDPGFX: RESET_GRAPHICS %dx%d monitors=%d" % (width, height, monitorCount))

    def _onCreateSurface(self, payload):
        """CREATE_SURFACE: surfaceId(2) + width(2) + height(2) + pixelFormat(1)"""
        if len(payload) < 7:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        width = struct.unpack_from('<H', payload, 2)[0]
        height = struct.unpack_from('<H', payload, 4)[0]
        pixelFormat = payload[6]
        self._surfaces[surfaceId] = (width, height, pixelFormat)
        self._surfaceData[surfaceId] = bytearray(width * height * 4)
        log.debug("RDPGFX: CREATE_SURFACE id=%d %dx%d fmt=0x%02x" %
                 (surfaceId, width, height, pixelFormat))

    def _onDeleteSurface(self, payload):
        if len(payload) < 2:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        self._surfaces.pop(surfaceId, None)
        self._surfaceData.pop(surfaceId, None)
        self._surfaceOutputMap.pop(surfaceId, None)
        log.debug("RDPGFX: DELETE_SURFACE id=%d" % surfaceId)

    def _onMapSurfaceToOutput(self, payload):
        """MAP_SURFACE_TO_OUTPUT: surfaceId(2) + reserved(2) + outputOriginX(4) + outputOriginY(4)"""
        if len(payload) < 12:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        outputOriginX = struct.unpack_from('<I', payload, 4)[0]
        outputOriginY = struct.unpack_from('<I', payload, 8)[0]
        self._surfaceOutputMap[surfaceId] = (outputOriginX, outputOriginY)
        log.debug("RDPGFX: MAP_SURFACE_TO_OUTPUT id=%d -> (%d,%d)" %
                 (surfaceId, outputOriginX, outputOriginY))

    def _onStartFrame(self, payload):
        """START_FRAME: timestamp(4) + frameId(4)"""
        if len(payload) < 8:
            return
        timestamp = struct.unpack_from('<I', payload, 0)[0]
        frameId = struct.unpack_from('<I', payload, 4)[0]
        self._currentFrameId = frameId
        log.debug("RDPGFX: START_FRAME id=%d ts=%d" % (frameId, timestamp))

    def _onEndFrame(self, payload):
        """END_FRAME: frameId(4). Send FRAME_ACKNOWLEDGE."""
        if len(payload) < 4:
            return
        frameId = struct.unpack_from('<I', payload, 0)[0]
        self._totalFramesDecoded += 1
        self._lastFrameTime = time.monotonic()
        log.debug("RDPGFX: END_FRAME id=%d (total=%d)" %
                  (frameId, self._totalFramesDecoded))
        self._sendFrameAcknowledge(frameId)

    def _onWireToSurface1(self, payload):
        """WIRE_TO_SURFACE_1: surfaceId(2) + codecId(2) + pixelFormat(1) + destRect(8) + bitmapData"""
        if len(payload) < 17:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        codecId = struct.unpack_from('<H', payload, 2)[0]
        pixelFormat = payload[4]
        # destRect: left(2) + top(2) + right(2) + bottom(2)
        left = struct.unpack_from('<H', payload, 5)[0]
        top = struct.unpack_from('<H', payload, 7)[0]
        right = struct.unpack_from('<H', payload, 9)[0]
        bottom = struct.unpack_from('<H', payload, 11)[0]
        bitmapDataLen = struct.unpack_from('<I', payload, 13)[0]
        bitmapData = payload[17:17 + bitmapDataLen]

        width = right - left
        height = bottom - top

        log.debug("RDPGFX: WTS1 surfId=%d codecId=0x%04X %dx%d bmpLen=%d" %
                 (surfaceId, codecId, width, height, len(bitmapData)))

        # CaVideo is heavy (RFX tile decode)
        if codecId == RDPGFX_CODECID_CAVIDEO:
            self._renderCaVideo(surfaceId, left, top, width, height, bitmapData)
        elif codecId == RDPGFX_CODECID_UNCOMPRESSED:
            self._renderUncompressed(surfaceId, left, top, width, height,
                                     pixelFormat, bitmapData)
        elif codecId == RDPGFX_CODECID_PLANAR:
            log.debug("RDPGFX: PLANAR codec not yet supported, %d bytes" % len(bitmapData))
        elif codecId == RDPGFX_CODECID_CAPROGRESSIVE:
            log.debug("RDPGFX: Progressive RemoteFX not yet supported, %d bytes" % len(bitmapData))
        elif codecId == RDPGFX_CODECID_CLEARCODEC:
            self._renderClearCodec(surfaceId, left, top, width, height,
                                   pixelFormat, bitmapData)
        elif codecId == RDPGFX_CODECID_ALPHA:
            log.debug("RDPGFX: Alpha codec not yet supported, %d bytes" % len(bitmapData))
        elif codecId == RDPGFX_CODECID_AVC420:
            self._renderAvc420(surfaceId, left, top, width, height, bitmapData)
        elif codecId in (RDPGFX_CODECID_AVC444, RDPGFX_CODECID_AVC444v2):
            self._renderAvc444(surfaceId, left, top, width, height, bitmapData)
        else:
            log.debug("RDPGFX: unsupported codec 0x%04x, %d bytes" % (codecId, len(bitmapData)))

    def _onWireToSurface2(self, payload):
        """WIRE_TO_SURFACE_2: surfaceId(2) + codecId(2) + codecContextId(4) + pixelFormat(1) + bitmapDataLen(4) + bitmapData"""
        if len(payload) < 13:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        codecId = struct.unpack_from('<H', payload, 2)[0]
        codecContextId = struct.unpack_from('<I', payload, 4)[0]
        pixelFormat = payload[8]
        bitmapDataLen = struct.unpack_from('<I', payload, 9)[0]
        bitmapData = payload[13:13 + bitmapDataLen]

        surfInfo = self._surfaces.get(surfaceId)
        w, h = (surfInfo[0], surfInfo[1]) if surfInfo else (0, 0)
        log.debug("RDPGFX: WTS2 surfId=%d codecId=0x%04X %dx%d bmpLen=%d" %
                 (surfaceId, codecId, w, h, len(bitmapData)))

        if codecId == RDPGFX_CODECID_CAPROGRESSIVE:
            surfInfo = self._surfaces.get(surfaceId)
            surfBuf = self._surfaceData.get(surfaceId)
            if surfInfo is None or surfBuf is None:
                log.warning("RDPGFX: WIRE_TO_SURFACE_2 unknown surface %d" % surfaceId)
                return
            w, h, fmt = surfInfo
            try:
                rects = self._rfxDecoder.decode(bitmapData, surfBuf, w, h)
                self._deliverSurfaceBitmap(surfaceId, rects)
            except Exception as e:
                log.warning("RDPGFX: Progressive decode error: %s" % str(e))
        elif codecId == RDPGFX_CODECID_CAVIDEO:
            surfInfo = self._surfaces.get(surfaceId)
            surfBuf = self._surfaceData.get(surfaceId)
            if surfInfo is None or surfBuf is None:
                log.warning("RDPGFX: WTS2 unknown surface %d" % surfaceId)
                return
            w, h, _ = surfInfo
            try:
                rects = self._rfxTileDecoder.decode(bitmapData, 0, 0, surfBuf, w, h)
                self._deliverSurfaceBitmap(surfaceId, rects)
            except Exception as e:
                log.warning("RDPGFX: CaVideo RFX decode error: %s" % str(e))
        elif codecId == RDPGFX_CODECID_AVC420:
            surfInfo = self._surfaces.get(surfaceId)
            if surfInfo is None:
                log.warning("RDPGFX: WTS2 unknown surface %d" % surfaceId)
                return
            w, h, _ = surfInfo
            self._renderAvc420(surfaceId, 0, 0, w, h, bitmapData)
        elif codecId in (RDPGFX_CODECID_AVC444, RDPGFX_CODECID_AVC444v2):
            surfInfo = self._surfaces.get(surfaceId)
            if surfInfo is None:
                log.warning("RDPGFX: WTS2 unknown surface %d" % surfaceId)
                return
            w, h, _ = surfInfo
            self._renderAvc444(surfaceId, 0, 0, w, h, bitmapData)
        else:
            log.debug("RDPGFX: WTS2 unsupported codec 0x%04x" % codecId)

    def _onSolidFill(self, payload):
        """SOLID_FILL: surfaceId(2) + fillPixel(4) + fillRectCount(2) + fillRects"""
        if len(payload) < 8:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        fillPixelB = payload[2]
        fillPixelG = payload[3]
        fillPixelR = payload[4]
        fillPixelA = payload[5]
        rectCount = struct.unpack_from('<H', payload, 6)[0]
        log.debug("RDPGFX: SOLID_FILL surf=%d color=(%d,%d,%d,%d) rects=%d" %
                  (surfaceId, fillPixelR, fillPixelG, fillPixelB, fillPixelA, rectCount))

        if self._gfxCallback is None:
            return

        # Parse each fill rectangle and render
        offset = 8
        for i in range(rectCount):
            if offset + 8 > len(payload):
                break
            left = struct.unpack_from('<H', payload, offset)[0]
            top = struct.unpack_from('<H', payload, offset + 2)[0]
            right = struct.unpack_from('<H', payload, offset + 4)[0]
            bottom = struct.unpack_from('<H', payload, offset + 6)[0]
            offset += 8
            w = right - left
            h = bottom - top
            log.debug("RDPGFX: SOLID_FILL rect[%d]=(%d,%d,%d,%d) %dx%d" %
                      (i, left, top, right, bottom, w, h))
            if w <= 0 or h <= 0:
                continue
            # Build BGRX pixel data for the solid fill
            pixel = bytes([fillPixelB, fillPixelG, fillPixelR, 0xFF])
            row = pixel * w
            bitmapData = row * h
            # Update surface buffer so cache operations see the fill
            self._blitToSurface(surfaceId, left, top, w, h, bitmapData)
            ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))
            self._deliverBitmap(ox + left, oy + top, w, h, 32, bitmapData)

    def _renderCaVideo(self, surfaceId, left, top, width, height, data):
        """Decode CaVideo (0x0003) RFX tile data onto surface (WTS1)."""
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            log.warning("RDPGFX: CaVideo WTS1 unknown surface %d" % surfaceId)
            return
        sw, sh, _ = surfInfo
        try:
            rects = self._rfxTileDecoder.decode(data, left, top, surfBuf, sw, sh)
            if not rects:
                return
            ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))
            stride = sw * 4
            for (rx, ry, rw, rh) in rects:
                needed = rw * rh * 4
                region = bytearray(needed)
                row_bytes = rw * 4
                for row in range(rh):
                    src_off = (ry + row) * stride + rx * 4
                    dst_off = row * row_bytes
                    if src_off + row_bytes <= len(surfBuf):
                        region[dst_off:dst_off + row_bytes] = surfBuf[src_off:src_off + row_bytes]
                self._deliverBitmap(ox + rx, oy + ry, rw, rh, 32, bytes(region))
        except Exception as e:
            log.warning("RDPGFX: CaVideo RFX decode error: %s" % e)

    def _getAvcDecoder(self):
        """Lazy-initialize and return the H.264/AVC decoder."""
        if self._avcDecoder is None:
            if not avc_module.is_available():
                log.warning("RDPGFX: AVC codec received but PyAV not available")
                return None
            try:
                self._avcDecoder = avc_module.AvcDecoder()
                log.debug("RDPGFX: AVC decoder initialized (hardware=%s)" %
                         self._avcDecoder.is_hardware)
            except Exception as e:
                log.warning("RDPGFX: failed to initialize AVC decoder: %s" % e)
                return None
        return self._avcDecoder

    def _renderAvc420(self, surfaceId, left, top, width, height, data):
        """Decode and render AVC420 (H.264 YUV420) bitmap data."""
        decoder = self._getAvcDecoder()
        if decoder is None:
            return
        try:
            bgra_bytes = decoder.decode_avc420(data, width, height)
            if bgra_bytes is None:
                return
            self._blitToSurface(surfaceId, left, top, width, height, bgra_bytes)
            ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))
            self._deliverBitmap(ox + left, oy + top, width, height, 32, bgra_bytes)
        except Exception as e:
            log.warning("RDPGFX: AVC420 decode error: %s" % e)

    def _renderAvc444(self, surfaceId, left, top, width, height, data):
        """Decode and render AVC444/AVC444v2 (H.264 YUV444) bitmap data."""
        decoder = self._getAvcDecoder()
        if decoder is None:
            return
        try:
            bgra_bytes = decoder.decode_avc444(data, width, height)
            if bgra_bytes is None:
                return
            self._blitToSurface(surfaceId, left, top, width, height, bgra_bytes)
            ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))
            self._deliverBitmap(ox + left, oy + top, width, height, 32, bgra_bytes)
        except Exception as e:
            log.warning("RDPGFX: AVC444 decode error: %s" % e)

    def _renderUncompressed(self, surfaceId, left, top, width, height, pixelFormat, data):
        """Render uncompressed XRGB_8888/ARGB_8888 bitmap data."""
        if self._gfxCallback is None:
            return

        bpp = 32
        stride = width * 4
        expected = stride * height
        if len(data) < expected:
            log.warning("RDPGFX: uncompressed data too short: %d < %d" % (len(data), expected))
            return

        # RDPGFX XRGB_8888: pixels are [B, G, R, X] (same as RDP BGRX)
        # Force alpha to 0xFF for Qt Format_RGB32 compatibility
        raw = bytearray(data[:expected])
        raw[3::4] = b'\xff' * (expected // 4)

        # Also update the surface buffer so cache operations work
        self._blitToSurface(surfaceId, left, top, width, height, raw)

        ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))
        self._deliverBitmap(ox + left, oy + top, width, height, bpp, bytes(raw))

    def _renderClearCodec(self, surfaceId, left, top, width, height, pixelFormat, data):
        """Decode ClearCodec bitmap per MS-RDPEGFX 2.2.4."""
        if len(data) < 12:
            return
        off = 0

        residual_len = struct.unpack_from('<I', data, off)[0]
        band_len = struct.unpack_from('<I', data, off + 4)[0]
        subcodec_len = struct.unpack_from('<I', data, off + 8)[0]

        off += 12
        out = bytearray(width * height * 4)

        # 1. Residual layer: 3 bytes per pixel (BGR), top-down
        if residual_len > 0:
            res_data = data[off:off + residual_len]
            off += residual_len
            n_pixels = width * height
            usable = min(len(res_data) // 3, n_pixels)
            if usable > 0:
                bgr = np.frombuffer(res_data[:usable * 3], dtype=np.uint8).reshape(usable, 3)
                bgra = np.empty((usable, 4), dtype=np.uint8)
                bgra[:, :3] = bgr
                bgra[:, 3] = 0xFF
                out[:usable * 4] = bgra.tobytes()

        # 2. Band layer
        if band_len > 0:
            band_data = data[off:off + band_len]
            off += band_len
            self._decodeClearCodecBands(band_data, out, width)

        # 3. Subcodec layer
        if subcodec_len > 0:
            sub_data = data[off:off + subcodec_len]
            off += subcodec_len
            self._decodeClearCodecSubcodec(sub_data, out, width)

        self._blitToSurface(surfaceId, left, top, width, height, out)
        ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))
        self._deliverBitmap(ox + left, oy + top, width, height, 32, bytes(out))

    def _decodeClearCodecBands(self, data, out, surfW):
        """Decode ClearCodec band layer matching grdp's decodeBands.
        Band header: xStart(2) yStart(2) xEnd(2) yEnd(2) blueBkg(1) greenBkg(1) redBkg(1).
        VBARs classified by top 2 bits of header."""
        off = 0
        remaining = len(data)

        while remaining >= 11:
            x_start = struct.unpack_from('<H', data, off)[0]
            y_start = struct.unpack_from('<H', data, off + 2)[0]
            x_end = struct.unpack_from('<H', data, off + 4)[0]
            y_end = struct.unpack_from('<H', data, off + 6)[0]
            blue_bg = data[off + 8]
            green_bg = data[off + 9]
            red_bg = data[off + 10]
            off += 11
            remaining -= 11

            band_h = y_end - y_start
            col_count = x_end - x_start
            if band_h <= 0 or col_count <= 0:
                continue

            for col in range(col_count):
                if remaining < 2:
                    return
                vbar_header = struct.unpack_from('<H', data, off)[0]
                off += 2
                remaining -= 2
                x = x_start + col

                top2 = vbar_header & 0xC000

                if top2 == 0xC000:
                    # SHORT_VBAR_CACHE_HIT — yOn=0 per grdp reference
                    idx = vbar_header & 0x3FFF
                    entry = self._ccShortVBarStorage[idx] if idx < len(self._ccShortVBarStorage) else None
                    if entry is not None:
                        pixels, count = entry
                        self._paintColumnBg(out, surfW, x, y_start, band_h, red_bg, green_bg, blue_bg)
                        self._paintVBarPixels(out, surfW, x, y_start, 0, pixels, count)

                elif top2 == 0x4000:
                    # SHORT_VBAR_CACHE_MISS
                    pix_count = vbar_header & 0x3FFF
                    if remaining < 1:
                        return
                    y_on = data[off]  # 1 byte
                    off += 1
                    remaining -= 1
                    need = pix_count * 3
                    pixels = bytes(data[off:off + min(need, remaining)])
                    off += min(need, remaining)
                    remaining -= min(need, remaining)
                    entry = (pixels, pix_count)
                    if self._ccShortVBarCursor < len(self._ccShortVBarStorage):
                        self._ccShortVBarStorage[self._ccShortVBarCursor] = entry
                    self._ccShortVBarCursor = (self._ccShortVBarCursor + 1) % len(self._ccShortVBarStorage)
                    self._paintColumnBg(out, surfW, x, y_start, band_h, red_bg, green_bg, blue_bg)
                    self._paintVBarPixels(out, surfW, x, y_start, y_on, pixels, pix_count)

                elif (vbar_header & 0x8000) == 0x8000:
                    # VBAR_CACHE_HIT
                    idx = vbar_header & 0x7FFF
                    entry = self._ccVBarStorage[idx] if idx < len(self._ccVBarStorage) else None
                    if entry is not None:
                        pixels, count = entry
                        self._paintVBarPixels(out, surfW, x, y_start, 0, pixels, count)

                else:
                    # VBAR_CACHE_MISS
                    pix_count = vbar_header & 0x7FFF
                    need = pix_count * 3
                    pixels = bytes(data[off:off + min(need, remaining)])
                    off += min(need, remaining)
                    remaining -= min(need, remaining)
                    entry = (pixels, pix_count)
                    if self._ccVBarCursor < len(self._ccVBarStorage):
                        self._ccVBarStorage[self._ccVBarCursor] = entry
                    self._ccVBarCursor = (self._ccVBarCursor + 1) % len(self._ccVBarStorage)
                    self._paintVBarPixels(out, surfW, x, y_start, 0, pixels, pix_count)

    def _paintColumnBg(self, out, surfW, x, yStart, height, r, g, b):
        """Fill a single column with background color (BGRA)."""
        for y in range(height):
            dy = yStart + y
            idx = (dy * surfW + x) * 4
            if idx + 3 < len(out):
                out[idx] = b
                out[idx + 1] = g
                out[idx + 2] = r
                out[idx + 3] = 0xFF

    def _paintVBarPixels(self, out, surfW, x, yStart, yOn, pixels, count):
        """Paint VBAR pixel data (BGR, 3 bytes per pixel) onto output buffer."""
        for y in range(count):
            si = y * 3
            dy = yStart + yOn + y
            di = (dy * surfW + x) * 4
            if si + 2 < len(pixels) and di + 3 < len(out):
                out[di] = pixels[si]         # B
                out[di + 1] = pixels[si + 1] # G
                out[di + 2] = pixels[si + 2] # R
                out[di + 3] = 0xFF

    def _decodeClearCodecSubcodec(self, data, out, surfW):
        """Decode ClearCodec subcodec layer matching grdp's decodeSubcodec."""
        off = 0
        remaining = len(data)
        while remaining >= 13:
            x_start = struct.unpack_from('<H', data, off)[0]
            y_start = struct.unpack_from('<H', data, off + 2)[0]
            w = struct.unpack_from('<H', data, off + 4)[0]
            h = struct.unpack_from('<H', data, off + 6)[0]
            bmp_len = struct.unpack_from('<I', data, off + 8)[0]
            subcodec_id = data[off + 12]
            off += 13
            remaining -= 13
            if bmp_len > remaining:
                break
            bmp_data = data[off:off + bmp_len]
            off += bmp_len
            remaining -= bmp_len
            if subcodec_id == 0:
                # RAW BGR
                usable_sc = min(len(bmp_data) // 3, w * h)
                if usable_sc > 0:
                    bgr = np.frombuffer(bmp_data[:usable_sc * 3], dtype=np.uint8).reshape(usable_sc, 3)
                    bgra = np.empty((usable_sc, 4), dtype=np.uint8)
                    bgra[:, :3] = bgr
                    bgra[:, 3] = 0xFF
                    bgra_2d = bgra.reshape(h, w, 4) if usable_sc == w * h else None
                    if bgra_2d is not None:
                        for y in range(h):
                            dy = y_start + y
                            di = (dy * surfW + x_start) * 4
                            if di + w * 4 <= len(out):
                                out[di:di + w * 4] = bgra_2d[y].tobytes()
            elif subcodec_id == 2:
                # NSCodec (single color fill): 3 bytes BGR
                if len(bmp_data) >= 3:
                    pixel = bytes([bmp_data[0], bmp_data[1], bmp_data[2], 0xFF])
                    row_data = pixel * w
                    for y in range(h):
                        dy = y_start + y
                        di = (dy * surfW + x_start) * 4
                        if di + w * 4 <= len(out):
                            out[di:di + w * 4] = row_data

    def _blitToSurface(self, surfaceId, left, top, width, height, data):
        """Write pixel data (top-down BGRA) into the surface buffer."""
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            return
        surfW = surfInfo[0]
        stride = width * 4
        for row in range(height):
            src_off = row * stride
            dst_off = ((top + row) * surfW + left) * 4
            if src_off + stride <= len(data) and dst_off + stride <= len(surfBuf):
                surfBuf[dst_off:dst_off + stride] = data[src_off:src_off + stride]

    def _onSurfaceToCache(self, payload):
        """SURFACETOCACHE: surfaceId(2) + cacheKey(8) + cacheSlot(2) + rectLeft(2) +
        rectTop(2) + rectRight(2) + rectBottom(2)"""
        if len(payload) < 20:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        cacheSlot = struct.unpack_from('<H', payload, 10)[0]
        rectLeft = struct.unpack_from('<H', payload, 12)[0]
        rectTop = struct.unpack_from('<H', payload, 14)[0]
        rectRight = struct.unpack_from('<H', payload, 16)[0]
        rectBottom = struct.unpack_from('<H', payload, 18)[0]

        w = rectRight - rectLeft
        h = rectBottom - rectTop
        if w <= 0 or h <= 0:
            return

        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            log.debug("RDPGFX: SURFACETOCACHE unknown surface %d" % surfaceId)
            return

        surfW = surfInfo[0]
        # Copy the rect from surface to cache
        region = bytearray(w * h * 4)
        for row in range(h):
            src_off = ((rectTop + row) * surfW + rectLeft) * 4
            dst_off = row * w * 4
            if src_off + w * 4 <= len(surfBuf):
                region[dst_off:dst_off + w * 4] = surfBuf[src_off:src_off + w * 4]

        self._gfxCache[cacheSlot] = (w, h, region)
        log.debug("RDPGFX: SURFACETOCACHE surf=%d slot=%d rect=(%d,%d,%d,%d)" %
                  (surfaceId, cacheSlot, rectLeft, rectTop, rectRight, rectBottom))

    def _onCacheToSurface(self, payload):
        """CACHETOSURFACE: cacheSlot(2) + surfaceId(2) + numDestPoints(2) + destPoints"""
        if len(payload) < 6:
            return
        cacheSlot = struct.unpack_from('<H', payload, 0)[0]
        surfaceId = struct.unpack_from('<H', payload, 2)[0]
        numDest = struct.unpack_from('<H', payload, 4)[0]

        cached = self._gfxCache.get(cacheSlot)
        if cached is None:
            log.debug("RDPGFX: CACHETOSURFACE missing cache slot %d" % cacheSlot)
            return

        cw, ch, cdata = cached
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            return

        surfW = surfInfo[0]
        ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))

        off = 6
        dest_coords = []
        for _ in range(numDest):
            if off + 4 > len(payload):
                break
            dx = struct.unpack_from('<H', payload, off)[0]
            dy = struct.unpack_from('<H', payload, off + 2)[0]
            off += 4
            dest_coords.append((dx, dy))

            # Blit cached data to surface buffer
            for row in range(ch):
                src_off = row * cw * 4
                dst_off = ((dy + row) * surfW + dx) * 4
                if dst_off + cw * 4 <= len(surfBuf) and src_off + cw * 4 <= len(cdata):
                    surfBuf[dst_off:dst_off + cw * 4] = cdata[src_off:src_off + cw * 4]

            # Deliver to display
            self._deliverBitmap(ox + dx, oy + dy, cw, ch, 32, bytes(cdata))

        first_coords = ", ".join("(%d,%d)" % (dx, dy) for dx, dy in dest_coords[:5])
        log.debug("RDPGFX: CACHETOSURFACE slot=%d surf=%d points=%d %dx%d first=[%s]%s" %
                  (cacheSlot, surfaceId, numDest, cw, ch, first_coords,
                   " ..." if numDest > 5 else ""))

    def _onEvictCacheEntry(self, payload):
        """EVICTCACHEENTRY: cacheSlot(2)"""
        if len(payload) < 2:
            return
        cacheSlot = struct.unpack_from('<H', payload, 0)[0]
        self._gfxCache.pop(cacheSlot, None)
        log.debug("RDPGFX: EVICTCACHEENTRY slot=%d" % cacheSlot)

    def _onDeleteEncodingContext(self, payload):
        """DELETEENCODINGCONTEXT: surfaceId(2) + codecContextId(4)"""
        if len(payload) < 6:
            return
        surfaceId = struct.unpack_from('<H', payload, 0)[0]
        codecContextId = struct.unpack_from('<I', payload, 2)[0]
        # Reset RFX Progressive tile state so stale data is not reused
        self._rfxDecoder.reset()
        log.debug("RDPGFX: DELETEENCODINGCTX surf=%d ctx=%d" % (surfaceId, codecContextId))

    def _deliverSurfaceBitmap(self, surfaceId, rects):
        """Deliver decoded Progressive tiles for a surface to the observer."""
        if self._gfxCallback is None or not rects:
            return
        surfInfo = self._surfaces.get(surfaceId)
        surfBuf = self._surfaceData.get(surfaceId)
        if surfInfo is None or surfBuf is None:
            return
        surfW, surfH, _ = surfInfo
        ox, oy = self._surfaceOutputMap.get(surfaceId, (0, 0))

        for (rx, ry, rw, rh) in rects:
            if rw <= 0 or rh <= 0:
                continue
            # Extract the region from the surface buffer
            region = bytearray(rw * rh * 4)
            for row in range(rh):
                src_off = ((ry + row) * surfW + rx) * 4
                dst_off = row * rw * 4
                region[dst_off:dst_off + rw * 4] = surfBuf[src_off:src_off + rw * 4]
            self._deliverBitmap(ox + rx, oy + ry, rw, rh, 32, bytes(region))

    def _deliverBitmap(self, x, y, width, height, bpp, data):
        """Deliver decoded bitmap to the observer via callback.
        Data is top-down BGRA scanlines from RDPGFX codecs.
        Pass isCompress='gfx' sentinel so the UI layer knows the data
        is already top-down and does not need the bottom-up flip.
        """
        if self._gfxCallback is None:
            return
        self._gfxCallback(x, y, x + width - 1, y + height - 1,
                          width, height, bpp, 'gfx', data)

    def _sendFrameAcknowledge(self, frameId):
        """Send RDPGFX FRAME_ACKNOWLEDGE PDU."""
        if self._gfxChannelId is None:
            return
        # FRAME_ACKNOWLEDGE payload: queueDepth(4) + frameId(4) + totalFramesDecoded(4)
        ackPayload = struct.pack('<III', 0, frameId, self._totalFramesDecoded)
        pduLen = 8 + len(ackPayload)
        gfxPdu = struct.pack('<HHI', RDPGFX_CMDID_FRAMEACKNOWLEDGE, 0, pduLen)
        gfxPdu += ackPayload

        cbId = self._channelCbId.get(self._gfxChannelId, 0)
        self._sendDvcData(self._gfxChannelId, cbId, gfxPdu)
        log.debug("RDPGFX: sent FRAME_ACKNOWLEDGE frameId=%d total=%d" %
                  (frameId, self._totalFramesDecoded))

    # ---------------------------------------------------------------
    # RDPGFX CAPS_ADVERTISE
    # ---------------------------------------------------------------

    def _sendRdpgfxCapsAdvertise(self, channelId, cbId):
        """Send RDPGFX CAPS_ADVERTISE PDU (MS-RDPEGFX 2.2.3)

        When PyAV is available, advertise v10 (AVC444) and v8.0 (AVC420)
        capsets without THINCLIENT flag — the flag causes the server to
        prefer RemoteFX over H.264 even when AVC is not disabled.
        When PyAV is unavailable, fall back to a single v8.0 capset with
        THINCLIENT | SMALL_CACHE | AVC_DISABLED."""
        capsSets = []
        if avc_module.is_available():
            # v10 capset — preferred; enables AVC444 + AVC420
            capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_10, 4,
                                        RDPGFX_CAPS_FLAG_SMALL_CACHE))
            # v8.0 capset — fallback; enables AVC420
            capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_8, 4,
                                        RDPGFX_CAPS_FLAG_SMALL_CACHE))
        else:
            capsSets.append(struct.pack('<III', RDPGFX_CAPVERSION_8, 4,
                                        RDPGFX_CAPS_FLAG_THINCLIENT |
                                        RDPGFX_CAPS_FLAG_SMALL_CACHE |
                                        RDPGFX_CAPS_FLAG_AVC_DISABLED))

        capsPayload = struct.pack('<H', len(capsSets))
        for cs in capsSets:
            capsPayload += cs

        pduLen = 8 + len(capsPayload)
        gfxPdu = struct.pack('<HHI', RDPGFX_CMDID_CAPSADVERTISE, 0, pduLen)
        gfxPdu += capsPayload

        self._sendDvcData(channelId, cbId, gfxPdu)
        if avc_module.is_available():
            log.debug("RDPGFX: sent CAPS_ADVERTISE (v10+v8.0, AVC enabled)")
        else:
            log.debug("RDPGFX: sent CAPS_ADVERTISE (v8.0, AVC disabled)")

    # ---------------------------------------------------------------
    # DVC transport
    # ---------------------------------------------------------------

    def _sendDvcData(self, channelId, cbId, data):
        """Send data on a dynamic virtual channel."""
        header = (DrdynvcCmd.DATA << 4) | cbId
        msg = bytearray([header])
        if cbId == 0:
            msg += struct.pack('<B', channelId)
        elif cbId == 1:
            msg += struct.pack('<H', channelId)
        elif cbId == 2:
            msg += struct.pack('<I', channelId)
        msg += data
        self._send(bytes(msg))

    def _send(self, data):
        """Send data as a Virtual Channel PDU on the drdynvc static channel."""
        if self._transport is not None:
            from rdpy.core.type import String
            flags = CHANNEL_FLAG_FIRST | CHANNEL_FLAG_LAST
            header = struct.pack('<II', len(data), flags)
            self._transport.send(String(header + data))
