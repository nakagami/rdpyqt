#
# Copyright (c) 2014-2015 Sylvain Peyrefitte
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
Implement the main graphic layer

In this layer are managed all mains bitmap update orders end user inputs
"""

from rdpy.core.layer import LayerAutomata
from rdpy.core.error import CallPureVirtualFuntion
from rdpy.core.type import ArrayType, Stream
import rdpy.core.log as log
import rdpy.protocol.rdp.tpkt as tpkt
from . import data, caps

class PDUClientListener(object):
    """
    @summary: Interface for PDU client automata listener
    """
    def onReady(self):
        """
        @summary: Event call when PDU layer is ready to send events
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onReady", "PDUClientListener"))
    
    def onSessionReady(self):
        """
        @summary: Event call when Windows session is ready
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onSessionReady", "PDUClientListener"))
    
    
    def onUpdate(self, rectangles):
        """
        @summary: call when a bitmap data is received from update PDU
        @param rectangles: [pdu.BitmapData] struct
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onUpdate", "PDUClientListener"))
    
    def onPointerHide(self):
        """
        @summary: call when server requests the pointer to be hidden
        """
        pass

    def onPointerCached(self, cacheIndex):
        """
        @summary: call when server switches to a previously cached pointer
        @param cacheIndex: index of cached pointer
        """
        pass

    def onPointerUpdate(self, xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask):
        """
        @summary: call when server sends a new pointer shape
        @param xorBpp: bits per pixel of XOR mask
        @param cacheIndex: cache index to store this pointer
        @param hotSpotX: hotspot X coordinate
        @param hotSpotY: hotspot Y coordinate
        @param width: pointer width
        @param height: pointer height
        @param andMask: AND mask data
        @param xorMask: XOR mask data
        """
        pass

    def recvDstBltOrder(self, order):
        """
        @param order: rectangle order
        """
        pass

class PDUServerListener(object):
    """
    @summary: Interface for PDU server automata listener
    """
    def onReady(self):
        """
        @summary: Event call when PDU layer is ready to send update
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onReady", "PDUServerListener"))
    
    def onSlowPathInput(self, slowPathInputEvents):
        """
        @summary: Event call when slow path input are available
        @param slowPathInputEvents: [data.SlowPathInputEvent]
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onSlowPathInput", "PDUServerListener"))
    
class PDULayer(LayerAutomata, tpkt.IFastPathListener):
    """
    @summary: Global channel for MCS that handle session
    identification user, licensing management, and capabilities exchange
    """
    def __init__(self):
        LayerAutomata.__init__(self, None)
        #server capabilities
        self._serverCapabilities = {
            caps.CapsType.CAPSTYPE_GENERAL : caps.Capability(caps.GeneralCapability()),
            caps.CapsType.CAPSTYPE_BITMAP : caps.Capability(caps.BitmapCapability()),
            caps.CapsType.CAPSTYPE_ORDER : caps.Capability(caps.OrderCapability()),
            caps.CapsType.CAPSTYPE_POINTER : caps.Capability(caps.PointerCapability(isServer = True)),
            caps.CapsType.CAPSTYPE_INPUT : caps.Capability(caps.InputCapability()),
            caps.CapsType.CAPSTYPE_VIRTUALCHANNEL : caps.Capability(caps.VirtualChannelCapability()),
            caps.CapsType.CAPSTYPE_FONT : caps.Capability(caps.FontCapability()),
            caps.CapsType.CAPSTYPE_COLORCACHE : caps.Capability(caps.ColorCacheCapability()),
            caps.CapsType.CAPSTYPE_SHARE : caps.Capability(caps.ShareCapability())
        }
        #client capabilities
        self._clientCapabilities = {
            caps.CapsType.CAPSTYPE_GENERAL : caps.Capability(caps.GeneralCapability()),
            caps.CapsType.CAPSTYPE_BITMAP : caps.Capability(caps.BitmapCapability()),
            caps.CapsType.CAPSTYPE_ORDER : caps.Capability(caps.OrderCapability()),
            caps.CapsType.CAPSTYPE_BITMAPCACHE_REV2 : caps.Capability(caps.BitmapCache2Capability()),
            caps.CapsType.CAPSTYPE_CONTROL : caps.Capability(caps.ControlCapability()),
            caps.CapsType.CAPSTYPE_ACTIVATION : caps.Capability(caps.WindowActivationCapability()),
            caps.CapsType.CAPSTYPE_POINTER : caps.Capability(caps.PointerCapability(isServer=True)),
            caps.CapsType.CAPSTYPE_SHARE : caps.Capability(caps.ShareCapability()),
            caps.CapsType.CAPSTYPE_COLORCACHE : caps.Capability(caps.ColorCacheCapability()),
            caps.CapsType.CAPSTYPE_INPUT : caps.Capability(caps.InputCapability()),
            caps.CapsType.CAPSTYPE_FONT : caps.Capability(caps.FontCapability()),
            caps.CapsType.CAPSTYPE_BRUSH : caps.Capability(caps.BrushCapability()),
            caps.CapsType.CAPSTYPE_GLYPHCACHE : caps.Capability(caps.GlyphCapability()),
            caps.CapsType.CAPSTYPE_VIRTUALCHANNEL : caps.Capability(caps.VirtualChannelCapability()),
            caps.CapsType.CAPSTYPE_SOUND : caps.Capability(caps.SoundCapability()),
            caps.CapsType.CAPSETTYPE_MULTIFRAGMENTUPDATE : caps.Capability(caps.MultiFragmentUpdate()),
            caps.CapsType.CAPSETTYPE_COMPDESK : caps.Capability(caps.DesktopCompositionCapability()),
            caps.CapsType.CAPSTYPE_RAIL : caps.Capability(caps.RemoteProgramsCapability()),
            caps.CapsType.CAPSETTYPE_SURFACE_COMMANDS : caps.Capability(caps.SurfaceCommandsCapability()),
            caps.CapsType.CAPSETTYPE_LARGE_POINTER : caps.Capability(caps.LargePointerCapability()),
            caps.CapsType.CAPSETTYPE_BITMAP_CODECS : caps.Capability(caps.BitmapCodecsCapability()),
            caps.CapsType.CAPSSETTYPE_FRAME_ACKNOWLEDGE : caps.Capability(caps.FrameAcknowledgeCapability()),
        }
        #share id between client and server
        self._shareId = 0x103EA
        #enable or not fast path
        self._fastPathSender = None
        
    def setFastPathSender(self, fastPathSender):
        """
        @param fastPathSender: {tpkt.FastPathSender}
        @note: implement tpkt.IFastPathListener
        """
        self._fastPathSender = fastPathSender
    
    def sendPDU(self, pduMessage):
        """
        @summary: Send a PDU data to transport layer
        @param pduMessage: PDU message
        """
        log.debug("PDULayer.sendPDU()")
        self._transport.send(data.PDU(self._transport.getUserId(), pduMessage))
        
    def sendDataPDU(self, pduData):
        """
        @summary: Send an PDUData to transport layer
        @param pduData: PDU data message
        """
        log.debug("PDULayer.sendDataPDU()")
        self.sendPDU(data.DataPDU(pduData, self._shareId))

class Client(PDULayer):
    """
    @summary: Client automata of PDU layer
    """
    def __init__(self, listener):
        """
        @param listener: PDUClientListener
        """
        PDULayer.__init__(self)
        self._listener = listener
        self._fastPathFragBuf = bytearray()
        self._fastPathFragType = None
        
    def connect(self):
        """
        @summary: Connect message in client automata
        """
        self._gccCore = self._transport.getGCCClientSettings().CS_CORE
        self.setNextState(self.recvDemandActivePDU)
        #check if client support fast path message
        self._clientFastPathSupported = False
        
    def close(self):
        """
        @summary: Send PDU close packet and call close method on transport method
        """
        self._transport.close()
        #self.sendDataPDU(data.ShutdownRequestPDU())
                             
    def recvDemandActivePDU(self, s):
        """
        @summary: Receive demand active PDU which contains 
        Server capabilities. In this version of RDPY only
        Restricted group of capabilities are used.
        Send Confirm Active PDU
        Send Finalize PDU
        Wait Server Synchronize PDU
        @param s: Stream
        """
        log.debug("PDULayer.recvDemandActivePDU()")
        pdu = data.PDU()
        s.readType(pdu)
        
        if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_SERVER_REDIR_PKT:
            log.debug("Received server redirection PDU during connection sequence")
            self._handleServerRedirection(pdu.pduMessage)
            return
        
        if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DEACTIVATEALLPDU:
            log.debug("Received DeactivateAll PDU, waiting for new DemandActive")
            return
        
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DEMANDACTIVEPDU:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvDemandActivePDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        
        self._shareId = pdu.pduMessage.shareId.value
        
        for cap in pdu.pduMessage.capabilitySets._array:
            self._serverCapabilities[cap.capabilitySetType] = cap
            
        #secure checksum cap here maybe protocol (another) design error
        self._transport._enableSecureCheckSum = bool(self._serverCapabilities[caps.CapsType.CAPSTYPE_GENERAL].capability.extraFlags & caps.GeneralExtraFlag.ENC_SALTED_CHECKSUM)
        
        self.sendConfirmActivePDU()
        #send synchronize
        self.sendClientFinalizeSynchronizePDU()
        self.setNextState(self.recvServerSynchronizePDU)
        
    def recvServerSynchronizePDU(self, s):
        """
        @summary: Receive from server 
        Wait Control Cooperate PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvServerSynchronizePDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DEACTIVATEALLPDU:
            log.debug("Received DeactivateAll during sync, restarting capability exchange")
            self.setNextState(self.recvDemandActivePDU)
            return
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_SYNCHRONIZE:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvServerSynchronizePDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        
        self.setNextState(self.recvServerControlCooperatePDU)
        
    def recvServerControlCooperatePDU(self, s):
        """
        @summary: Receive control cooperate PDU from server
        Wait Control Granted PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvServerControlCooperatePDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DEACTIVATEALLPDU:
            log.debug("Received DeactivateAll during control cooperate, restarting capability exchange")
            self.setNextState(self.recvDemandActivePDU)
            return
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_CONTROL or pdu.pduMessage.pduData.action.value != data.Action.CTRLACTION_COOPERATE:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvServerControlCooperatePDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        
        self.setNextState(self.recvServerControlGrantedPDU)
        
    def recvServerControlGrantedPDU(self, s):
        """
        @summary: Receive last control PDU the granted control PDU
        Wait Font map PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvServerControlGrantedPDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DEACTIVATEALLPDU:
            log.debug("Received DeactivateAll during control granted, restarting capability exchange")
            self.setNextState(self.recvDemandActivePDU)
            return
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_CONTROL or pdu.pduMessage.pduData.action.value != data.Action.CTRLACTION_GRANTED_CONTROL:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvServerControlGrantedPDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        
        self.setNextState(self.recvServerFontMapPDU)
        
    def recvServerFontMapPDU(self, s):
        """
        @summary: Last useless connection packet from server to client
        Wait any PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvServerFontMapPDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DEACTIVATEALLPDU:
            log.debug("Received DeactivateAll during font map, restarting capability exchange")
            self.setNextState(self.recvDemandActivePDU)
            return
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_FONTMAP:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvServerFontMapPDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        
        # Send SuppressOutputPDU with ALLOW_DISPLAY_UPDATES
        # Required by GNOME Remote Desktop to start sending graphics
        self._sendSuppressOutput()
        
        self.setNextState(self.recvPDU)
        #here i'm connected
        self._listener.onReady()
        
    def recvPDU(self, s):
        """
        @summary: Main receive function after connection sequence
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvPDU()")
        pdus = ArrayType(data.PDU)
        s.readType(pdus)
        for pdu in pdus:
            if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DATAPDU:
                self.readDataPDU(pdu.pduMessage)
            elif pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DEACTIVATEALLPDU:
                #use in deactivation-reactivation sequence
                #next state is either a capabilities re exchange or disconnection
                #http://msdn.microsoft.com/en-us/library/cc240454.aspx
                self.setNextState(self.recvDemandActivePDU)
            elif pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_SERVER_REDIR_PKT:
                log.debug("Received server redirection PDU")
                self._handleServerRedirection(pdu.pduMessage)
        
    def recvFastPath(self, secFlag, fastPathS):
        """
        @summary: Implement IFastPathListener interface
        Fast path is needed by RDP 8.0
        Manually parses FastPath updates with fragment reassembly.
        @param fastPathS: {Stream} that contain fast path data
        @param secFlag: {SecFlags}
        """
        import struct
        log.debug("PDULayer.recvFastPath()")

        rawData = fastPathS.getvalue()[fastPathS.pos:]
        offset = 0

        while offset < len(rawData):
            if offset + 1 > len(rawData):
                break
            updateHeader = rawData[offset]
            offset += 1

            updateCode = updateHeader & 0x0f
            fragmentation = (updateHeader >> 4) & 0x03
            compression = (updateHeader >> 6) & 0x03

            # Read compressionFlags if compression is indicated
            if compression & data.FastPathOutputCompression.FASTPATH_OUTPUT_COMPRESSION_USED:
                if offset + 1 > len(rawData):
                    break
                # compressionFlags = rawData[offset]
                offset += 1

            if offset + 2 > len(rawData):
                break
            size = struct.unpack_from('<H', rawData, offset)[0]
            offset += 2

            if offset + size > len(rawData):
                break
            updatePayload = rawData[offset:offset + size]
            offset += size

            # Fragment reassembly (MS-RDPBCGR 2.2.9.1.2.1)
            FRAG_SINGLE = 0
            FRAG_LAST = 1
            FRAG_FIRST = 2
            FRAG_NEXT = 3

            if fragmentation == FRAG_FIRST:
                self._fastPathFragBuf = bytearray(updatePayload)
                self._fastPathFragType = updateCode
                continue
            elif fragmentation == FRAG_NEXT:
                self._fastPathFragBuf.extend(updatePayload)
                continue
            elif fragmentation == FRAG_LAST:
                self._fastPathFragBuf.extend(updatePayload)
                updatePayload = bytes(self._fastPathFragBuf)
                updateCode = self._fastPathFragType if self._fastPathFragType is not None else updateCode
                self._fastPathFragBuf = bytearray()
                self._fastPathFragType = None
            # else FRAG_SINGLE: use updatePayload as-is

            self._dispatchFastPathUpdate(updateCode, updatePayload)

    def _dispatchFastPathUpdate(self, updateCode, payload):
        """Dispatch a complete (reassembled) FastPath update."""
        if updateCode == data.FastPathUpdateType.FASTPATH_UPDATETYPE_BITMAP:
            try:
                pduData = data.FastPathBitmapUpdateDataPDU()
                s = Stream(payload)
                s.readType(pduData)
                self._listener.onUpdate(pduData.rectangles._array)
            except Exception as e:
                log.warning("Failed to parse bitmap update: %s" % e)
        elif updateCode == data.FastPathUpdateType.FASTPATH_UPDATETYPE_PTR_NULL:
            self._listener.onPointerHide()
        elif updateCode == data.FastPathUpdateType.FASTPATH_UPDATETYPE_PTR_DEFAULT:
            self._listener.onPointerDefault()
        elif updateCode == data.FastPathUpdateType.FASTPATH_UPDATETYPE_COLOR:
            try:
                ud = data.FastPathColorPointerPDU()
                s = Stream(payload)
                s.readType(ud)
                self._listener.onPointerUpdate(
                    24, ud.cacheIndex.value,
                    ud.hotSpotX.value, ud.hotSpotY.value,
                    ud.width.value, ud.height.value,
                    ud.andMaskData.value, ud.xorMaskData.value
                )
            except Exception as e:
                log.warning("Failed to parse color pointer update: %s" % e)
        elif updateCode == data.FastPathUpdateType.FASTPATH_UPDATETYPE_CACHED:
            try:
                ud = data.FastPathCachedPointerPDU()
                s = Stream(payload)
                s.readType(ud)
                self._listener.onPointerCached(ud.cacheIndex.value)
            except Exception as e:
                log.warning("Failed to parse cached pointer update: %s" % e)
        elif updateCode == data.FastPathUpdateType.FASTPATH_UPDATETYPE_POINTER:
            try:
                ud = data.FastPathPointerUpdatePDU()
                s = Stream(payload)
                s.readType(ud)
                self._listener.onPointerUpdate(
                    ud.xorBpp.value, ud.cacheIndex.value,
                    ud.hotSpotX.value, ud.hotSpotY.value,
                    ud.width.value, ud.height.value,
                    ud.andMaskData.value, ud.xorMaskData.value
                )
            except Exception as e:
                log.warning("Failed to parse pointer update: %s" % e)
        elif updateCode == data.FastPathUpdateType.FASTPATH_UPDATETYPE_SURFCMDS:
            try:
                self._handleSurfaceCommands(payload)
            except Exception as e:
                log.warning("Failed to handle surface commands: %s" % e)
        
    def readDataPDU(self, dataPDU):
        """
        @summary: read a data PDU object
        @param dataPDU: DataPDU object
        """
        log.debug("PDULayer.readDataPDU()")
        if dataPDU.shareDataHeader.pduType2.value == data.PDUType2.PDUTYPE2_SET_ERROR_INFO_PDU:
            #ignore 0 error code because is not an error code
            if dataPDU.pduData.errorInfo.value == 0:
                return
            errorCode = dataPDU.pduData.errorInfo.value
            errorMessage = "Unknown code %s"%hex(errorCode)
            if errorCode in data.ErrorInfo._MESSAGES_:
                errorMessage = data.ErrorInfo._MESSAGES_[errorCode]
            log.error("INFO PDU : %s"%errorMessage)
            
        elif dataPDU.shareDataHeader.pduType2.value == data.PDUType2.PDUTYPE2_SHUTDOWN_DENIED:
            #may be an event to ask to user
            self._transport.close()
        elif dataPDU.shareDataHeader.pduType2.value == data.PDUType2.PDUTYPE2_SAVE_SESSION_INFO:
            #handle session event
            self._listener.onSessionReady()
        elif dataPDU.shareDataHeader.pduType2.value == data.PDUType2.PDUTYPE2_UPDATE:
            self.readUpdateDataPDU(dataPDU.pduData)
    
    def readUpdateDataPDU(self, updateDataPDU):
        """
        @summary: Read an update data PDU data
        dispatch update data
        @param: {UpdateDataPDU} object
        """
        log.debug("PDULayer.readUpdateDataPDU()")
        if updateDataPDU.updateType.value == data.UpdateType.UPDATETYPE_BITMAP:
            self._listener.onUpdate(updateDataPDU.updateData.rectangles._array)
        
    def sendConfirmActivePDU(self):
        """
        @summary: Send all client capabilities
        """
        log.debug("PDULayer.sendConfirmActivePDU()")
        #init general capability
        generalCapability = self._clientCapabilities[caps.CapsType.CAPSTYPE_GENERAL].capability
        generalCapability.osMajorType.value = caps.MajorType.OSMAJORTYPE_WINDOWS
        generalCapability.osMinorType.value = caps.MinorType.OSMINORTYPE_WINDOWS_NT
        generalCapability.extraFlags.value = (caps.GeneralExtraFlag.LONG_CREDENTIALS_SUPPORTED |
                                              caps.GeneralExtraFlag.NO_BITMAP_COMPRESSION_HDR |
                                              caps.GeneralExtraFlag.AUTORECONNECT_SUPPORTED)
        if self._fastPathSender is not None:
            generalCapability.extraFlags.value |= caps.GeneralExtraFlag.FASTPATH_OUTPUT_SUPPORTED
        generalCapability.refreshRectSupport.value = 1
        generalCapability.suppressOutputSupport.value = 1
        
        #init bitmap capability
        bitmapCapability = self._clientCapabilities[caps.CapsType.CAPSTYPE_BITMAP].capability
        bitmapCapability.preferredBitsPerPixel.value = 32
        bitmapCapability.desktopWidth = self._gccCore.desktopWidth
        bitmapCapability.desktopHeight = self._gccCore.desktopHeight
        bitmapCapability.desktopResizeFlag.value = 0x0001
         
        #init order capability
        orderCapability = self._clientCapabilities[caps.CapsType.CAPSTYPE_ORDER].capability
        orderCapability.orderFlags.value = (caps.OrderFlag.NEGOTIATEORDERSUPPORT |
                                            caps.OrderFlag.ZEROBOUNDSDELTASSUPPORT |
                                            caps.OrderFlag.COLORINDEXSUPPORT |
                                            caps.OrderFlag.ORDERFLAGS_EXTRA_FLAGS)
        orderCapability.orderSupportExFlags.value |= caps.OrderEx.ORDERFLAGS_EX_ALTSEC_FRAME_MARKER_SUPPORT
        orderCapability.textANSICodePage.value = 0x04e4
        for idx in [caps.Order.TS_NEG_DSTBLT_INDEX, caps.Order.TS_NEG_PATBLT_INDEX,
                    caps.Order.TS_NEG_SCRBLT_INDEX]:
            orderCapability.orderSupport._array[idx].value = 1
        
        #init bitmap cache rev2 capability
        bmpCacheCap = self._clientCapabilities[caps.CapsType.CAPSTYPE_BITMAPCACHE_REV2].capability
        bmpCacheCap.cacheFlags.value = caps.BitmapCache2Capability.ALLOW_CACHE_WAITING_LIST_FLAG
        bmpCacheCap.numCellCaches.value = 5
        bmpCacheCap.bitmapCache0CellInfo.value = 0x258
        bmpCacheCap.bitmapCache1CellInfo.value = 0x258
        bmpCacheCap.bitmapCache2CellInfo.value = 0x800
        bmpCacheCap.bitmapCache3CellInfo.value = 0x1000
        bmpCacheCap.bitmapCache4CellInfo.value = 0x800
        
        #init pointer capability
        pointerCap = self._clientCapabilities[caps.CapsType.CAPSTYPE_POINTER].capability
        pointerCap.colorPointerFlag.value = 1
        pointerCap.colorPointerCacheSize.value = 20
        pointerCap.pointerCacheSize.value = 20
        
        #init brush capability
        brushCap = self._clientCapabilities[caps.CapsType.CAPSTYPE_BRUSH].capability
        brushCap.brushSupportLevel.value = caps.BrushSupport.BRUSH_COLOR_8x8
        
        #init sound capability
        soundCap = self._clientCapabilities[caps.CapsType.CAPSTYPE_SOUND].capability
        soundCap.soundFlags.value = caps.SoundFlag.SOUND_BEEPS_FLAG
        
        #init input capability
        inputCapability = self._clientCapabilities[caps.CapsType.CAPSTYPE_INPUT].capability
        inputCapability.inputFlags.value = caps.InputFlags.INPUT_FLAG_SCANCODES | caps.InputFlags.INPUT_FLAG_MOUSEX | caps.InputFlags.INPUT_FLAG_UNICODE
        inputCapability.keyboardLayout = self._gccCore.kbdLayout
        inputCapability.keyboardType = self._gccCore.keyboardType
        inputCapability.keyboardSubType = self._gccCore.keyboardSubType
        inputCapability.keyboardFunctionKey = self._gccCore.keyboardFnKeys
        inputCapability.imeFileName = self._gccCore.imeFileName
        
        #init virtual channel capability
        vcCap = self._clientCapabilities[caps.CapsType.CAPSTYPE_VIRTUALCHANNEL].capability
        vcCap.VCChunkSize.value = 1600
        
        #init RAIL (Remote Programs) capability
        railCap = self._clientCapabilities[caps.CapsType.CAPSTYPE_RAIL].capability
        railCap.railSupportLevel.value = (caps.RemoteProgramsCapability.RAIL_LEVEL_SUPPORTED |
                                          caps.RemoteProgramsCapability.RAIL_LEVEL_DOCKED_LANGBAR_SUPPORTED |
                                          caps.RemoteProgramsCapability.RAIL_LEVEL_SHELL_INTEGRATION_SUPPORTED |
                                          caps.RemoteProgramsCapability.RAIL_LEVEL_LANGUAGE_IME_SYNC_SUPPORTED |
                                          caps.RemoteProgramsCapability.RAIL_LEVEL_SERVER_TO_CLIENT_IME_SYNC_SUPPORTED |
                                          caps.RemoteProgramsCapability.RAIL_LEVEL_HIDE_MINIMIZED_APPS_SUPPORTED |
                                          caps.RemoteProgramsCapability.RAIL_LEVEL_WINDOW_CLOAKING_SUPPORTED |
                                          caps.RemoteProgramsCapability.RAIL_LEVEL_HANDSHAKE_EX_SUPPORTED)
        
        #init multifragment update - large buffer for GFX
        multiFragCap = self._clientCapabilities[caps.CapsType.CAPSETTYPE_MULTIFRAGMENTUPDATE].capability
        multiFragCap.MaxRequestSize.value = 0x3F0000
        
        #init surface commands capability
        surfCmdsCap = self._clientCapabilities[caps.CapsType.CAPSETTYPE_SURFACE_COMMANDS].capability
        surfCmdsCap.cmdFlags.value = (caps.SurfaceCommandsCapability.SURFCMDS_SET_SURFACE_BITS |
                                      caps.SurfaceCommandsCapability.SURFCMDS_STREAM_SURFACE_BITS |
                                      caps.SurfaceCommandsCapability.SURFCMDS_FRAME_MARKER)
        
        #init large pointer capability
        largePointerCap = self._clientCapabilities[caps.CapsType.CAPSETTYPE_LARGE_POINTER].capability
        largePointerCap.largePointerSupportFlags.value = caps.LargePointerCapability.LARGE_POINTER_FLAG_96x96
        
        #init bitmap codecs capability (NSCodec + RemoteFX)
        self._clientCapabilities[caps.CapsType.CAPSETTYPE_BITMAP_CODECS] = caps.Capability(caps.BitmapCodecsCapability.buildClientCodecs())
        
        #init frame acknowledge capability
        frameAckCap = self._clientCapabilities[caps.CapsType.CAPSSETTYPE_FRAME_ACKNOWLEDGE].capability
        frameAckCap.maxUnacknowledgedFrameCount.value = 2
        
        #init share capability with actual user channel ID
        shareCap = self._clientCapabilities[caps.CapsType.CAPSTYPE_SHARE].capability
        shareCap.nodeId.value = self._transport.getUserId()
        
        #make active PDU packet
        confirmActivePDU = data.ConfirmActivePDU()
        confirmActivePDU.shareId.value = self._shareId
        confirmActivePDU.capabilitySets._array = list(self._clientCapabilities.values())
        self.sendPDU(confirmActivePDU)
        
    def sendClientFinalizeSynchronizePDU(self):
        """
        @summary: send a synchronize PDU from client to server
        """
        log.debug("PDULayer.sendClientFinalizeSynchronizePDU()")
        synchronizePDU = data.SynchronizeDataPDU(self._transport.getChannelId())
        self.sendDataPDU(synchronizePDU)
        
        #ask for cooperation
        controlCooperatePDU = data.ControlDataPDU(data.Action.CTRLACTION_COOPERATE)
        self.sendDataPDU(controlCooperatePDU)
        
        #request control
        controlRequestPDU = data.ControlDataPDU(data.Action.CTRLACTION_REQUEST_CONTROL)
        self.sendDataPDU(controlRequestPDU)
        
        #TODO persistent key list http://msdn.microsoft.com/en-us/library/cc240494.aspx
        
        #deprecated font list pdu
        fontListPDU = data.FontListDataPDU()
        self.sendDataPDU(fontListPDU)
        
    def sendInputEvents(self, pointerEvents):
        """
        @summary: send client input events
        @param pointerEvents: list of pointer events
        """
        log.debug("PDULayer.sendInputEvents()")
        pdu = data.ClientInputEventPDU()
        pdu.slowPathInputEvents._array = [data.SlowPathInputEvent(x) for x in pointerEvents]
        self.sendDataPDU(pdu)
    
    def _sendSuppressOutput(self):
        """
        @summary: Send SuppressOutputPDU with ALLOW_DISPLAY_UPDATES.
        Required by GNOME Remote Desktop to start sending graphics.
        """
        log.debug("PDULayer._sendSuppressOutput()")
        suppressPDU = data.SupressOutputDataPDU()
        suppressPDU.allowDisplayUpdates.value = data.Display.ALLOW_DISPLAY_UPDATES
        suppressPDU.desktopRect.left.value = 0
        suppressPDU.desktopRect.top.value = 0
        suppressPDU.desktopRect.right.value = self._gccCore.desktopWidth.value
        suppressPDU.desktopRect.bottom.value = self._gccCore.desktopHeight.value
        self.sendDataPDU(suppressPDU)

    def requestFullRefresh(self):
        """Request a full-screen refresh to try to recover from an H.264 freeze.

        Sends SUPPRESS_DISPLAY_UPDATES followed immediately by
        ALLOW_DISPLAY_UPDATES.  Some RDP servers respond by restarting their
        display stream with a new key frame; others (notably Windows with
        RDPGFX H.264) ignore this and continue with the existing GOP.

        The decoder is NOT reset before this call (see drdynvc._onAvcNoOutput):
        the existing decoder keeps its SPS/PPS context so it can decode the
        server's next IDR frame even if SPS is not re-sent.
        """
        if not hasattr(self, '_gccCore'):
            return
        log.debug("PDULayer.requestFullRefresh()")
        try:
            suppressPDU = data.SupressOutputDataPDU()
            suppressPDU.allowDisplayUpdates.value = data.Display.SUPPRESS_DISPLAY_UPDATES
            self.sendDataPDU(suppressPDU)

            allowPDU = data.SupressOutputDataPDU()
            allowPDU.allowDisplayUpdates.value = data.Display.ALLOW_DISPLAY_UPDATES
            allowPDU.desktopRect.left.value = 0
            allowPDU.desktopRect.top.value = 0
            allowPDU.desktopRect.right.value = self._gccCore.desktopWidth.value
            allowPDU.desktopRect.bottom.value = self._gccCore.desktopHeight.value
            self.sendDataPDU(allowPDU)
        except Exception as e:
            log.debug("PDULayer.requestFullRefresh error: %s" % e)
    
    def _handleSurfaceCommands(self, surfData):
        """
        @summary: Parse surface commands from fast path, decode bitmaps (NSCodec),
        deliver them to the display, and acknowledge frame markers.
        @param surfData: raw bytes of surface command data
        @see: MS-RDPBCGR 2.2.9.1.2.1.10
        """
        import struct
        from rdpy.protocol.rdp.nscodec import decode_nscodec

        offset = 0
        while offset + 2 <= len(surfData):
            cmdType = struct.unpack_from('<H', surfData, offset)[0]
            offset += 2

            if cmdType == data.SurfaceCommand.CMDTYPE_FRAME_MARKER:
                # frameAction(2) + frameId(4) = 6 bytes
                if offset + 6 > len(surfData):
                    break
                frameAction = struct.unpack_from('<H', surfData, offset)[0]
                frameId = struct.unpack_from('<I', surfData, offset + 2)[0]
                offset += 6
                if frameAction == data.FrameMarkerAction.FRAME_END:
                    self._sendFrameAcknowledge(frameId)

            elif (cmdType == data.SurfaceCommand.CMDTYPE_SET_SURFACE_BITS or
                  cmdType == data.SurfaceCommand.CMDTYPE_STREAM_SURFACE_BITS):
                # destLeft(2) + destTop(2) + destRight(2) + destBottom(2) = 8
                # TS_BITMAP_DATA_EX: bpp(1) + flags(1) + reserved(1) + codecID(1) + width(2) + height(2) + bitmapDataLength(4) = 12
                # Total header after cmdType: 20 bytes
                if offset + 20 > len(surfData):
                    break
                destLeft = struct.unpack_from('<H', surfData, offset)[0]
                destTop = struct.unpack_from('<H', surfData, offset + 2)[0]
                destRight = struct.unpack_from('<H', surfData, offset + 4)[0]
                destBottom = struct.unpack_from('<H', surfData, offset + 6)[0]

                bpp = surfData[offset + 8]
                flags = surfData[offset + 9]
                # reserved = surfData[offset + 10]
                codecID = surfData[offset + 11]
                width = struct.unpack_from('<H', surfData, offset + 12)[0]
                height = struct.unpack_from('<H', surfData, offset + 14)[0]
                bitmapDataLength = struct.unpack_from('<I', surfData, offset + 16)[0]
                offset += 20

                # Extended compressed bitmap header (24 bytes) included in bitmapDataLength
                if flags & 0x01:
                    offset += 24
                    bitmapDataLength -= 24

                if offset + bitmapDataLength > len(surfData):
                    log.warning("Surface bits data truncated")
                    break

                bitmapData = surfData[offset:offset + bitmapDataLength]
                offset += bitmapDataLength

                pixels = None
                outBpp = bpp
                if codecID == 0:
                    # Uncompressed (top-down)
                    pixels = bitmapData
                    outBpp = bpp
                elif codecID == 1:
                    # NSCodec decodes to top-down BGRA, but empirically the
                    # output needs to be flipped vertically before passing to Qt
                    # (mirrors grdp's explicit vertical flip after decodeNSCodec).
                    raw = decode_nscodec(bytes(bitmapData), width, height)
                    outBpp = 32
                    if raw is not None:
                        import numpy as _np
                        arr = _np.frombuffer(raw, dtype=_np.uint8).reshape(height, width, 4)
                        pixels = arr[::-1].tobytes()
                    else:
                        pixels = None
                else:
                    log.warning("Unsupported surface codec: %d" % codecID)
                    continue

                if pixels is None:
                    continue

                # Pass isCompress='gfx' so RDPBitmapToQtImage calls .copy() on the
                # QImage, making Qt own the pixel buffer and avoiding cross-thread
                # dangling-pointer issues with Python-managed buffers.
                if hasattr(self._listener, '_onGfxBitmap'):
                    self._listener._onGfxBitmap(
                        destLeft, destTop, destRight, destBottom,
                        width, height, outBpp, 'gfx', pixels)
            else:
                log.debug("Unknown surface command type: %s" % hex(cmdType))
                break
    
    def _sendFrameAcknowledge(self, frameId):
        """
        @summary: Send frame acknowledge PDU for GFX frame marker
        @param frameId: frame ID to acknowledge
        """
        log.debug("PDULayer._sendFrameAcknowledge(frameId=%d)" % frameId)
        frameAckData = data.FrameAcknowledgeDataPDU(frameId)
        self.sendDataPDU(frameAckData)
    
    def _handleServerRedirection(self, redirPDU):
        """
        @summary: Handle server redirection PDU
        @param redirPDU: ServerRedirectionPDU object
        """
        log.debug("Server redirection requested")
        if hasattr(self._listener, 'onRedirect'):
            self._listener.onRedirect(redirPDU)
        
class Server(PDULayer):
    """
    @summary: Server Automata of PDU layer
    """
    def __init__(self, listener):
        """
        @param listener: PDUServerListener
        """
        PDULayer.__init__(self)
        self._listener = listener
        #fast path layer
        self._fastPathSender = None
        
    def connect(self):
        """
        @summary: Connect message for server automata
        """
        self.sendDemandActivePDU()
        self.setNextState(self.recvConfirmActivePDU)      
        
    def recvConfirmActivePDU(self, s):
        """
        @summary: Receive confirm active PDU from client
        Capabilities exchange
        Wait Client Synchronize PDU
        @param s: Stream
        """
        log.debug("PDULayer.recvConfirmActivePDU()")
        pdu = data.PDU()
        s.readType(pdu)
        
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_CONFIRMACTIVEPDU:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvConfirmActivePDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        
        for cap in pdu.pduMessage.capabilitySets._array:
            self._clientCapabilities[cap.capabilitySetType] = cap
            
        #find use full flag
        self._clientFastPathSupported = bool(self._clientCapabilities[caps.CapsType.CAPSTYPE_GENERAL].capability.extraFlags.value & caps.GeneralExtraFlag.FASTPATH_OUTPUT_SUPPORTED)
        
        #secure checksum cap here maybe protocol (another) design error
        self._transport._enableSecureCheckSum = bool(self._clientCapabilities[caps.CapsType.CAPSTYPE_GENERAL].capability.extraFlags & caps.GeneralExtraFlag.ENC_SALTED_CHECKSUM)
        
        self.setNextState(self.recvClientSynchronizePDU)
        
    def recvClientSynchronizePDU(self, s):
        """
        @summary: Receive from client 
        Wait Control Cooperate PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvClientSynchronizePDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_SYNCHRONIZE:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvClientSynchronizePDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        self.setNextState(self.recvClientControlCooperatePDU)
        
    def recvClientControlCooperatePDU(self, s):
        """
        @summary: Receive control cooperate PDU from client
        Wait Control Request PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvClientControlCooperatePDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_CONTROL or pdu.pduMessage.pduData.action.value != data.Action.CTRLACTION_COOPERATE:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvClientControlCooperatePDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        self.setNextState(self.recvClientControlRequestPDU)
        
    def recvClientControlRequestPDU(self, s):
        """
        @summary: Receive last control PDU the request control PDU from client
        Wait Font List PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvClientControlRequestPDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_CONTROL or pdu.pduMessage.pduData.action.value != data.Action.CTRLACTION_REQUEST_CONTROL:
            #not a blocking error because in deactive reactive sequence 
            #input can be send too but ignored
            log.debug("recvClientControlRequestPDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        self.setNextState(self.recvClientFontListPDU)
        
    def recvClientFontListPDU(self, s):
        """
        @summary: Last synchronize packet from client to server
        Send Server Finalize PDUs
        Wait any PDU
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvClientFontListPDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value != data.PDUType.PDUTYPE_DATAPDU or pdu.pduMessage.shareDataHeader.pduType2.value != data.PDUType2.PDUTYPE2_FONTLIST:
            #not a blocking error because in deactive reactive sequence 
            #input can be send but ignored
            log.debug("recvClientFontListPDU() Ignore message type %s during connection sequence"%hex(pdu.shareControlHeader.pduType.value))
            return
        
        #finalize server
        self.sendServerFinalizeSynchronizePDU()
        self.setNextState(self.recvPDU)
        #now i'm ready
        self._listener.onReady()
        
    def recvPDU(self, s):
        """
        @summary: Main receive function after connection sequence
        @param s: Stream from transport layer
        """
        log.debug("PDULayer.recvPDU()")
        pdu = data.PDU()
        s.readType(pdu)
        if pdu.shareControlHeader.pduType.value == data.PDUType.PDUTYPE_DATAPDU:
            self.readDataPDU(pdu.pduMessage)
            
    def readDataPDU(self, dataPDU):
        """
        @summary: read a data PDU object
        @param dataPDU: DataPDU object
        """
        log.debug("PDULayer.readDataPDU()")
        if dataPDU.shareDataHeader.pduType2.value == data.PDUType2.PDUTYPE2_SET_ERROR_INFO_PDU:
            errorMessage = "Unknown code %s"%hex(dataPDU.pduData.errorInfo.value)
            if data.ErrorInfo._MESSAGES_.has_key(dataPDU.pduData.errorInfo):
                errorMessage = data.ErrorInfo._MESSAGES_[dataPDU.pduData.errorInfo]
            log.error("INFO PDU : %s"%errorMessage)
            
        elif dataPDU.shareDataHeader.pduType2.value == data.PDUType2.PDUTYPE2_INPUT:
            self._listener.onSlowPathInput(dataPDU.pduData.slowPathInputEvents._array)
            
        elif dataPDU.shareDataHeader.pduType2.value == data.PDUType2.PDUTYPE2_SHUTDOWN_REQUEST:
            log.debug("Receive Shutdown Request")
            self._transport.close()
            
    def recvFastPath(self, fastPathS):
        """
        @summary: Implement IFastPathListener interface
        Fast path is needed by RDP 8.0
        @param fastPathS: Stream that contain fast path data
        """
        log.debug("PDULayer.readFlashPath()")
        pass
        
    def sendDemandActivePDU(self):
        """
        @summary: Send server capabilities server automata PDU
        """
        log.debug("PDULayer.sendDemandActivePDU()")
        #init general capability
        generalCapability = self._serverCapabilities[caps.CapsType.CAPSTYPE_GENERAL].capability
        generalCapability.osMajorType.value = caps.MajorType.OSMAJORTYPE_WINDOWS
        generalCapability.osMinorType.value = caps.MinorType.OSMINORTYPE_WINDOWS_NT
        generalCapability.extraFlags.value = caps.GeneralExtraFlag.LONG_CREDENTIALS_SUPPORTED | caps.GeneralExtraFlag.NO_BITMAP_COMPRESSION_HDR | caps.GeneralExtraFlag.FASTPATH_OUTPUT_SUPPORTED | caps.GeneralExtraFlag.ENC_SALTED_CHECKSUM
        
        inputCapability = self._serverCapabilities[caps.CapsType.CAPSTYPE_INPUT].capability
        inputCapability.inputFlags.value = caps.InputFlags.INPUT_FLAG_SCANCODES | caps.InputFlags.INPUT_FLAG_MOUSEX
        
        demandActivePDU = data.DemandActivePDU()
        demandActivePDU.shareId.value = self._shareId
        demandActivePDU.capabilitySets._array = list(self._serverCapabilities.values())
        self.sendPDU(demandActivePDU)
        
    def sendServerFinalizeSynchronizePDU(self):
        """
        @summary: Send last synchronize packet from server to client
        """
        log.debug("PDULayer.sendServerFinalizeSynchronizePDU()")
        synchronizePDU = data.SynchronizeDataPDU(self._transport.getChannelId())
        self.sendDataPDU(synchronizePDU)
        
        #ask for cooperation
        controlCooperatePDU = data.ControlDataPDU(data.Action.CTRLACTION_COOPERATE)
        self.sendDataPDU(controlCooperatePDU)
        
        #request control
        controlRequestPDU = data.ControlDataPDU(data.Action.CTRLACTION_GRANTED_CONTROL)
        self.sendDataPDU(controlRequestPDU)
        
        #TODO persistent key list http://msdn.microsoft.com/en-us/library/cc240494.aspx
        
        #deprecated font list pdu
        fontMapPDU = data.FontMapDataPDU()
        self.sendDataPDU(fontMapPDU)
        
    def sendPDU(self, pduMessage):
        """
        @summary: Send a PDU data to transport layer
        @param pduMessage: PDU message
        """
        log.debug("PDULayer.sendPDU()")
        PDULayer.sendPDU(self, pduMessage)
        #restart capabilities exchange in case of deactive reactive sequence
        if isinstance(pduMessage, data.DeactiveAllPDU):
            self.sendDemandActivePDU()
            self.setNextState(self.recvConfirmActivePDU)
        
    def sendBitmapUpdatePDU(self, bitmapDatas):
        """
        @summary: Send bitmap update data
        @param bitmapDatas: List of data.BitmapData
        """
        log.debug("PDULayer.sendBitmapUpdatePDU()")
        #check bitmap header for client that want it (very old client)
        if self._clientCapabilities[caps.CapsType.CAPSTYPE_GENERAL].capability.extraFlags.value & caps.GeneralExtraFlag.NO_BITMAP_COMPRESSION_HDR:
            for bitmapData in bitmapDatas:
                if bitmapData.flags.value & data.BitmapFlag.BITMAP_COMPRESSION:
                    bitmapData.flags.value |= data.BitmapFlag.NO_BITMAP_COMPRESSION_HDR
        
        if self._clientFastPathSupported and self._fastPathSender is not None:
            #fast path case
            fastPathUpdateDataPDU = data.FastPathBitmapUpdateDataPDU()
            fastPathUpdateDataPDU.rectangles._array = bitmapDatas
            self._fastPathSender.sendFastPath(0, data.FastPathUpdatePDU(fastPathUpdateDataPDU))
        else:
            #slow path case
            updateDataPDU = data.BitmapUpdateDataPDU()
            updateDataPDU.rectangles._array = bitmapDatas
            self.sendDataPDU(data.UpdateDataPDU(updateDataPDU))
