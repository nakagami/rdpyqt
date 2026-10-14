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
RDPDR - Device Redirection Virtual Channel Extension (MS-RDPEFS).
Handles the "rdpdr" static virtual channel: the core initialization
sequence (announce, client name, capabilities, client id), the device
list announce and the dispatching of I/O request packets (IRP) from the
server to the redirected client devices.

Device implementations live in their own modules (see rdpdr_drive.py for
file system / drive redirection) and derive from RdpdrDevice.

Ported from FreeRDP channels/rdpdr/client (rdpdr_main.c, rdpdr_capabilities.c,
irp.c, devman.c).

Protocol reference: [MS-RDPEFS]
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-rdpefs/
"""

import os
import socket
import struct
import threading

from rdpy.core.layer import LayerAutomata
import rdpy.core.log as log

# Virtual Channel PDU flags (MS-RDPBCGR 2.2.6.1.1)
CHANNEL_FLAG_FIRST = 0x00000001
CHANNEL_FLAG_LAST = 0x00000002
CHANNEL_FLAG_SUSPEND = 0x00000020
CHANNEL_FLAG_RESUME = 0x00000040

# Protocol versions (MS-RDPEFS 2.2.2.2)
RDPDR_VERSION_MAJOR = 0x0001
RDPDR_VERSION_MINOR_RDP50 = 0x0002
RDPDR_VERSION_MINOR_RDP51 = 0x0005
RDPDR_VERSION_MINOR_RDP52 = 0x000A
RDPDR_VERSION_MINOR_RDP6X = 0x000C
RDPDR_VERSION_MINOR_RDP10X = 0x000D

# Header sizes
RDPDR_DEVICE_IO_REQUEST_LENGTH = 24
RDPDR_DEVICE_IO_RESPONSE_LENGTH = 16

# Component (MS-RDPEFS 2.2.1.1)
RDPDR_CTYP_CORE = 0x4472
RDPDR_CTYP_PRN = 0x5052

# Packet ids (MS-RDPEFS 2.2.1.1)
PAKID_CORE_SERVER_ANNOUNCE = 0x496E
PAKID_CORE_CLIENTID_CONFIRM = 0x4343
PAKID_CORE_CLIENT_NAME = 0x434E
PAKID_CORE_DEVICELIST_ANNOUNCE = 0x4441
PAKID_CORE_DEVICE_REPLY = 0x6472
PAKID_CORE_DEVICE_IOREQUEST = 0x4952
PAKID_CORE_DEVICE_IOCOMPLETION = 0x4943
PAKID_CORE_SERVER_CAPABILITY = 0x5350
PAKID_CORE_CLIENT_CAPABILITY = 0x4350
PAKID_CORE_DEVICELIST_REMOVE = 0x444D
PAKID_CORE_USER_LOGGEDON = 0x554C
PAKID_PRN_CACHE_DATA = 0x5043
PAKID_PRN_USING_XPS = 0x5543

# Capability types (MS-RDPEFS 2.2.1.2)
CAP_GENERAL_TYPE = 0x0001
CAP_PRINTER_TYPE = 0x0002
CAP_PORT_TYPE = 0x0003
CAP_DRIVE_TYPE = 0x0004
CAP_SMARTCARD_TYPE = 0x0005

# Capability versions
GENERAL_CAPABILITY_VERSION_01 = 0x00000001
GENERAL_CAPABILITY_VERSION_02 = 0x00000002
PRINT_CAPABILITY_VERSION_01 = 0x00000001
PORT_CAPABILITY_VERSION_01 = 0x00000001
DRIVE_CAPABILITY_VERSION_01 = 0x00000001
DRIVE_CAPABILITY_VERSION_02 = 0x00000002
SMARTCARD_CAPABILITY_VERSION_01 = 0x00000001

RDPDR_CAPABILITY_HEADER_LENGTH = 8

# General capability ioCode1 (MS-RDPEFS 2.2.2.7.1)
RDPDR_IRP_MJ_CREATE = 0x00000001
RDPDR_IRP_MJ_CLEANUP = 0x00000002
RDPDR_IRP_MJ_CLOSE = 0x00000004
RDPDR_IRP_MJ_READ = 0x00000008
RDPDR_IRP_MJ_WRITE = 0x00000010
RDPDR_IRP_MJ_FLUSH_BUFFERS = 0x00000020
RDPDR_IRP_MJ_SHUTDOWN = 0x00000040
RDPDR_IRP_MJ_DEVICE_CONTROL = 0x00000080
RDPDR_IRP_MJ_QUERY_VOLUME_INFORMATION = 0x00000100
RDPDR_IRP_MJ_SET_VOLUME_INFORMATION = 0x00000200
RDPDR_IRP_MJ_QUERY_INFORMATION = 0x00000400
RDPDR_IRP_MJ_SET_INFORMATION = 0x00000800
RDPDR_IRP_MJ_DIRECTORY_CONTROL = 0x00001000
RDPDR_IRP_MJ_LOCK_CONTROL = 0x00002000
RDPDR_IRP_MJ_QUERY_SECURITY = 0x00004000
RDPDR_IRP_MJ_SET_SECURITY = 0x00008000

# General capability extendedPDU
RDPDR_DEVICE_REMOVE_PDUS = 0x00000001
RDPDR_CLIENT_DISPLAY_NAME_PDU = 0x00000002
RDPDR_USER_LOGGEDON_PDU = 0x00000004

# General capability extraFlags1
ENABLE_ASYNCIO = 0x00000001

# Device types (MS-RDPEFS 2.2.1.3)
RDPDR_DTYP_SERIAL = 0x00000001
RDPDR_DTYP_PARALLEL = 0x00000002
RDPDR_DTYP_PRINT = 0x00000004
RDPDR_DTYP_FILESYSTEM = 0x00000008
RDPDR_DTYP_SMARTCARD = 0x00000020

# IRP major functions (MS-RDPEFS 2.2.1.4)
IRP_MJ_CREATE = 0x00000000
IRP_MJ_CLOSE = 0x00000002
IRP_MJ_READ = 0x00000003
IRP_MJ_WRITE = 0x00000004
IRP_MJ_QUERY_INFORMATION = 0x00000005
IRP_MJ_SET_INFORMATION = 0x00000006
IRP_MJ_QUERY_VOLUME_INFORMATION = 0x0000000A
IRP_MJ_SET_VOLUME_INFORMATION = 0x0000000B
IRP_MJ_DIRECTORY_CONTROL = 0x0000000C
IRP_MJ_DEVICE_CONTROL = 0x0000000E
IRP_MJ_LOCK_CONTROL = 0x00000011

# IRP minor functions
IRP_MN_QUERY_DIRECTORY = 0x00000001
IRP_MN_NOTIFY_CHANGE_DIRECTORY = 0x00000002

# NTSTATUS values used by the channel and the devices ([MS-ERREF] 2.3)
STATUS_SUCCESS = 0x00000000
STATUS_NO_MORE_FILES = 0x80000006
STATUS_DEVICE_BUSY = 0x80000011
STATUS_UNSUCCESSFUL = 0xC0000001
STATUS_INVALID_HANDLE = 0xC0000008
STATUS_INVALID_PARAMETER = 0xC000000D
STATUS_NO_SUCH_DEVICE = 0xC000000E
STATUS_NO_SUCH_FILE = 0xC000000F
STATUS_ACCESS_DENIED = 0xC0000022
STATUS_OBJECT_NAME_INVALID = 0xC0000033
STATUS_OBJECT_NAME_COLLISION = 0xC0000035
STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
STATUS_DISK_FULL = 0xC000007F
STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
STATUS_NOT_SUPPORTED = 0xC00000BB
STATUS_DIRECTORY_NOT_EMPTY = 0xC0000101
STATUS_NOT_A_DIRECTORY = 0xC0000103

# Channel state machine (FreeRDP enum RDPDR_CHANNEL_STATE)
STATE_INITIAL = 0
STATE_ANNOUNCE = 1
STATE_ANNOUNCE_REPLY = 2
STATE_NAME_REQUEST = 3
STATE_SERVER_CAPS = 4
STATE_CLIENT_CAPS = 5
STATE_CLIENTID_CONFIRM = 6
STATE_READY = 7
STATE_USER_LOGGEDON = 8

_STATE_NAMES = {
    STATE_INITIAL: "INITIAL",
    STATE_ANNOUNCE: "ANNOUNCE",
    STATE_ANNOUNCE_REPLY: "ANNOUNCE_REPLY",
    STATE_NAME_REQUEST: "NAME_REQUEST",
    STATE_SERVER_CAPS: "SERVER_CAPS",
    STATE_CLIENT_CAPS: "CLIENT_CAPS",
    STATE_CLIENTID_CONFIRM: "CLIENTID_CONFIRM",
    STATE_READY: "READY",
    STATE_USER_LOGGEDON: "USER_LOGGEDON",
}

_PACKET_NAMES = {
    PAKID_CORE_SERVER_ANNOUNCE: "SERVER_ANNOUNCE",
    PAKID_CORE_CLIENTID_CONFIRM: "CLIENTID_CONFIRM",
    PAKID_CORE_CLIENT_NAME: "CLIENT_NAME",
    PAKID_CORE_DEVICELIST_ANNOUNCE: "DEVICELIST_ANNOUNCE",
    PAKID_CORE_DEVICE_REPLY: "DEVICE_REPLY",
    PAKID_CORE_DEVICE_IOREQUEST: "DEVICE_IOREQUEST",
    PAKID_CORE_DEVICE_IOCOMPLETION: "DEVICE_IOCOMPLETION",
    PAKID_CORE_SERVER_CAPABILITY: "SERVER_CAPABILITY",
    PAKID_CORE_CLIENT_CAPABILITY: "CLIENT_CAPABILITY",
    PAKID_CORE_DEVICELIST_REMOVE: "DEVICELIST_REMOVE",
    PAKID_CORE_USER_LOGGEDON: "USER_LOGGEDON",
    PAKID_PRN_CACHE_DATA: "PRN_CACHE_DATA",
    PAKID_PRN_USING_XPS: "PRN_USING_XPS",
}

_DEVICE_TYPE_NAMES = {
    RDPDR_DTYP_SERIAL: "serial",
    RDPDR_DTYP_PARALLEL: "parallel",
    RDPDR_DTYP_PRINT: "printer",
    RDPDR_DTYP_FILESYSTEM: "drive",
    RDPDR_DTYP_SMARTCARD: "smartcard",
}


class StreamReader(object):
    """
    @summary: Minimal little-endian reader over a bytes buffer.
    Raises ValueError when a read goes past the end of the buffer.
    """

    def __init__(self, data, offset=0):
        self._data = bytes(data)
        self._pos = offset

    def remaining(self):
        return len(self._data) - self._pos

    def tell(self):
        return self._pos

    def _need(self, n):
        if n < 0 or self._pos + n > len(self._data):
            raise ValueError("rdpdr: truncated PDU (need %d bytes, have %d)" % (n, self.remaining()))

    def u8(self):
        self._need(1)
        v = self._data[self._pos]
        self._pos += 1
        return v

    def u16(self):
        self._need(2)
        v = struct.unpack_from('<H', self._data, self._pos)[0]
        self._pos += 2
        return v

    def u32(self):
        self._need(4)
        v = struct.unpack_from('<I', self._data, self._pos)[0]
        self._pos += 4
        return v

    def u64(self):
        self._need(8)
        v = struct.unpack_from('<Q', self._data, self._pos)[0]
        self._pos += 8
        return v

    def i64(self):
        self._need(8)
        v = struct.unpack_from('<q', self._data, self._pos)[0]
        self._pos += 8
        return v

    def read(self, n):
        self._need(n)
        v = self._data[self._pos:self._pos + n]
        self._pos += n
        return v

    def skip(self, n):
        self._need(n)
        self._pos += n


def decodeUnicodeString(raw):
    """
    @summary: Decode a UTF-16LE string sent by the server, stopping at the
    first null terminator.
    @param raw: {bytes}
    @return: {str}
    """
    if len(raw) & 1:
        raw = raw[:-1]
    return raw.decode('utf-16-le', errors='replace').split('\x00', 1)[0]


class Irp(object):
    """
    @summary: I/O request packet (DR_DEVICE_IOREQUEST) received from the server.

    The device writes its response body into `output` and sets `ioStatus`,
    then calls complete(). complete() may be called from any thread: the
    response is always sent from the reactor thread.
    """

    def __init__(self, layer, device, fileId, completionId, majorFunction, minorFunction,
                 input, generation):
        self._layer = layer
        self.device = device
        self.deviceId = device.id
        self.fileId = fileId
        self.completionId = completionId
        self.majorFunction = majorFunction
        self.minorFunction = minorFunction
        # StreamReader positioned on the request specific parameters
        self.input = input
        # response body, appended after the DR_DEVICE_IOCOMPLETION header
        self.output = bytearray()
        self.ioStatus = STATUS_SUCCESS
        self._generation = generation
        self._done = False

    def complete(self):
        """
        @summary: Send the DR_DEVICE_IOCOMPLETION for this IRP to the server
        """
        if self._done:
            return
        self._done = True
        self._layer._completeIrp(self, self._generation)

    def discard(self):
        """
        @summary: Drop the IRP without answering (FreeRDP irp->Discard)
        """
        self._done = True


class RdpdrDevice(object):
    """
    @summary: Base class for a redirected device (FreeRDP DEVICE).

    Subclasses must set deviceType, name and deviceData and override
    irpRequest(). The id is assigned by the DeviceManager.
    """

    def __init__(self, deviceType, name, deviceData=b''):
        """
        @param deviceType: {int} RDPDR_DTYP_*
        @param name: {str} preferred DOS name (only 7 ASCII characters are sent)
        @param deviceData: {bytes} device specific announce data
        """
        self.deviceType = deviceType
        self.name = name
        self.deviceData = deviceData
        self.id = 0
        self.devman = None

    def init(self):
        """
        @summary: Called when the server (re)starts the channel initialization
        """
        pass

    def irpRequest(self, irp):
        """
        @summary: Handle an IRP. The default answers STATUS_NOT_SUPPORTED.
        @param irp: {Irp}
        """
        irp.ioStatus = STATUS_NOT_SUPPORTED
        irp.complete()

    def customComponentRequest(self, component, packetId, s):
        """
        @summary: Handle a non-core component PDU (e.g. printer)
        @return: {bool} True if handled
        """
        return False

    def free(self):
        """
        @summary: Release device resources (connection closed or device removed)
        """
        pass


class DeviceManager(object):
    """
    @summary: Registry of redirected devices (FreeRDP devman.c).

    FreeRDP uses one id sequence for device ids and file ids, which is
    reproduced here (nextId is thread safe because drive workers allocate
    file ids from their own thread).
    """

    def __init__(self):
        self._devices = {}
        self._idSequence = 1
        self._lock = threading.Lock()

    def nextId(self):
        with self._lock:
            value = self._idSequence
            self._idSequence = (self._idSequence + 1) & 0xFFFFFFFF or 1
            return value

    def registerDevice(self, device):
        device.id = self.nextId()
        device.devman = self
        self._devices[device.id] = device
        return device.id

    def unregisterDevice(self, deviceId):
        device = self._devices.pop(deviceId, None)
        if device is not None:
            device.free()
        return device

    def getDeviceById(self, deviceId):
        return self._devices.get(deviceId)

    def getDeviceByType(self, deviceType):
        for device in self._devices.values():
            if device.deviceType == deviceType:
                return device
        return None

    def devices(self):
        return list(self._devices.values())

    def hasType(self, deviceType):
        return self.getDeviceByType(deviceType) is not None

    def freeAll(self):
        for deviceId in list(self._devices.keys()):
            self.unregisterDevice(deviceId)


class RdpdrLayer(LayerAutomata):
    """
    @summary: RDPDR static virtual channel layer (MS-RDPEFS client side).

    Usage: register devices with addDevice() / addDrive() before the
    connection is established; the layer announces them to the server once
    the user is logged on and dispatches the server I/O requests to them.
    """

    # VChannel chunk size negotiated in Virtual Channel Capability Set
    _VC_CHUNK_SIZE = 1600

    def __init__(self):
        LayerAutomata.__init__(self, None)
        self._vchanBuf = bytearray()
        self._vchanTotal = 0
        self._firstFlagReceived = False

        self._devman = DeviceManager()
        self._state = STATE_INITIAL

        self._serverVersionMajor = 0
        self._serverVersionMinor = 0
        self._serverOsType = 0
        self._serverOsVersion = 0
        self._serverIOCode1 = 0
        self._serverIOCode2 = 0
        self._serverExtendedPDU = 0
        self._serverExtraFlags1 = 0
        self._serverExtraFlags2 = 0
        self._serverSpecialTypeDeviceCap = 0
        self._serverCapabilities = set()

        self._clientVersionMajor = RDPDR_VERSION_MAJOR
        self._clientVersionMinor = RDPDR_VERSION_MINOR_RDP10X
        self._clientOsType = 0
        self._clientOsVersion = 0
        self._clientExtendedPDU = (RDPDR_DEVICE_REMOVE_PDUS | RDPDR_CLIENT_DISPLAY_NAME_PDU |
                                   RDPDR_USER_LOGGEDON_PDU)
        self._clientIOCode1 = (
            RDPDR_IRP_MJ_CREATE | RDPDR_IRP_MJ_CLEANUP | RDPDR_IRP_MJ_CLOSE | RDPDR_IRP_MJ_READ |
            RDPDR_IRP_MJ_WRITE | RDPDR_IRP_MJ_FLUSH_BUFFERS | RDPDR_IRP_MJ_SHUTDOWN |
            RDPDR_IRP_MJ_DEVICE_CONTROL | RDPDR_IRP_MJ_QUERY_VOLUME_INFORMATION |
            RDPDR_IRP_MJ_SET_VOLUME_INFORMATION | RDPDR_IRP_MJ_QUERY_INFORMATION |
            RDPDR_IRP_MJ_SET_INFORMATION | RDPDR_IRP_MJ_DIRECTORY_CONTROL |
            RDPDR_IRP_MJ_LOCK_CONTROL | RDPDR_IRP_MJ_QUERY_SECURITY | RDPDR_IRP_MJ_SET_SECURITY)
        self._clientIOCode2 = 0
        self._clientExtraFlags1 = ENABLE_ASYNCIO
        self._clientExtraFlags2 = 0
        self._clientSpecialTypeDeviceCap = 0

        self._clientID = 0
        self._computerName = (socket.gethostname() or "rdpyqt").split('.')[0][:15] or "rdpyqt"
        self._userLoggedOn = False
        self._haveClientId = False
        self._haveServerCaps = False

        # Incremented each time the server restarts the channel; IRP
        # completions issued for an older generation are dropped (FreeRDP
        # "clearing" flag).
        self._generation = 0
        self._closed = False

        # Hook used to run completions on the reactor thread. Overridable for tests.
        self._callFromReactor = None

    # -------------------------------------------------------------------
    # Configuration API
    # -------------------------------------------------------------------

    def setComputerName(self, name):
        """
        @summary: Name shown by the server for this client ("<drive> on <name>")
        @param name: {str}
        """
        if name:
            self._computerName = name.split('.')[0][:15]

    def addDevice(self, device):
        """
        @summary: Register a redirected device
        @param device: {RdpdrDevice}
        @return: {int} device id
        """
        deviceId = self._devman.registerDevice(device)
        log.info("RDPDR: registered [%s] device '%s' (id=%d)" %
                 (_DEVICE_TYPE_NAMES.get(device.deviceType, "?"), device.name, deviceId))
        if self._state in (STATE_READY, STATE_USER_LOGGEDON) and self._transport is not None:
            # hotplug: device added while the session is running
            self._sendDeviceListAnnounce(self._userLoggedOn)
        return deviceId

    def addDrive(self, name, path, asyncIo=True):
        """
        @summary: Redirect a local directory as a drive (FreeRDP /drive:name,path)
        @param name: {str} drive name shown on the server
        @param path: {str} local directory
        @return: {int} device id
        """
        from . import rdpdr_drive
        return self.addDevice(rdpdr_drive.DriveDevice(name, path, asyncIo=asyncIo))

    def removeDevice(self, deviceId):
        """
        @summary: Remove a device and notify the server (Device List Remove)
        @param deviceId: {int}
        """
        if self._devman.unregisterDevice(deviceId) is None:
            return
        if (self._transport is not None and self._state in (STATE_READY, STATE_USER_LOGGEDON) and
                self._serverExtendedPDU & RDPDR_DEVICE_REMOVE_PDUS):
            body = struct.pack('<HHII', RDPDR_CTYP_CORE, PAKID_CORE_DEVICELIST_REMOVE, 1, deviceId)
            self._send(body)

    def getDevices(self):
        return self._devman.devices()

    def hasDevices(self):
        return len(self._devman.devices()) > 0

    def shutdown(self):
        """
        @summary: Release every device (stop worker threads, close files)
        """
        self._closed = True
        self._generation += 1
        self._devman.freeAll()

    # -------------------------------------------------------------------
    # Layer events
    # -------------------------------------------------------------------

    def connect(self):
        log.debug("RdpdrLayer.connect() devices=%d" % len(self._devman.devices()))

    def close(self):
        self.shutdown()
        LayerAutomata.close(self)

    def recv(self, s):
        """
        @summary: Receive data on the rdpdr static virtual channel with VChannel reassembly
        """
        data = s.read()
        if len(data) < 8:
            log.warning("RDPDR: recv data too short (%d bytes)" % len(data))
            return
        totalLen, flags = struct.unpack_from('<II', data, 0)
        payload = data[8:]

        if flags & (CHANNEL_FLAG_SUSPEND | CHANNEL_FLAG_RESUME):
            # MS-RDPBCGR 2.2.6.1: suspend / resume carry no channel data
            return

        if flags & CHANNEL_FLAG_FIRST:
            self._vchanBuf = bytearray(payload)
            self._vchanTotal = totalLen
            self._firstFlagReceived = True
        elif self._firstFlagReceived:
            self._vchanBuf += payload
        else:
            log.warning("RDPDR: chunk without CHANNEL_FLAG_FIRST, dropped")
            return

        if len(self._vchanBuf) > self._vchanTotal:
            log.warning("RDPDR: reassembled data larger than announced length, dropped")
            self._vchanBuf = bytearray()
            self._firstFlagReceived = False
            return

        if flags & CHANNEL_FLAG_LAST:
            self._firstFlagReceived = False
            pdu = bytes(self._vchanBuf)
            self._vchanBuf = bytearray()
            if len(pdu) != self._vchanTotal:
                log.warning("RDPDR: reassembled %d bytes, expected %d" % (len(pdu), self._vchanTotal))
                return
            try:
                self._processReceive(pdu)
            except ValueError as e:
                log.warning("RDPDR: %s" % e)

    # -------------------------------------------------------------------
    # Receive dispatch (rdpdr_process_receive)
    # -------------------------------------------------------------------

    def _processReceive(self, data):
        if len(data) < 4:
            return
        s = StreamReader(data)
        component = s.u16()
        packetId = s.u16()
        log.debug("RDPDR: recv component=0x%04x %s(0x%04x) len=%d" %
                  (component, _PACKET_NAMES.get(packetId, "?"), packetId, len(data)))

        if component != RDPDR_CTYP_CORE:
            self._processComponent(component, packetId, s)
            return

        if not self._checkChannelState(packetId):
            return

        if packetId == PAKID_CORE_SERVER_ANNOUNCE:
            self._haveClientId = False
            self._haveServerCaps = False
            self._processServerAnnounce(s)
            self._sendClientAnnounceReply()
            self._sendClientNameRequest()
            self._processInit()

        elif packetId == PAKID_CORE_SERVER_CAPABILITY:
            self._processCapabilityRequest(s)
            self._sendCapabilityResponse()
            self._haveServerCaps = True
            self._tryAdvance(True)

        elif packetId == PAKID_CORE_CLIENTID_CONFIRM:
            self._processServerClientIdConfirm(s)
            self._haveClientId = True
            self._tryAdvance(True)

        elif packetId == PAKID_CORE_USER_LOGGEDON:
            if not self._haveServerCaps:
                # Windows re-announces the channel after logon and may send
                # USER_LOGGEDON before the new SERVER_CAPABILITY arrives;
                # the announce is sent by _tryAdvance() later.
                log.warning("RDPDR: USER_LOGGEDON in state %s before server caps, ignoring" %
                            _STATE_NAMES.get(self._state))
                self._userLoggedOn = True
            else:
                self._sendDeviceListAnnounce(True)
                self._tryAdvance(False)

        elif packetId == PAKID_CORE_DEVICE_REPLY:
            if s.remaining() >= 8:
                deviceId = s.u32()
                status = s.u32()
                if status != STATUS_SUCCESS:
                    log.warning("RDPDR: server rejected device %d (status=0x%08x)" % (deviceId, status))
                    self._devman.unregisterDevice(deviceId)
                else:
                    log.debug("RDPDR: server accepted device %d" % deviceId)

        elif packetId == PAKID_CORE_DEVICE_IOREQUEST:
            self._processIrp(data)

        else:
            log.warning("RDPDR: RDPDR_CTYP_CORE unknown PacketId 0x%04x" % packetId)

    def _processComponent(self, component, packetId, s):
        """
        @summary: Non-core component PDU (only printer exists, FreeRDP rdpdr_process_component)
        """
        if component == RDPDR_CTYP_PRN:
            device = self._devman.getDeviceByType(RDPDR_DTYP_PRINT)
            if device is not None and device.customComponentRequest(component, packetId, s):
                return
        log.debug("RDPDR: unhandled component 0x%04x packetId 0x%04x" % (component, packetId))

    # -------------------------------------------------------------------
    # State machine (rdpdr_check_channel_state)
    # -------------------------------------------------------------------

    def _stateAdvance(self, nextState):
        if nextState != self._state:
            log.debug("RDPDR: state %s -> %s" %
                      (_STATE_NAMES.get(self._state), _STATE_NAMES.get(nextState)))
        self._state = nextState

    def _stateCheck(self, packetId, nextState, allowed):
        if self._state not in allowed:
            log.error("RDPDR: received %s, expected states [%s] but have state %s, aborting." %
                      (_PACKET_NAMES.get(packetId, hex(packetId)),
                       "|".join(_STATE_NAMES.get(x, "?") for x in allowed),
                       _STATE_NAMES.get(self._state)))
            self._stateAdvance(STATE_INITIAL)
            return False
        self._stateAdvance(nextState)
        return True

    def _checkExtendedPduFlag(self, flag):
        client = (self._clientExtendedPDU & flag) != 0
        server = (self._serverExtendedPDU & flag) != 0
        if not client or not server:
            log.warning("RDPDR: extended PDU flag 0x%x not supported by client=%s server=%s" %
                        (flag, client, server))
            return False
        return True

    def _checkChannelState(self, packetId):
        if packetId == PAKID_CORE_SERVER_ANNOUNCE:
            # windows servers send this again after login to reinitialize the channel
            self._stateAdvance(STATE_INITIAL)
            return self._stateCheck(packetId, STATE_ANNOUNCE, (STATE_INITIAL,))
        if packetId == PAKID_CORE_SERVER_CAPABILITY:
            return self._stateCheck(packetId, STATE_SERVER_CAPS, (
                STATE_NAME_REQUEST, STATE_SERVER_CAPS, STATE_READY, STATE_CLIENT_CAPS,
                STATE_CLIENTID_CONFIRM, STATE_USER_LOGGEDON))
        if packetId == PAKID_CORE_CLIENTID_CONFIRM:
            return self._stateCheck(packetId, STATE_CLIENTID_CONFIRM, (
                STATE_NAME_REQUEST, STATE_SERVER_CAPS, STATE_CLIENT_CAPS, STATE_READY,
                STATE_USER_LOGGEDON))
        if packetId == PAKID_CORE_USER_LOGGEDON:
            if not self._checkExtendedPduFlag(RDPDR_USER_LOGGEDON_PDU):
                return False
            return self._stateCheck(packetId, STATE_USER_LOGGEDON, (
                STATE_NAME_REQUEST, STATE_CLIENT_CAPS, STATE_CLIENTID_CONFIRM, STATE_READY))
        return self._stateCheck(packetId, STATE_READY, (STATE_READY,))

    def _tryAdvance(self, announce):
        if self._haveClientId and self._haveServerCaps:
            if announce:
                self._sendDeviceListAnnounce(self._userLoggedOn)
            self._stateAdvance(STATE_READY)

    # -------------------------------------------------------------------
    # Server Announce / Client Announce Reply / Client Name (MS-RDPEFS 2.2.2.2-2.2.2.4)
    # -------------------------------------------------------------------

    def _processServerAnnounce(self, s):
        self._serverVersionMajor = s.u16()
        self._serverVersionMinor = s.u16()
        receivedId = s.u32()

        # Mirroring the server supplied id breaks the channel for Windows XP:
        # use a random id for old servers ([MS-RDPEFS] 3.2.5.1.3)
        clientId = receivedId
        if (self._serverVersionMajor == RDPDR_VERSION_MAJOR and
                self._serverVersionMinor < RDPDR_VERSION_MINOR_RDP6X):
            while clientId in (0, 0xFFFFFFFF, receivedId):
                clientId = struct.unpack('<I', os.urandom(4))[0]
        self._clientID = clientId

        self._clientVersionMajor = min(RDPDR_VERSION_MAJOR, self._serverVersionMajor)
        self._clientVersionMinor = min(RDPDR_VERSION_MINOR_RDP10X, self._serverVersionMinor)
        log.debug("RDPDR: server announces version %d.%d, client uses %d.%d" %
                  (self._serverVersionMajor, self._serverVersionMinor,
                   self._clientVersionMajor, self._clientVersionMinor))

    def _sendClientAnnounceReply(self):
        self._stateAdvance(STATE_ANNOUNCE_REPLY)
        self._send(struct.pack('<HHHHI', RDPDR_CTYP_CORE, PAKID_CORE_CLIENTID_CONFIRM,
                               self._clientVersionMajor, self._clientVersionMinor,
                               self._clientID))

    def _sendClientNameRequest(self):
        self._stateAdvance(STATE_NAME_REQUEST)
        nameW = (self._computerName + '\0').encode('utf-16-le')
        pdu = struct.pack('<HHIII', RDPDR_CTYP_CORE, PAKID_CORE_CLIENT_NAME,
                          1,          # unicodeFlag
                          0,          # codePage, must be zero
                          len(nameW))  # computerNameLen including null terminator
        self._send(pdu + nameW)

    def _processInit(self):
        """
        @summary: (Re)initialize devices after a Server Announce (rdpdr_process_init)
        """
        self._userLoggedOn = False
        # discard the completions of IRPs issued before the re-announce
        self._generation += 1
        for device in self._devman.devices():
            try:
                device.init()
            except Exception as e:
                log.error("RDPDR: device %d init failed: %s" % (device.id, e))

    def _processServerClientIdConfirm(self, s):
        versionMajor = s.u16()
        versionMinor = s.u16()
        clientId = s.u32()
        if versionMajor != self._clientVersionMajor or versionMinor != self._clientVersionMinor:
            log.warning("RDPDR: clientid confirm requests version %d.%d (client used %d.%d)" %
                        (versionMajor, versionMinor, self._clientVersionMajor,
                         self._clientVersionMinor))
            self._clientVersionMajor = versionMajor
            self._clientVersionMinor = versionMinor
        self._clientID = clientId

    # -------------------------------------------------------------------
    # Capabilities (MS-RDPEFS 2.2.2.7 / 2.2.2.8)
    # -------------------------------------------------------------------

    def _processCapabilityRequest(self, s):
        self._stateAdvance(STATE_CLIENT_CAPS)
        numCapabilities = s.u16()
        s.skip(2)  # padding
        self._serverCapabilities = set()
        for _ in range(numCapabilities):
            capType = s.u16()
            capLen = s.u16()
            version = s.u32()
            if capLen < RDPDR_CAPABILITY_HEADER_LENGTH:
                raise ValueError("rdpdr: capability 0x%x has invalid length %d" % (capType, capLen))
            body = StreamReader(s.read(capLen - RDPDR_CAPABILITY_HEADER_LENGTH))
            self._serverCapabilities.add(capType)
            if capType == CAP_GENERAL_TYPE:
                self._processGeneralCapset(body, version)
            log.debug("RDPDR: server capability type=%d version=%d length=%d" %
                      (capType, version, capLen))

    def _processGeneralCapset(self, s, version):
        self._serverOsType = s.u32()
        self._serverOsVersion = s.u32()
        self._serverVersionMajor = s.u16()
        self._serverVersionMinor = s.u16()
        self._serverIOCode1 = s.u32()
        self._serverIOCode2 = s.u32()
        self._serverExtendedPDU = s.u32()
        self._serverExtraFlags1 = s.u32()
        self._serverExtraFlags2 = s.u32()
        if version == GENERAL_CAPABILITY_VERSION_01 or s.remaining() < 4:
            self._serverSpecialTypeDeviceCap = 0
        else:
            self._serverSpecialTypeDeviceCap = s.u32()
        log.debug("RDPDR: server general caps ioCode1=0x%08x extendedPDU=0x%08x extraFlags1=0x%08x" %
                  (self._serverIOCode1, self._serverExtendedPDU, self._serverExtraFlags1))

    @staticmethod
    def _capsetHeader(capType, length, version):
        return struct.pack('<HHI', capType, RDPDR_CAPABILITY_HEADER_LENGTH + length, version)

    def _generalCapset(self):
        ioCode1 = self._clientIOCode1 & self._serverIOCode1
        ioCode2 = self._clientIOCode2 & self._serverIOCode2
        return self._capsetHeader(CAP_GENERAL_TYPE, 36, GENERAL_CAPABILITY_VERSION_02) + struct.pack(
            '<IIHHIIIIII',
            self._clientOsType,          # osType, ignored on receipt
            self._clientOsVersion,       # osVersion, must be zero
            self._clientVersionMajor,    # protocolMajorVersion
            self._clientVersionMinor,    # protocolMinorVersion
            ioCode1,
            ioCode2,                     # reserved, must be zero
            self._clientExtendedPDU,
            self._clientExtraFlags1,
            self._clientExtraFlags2,     # reserved, must be zero
            self._clientSpecialTypeDeviceCap)

    def _sendCapabilityResponse(self):
        # Only send capabilities the server announced and we have devices for
        drives = self._devman.hasType(RDPDR_DTYP_FILESYSTEM) and CAP_DRIVE_TYPE in self._serverCapabilities
        serial = self._devman.hasType(RDPDR_DTYP_SERIAL) and CAP_PORT_TYPE in self._serverCapabilities
        parallel = self._devman.hasType(RDPDR_DTYP_PARALLEL) and CAP_PORT_TYPE in self._serverCapabilities
        smart = self._devman.hasType(RDPDR_DTYP_SMARTCARD) and CAP_SMARTCARD_TYPE in self._serverCapabilities
        printer = self._devman.hasType(RDPDR_DTYP_PRINT) and CAP_PRINTER_TYPE in self._serverCapabilities

        capsets = [self._generalCapset()]
        if printer:
            capsets.append(self._capsetHeader(CAP_PRINTER_TYPE, 0, PRINT_CAPABILITY_VERSION_01))
        if serial or parallel:
            capsets.append(self._capsetHeader(CAP_PORT_TYPE, 0, PORT_CAPABILITY_VERSION_01))
        if drives:
            capsets.append(self._capsetHeader(CAP_DRIVE_TYPE, 0, DRIVE_CAPABILITY_VERSION_02))
        if smart:
            capsets.append(self._capsetHeader(CAP_SMARTCARD_TYPE, 0, SMARTCARD_CAPABILITY_VERSION_01))

        pdu = struct.pack('<HHHH', RDPDR_CTYP_CORE, PAKID_CORE_CLIENT_CAPABILITY, len(capsets), 0)
        self._send(pdu + b''.join(capsets))

    # -------------------------------------------------------------------
    # Device list announce (MS-RDPEFS 2.2.2.9)
    # -------------------------------------------------------------------

    def _sendDeviceListAnnounce(self, userLoggedOn):
        if userLoggedOn:
            self._userLoggedOn = True
        loggedOn = userLoggedOn or self._userLoggedOn

        entries = []
        for device in self._devman.devices():
            # 1. versionMinor 0x0005 doesn't send PAKID_CORE_USER_LOGGEDON so
            #    all devices are sent regardless of the logon state
            # 2. smartcard devices are always sent
            # 3. other devices are sent only after the user logged on
            if not (self._clientVersionMinor == RDPDR_VERSION_MINOR_RDP51 or
                    device.deviceType == RDPDR_DTYP_SMARTCARD or loggedOn):
                continue
            dosName = bytes((c if c < 0x80 else 0x5F) for c in device.name.encode('ascii', 'replace')[:7])
            dosName = dosName.ljust(8, b'\x00')
            data = device.deviceData or b''
            entries.append(struct.pack('<II', device.deviceType, device.id) + dosName +
                           struct.pack('<I', len(data)) + data)
            log.info("RDPDR: announce [%s] device '%s' (type=%d id=%d)" %
                     (_DEVICE_TYPE_NAMES.get(device.deviceType, "?"), device.name,
                      device.deviceType, device.id))

        if not entries:
            return
        pdu = struct.pack('<HHI', RDPDR_CTYP_CORE, PAKID_CORE_DEVICELIST_ANNOUNCE, len(entries))
        self._send(pdu + b''.join(entries))

    # -------------------------------------------------------------------
    # I/O requests (irp.c / rdpdr_process_irp)
    # -------------------------------------------------------------------

    def _processIrp(self, data):
        s = StreamReader(data, 4)
        deviceId = s.u32()
        fileId = s.u32()
        completionId = s.u32()
        majorFunction = s.u32()
        minorFunction = s.u32()

        device = self._devman.getDeviceById(deviceId)
        if device is None:
            # FreeRDP dummy_irp_response (ignoreInvalidDevices)
            log.warning("RDPDR: IRP for unknown device %d (fileId=%d completionId=%d)" %
                        (deviceId, fileId, completionId))
            self._send(self._ioCompletionHeader(deviceId, completionId, STATUS_UNSUCCESSFUL))
            return

        irp = Irp(self, device, fileId, completionId, majorFunction, minorFunction, s,
                  self._generation)
        try:
            device.irpRequest(irp)
        except Exception as e:
            log.error("RDPDR: device %d IRP 0x%x failed: %s" % (deviceId, majorFunction, e))
            irp.output = bytearray()
            irp.ioStatus = STATUS_UNSUCCESSFUL
            irp.complete()

    @staticmethod
    def _ioCompletionHeader(deviceId, completionId, ioStatus):
        return struct.pack('<HHIII', RDPDR_CTYP_CORE, PAKID_CORE_DEVICE_IOCOMPLETION,
                           deviceId, completionId, ioStatus & 0xFFFFFFFF)

    def _completeIrp(self, irp, generation):
        """
        @summary: Called by Irp.complete() from any thread
        """
        pdu = self._ioCompletionHeader(irp.deviceId, irp.completionId, irp.ioStatus) + bytes(irp.output)
        if threading.current_thread() is threading.main_thread() and self._callFromReactor is None:
            self._sendCompletion(pdu, generation)
            return
        callFromReactor = self._callFromReactor
        if callFromReactor is None:
            from twisted.internet import reactor
            callFromReactor = reactor.callFromThread
        callFromReactor(self._sendCompletion, pdu, generation)

    def _sendCompletion(self, pdu, generation):
        if self._closed or generation != self._generation:
            log.debug("RDPDR: dropping stale IRP completion")
            return
        self._send(pdu)

    # -------------------------------------------------------------------
    # Send helpers
    # -------------------------------------------------------------------

    def _send(self, data):
        """
        @summary: Send data via the static virtual channel, chunked to VCChunkSize
        """
        if self._transport is None:
            log.warning("RDPDR: _send() skipped - transport is None")
            return

        from rdpy.core.type import String
        totalLen = len(data)
        offset = 0
        while offset < totalLen:
            chunk = data[offset:offset + self._VC_CHUNK_SIZE]
            flags = 0
            if offset == 0:
                flags |= CHANNEL_FLAG_FIRST
            if offset + len(chunk) >= totalLen:
                flags |= CHANNEL_FLAG_LAST
            self._transport.send(String(struct.pack('<II', totalLen, flags) + chunk))
            offset += len(chunk)
