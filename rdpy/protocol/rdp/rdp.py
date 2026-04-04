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
Use to manage RDP stack in twisted
"""

from rdpy.core import layer
from rdpy.core.error import CallPureVirtualFuntion, InvalidValue
from . import pdu
import rdpy.core.log as log
from . import tpkt, x224, sec, drdynvc, rdpsnd, cliprdr
from .t125 import mcs, gcc
from .nla import cssp, ntlm

class _StubVChannel(layer.LayerAutomata):
    """Stub virtual channel layer that accepts and discards all data."""
    def __init__(self):
        layer.LayerAutomata.__init__(self, None)
    def connect(self):
        pass
    def recv(self, s):
        pass

class SecurityLevel(object):
    """
    @summary: RDP security level
    """
    RDP_LEVEL_RDP = 0
    RDP_LEVEL_SSL = 1
    RDP_LEVEL_NLA = 2

class RDPClientController(pdu.layer.PDUClientListener):
    """
    Manage RDP stack as client
    """
    def __init__(self):
        #list of observer
        self._clientObserver = []
        #dynamic virtual channel layer
        self._drdynvcLayer = drdynvc.DrdynvcLayer()
        self._drdynvcLayer.setGfxCallback(self._onGfxBitmap)
        self._drdynvcLayer.setCapsConfirmCallback(self._onGfxCapsConfirm)
        #audio output virtual channel layer
        self._rdpsndLayer = rdpsnd.RdpsndLayer()
        self._drdynvcLayer.setRdpsndLayer(self._rdpsndLayer)
        #clipboard virtual channel layer
        self._cliprdrLayer = cliprdr.CliprdrLayer()
        #PDU layer
        self._pduLayer = pdu.layer.Client(self)
        # AVC freeze recovery: when H.264 output stalls for too long, request
        # a full-screen refresh (IDR) from the server via REFRESH_RECT PDU.
        self._drdynvcLayer.setRequestRefreshCallback(self._pduLayer.requestFullRefresh)
        #secure layer
        self._secLayer = sec.Client(self._pduLayer)
        #multi channel service with drdynvc, rdpsnd, cliprdr, and rdpdr virtual channels
        self._mcsLayer = mcs.Client(self._secLayer, [
            (gcc.ChannelDef(b"rdpdr", gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP), _StubVChannel()),
            (gcc.ChannelDef(b"rdpsnd", gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP), self._rdpsndLayer),
            (gcc.ChannelDef(b"cliprdr", gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP | gcc.ChannelOptions.CHANNEL_OPTION_COMPRESS_RDP | gcc.ChannelOptions.CHANNEL_OPTION_SHOW_PROTOCOL), self._cliprdrLayer),
            (gcc.ChannelDef(b"drdynvc", gcc.ChannelOptions.CHANNEL_OPTION_INITIALIZED | gcc.ChannelOptions.CHANNEL_OPTION_ENCRYPT_RDP), self._drdynvcLayer),
        ])
        #transport pdu layer
        self._x224Layer = x224.Client(self._mcsLayer)
        #transport packet (protocol layer)
        self._tpktLayer = tpkt.TPKT(self._x224Layer)
        #fastpath stack
        self._pduLayer.initFastPath(self._secLayer)
        self._secLayer.initFastPath(self._tpktLayer)
        #is pdu layer is ready to send
        self._isReady = False
        # GFX input gating: suppress input events until CAPS_CONFIRM or timeout
        self._gfxPending = False
        self._gfxTimer = None
        #routing token for server redirection
        self._redirectRoutingToken = None
        
    def setClipboardCallbacks(self, onRemoteChanged, getLocalText):
        """
        @summary: Wire up clipboard callbacks between UI and CLIPRDR layer
        @param onRemoteChanged: callback(text: str) when server clipboard changes
        @param getLocalText: callback() -> str returning current local clipboard text
        """
        self._cliprdrLayer.setRemoteClipboardCallback(onRemoteChanged)
        self._cliprdrLayer.setLocalClipboardGetter(getLocalText)

    def onLocalClipboardChanged(self):
        """
        @summary: Notify the server that the local clipboard has changed
        """
        self._cliprdrLayer.onLocalClipboardChanged()

    def getProtocol(self):
        """
        @return: return Protocol layer for twisted
        In case of RDP TPKT is the Raw layer
        """
        return cssp.CSSP(self._tpktLayer, ntlm.NTLMv2(self._secLayer._info.domain.value, self._secLayer._info.userName.value, self._secLayer._info.password.value))
    
    def getColorDepth(self):
        """
        @return: color depth set by the server (15, 16, 24)
        """
        return self._pduLayer._serverCapabilities[pdu.caps.CapsType.CAPSTYPE_BITMAP].capability.preferredBitsPerPixel.value
    
    def getKeyEventUniCodeSupport(self):
        """
        @return: True if server support unicode input
        """
        return self._pduLayer._serverCapabilities[pdu.caps.CapsType.CAPSTYPE_INPUT].capability.inputFlags.value & pdu.caps.InputFlags.INPUT_FLAG_UNICODE
        
    def setPerformanceSession(self):
        """
        @summary: Set particular flag in RDP stack to avoid wall-paper, theme, menu animation etc...
        """
        self._secLayer._info.extendedInfo.performanceFlags.value = (
            sec.PerfFlag.PERF_DISABLE_WALLPAPER |
            sec.PerfFlag.PERF_DISABLE_MENUANIMATIONS |
            sec.PerfFlag.PERF_DISABLE_CURSOR_SHADOW |
            sec.PerfFlag.PERF_DISABLE_FULLWINDOWDRAG |
            sec.PerfFlag.PERF_ENABLE_FONT_SMOOTHING |
            sec.PerfFlag.PERF_ENABLE_DESKTOP_COMPOSITION
        )
        
    def setScreen(self, width, height):
        """
        @summary: Set screen dim of session
        @param width: width in pixel of screen
        @param height: height in pixel of screen
        """
        #set screen definition in MCS layer
        self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE).desktopHeight.value = height
        self._mcsLayer._clientSettings.getBlock(gcc.MessageType.CS_CORE).desktopWidth.value = width
        
    def setUsername(self, username):
        """
        @summary: Set the username for session
        @param username: {string} username of session
        """
        #username in PDU info packet
        self._secLayer._info.userName.value = username
        self._secLayer._licenceManager._username = username
        
    def setPassword(self, password):
        """
        @summary: Set password for session
        @param password: {string} password of session
        """
        self.setAutologon()
        self._secLayer._info.password.value = password
        
    def setDomain(self, domain):
        """
        @summary: Set the windows domain of session
        @param domain: {string} domain of session
        """
        self._secLayer._info.domain.value = domain
        
    def setAutologon(self):
        """
        @summary: enable autologon
        """
        self._secLayer._info.flag |= sec.InfoFlag.INFO_AUTOLOGON
        
    def setAlternateShell(self, appName):
        """
        @summary: set application name of app which start at the begining of session
        @param appName: {string} application name
        """
        self._secLayer._info.alternateShell.value = appName
        
    def setKeyboardLayout(self, layout):
        """
        @summary: keyboard layout
        @param layout: name of gcc.KeyboardLayout attribute (e.g. US, FRENCH)
        """
        value = getattr(gcc.KeyboardLayout, layout.upper(), None)
        if value is None:
            log.warning("Unknown keyboard layout: %s, falling back to US" % layout)
            value = gcc.KeyboardLayout.US
        self._mcsLayer._clientSettings.CS_CORE.kbdLayout.value = value

    def setKeyboardType(self, keyboard_type):
        """
        @summary: keyboard type
        @param keyboard_type: name of gcc.KeyboardType attribute (e.g. IBM_101_102_KEYS)
        """
        value = getattr(gcc.KeyboardType, keyboard_type.upper(), None)
        if value is None:
            log.warning("Unknown keyboard type: %s, falling back to IBM_101_102_KEYS" % keyboard_type)
            value = gcc.KeyboardType.IBM_101_102_KEYS
        self._mcsLayer._clientSettings.CS_CORE.keyboardType.value = value

    def setHostname(self, hostname):
        """
        @summary: set hostname of machine
        """
#        self._mcsLayer._clientSettings.CS_CORE.clientName.value = hostname[:15] + "\x00" * (15 - len(hostname))
        self._secLayer._licenceManager._hostname = hostname

    def setRoutingToken(self, token):
        """
        @summary: Set routing token for server redirection reconnection
        @param token: {bytes} routing token from server redirection PDU
        """
        self._x224Layer.setRoutingToken(token)
        
    def setSecurityLevel(self, level):
        """
        @summary: Request basic security
        @param level: {SecurityLevel}
        """
        if level == SecurityLevel.RDP_LEVEL_RDP:
            self._x224Layer._requestedProtocol = x224.Protocols.PROTOCOL_RDP
        elif level == SecurityLevel.RDP_LEVEL_SSL:
            self._x224Layer._requestedProtocol = x224.Protocols.PROTOCOL_SSL
        elif level == SecurityLevel.RDP_LEVEL_NLA:
            self._x224Layer._requestedProtocol = x224.Protocols.PROTOCOL_SSL | x224.Protocols.PROTOCOL_HYBRID
        
    def addClientObserver(self, observer):
        """
        @summary: Add observer to RDP protocol
        @param observer: new observer to add
        """
        self._clientObserver.append(observer)
        
    def removeClientObserver(self, observer):
        """
        @summary: Remove observer to RDP protocol stack
        @param observer: observer to remove
        """
        for i in range(0, len(self._clientObserver)):
            if self._clientObserver[i] == observer:
                del self._clientObserver[i]
                return
        
    def onUpdate(self, rectangles):
        """
        @summary: Call when a bitmap data is received from update PDU
        @param rectangles: [pdu.BitmapData] struct
        """
        for observer in self._clientObserver:
            #for each rectangle in update PDU
            for rectangle in rectangles:
                observer.onUpdate(rectangle.destLeft.value, rectangle.destTop.value, rectangle.destRight.value, rectangle.destBottom.value, rectangle.width.value, rectangle.height.value, rectangle.bitsPerPixel.value, rectangle.flags.value & pdu.data.BitmapFlag.BITMAP_COMPRESSION, rectangle.bitmapDataStream.value)

    def _onGfxBitmap(self, destLeft, destTop, destRight, destBottom, width, height, bpp, isCompress, data):
        """Callback from DrdynvcLayer for RDPGFX bitmap delivery."""
        for observer in self._clientObserver:
            observer.onUpdate(destLeft, destTop, destRight, destBottom,
                              width, height, bpp, isCompress, data)

    def onPointerHide(self):
        """
        @summary: Call when server requests the pointer to be hidden
        """
        for observer in self._clientObserver:
            observer.onPointerHide()

    def onPointerDefault(self):
        """
        @summary: Call when server requests the default system pointer
        """
        for observer in self._clientObserver:
            observer.onPointerDefault()

    def onPointerCached(self, cacheIndex):
        """
        @summary: Call when server switches to a previously cached pointer
        @param cacheIndex: index of cached pointer
        """
        for observer in self._clientObserver:
            observer.onPointerCached(cacheIndex)

    def onPointerUpdate(self, xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask):
        """
        @summary: Call when server sends a new pointer shape
        @param xorBpp: bits per pixel of XOR mask
        @param cacheIndex: cache index to store this pointer
        @param hotSpotX: hotspot X coordinate
        @param hotSpotY: hotspot Y coordinate
        @param width: pointer width
        @param height: pointer height
        @param andMask: AND mask data
        @param xorMask: XOR mask data
        """
        for observer in self._clientObserver:
            observer.onPointerUpdate(xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask)

    def onReady(self):
        """
        @summary: Call when PDU layer is connected
        """
        self._isReady = True
        # Start GFX input gating: suppress input until CAPS_CONFIRM or timeout
        self._gfxPending = True
        from twisted.internet import reactor
        self._gfxTimer = reactor.callLater(5, self._onGfxTimeout)
        log.debug("RDPClientController: GFX input gating active (5s timeout)")
        #signal all listener
        for observer in self._clientObserver:
            observer.onReady()

    def _onGfxCapsConfirm(self):
        """Called by DrdynvcLayer when RDPGFX CAPS_CONFIRM is received."""
        log.debug("RDPClientController: GFX CAPS_CONFIRM received, enabling input")
        self._gfxPending = False
        if self._gfxTimer and self._gfxTimer.active():
            self._gfxTimer.cancel()
            self._gfxTimer = None

    def _onGfxTimeout(self):
        """Fallback: enable input after timeout even without CAPS_CONFIRM."""
        log.debug("RDPClientController: GFX timeout, enabling input")
        self._gfxPending = False
        self._gfxTimer = None
            
    def onSessionReady(self):
        """
        @summary: Call when Windows session is ready (connected)
        """
        self._isReady = True
        #signal all listener
        for observer in self._clientObserver:
            observer.onSessionReady()
            
    def onClose(self):
        """
        @summary: Event call when RDP stack is closed
        """
        self._isReady = False
        self._gfxPending = False
        if self._gfxTimer and self._gfxTimer.active():
            self._gfxTimer.cancel()
            self._gfxTimer = None
        self._rdpsndLayer._stopAudio()
        for observer in self._clientObserver:
            observer.onClose()
    
    def onRedirect(self, redirPDU):
        """
        @summary: Called when server sends a redirection PDU (e.g., GNOME Remote Desktop).
        Extracts routing token and closes connection to trigger redirect reconnection.
        @param redirPDU: ServerRedirectionPDU object
        """
        log.debug("Server redirection received, flags=0x%x" % redirPDU.redirFlags.value)
        lbInfo = redirPDU.getLoadBalanceInfo()
        if lbInfo:
            log.debug("Load balance info: %s" % repr(lbInfo))
            self._redirectRoutingToken = lbInfo
            self.close()
        else:
            log.warning("Server redirection PDU received but no load balance info found")
    
    def sendPointerEvent(self, x, y, button, isPressed):
        """
        @summary: send pointer events
        @param x: x position of pointer
        @param y: y position of pointer
        @param button: 1 or 2 or 3
        @param isPressed: true if button is pressed or false if it's released
        """
        if not self._isReady or self._gfxPending:
            return

        try:
            if button == 4 or button == 5:
                event = pdu.data.PointerExEvent()
                if isPressed:
                    event.pointerFlags.value |= pdu.data.PointerExFlag.PTRXFLAGS_DOWN

                if button == 4:
                    event.pointerFlags.value |= pdu.data.PointerExFlag.PTRXFLAGS_BUTTON1
                elif button == 5:
                    event.pointerFlags.value |= pdu.data.PointerExFlag.PTRXFLAGS_BUTTON2

            else:
                event = pdu.data.PointerEvent()
                if isPressed:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_DOWN
                
                if button == 1:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_BUTTON1
                elif button == 2:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_BUTTON2
                elif button == 3:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_BUTTON3
                else:
                    event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_MOVE
            
            # position
            event.xPos.value = x
            event.yPos.value = y
            
            # send proper event
            self._pduLayer.sendInputEvents([event])
            
        except InvalidValue:
            log.debug("try send pointer event with incorrect position")
    
    def sendWheelEvent(self, x, y, scroll, isHorizontal=False):
        """
        @summary: Send a mouse wheel event
        @param x: x position of pointer
        @param y: y position of pointer
        @param scroll: signed scroll value (positive=up/right, negative=down/left)
        @param isHorizontal: horizontal wheel (default is vertical)
        """
        if not self._isReady or self._gfxPending:
            return

        try:
            event = pdu.data.PointerEvent()
            if isHorizontal:
                event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_HWHEEL
            else:
                event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_WHEEL

            if scroll < 0:
                event.pointerFlags.value |= pdu.data.PointerFlag.PTRFLAGS_WHEEL_NEGATIVE

            # Match grdp: uint8 cast of signed value gives 256-complement for negatives
            # e.g. scroll=-1 → ts=255 (0xFF), scroll=1 → ts=1
            ts = scroll & 0xFF
            event.pointerFlags.value |= (pdu.data.PointerFlag.WheelRotationMask & ts)

            #position
            event.xPos.value = x
            event.yPos.value = y

            #send proper event
            self._pduLayer.sendInputEvents([event])

        except InvalidValue:
            log.debug("try send wheel event with incorrect position")
            
    def sendKeyEventScancode(self, code, isPressed, extended = False):
        """
        @summary: Send a scan code to RDP stack
        @param code: scan code
        @param isPressed: True if key is pressed and false if it's released
        @param extended: {boolean} extended scancode like ctr or win button
        """
        if not self._isReady or self._gfxPending:
            return
        
        try:
            event = pdu.data.ScancodeKeyEvent()
            event.keyCode.value = code
            if not isPressed:
                event.keyboardFlags.value |= pdu.data.KeyboardFlag.KBDFLAGS_RELEASE
            
            if extended:
                event.keyboardFlags.value |= pdu.data.KeyboardFlag.KBDFLAGS_EXTENDED
                
            #send event
            self._pduLayer.sendInputEvents([event])
            
        except InvalidValue:
            log.debug("try send bad key event")
            
    def sendKeyEventUnicode(self, code, isPressed):
        """
        @summary: Send a scan code to RDP stack
        @param code: unicode
        @param isPressed: True if key is pressed and false if it's released
        """
        if not self._isReady or self._gfxPending:
            return
        
        try:
            event = pdu.data.UnicodeKeyEvent()
            event.unicode.value = code
            if not isPressed:
                event.keyboardFlags.value |= pdu.data.KeyboardFlag.KBDFLAGS_RELEASE
            
            #send event
            self._pduLayer.sendInputEvents([event])
            
        except InvalidValue:
            log.debug("try send bad key event")
            
    def sendRefreshOrder(self, left, top, right, bottom):
        """
        @summary: Force server to resend a particular zone
        @param left: left coordinate
        @param top: top coordinate
        @param right: right coordinate
        @param bottom: bottom coordinate
        """
        refreshPDU = pdu.data.RefreshRectPDU()
        rect = pdu.data.InclusiveRectangle()
        rect.left.value = left
        rect.top.value = top
        rect.right.value = right
        rect.bottom.value = bottom
        refreshPDU.areasToRefresh._array.append(rect)
        self._pduLayer.sendDataPDU(refreshPDU)
            
    def close(self):
        """
        @summary: Close protocol stack
        """
        import traceback
        log.debug("RDPClientController.close() called from:\n%s" %
                  "".join(traceback.format_stack()))
        self._pduLayer.close()

class RDPServerController(pdu.layer.PDUServerListener):
    """
    @summary: Controller use in server side mode
    """               
    def __init__(self, colorDepth, privateKeyFileName = None, certificateFileName = None):
        """
        @param privateKeyFileName: file contain server private key
        @param certficiateFileName: file that contain public key
        @param colorDepth: 15, 16, 24
        """
        self._isReady = False
        #list of observer
        self._serverObserver = []
        #build RDP protocol stack
        self._pduLayer = pdu.layer.Server(self)
        #secure layer
        self._secLayer = sec.Server(self._pduLayer)
        #multi channel service
        self._mcsLayer = mcs.Server(self._secLayer)
        #transport pdu layer
        self._x224Layer = x224.Server(self._mcsLayer, privateKeyFileName, certificateFileName, False)
        #transport packet (protocol layer)
        self._tpktLayer = tpkt.TPKT(self._x224Layer)
        
        #fastpath stack
        self._pduLayer.initFastPath(self._secLayer)
        self._secLayer.initFastPath(self._tpktLayer)
        #set color depth of session
        self.setColorDepth(colorDepth)
        
    def close(self):
        """
        @summary: Close protocol stack
        """
        self._pduLayer.close()
        
    def getProtocol(self):
        """
        @return: the twisted protocol layer
        in RDP case is TPKT layer
        """
        return self._tpktLayer
    
    def getHostname(self):
        """
        @return: name of client (information done by RDP)
        """
        return self._mcsLayer._clientSettings.CS_CORE.clientName.value.decode("utf-8").strip("\x00")
    
    def getUsername(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: username send by client may be an empty string
        """
        return self._secLayer._info.userName.value
    
    def getPassword(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: password send by client may be an empty string
        """
        return self._secLayer._info.password.value
    
    def getDomain(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: domain send by client may be an empty string
        """
        return self._secLayer._info.domain.value
    
    def getCredentials(self):
        """
        @summary: Must be call after on ready event else always empty string
        @return: tuple(domain, username, password)
        """
        return (self.getDomain(), self.getUsername(), self.getPassword())
    
    def getColorDepth(self):
        """
        @return: color depth define by server
        """
        return self._colorDepth
    
    def getScreen(self):
        """
        @return: tuple(width, height) of client asked screen
        """
        bitmapCap = self._pduLayer._clientCapabilities[pdu.caps.CapsType.CAPSTYPE_BITMAP].capability
        return (bitmapCap.desktopWidth.value, bitmapCap.desktopHeight.value)
    
    def addServerObserver(self, observer):
        """
        @summary: Add observer to RDP protocol
        @param observer: new observer to add
        """
        self._serverObserver.append(observer)
        
    def setColorDepth(self, colorDepth):
        """
        @summary:  Set color depth of session
                    if PDU stack is already connected send a deactive-reactive sequence
                    and an onReady message is re send when client is ready
        @param colorDepth: {integer} depth of session (15, 16, 24)
        """
        self._colorDepth = colorDepth
        self._pduLayer._serverCapabilities[pdu.caps.CapsType.CAPSTYPE_BITMAP].capability.preferredBitsPerPixel.value = colorDepth
        if self._isReady:
            #restart connection sequence
            self._isReady = False
            self._pduLayer.sendPDU(pdu.data.DeactiveAllPDU())
            
    def setKeyEventUnicodeSupport(self):
        """
        @summary: Enable key event in unicode format
        """
        self._pduLayer._serverCapabilities[pdu.caps.CapsType.CAPSTYPE_INPUT].capability.inputFlags.value |= pdu.caps.InputFlags.INPUT_FLAG_UNICODE
    
    def onReady(self):
        """
        @summary: RDP stack is now ready
        """
        self._isReady = True
        for observer in self._serverObserver:
            observer.onReady()
            
    def onClose(self):
        """
        @summary: Event call when RDP stack is closed
        """
        self._isReady = False
        for observer in self._serverObserver:
            observer.onClose()
            
    def onSlowPathInput(self, slowPathInputEvents):
        """
        @summary: Event call when slow path input are available
        @param slowPathInputEvents: [data.SlowPathInputEvent]
        """
        for observer in self._serverObserver:
            for event in slowPathInputEvents:
                #scan code
                if event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_SCANCODE:
                    observer.onKeyEventScancode(event.slowPathInputData.keyCode.value, not (event.slowPathInputData.keyboardFlags.value & pdu.data.KeyboardFlag.KBDFLAGS_RELEASE), bool(event.slowPathInputData.keyboardFlags.value & pdu.data.KeyboardFlag.KBDFLAGS_EXTENDED))
                #unicode
                elif event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_UNICODE:
                    observer.onKeyEventUnicode(event.slowPathInputData.unicode.value, not (event.slowPathInputData.keyboardFlags.value & pdu.data.KeyboardFlag.KBDFLAGS_RELEASE))
                #mouse events
                elif event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_MOUSE:
                    isPressed = event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_DOWN
                    button = 0
                    if event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_BUTTON1:
                        button = 1
                    elif event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_BUTTON2:
                        button = 2
                    elif event.slowPathInputData.pointerFlags.value & pdu.data.PointerFlag.PTRFLAGS_BUTTON3:
                        button = 3
                    observer.onPointerEvent(event.slowPathInputData.xPos.value, event.slowPathInputData.yPos.value, button, isPressed)
                elif event.messageType.value == pdu.data.InputMessageType.INPUT_EVENT_MOUSEX:
                    isPressed = event.slowPathInputData.pointerFlags.value & pdu.data.PointerExFlag.PTRXFLAGS_DOWN
                    button = 0
                    if event.slowPathInputData.pointerFlags.value & pdu.data.PointerExFlag.PTRXFLAGS_BUTTON1:
                        button = 4
                    elif event.slowPathInputData.pointerFlags.value & pdu.data.PointerExFlag.PTRXFLAGS_BUTTON2:
                        button = 5
                    observer.onPointerEvent(event.slowPathInputData.xPos.value, event.slowPathInputData.yPos.value, button, isPressed)

    
    def sendUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data):
        """
        @summary: send bitmap update
        @param destLeft: xmin position
        @param destTop: ymin position
        @param destRight: xmax position because RDP can send bitmap with padding
        @param destBottom: ymax position because RDP can send bitmap with padding
        @param width: width of bitmap
        @param height: height of bitmap
        @param bitsPerPixel: number of bit per pixel
        @param isCompress: use RLE compression
        @param data: bitmap data
        """
        if not self._isReady:
            return
        bitmapData = pdu.data.BitmapData(destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, data)
        if isCompress:
            bitmapData.flags.value = pdu.data.BitmapFlag.BITMAP_COMPRESSION
        
        self._pduLayer.sendBitmapUpdatePDU([bitmapData])

class ClientFactory(layer.RawLayerClientFactory):
    """
    @summary: Factory of Client RDP protocol
    @param reason: twisted reason
    """
    _redirectRoutingToken = None
    
    def connectionLost(self, csspLayer, reason):
        #retrieve controller
        tpktLayer = csspLayer._layer
        x224Layer = tpktLayer._presentation
        mcsLayer = x224Layer._presentation
        secLayer = mcsLayer._channels[mcs.Channel.MCS_GLOBAL_CHANNEL]
        pduLayer = secLayer._presentation
        controller = pduLayer._listener
        #save redirect routing token before closing
        if controller._redirectRoutingToken:
            self._redirectRoutingToken = controller._redirectRoutingToken
        controller.onClose()
        
    def buildRawLayer(self, addr):
        """
        @summary: Function call from twisted and build rdp protocol stack
        @param addr: destination address
        """
        controller = RDPClientController()
        self.buildObserver(controller, addr)
        return controller.getProtocol()
    
    def buildObserver(self, controller, addr):
        """
        @summary: Build observer use for connection
        @param controller: RDPClientController
        @param addr: destination address
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "buildObserver", "ClientFactory"))

class ServerFactory(layer.RawLayerServerFactory):
    """
    @summary: Factory of Server RDP protocol
    """
    def __init__(self, colorDepth, privateKeyFileName = None, certificateFileName = None):
        """
        @param colorDepth: color depth of session
        @param privateKeyFileName: file contain server private key (if none -> back to standard RDP security)
        @param certficiateFileName: file that contain public key (if none -> back to standard RDP security)
        """
        self._colorDepth = colorDepth
        self._privateKeyFileName = privateKeyFileName
        self._certificateFileName = certificateFileName
    
    def connectionLost(self, tpktLayer, reason):
        """
        @param reason: twisted reason
        """
        #retrieve controller
        x224Layer = tpktLayer._presentation
        mcsLayer = x224Layer._presentation
        secLayer = mcsLayer._channels[mcs.Channel.MCS_GLOBAL_CHANNEL]
        pduLayer = secLayer._presentation
        controller = pduLayer._listener
        controller.onClose()
    
    def buildRawLayer(self, addr):
        """
        @summary: Function call from twisted and build rdp protocol stack
        @param addr: destination address
        """
        controller = RDPServerController(self._colorDepth, self._privateKeyFileName, self._certificateFileName)
        self.buildObserver(controller, addr)
        return controller.getProtocol()
    
    def buildObserver(self, controller, addr):
        """
        @summary: Build observer use for connection
        @param controller: RDP stack controller
        @param addr: destination address
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "buildObserver", "ServerFactory")) 
        
class RDPClientObserver(object):
    """
    @summary: Class use to inform all RDP event handle by RDPY
    """
    def __init__(self, controller):
        """
        @param controller: RDP controller use to interact with protocol
        """
        self._controller = controller
        self._controller.addClientObserver(self)
        
    def onReady(self):
        """
        @summary: Stack is ready and connected
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onReady", "RDPClientObserver")) 
    
    def onSessionReady(self):
        """
        @summary: Windows session is ready
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onSessionReady", "RDPClientObserver")) 
    
    def onClose(self):
        """
        @summary: Stack is closes
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onClose", "RDPClientObserver")) 
    
    def onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data):
        """
        @summary: Notify bitmap update
        @param destLeft: xmin position
        @param destTop: ymin position
        @param destRight: xmax position because RDP can send bitmap with padding
        @param destBottom: ymax position because RDP can send bitmap with padding
        @param width: width of bitmap
        @param height: height of bitmap
        @param bitsPerPixel: number of bit per pixel
        @param isCompress: use RLE compression
        @param data: bitmap data
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onUpdate", "RDPClientObserver"))

    def onPointerHide(self):
        """
        @summary: Called when the server hides the pointer
        """
        pass

    def onPointerDefault(self):
        """
        @summary: Called when the server requests the default system pointer
        """
        pass

    def onPointerCached(self, cacheIndex):
        """
        @summary: Called when the server switches to a cached pointer
        @param cacheIndex: index of cached pointer
        """
        pass

    def onPointerUpdate(self, xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask):
        """
        @summary: Called when the server sends a new pointer shape
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
    
class RDPServerObserver(object):
    """
    @summary: Class use to inform all RDP event handle by RDPY
    """
    def __init__(self, controller):
        """
        @param controller: RDP controller use to interact with protocol
        """
        self._controller = controller
        self._controller.addServerObserver(self)
        
    def onReady(self):
        """
        @summary: Stack is ready and connected
        May be called after an setColorDepth too
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onReady", "RDPServerObserver"))
    
    def onClose(self):
        """
        @summary: Stack is closes
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onClose", "RDPClientObserver")) 
    
    def onKeyEventScancode(self, code, isPressed, isExtended):
        """
        @summary: Event call when a keyboard event is catch in scan code format
        @param code: {integer} scan code of key
        @param isPressed: {boolean} True if key is down
        @param isExtended: {boolean} True if a special key
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onKeyEventScanCode", "RDPServerObserver"))
    
    def onKeyEventUnicode(self, code, isPressed):
        """
        @summary: Event call when a keyboard event is catch in unicode format
        @param code: unicode of key
        @param isPressed: True if key is down
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onKeyEventUnicode", "RDPServerObserver"))
    
    def onPointerEvent(self, x, y, button, isPressed):
        """
        @summary: Event call on mouse event
        @param x: x position
        @param y: y position
        @param button: 1, 2, 3, 4 or 5 button
        @param isPressed: True if mouse button is pressed
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onPointerEvent", "RDPServerObserver"))
