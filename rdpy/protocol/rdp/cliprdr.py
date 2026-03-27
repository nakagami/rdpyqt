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
CLIPRDR - Clipboard Virtual Channel Extension (MS-RDPECLIP).
Handles the "cliprdr" static virtual channel for bidirectional
clipboard sharing between the RDP client and server.
Only text formats (CF_UNICODETEXT / CF_TEXT) are supported.

Protocol reference: [MS-RDPECLIP]
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpeclip/
"""

import struct
from rdpy.core.layer import LayerAutomata
import rdpy.core.log as log

# Virtual Channel PDU flags
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002
CHANNEL_FLAG_SHOW_PROTOCOL = 0x00000010

# CLIPRDR PDU types (MS-RDPECLIP 2.2.1)
CB_MONITOR_READY = 0x0001
CB_FORMAT_LIST = 0x0002
CB_FORMAT_LIST_RESPONSE = 0x0003
CB_FORMAT_DATA_REQUEST = 0x0004
CB_FORMAT_DATA_RESPONSE = 0x0005
CB_TEMP_DIRECTORY = 0x0006
CB_CLIP_CAPS = 0x0007
CB_FILECONTENTS_REQUEST = 0x0008
CB_FILECONTENTS_RESPONSE = 0x0009
CB_LOCK_CLIPDATA = 0x000A
CB_UNLOCK_CLIPDATA = 0x000B

# CLIPRDR message flags
CB_RESPONSE_OK = 0x0001
CB_RESPONSE_FAIL = 0x0002
CB_ASCII_NAMES = 0x0004

# Clipboard format IDs (Windows standard)
CF_TEXT = 1
CF_UNICODETEXT = 13

# General capability set flags (MS-RDPECLIP 2.2.2.1.1.1)
CB_USE_LONG_FORMAT_NAMES = 0x00000002
CB_STREAM_FILECLIP_ENABLED = 0x00000004
CB_FILECLIP_NO_FILE_PATHS = 0x00000008
CB_CAN_LOCK_CLIPDATA = 0x00000010
CB_HUGE_FILE_SUPPORT_ENABLED = 0x00000020

# Capability set type
CB_CAPSTYPE_GENERAL = 0x0001

_MSG_NAMES = {
    CB_MONITOR_READY: "MONITOR_READY",
    CB_FORMAT_LIST: "FORMAT_LIST",
    CB_FORMAT_LIST_RESPONSE: "FORMAT_LIST_RESPONSE",
    CB_FORMAT_DATA_REQUEST: "FORMAT_DATA_REQUEST",
    CB_FORMAT_DATA_RESPONSE: "FORMAT_DATA_RESPONSE",
    CB_TEMP_DIRECTORY: "TEMP_DIRECTORY",
    CB_CLIP_CAPS: "CLIP_CAPS",
    CB_FILECONTENTS_REQUEST: "FILECONTENTS_REQUEST",
    CB_FILECONTENTS_RESPONSE: "FILECONTENTS_RESPONSE",
    CB_LOCK_CLIPDATA: "LOCK_CLIPDATA",
    CB_UNLOCK_CLIPDATA: "UNLOCK_CLIPDATA",
}


class CliprdrLayer(LayerAutomata):
    """
    CLIPRDR static virtual channel layer.

    Implements the MS-RDPECLIP protocol for clipboard redirection.
    Only CF_UNICODETEXT is negotiated so all clipboard data is text.
    """

    def __init__(self):
        LayerAutomata.__init__(self, None)
        # VChannel reassembly buffer
        self._vchanBuf = b''
        # Server capability flags
        self._serverGeneralFlags = 0
        # Whether the server uses long format names
        self._useLongFormatNames = False
        # Callback: called with (text: str) when the server's clipboard is received
        self._onRemoteClipboardChanged = None
        # Callback: called with no args to request current local clipboard text
        self._getLocalClipboardText = None
        # Tracks whether we are suppressing the next local clipboard change
        # to avoid echo loops (server→client paste triggers local clipboard change)
        self._suppressNextLocalChange = False

    def setRemoteClipboardCallback(self, callback):
        """Set callback(text: str) invoked when server clipboard data arrives."""
        self._onRemoteClipboardChanged = callback

    def setLocalClipboardGetter(self, callback):
        """Set callback() -> str that returns current local clipboard text."""
        self._getLocalClipboardText = callback

    def connect(self):
        log.debug("CliprdrLayer.connect()")

    def recv(self, s):
        """Receive data on the cliprdr static virtual channel with VChannel reassembly."""
        data = s.read()
        log.debug("CLIPRDR DEBUG: recv() called, data length=%d" % len(data))
        if len(data) < 8:
            log.warning("CLIPRDR: recv data too short (%d bytes)" % len(data))
            return
        totalLen = struct.unpack_from('<I', data, 0)[0]
        flags = struct.unpack_from('<I', data, 4)[0]
        payload = data[8:]
        log.debug("CLIPRDR DEBUG: recv totalLen=%d flags=0x%08x payloadLen=%d" % (totalLen, flags, len(payload)))

        if flags & CHANNEL_FLAG_FIRST:
            self._vchanBuf = payload
        else:
            self._vchanBuf += payload

        if flags & CHANNEL_FLAG_LAST:
            log.debug("CLIPRDR DEBUG: reassembled %d bytes, processing" % len(self._vchanBuf))
            if len(self._vchanBuf) >= 1:
                self._processData(bytes(self._vchanBuf))
            self._vchanBuf = b''

    def _processData(self, data):
        """Dispatch a reassembled CLIPRDR PDU."""
        if len(data) < 8:
            return

        # CLIPRDR_HEADER: msgType(2) + msgFlags(2) + dataLen(4)
        msgType = struct.unpack_from('<H', data, 0)[0]
        msgFlags = struct.unpack_from('<H', data, 2)[0]
        dataLen = struct.unpack_from('<I', data, 4)[0]
        body = data[8:8 + dataLen]

        log.debug("CLIPRDR: recv %s(0x%04x) flags=0x%04x dataLen=%d" %
                 (_MSG_NAMES.get(msgType, "?"), msgType, msgFlags, dataLen))

        if msgType == CB_CLIP_CAPS:
            self._processClipCaps(body)
        elif msgType == CB_MONITOR_READY:
            self._processMonitorReady(body)
        elif msgType == CB_FORMAT_LIST:
            self._processFormatList(body, msgFlags)
        elif msgType == CB_FORMAT_LIST_RESPONSE:
            self._processFormatListResponse(msgFlags)
        elif msgType == CB_FORMAT_DATA_REQUEST:
            self._processFormatDataRequest(body)
        elif msgType == CB_FORMAT_DATA_RESPONSE:
            self._processFormatDataResponse(body, msgFlags)
        elif msgType == CB_LOCK_CLIPDATA:
            pass
        elif msgType == CB_UNLOCK_CLIPDATA:
            pass
        else:
            log.debug("CLIPRDR: unhandled msgType=0x%04x" % msgType)

    # -------------------------------------------------------------------
    # Clipboard Capabilities (MS-RDPECLIP 2.2.2.1)
    # -------------------------------------------------------------------

    def _processClipCaps(self, body):
        """Parse Clipboard Capabilities PDU from server."""
        if len(body) < 4:
            return
        cCapabilitySets = struct.unpack_from('<H', body, 0)[0]
        # pad1(2)
        offset = 4
        for _ in range(cCapabilitySets):
            if offset + 4 > len(body):
                break
            capType = struct.unpack_from('<H', body, offset)[0]
            capLen = struct.unpack_from('<H', body, offset + 2)[0]
            if capType == CB_CAPSTYPE_GENERAL and capLen >= 12:
                # version(4) + generalFlags(4)
                self._serverGeneralFlags = struct.unpack_from('<I', body, offset + 8)[0]
                self._useLongFormatNames = bool(
                    self._serverGeneralFlags & CB_USE_LONG_FORMAT_NAMES)
                log.debug("CLIPRDR: server generalFlags=0x%08x longNames=%s" %
                         (self._serverGeneralFlags, self._useLongFormatNames))
            offset += capLen

    def _sendClipCaps(self):
        """Send Clipboard Capabilities PDU to server."""
        # General capability set: capabilitySetType(2) + lengthCapability(2) + version(4) + generalFlags(4)
        generalFlags = CB_USE_LONG_FORMAT_NAMES
        capSet = struct.pack('<HH', CB_CAPSTYPE_GENERAL, 12)
        capSet += struct.pack('<I', 2)  # version 2
        capSet += struct.pack('<I', generalFlags)

        # cCapabilitySets(2) + pad1(2) + capabilitySet
        body = struct.pack('<HH', 1, 0) + capSet
        self._sendPDU(CB_CLIP_CAPS, 0, body)
        log.debug("CLIPRDR: sent Clip Caps")

    # -------------------------------------------------------------------
    # Monitor Ready (MS-RDPECLIP 2.2.2.2)
    # -------------------------------------------------------------------

    def _processMonitorReady(self, body):
        """Handle Monitor Ready PDU: server is ready for clipboard exchange."""
        log.debug("CLIPRDR: server Monitor Ready")
        # Respond with our capabilities and an initial format list
        self._sendClipCaps()
        self._sendFormatList()

    # -------------------------------------------------------------------
    # Format List (MS-RDPECLIP 2.2.3.1)
    # -------------------------------------------------------------------

    def _sendFormatList(self):
        """Send Format List PDU advertising CF_UNICODETEXT."""
        if self._useLongFormatNames:
            # Long Format Name: formatId(4) + wszFormatName (null-terminated UTF-16LE)
            body = struct.pack('<I', CF_UNICODETEXT)
            body += '\0'.encode('utf-16-le')  # empty name = standard format
        else:
            # Short Format Name: formatId(4) + formatName[32]
            body = struct.pack('<I', CF_UNICODETEXT)
            body += b'\x00' * 32
        self._sendPDU(CB_FORMAT_LIST, 0, body)
        log.debug("CLIPRDR: sent Format List (CF_UNICODETEXT)")

    def _processFormatList(self, body, msgFlags):
        """Handle Format List PDU from server (server clipboard changed)."""
        # Parse formats offered by the server
        formats = self._parseFormatList(body, msgFlags)
        log.debug("CLIPRDR: server Format List: %s" % formats)

        # Always respond with OK
        self._sendPDU(CB_FORMAT_LIST_RESPONSE, CB_RESPONSE_OK, b'')

        # Request text data if available
        requestFormatId = None
        for fmtId, fmtName in formats:
            if fmtId == CF_UNICODETEXT:
                requestFormatId = CF_UNICODETEXT
                break
            elif fmtId == CF_TEXT:
                requestFormatId = CF_TEXT
                break

        if requestFormatId is not None:
            self._sendFormatDataRequest(requestFormatId)

    def _parseFormatList(self, body, msgFlags):
        """Parse format list entries. Returns list of (formatId, formatName)."""
        formats = []
        if self._useLongFormatNames and not (msgFlags & CB_ASCII_NAMES):
            # Long Format Names (MS-RDPECLIP 2.2.3.1.1.1)
            offset = 0
            while offset + 4 <= len(body):
                fmtId = struct.unpack_from('<I', body, offset)[0]
                offset += 4
                # Read null-terminated UTF-16LE string
                nameEnd = offset
                while nameEnd + 1 < len(body):
                    if body[nameEnd] == 0 and body[nameEnd + 1] == 0:
                        break
                    nameEnd += 2
                name = body[offset:nameEnd].decode('utf-16-le', errors='replace')
                offset = nameEnd + 2  # skip null terminator
                formats.append((fmtId, name))
        else:
            # Short Format Names (MS-RDPECLIP 2.2.3.1.1.2)
            offset = 0
            while offset + 36 <= len(body):
                fmtId = struct.unpack_from('<I', body, offset)[0]
                nameBytes = body[offset + 4:offset + 36]
                if msgFlags & CB_ASCII_NAMES:
                    name = nameBytes.split(b'\x00')[0].decode('ascii', errors='replace')
                else:
                    name = nameBytes.decode('utf-16-le', errors='replace').split('\x00')[0]
                formats.append((fmtId, name))
                offset += 36
        return formats

    def _processFormatListResponse(self, msgFlags):
        """Handle Format List Response (acknowledgement from server)."""
        if msgFlags & CB_RESPONSE_OK:
            log.debug("CLIPRDR: Format List Response OK")
        else:
            log.warning("CLIPRDR: Format List Response FAIL")

    # -------------------------------------------------------------------
    # Format Data Request / Response (MS-RDPECLIP 2.2.5)
    # -------------------------------------------------------------------

    def _sendFormatDataRequest(self, formatId):
        """Send Format Data Request to server."""
        body = struct.pack('<I', formatId)
        self._sendPDU(CB_FORMAT_DATA_REQUEST, 0, body)
        log.debug("CLIPRDR: sent Format Data Request formatId=%d" % formatId)

    def _processFormatDataRequest(self, body):
        """Handle Format Data Request from server (server wants our clipboard)."""
        if len(body) < 4:
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_FAIL, b'')
            return

        requestedFormat = struct.unpack_from('<I', body, 0)[0]
        log.debug("CLIPRDR: server requests format %d" % requestedFormat)

        text = ''
        if self._getLocalClipboardText is not None:
            text = self._getLocalClipboardText() or ''

        # Cap clipboard text to a reasonable maximum (1 MB) to avoid
        # excessive memory use and network traffic.
        _MAX_CLIP_BYTES = 1024 * 1024

        if requestedFormat == CF_UNICODETEXT:
            encoded = (text + '\0').encode('utf-16-le')
            if len(encoded) > _MAX_CLIP_BYTES:
                log.warning("CLIPRDR: clipboard text too large (%d bytes), truncating" % len(encoded))
                # Truncate to an even byte boundary (UTF-16LE)
                encoded = encoded[:_MAX_CLIP_BYTES & ~1]
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_OK, encoded)
        elif requestedFormat == CF_TEXT:
            encoded = (text + '\0').encode('ascii', errors='replace')
            if len(encoded) > _MAX_CLIP_BYTES:
                log.warning("CLIPRDR: clipboard text too large (%d bytes), truncating" % len(encoded))
                encoded = encoded[:_MAX_CLIP_BYTES]
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_OK, encoded)
        else:
            self._sendPDU(CB_FORMAT_DATA_RESPONSE, CB_RESPONSE_FAIL, b'')

    def _processFormatDataResponse(self, body, msgFlags):
        """Handle Format Data Response from server (clipboard data received)."""
        if not (msgFlags & CB_RESPONSE_OK):
            log.warning("CLIPRDR: Format Data Response FAIL")
            return

        # Try to decode as UTF-16LE (CF_UNICODETEXT), strip null terminator
        try:
            text = body.decode('utf-16-le')
        except (UnicodeDecodeError, ValueError):
            # Fallback: try as plain text
            try:
                text = body.decode('utf-8', errors='replace')
            except Exception:
                text = body.decode('latin-1')

        # Strip null terminators
        text = text.rstrip('\x00')

        if text and self._onRemoteClipboardChanged is not None:
            log.debug("CLIPRDR: received text (%d chars)" % len(text))
            self._suppressNextLocalChange = True
            self._onRemoteClipboardChanged(text)

    # -------------------------------------------------------------------
    # Public API for local clipboard changes
    # -------------------------------------------------------------------

    def onLocalClipboardChanged(self):
        """Called when the local clipboard content changes.
        Sends a Format List to the server to advertise new content."""
        log.debug("CLIPRDR DEBUG: onLocalClipboardChanged() suppress=%s transport=%s" %
                 (self._suppressNextLocalChange,
                  type(self._transport).__name__ if self._transport else "None"))
        if self._suppressNextLocalChange:
            self._suppressNextLocalChange = False
            log.debug("CLIPRDR DEBUG: suppressed (echo prevention)")
            return
        if self._transport is not None:
            self._sendFormatList()
            log.debug("CLIPRDR: local clipboard changed, sent Format List")
        else:
            log.warning("CLIPRDR DEBUG: local clipboard changed but transport is None!")

    # -------------------------------------------------------------------
    # Send helpers
    # -------------------------------------------------------------------

    def _sendPDU(self, msgType, msgFlags, body):
        """Send a CLIPRDR PDU wrapped in VChannel header."""
        log.debug("CLIPRDR DEBUG: _sendPDU type=%s(0x%04x) flags=0x%04x bodyLen=%d" %
                 (_MSG_NAMES.get(msgType, "?"), msgType, msgFlags, len(body)))
        # CLIPRDR_HEADER: msgType(2) + msgFlags(2) + dataLen(4) + body
        cliprdrPdu = struct.pack('<HHI', msgType, msgFlags, len(body)) + body
        self._send(cliprdrPdu)

    # VChannel chunk size negotiated in Virtual Channel Capability Set
    _VC_CHUNK_SIZE = 1600

    def _send(self, data):
        """Send data via the static virtual channel, chunking if needed.

        RDP virtual channel data must be sent in chunks no larger than
        VCChunkSize (1600 bytes).  Each chunk carries a VChannel header
        with totalLength and FIRST/LAST flags.
        """
        log.debug("CLIPRDR DEBUG: _send() transport=%s dataLen=%d" %
                 (type(self._transport).__name__ if self._transport else "None", len(data)))
        if self._transport is None:
            log.warning("CLIPRDR DEBUG: _send() SKIPPED - transport is None!")
            return

        from rdpy.core.type import String
        totalLen = len(data)
        offset = 0
        while offset < totalLen:
            chunk = data[offset:offset + self._VC_CHUNK_SIZE]
            flags = CHANNEL_FLAG_SHOW_PROTOCOL
            if offset == 0:
                flags |= CHANNEL_FLAG_FIRST
            if offset + len(chunk) >= totalLen:
                flags |= CHANNEL_FLAG_LAST
            header = struct.pack('<II', totalLen, flags)
            self._transport.send(String(header + chunk))
            offset += len(chunk)
