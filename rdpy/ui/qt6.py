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
Qt specific code

QRemoteDesktop is a widget use for render in rdpy
"""

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QCursor, QPixmap
from rdpy.protocol.rdp.rdp import RDPClientObserver
from rdpy.core.error import CallPureVirtualFuntion
import sys

from rdpy.core import rle
import rdpy.core.log as log

# Scan code swap table for --swap-alt-meta: swaps Alt keys with Meta (Windows) keys.
_ALT_META_SWAP = {
    0x38:   0xE05B,  # Left Alt -> Left Windows
    0xE05B: 0x38,    # Left Windows -> Left Alt
    0xE038: 0xE05C,  # Right Alt -> Right Windows
    0xE05C: 0xE038,  # Right Windows -> Right Alt
}

class QAdaptor(object):
    """
    @summary:  Adaptor model with link between protocol
                And Qt widget
    """
    def sendMouseEvent(self, e, isPressed):
        """
        @summary: Interface to send mouse event to protocol stack
        @param e: QMouseEvent
        @param isPressed: event come from press or release action
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "sendMouseEvent", "QAdaptor"))

    def sendKeyEvent(self, e, isPressed):
        """
        @summary: Interface to send key event to protocol stack
        @param e: QEvent
        @param isPressed: event come from press or release action
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "sendKeyEvent", "QAdaptor"))

    def sendWheelEvent(self, e):
        """
        @summary: Interface to send wheel event to protocol stack
        @param e: QWheelEvent
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "sendWheelEvent", "QAdaptor")) 

    def closeEvent(self, e):
        """
        @summary: Call when you want to close connection
        @param: QCloseEvent
        """ 
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "closeEvent", "QAdaptor"))

    def onResizeRequest(self, width, height):
        """
        @summary: Interface to handle window resize and request RDP reconnection
        @param width: {int} new width
        @param height: {int} new height
        """
        raise CallPureVirtualFuntion("%s:%s defined by interface %s"%(self.__class__, "onResizeRequest", "QAdaptor"))
    
def qtImageFormatFromRFBPixelFormat(pixelFormat):
    """
    @summary: convert RFB pixel format to QtGui.QImage format
    """
    if pixelFormat.BitsPerPixel.value == 32:
        return QtGui.QImage.Format.Format_RGB32
    elif pixelFormat.BitsPerPixel.value == 16:
        return QtGui.QImage.Format.Format_RGB16

def _flip_rows_inplace(buf, width, height, bytes_per_pixel):
    """Flip bottom-up bitmap data to top-down order in-place."""
    stride = width * bytes_per_pixel
    half = height // 2
    for y in range(half):
        top = y * stride
        bot = (height - 1 - y) * stride
        buf[top:top + stride], buf[bot:bot + stride] = buf[bot:bot + stride], buf[top:top + stride]


def RDPBitmapToQtImage(width, height, bitsPerPixel, isCompress, data):
    image = None

    # Ensure data is bytes for QImage compatibility
    if isinstance(data, str):
        data = data.encode('latin-1')
    elif not isinstance(data, (bytes, bytearray)):
        data = bytes(data)

    # Compressed data is already in top-down order from the RLE decoder.
    # We attach the buffer as _buffer_ref on the QImage so Python keeps
    # the data alive — avoiding an expensive .copy().
    #
    # Uncompressed data is bottom-up (Windows BMP convention).  We flip
    # it in-place inside a bytearray then wrap with QImage, avoiding the
    # extra allocation that .mirrored() would cause.

    if bitsPerPixel == 15:
        bpp = 2
        fmt = QtGui.QImage.Format.Format_RGB555
        if isCompress:
            buf = rle.bitmap_decompress(data, width, height, bpp)
            image = QtGui.QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            raw = bytearray(data)
            _flip_rows_inplace(raw, width, height, bpp)
            image = QtGui.QImage(raw, width, height, width * bpp, fmt)
            image._buffer_ref = raw

    elif bitsPerPixel == 16:
        bpp = 2
        fmt = QtGui.QImage.Format.Format_RGB16
        if isCompress:
            buf = rle.bitmap_decompress(data, width, height, bpp)
            image = QtGui.QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            raw = bytearray(data)
            _flip_rows_inplace(raw, width, height, bpp)
            image = QtGui.QImage(raw, width, height, width * bpp, fmt)
            image._buffer_ref = raw

    elif bitsPerPixel == 24:
        bpp = 3
        fmt = QtGui.QImage.Format.Format_BGR888
        if isCompress:
            buf = rle.bitmap_decompress(data, width, height, bpp)
            image = QtGui.QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            raw = bytearray(data)
            _flip_rows_inplace(raw, width, height, bpp)
            image = QtGui.QImage(raw, width, height, width * bpp, fmt)
            image._buffer_ref = raw

    elif bitsPerPixel == 32:
        bpp = 4
        fmt = QtGui.QImage.Format.Format_RGB32
        if isCompress:
            buf = rle.bitmap_decompress4(data, width, height)
            image = QtGui.QImage(buf, width, height, width * bpp, fmt)
            image._buffer_ref = buf
        else:
            raw = bytearray(data)
            raw[3::4] = b'\xff' * (width * height)
            _flip_rows_inplace(raw, width, height, bpp)
            image = QtGui.QImage(raw, width, height, width * bpp, fmt)
            image._buffer_ref = raw
    else:
        log.error("Receive image in bad format")
        image = QtGui.QImage(width, height, QtGui.QImage.Format.Format_RGB32)
    return image


class RDPClientQt(RDPClientObserver, QAdaptor):
    """
    @summary: Adaptor for RDP client
    """
    def __init__(self, controller, width, height, swap_alt_meta=False, widget=None):
        """
        @param controller: {RDPClientController} RDP controller
        @param width: {int} width of widget
        @param height: {int} height of widget
        @param swap_alt_meta: {bool} swap Alt and Meta (Windows) keys
        @param widget: {QRemoteDesktop} existing widget to reuse (for resize reconnection)
        """
        RDPClientObserver.__init__(self, controller)
        if widget is not None:
            self._widget = widget
            self._widget.setAdaptor(self)
        else:
            self._widget = QRemoteDesktop(width, height, self)
        self._pointerCache = {}
        self._swap_alt_meta = swap_alt_meta
        self._resizeCallback = None
        #set widget screen to RDP stack
        controller.setScreen(width, height)

        # Clipboard integration
        self._clipboard = QtWidgets.QApplication.clipboard()
        self._clipboardConnected = True
        self._clipboard.dataChanged.connect(self._onLocalClipboardChanged)
        controller.setClipboardCallbacks(
            self._onRemoteClipboardText,
            self._getLocalClipboardText,
        )

    def getWidget(self):
        """
        @return: widget use for render
        """
        return self._widget

    def disconnectClipboard(self):
        """Disconnect QClipboard signal to prevent stale sends after reconnection."""
        if self._clipboardConnected:
            try:
                self._clipboard.dataChanged.disconnect(self._onLocalClipboardChanged)
            except TypeError:
                pass
            self._clipboardConnected = False

    def _onLocalClipboardChanged(self):
        """Notify the RDP server that local clipboard content changed."""
        log.info("CLIPRDR DEBUG [Qt]: _onLocalClipboardChanged() fired")
        self._controller.onLocalClipboardChanged()

    def _onRemoteClipboardText(self, text):
        """Update the local clipboard with text received from the RDP server."""
        log.info("CLIPRDR DEBUG [Qt]: _onRemoteClipboardText() len=%d" % len(text))
        mimeData = QtCore.QMimeData()
        mimeData.setText(text)
        self._clipboard.setMimeData(mimeData)

    def _getLocalClipboardText(self):
        """Return current local clipboard text for sending to the RDP server."""
        mimeData = self._clipboard.mimeData()
        text = ""
        if mimeData and mimeData.hasText():
            text = mimeData.text()
        log.info("CLIPRDR DEBUG [Qt]: _getLocalClipboardText() -> %d chars" % len(text))
        return text

    def sendMouseEvent(self, e, isPressed):
        """
        @summary: Convert Qt mouse event to RDP mouse event
        @param e: qMouseEvent
        @param isPressed: event come from press(true) or release(false) action
        """
        button = e.button()
        buttonNumber = 0
        if button == QtCore.Qt.MouseButton.LeftButton:
            buttonNumber = 1
        elif button == QtCore.Qt.MouseButton.RightButton:
            buttonNumber = 2
        elif button == QtCore.Qt.MouseButton.MiddleButton:
            buttonNumber = 3
        self._controller.sendPointerEvent(int(e.pos().x()), int(e.pos().y()), buttonNumber, isPressed)

    # Mapping from macOS native key codes (kVK_xxx) to RDP scan codes.
    # Extended keys use the 0xE0xx form; the upper byte is stripped and
    # extended=True is passed to sendKeyEventScancode.
    _MACOS_KEYCODE_MAP = {
        0x00: 0x1E,       # kVK_ANSI_A
        0x01: 0x1F,       # kVK_ANSI_S
        0x02: 0x20,       # kVK_ANSI_D
        0x03: 0x21,       # kVK_ANSI_F
        0x04: 0x23,       # kVK_ANSI_H
        0x05: 0x22,       # kVK_ANSI_G
        0x06: 0x2C,       # kVK_ANSI_Z
        0x07: 0x2D,       # kVK_ANSI_X
        0x08: 0x2E,       # kVK_ANSI_C
        0x09: 0x2F,       # kVK_ANSI_V
        0x0B: 0x30,       # kVK_ANSI_B
        0x0C: 0x10,       # kVK_ANSI_Q
        0x0D: 0x11,       # kVK_ANSI_W
        0x0E: 0x12,       # kVK_ANSI_E
        0x0F: 0x13,       # kVK_ANSI_R
        0x10: 0x15,       # kVK_ANSI_Y
        0x11: 0x14,       # kVK_ANSI_T
        0x12: 0x02,       # kVK_ANSI_1
        0x13: 0x03,       # kVK_ANSI_2
        0x14: 0x04,       # kVK_ANSI_3
        0x15: 0x05,       # kVK_ANSI_4
        0x16: 0x07,       # kVK_ANSI_6
        0x17: 0x06,       # kVK_ANSI_5
        0x18: 0x0D,       # kVK_ANSI_Equal
        0x19: 0x0A,       # kVK_ANSI_9
        0x1A: 0x08,       # kVK_ANSI_7
        0x1B: 0x0C,       # kVK_ANSI_Minus
        0x1C: 0x09,       # kVK_ANSI_8
        0x1D: 0x0B,       # kVK_ANSI_0
        0x1E: 0x1B,       # kVK_ANSI_RightBracket
        0x1F: 0x18,       # kVK_ANSI_O
        0x20: 0x16,       # kVK_ANSI_U
        0x21: 0x1A,       # kVK_ANSI_LeftBracket
        0x22: 0x17,       # kVK_ANSI_I
        0x23: 0x19,       # kVK_ANSI_P
        0x24: 0x1C,       # kVK_Return
        0x25: 0x26,       # kVK_ANSI_L
        0x26: 0x24,       # kVK_ANSI_J
        0x27: 0x28,       # kVK_ANSI_Quote
        0x28: 0x25,       # kVK_ANSI_K
        0x29: 0x27,       # kVK_ANSI_Semicolon
        0x2A: 0x2B,       # kVK_ANSI_Backslash
        0x2B: 0x33,       # kVK_ANSI_Comma
        0x2C: 0x35,       # kVK_ANSI_Slash
        0x2D: 0x31,       # kVK_ANSI_N
        0x2E: 0x32,       # kVK_ANSI_M
        0x2F: 0x34,       # kVK_ANSI_Period
        0x30: 0x0F,       # kVK_Tab
        0x31: 0x39,       # kVK_Space
        0x32: 0x29,       # kVK_ANSI_Grave
        0x33: 0x0E,       # kVK_Delete (Backspace)
        0x35: 0x01,       # kVK_Escape
        0x37: 0xE05B,     # kVK_Command (Left Windows key, extended)
        0x38: 0x2A,       # kVK_Shift
        0x39: 0x3A,       # kVK_CapsLock
        0x3A: 0x38,       # kVK_Option (Left Alt)
        0x3B: 0x1D,       # kVK_Control (Left Ctrl)
        0x3C: 0x36,       # kVK_RightShift
        0x3D: 0xE038,     # kVK_RightOption (Right Alt, extended)
        0x3E: 0xE01D,     # kVK_RightControl (extended)
        0x40: 0x68,       # kVK_F17
        0x41: 0x53,       # kVK_ANSI_KeypadDecimal
        0x43: 0x37,       # kVK_ANSI_KeypadMultiply
        0x45: 0x4E,       # kVK_ANSI_KeypadPlus
        0x47: 0x45,       # kVK_ANSI_KeypadClear (NumLock)
        0x4B: 0xE035,     # kVK_ANSI_KeypadDivide (extended)
        0x4C: 0xE01C,     # kVK_ANSI_KeypadEnter (extended)
        0x4E: 0x4A,       # kVK_ANSI_KeypadMinus
        0x4F: 0x69,       # kVK_F18
        0x50: 0x6A,       # kVK_F19
        0x52: 0x52,       # kVK_ANSI_Keypad0
        0x53: 0x4F,       # kVK_ANSI_Keypad1
        0x54: 0x50,       # kVK_ANSI_Keypad2
        0x55: 0x51,       # kVK_ANSI_Keypad3
        0x56: 0x4B,       # kVK_ANSI_Keypad4
        0x57: 0x4C,       # kVK_ANSI_Keypad5
        0x58: 0x4D,       # kVK_ANSI_Keypad6
        0x59: 0x47,       # kVK_ANSI_Keypad7
        0x5B: 0x48,       # kVK_ANSI_Keypad8
        0x5C: 0x49,       # kVK_ANSI_Keypad9
        0x5D: 0x7D,       # kVK_JIS_Yen
        0x5E: 0x73,       # kVK_JIS_Underscore
        0x5F: 0x53,       # kVK_JIS_KeypadComma
        0x60: 0x3F,       # kVK_F5
        0x61: 0x40,       # kVK_F6
        0x62: 0x41,       # kVK_F7
        0x63: 0x3D,       # kVK_F3
        0x64: 0x42,       # kVK_F8
        0x65: 0x43,       # kVK_F9
        0x66: 0x3A,       # kVK_JIS_Eisu
        0x67: 0x57,       # kVK_F11
        0x68: 0x70,       # kVK_JIS_Kana
        0x69: 0x64,       # kVK_F13
        0x6A: 0x67,       # kVK_F16
        0x6B: 0x65,       # kVK_F14
        0x6D: 0x44,       # kVK_F10
        0x6F: 0x58,       # kVK_F12
        0x71: 0x66,       # kVK_F15
        0x72: 0xE052,     # kVK_Help (Insert, extended)
        0x73: 0xE047,     # kVK_Home (extended)
        0x74: 0xE049,     # kVK_PageUp (extended)
        0x75: 0xE053,     # kVK_ForwardDelete (Delete, extended)
        0x76: 0x3E,       # kVK_F4
        0x77: 0xE04F,     # kVK_End (extended)
        0x78: 0x3C,       # kVK_F2
        0x79: 0xE051,     # kVK_PageDown (extended)
        0x7A: 0x3B,       # kVK_F1
        0x7B: 0xE04B,     # kVK_LeftArrow (extended)
        0x7C: 0xE04D,     # kVK_RightArrow (extended)
        0x7D: 0xE050,     # kVK_DownArrow (extended)
        0x7E: 0xE048,     # kVK_UpArrow (extended)
    }

    def sendKeyEvent(self, e, isPressed):
        """
        @summary: Convert Qt key press event to RDP press event
        @param e: QKeyEvent
        @param isPressed: event come from press or release action
        """
        if sys.platform == "darwin":
            native_code = e.nativeVirtualKey()
            # macOS native key codes (kVK_xxx) differ entirely from RDP
            # scan codes, so use the explicit translation map.  Keys not in
            # the map have no direct RDP equivalent and are silently dropped.
            code = self._MACOS_KEYCODE_MAP.get(native_code)
            if code is None:
                return
        elif sys.platform == "linux":
            # X11/XKB native scan codes are 8 higher than RDP scan codes.
            code = e.nativeScanCode() - 8
        else:
            code = e.nativeScanCode()
        if self._swap_alt_meta:
            code = _ALT_META_SWAP.get(code, code)
        # Detect extended keys (e.g. arrows, Home, End): scan codes whose
        # upper byte is 0xE0 are sent with KBDFLAGS_EXTENDED set.
        extended = (code >> 8 == 0xE0)
        if extended:
            code = code & 0xFF
        self._controller.sendKeyEventScancode(code, isPressed, extended)

    def sendWheelEvent(self, e):
        """
        @summary: Convert Qt wheel event to RDP Wheel event
        @param e: QWheelEvent
        """
        # angleDelta() returns 1/8-degree units; one standard mouse wheel notch = 120
        angle = e.angleDelta()
        x = int(e.position().x())
        y = int(e.position().y())

        # Vertical scroll
        delta_y = angle.y()
        if delta_y != 0:
            scroll = delta_y
            if scroll == 0:
                scroll = 1 if delta_y > 0 else -1
            self._controller.sendWheelEvent(x, y, scroll, False)

        # Horizontal scroll
        delta_x = angle.x()
        if delta_x != 0:
            scroll = delta_x
            if scroll == 0:
                scroll = 1 if delta_x > 0 else -1
            self._controller.sendWheelEvent(x, y, scroll, True)

    def closeEvent(self, e):
        """
        @summary: Convert Qt close widget event into close stack event
        @param e: QCloseEvent
        """
        self._controller.close()

    def setResizeCallback(self, callback):
        """
        @param callback: callable(width, height) to invoke on resize
        """
        self._resizeCallback = callback

    def onResizeRequest(self, width, height):
        """
        @summary: Called when the user resizes the widget, triggers reconnection
        """
        if self._resizeCallback:
            self._resizeCallback(width, height)

    def onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data):
        """
        @summary: Notify bitmap update
        @param destLeft: {int} xmin position
        @param destTop: {int} ymin position
        @param destRight: {int} xmax position because RDP can send bitmap with padding
        @param destBottom: {int} ymax position because RDP can send bitmap with padding
        @param width: {int} width of bitmap
        @param height: {int} height of bitmap
        @param bitsPerPixel: {int} number of bit per pixel
        @param isCompress: {bool} use RLE compression
        @param data: {str} bitmap data
        """
        image = RDPBitmapToQtImage(width, height, bitsPerPixel, isCompress, data)
        self._widget.notifyImage(destLeft, destTop, image, destRight - destLeft + 1, destBottom - destTop + 1)

    def onReady(self):
        """
        @summary: Call when stack is ready
        @see: rdp.RDPClientObserver.onReady
        """
        #do something maybe a loader

    def onSessionReady(self):
        """
        @summary: Windows session is ready
        @see: rdp.RDPClientObserver.onSessionReady
        """
        pass

    def onClose(self):
        """
        @summary: Call when stack is close
        @see: rdp.RDPClientObserver.onClose
        """
        #do something maybe a message

    def onPointerHide(self):
        """
        @summary: Called when the server hides the pointer
        @see: rdp.RDPClientObserver.onPointerHide
        """
        self._widget.setCursor(QCursor(Qt.CursorShape.BlankCursor))

    def onPointerDefault(self):
        """
        @summary: Called when the server restores the default pointer
        @see: rdp.RDPClientObserver.onPointerDefault
        """
        self._widget.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def onPointerCached(self, cacheIndex):
        """
        @summary: Called when the server switches to a cached pointer
        @see: rdp.RDPClientObserver.onPointerCached
        """
        cursor = self._pointerCache.get(cacheIndex)
        if cursor is not None:
            self._widget.setCursor(cursor)
        else:
            log.debug("RDPClientQt.onPointerCached() cache miss for index %d, restoring default" % cacheIndex)
            self._widget.setCursor(QCursor(Qt.CursorShape.ArrowCursor))

    def onPointerUpdate(self, xorBpp, cacheIndex, hotSpotX, hotSpotY, width, height, andMask, xorMask):
        """
        @summary: Called when the server sends a new pointer shape
        @see: rdp.RDPClientObserver.onPointerUpdate
        """
        if width == 0 or height == 0:
            return

        # AND mask rows are padded to 2-byte (word) boundaries
        and_stride = ((width + 15) // 16) * 2

        if xorBpp == 32:
            # XOR mask is BGRA, bottom-up scan lines
            xor_stride = width * 4
            # Detect whether the XOR data contains any real alpha values.
            has_alpha = any(xorMask[i] != 0 for i in range(3, len(xorMask), 4))
            buf = bytearray(width * height * 4)
            for y in range(height):
                src_y = height - 1 - y  # bottom-up to top-down
                src_start = src_y * xor_stride
                dst_start = y * width * 4
                n = min(width * 4, len(xorMask) - src_start)
                if n > 0:
                    buf[dst_start:dst_start + n] = xorMask[src_start:src_start + n]
                and_row_start = src_y * and_stride
                if not has_alpha:
                    # Batch set all alpha to 255 (opaque), then fix transparent
                    buf[dst_start + 3 : dst_start + width * 4 : 4] = b'\xff' * width
                    for x in range(width):
                        byte_offset = and_row_start + x // 8
                        if byte_offset < len(andMask) and (andMask[byte_offset] >> (7 - (x & 7))) & 1:
                            px = dst_start + x * 4
                            if buf[px] == 0 and buf[px+1] == 0 and buf[px+2] == 0:
                                buf[px+3] = 0
                else:
                    for x in range(width):
                        byte_offset = and_row_start + x // 8
                        if byte_offset < len(andMask) and (andMask[byte_offset] >> (7 - (x & 7))) & 1:
                            px = dst_start + x * 4
                            if not (buf[px] == 0 and buf[px+1] == 0 and buf[px+2] == 0 and buf[px+3] == 0):
                                buf[px+3] = 255
            image = QtGui.QImage(bytes(buf), width, height, width * 4, QtGui.QImage.Format.Format_ARGB32)

        elif xorBpp == 24:
            # XOR mask is BGR, no alpha channel; use AND mask for transparency
            xor_stride = width * 3
            buf = bytearray(width * height * 4)
            for y in range(height):
                src_y = height - 1 - y
                dst_row = y * width * 4
                src_row = src_y * xor_stride
                # Copy BGR data and set default alpha to 255
                for x in range(width):
                    src_off = src_row + x * 3
                    dst_off = dst_row + x * 4
                    if src_off + 2 < len(xorMask):
                        buf[dst_off:dst_off + 3] = xorMask[src_off:src_off + 3]
                    buf[dst_off + 3] = 255
                # Fix transparent pixels based on AND mask
                and_row_start = src_y * and_stride
                for x in range(width):
                    byte_offset = and_row_start + x // 8
                    if byte_offset < len(andMask) and (andMask[byte_offset] >> (7 - (x & 7))) & 1:
                        dst_off = dst_row + x * 4
                        if buf[dst_off] == 0 and buf[dst_off+1] == 0 and buf[dst_off+2] == 0:
                            buf[dst_off + 3] = 0
            image = QtGui.QImage(bytes(buf), width, height, width * 4, QtGui.QImage.Format.Format_ARGB32)

        elif xorBpp == 1:
            # Monochrome: 1bpp XOR mask + AND mask; rows padded to 2-byte boundary
            xor_stride = ((width + 15) // 16) * 2
            buf = bytearray(width * height * 4)
            for y in range(height):
                src_y = height - 1 - y
                for x in range(width):
                    xor_byte_offset = src_y * xor_stride + x // 8
                    xor_bit = (xorMask[xor_byte_offset] >> (7 - (x & 7))) & 1 if xor_byte_offset < len(xorMask) else 0
                    and_byte_offset = src_y * and_stride + x // 8
                    and_bit = (andMask[and_byte_offset] >> (7 - (x & 7))) & 1 if and_byte_offset < len(andMask) else 0
                    px = (y * width + x) * 4
                    # AND=0, XOR=0 => black opaque
                    # AND=0, XOR=1 => white opaque
                    # AND=1, XOR=0 => transparent
                    # AND=1, XOR=1 => inverted (approximate as black)
                    if and_bit == 0:
                        buf[px:px + 4] = b'\xff\xff\xff\xff' if xor_bit else b'\x00\x00\x00\xff'
                    elif xor_bit == 0:
                        buf[px:px + 4] = b'\x00\x00\x00\x00'
                    else:
                        buf[px:px + 4] = b'\x00\x00\x00\xff'
            image = QtGui.QImage(bytes(buf), width, height, width * 4, QtGui.QImage.Format.Format_ARGB32)

        else:
            log.debug("RDPClientQt.onPointerUpdate() unsupported bpp=%d" % xorBpp)
            return

        pixmap = QPixmap.fromImage(image)
        cursor = QCursor(pixmap, hotSpotX, hotSpotY)
        self._pointerCache[cacheIndex] = cursor
        self._widget.setCursor(cursor)


class QRemoteDesktop(QtWidgets.QWidget):
    """
    @summary: Qt display widget
    """
    _updateSignal = QtCore.pyqtSignal(int, int, QtGui.QImage, int, int)

    def __init__(self, width, height, adaptor):
        """
        @param adaptor: {QAdaptor}
        @param width: {int} width of widget
        @param height: {int} height of widget
        """
        super(QRemoteDesktop, self).__init__()
        #adaptor use to send
        self._adaptor = adaptor
        self._buffer = None
        #bind mouse event
        self.setMouseTracking(True)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self._updateSignal.connect(self._doNotifyImage)
        #resize debounce
        self._programmaticResize = False
        self._resizeTimer = QtCore.QTimer()
        self._resizeTimer.setSingleShot(True)
        self._resizeTimer.timeout.connect(self._handleResizeTimeout)
        self._pendingSize = None
        #set correct size; buffer is created in resizeEvent
        self.resize(width, height)

    def notifyImage(self, x, y, qimage, width, height):
        """
        @summary: Function called from QAdaptor
        @param x: x position of new image
        @param y: y position of new image
        @param qimage: new QImage
        @param width: width of the image region
        @param height: height of the image region
        """
        self._updateSignal.emit(x, y, qimage, width, height)

    def _doNotifyImage(self, x, y, qimage, width, height):
        """
        @summary: Slot connected to _updateSignal, runs on the Qt main thread
        @param x: x position of new image
        @param y: y position of new image
        @param qimage: new QImage
        @param width: width of the image region
        @param height: height of the image region
        """
        #fill buffer image
        with QtGui.QPainter(self._buffer) as qp:
            qp.drawImage(x, y, qimage, 0, 0, width, height)
        #force update only the dirty region (avoids full-widget repaint)
        self.update(x, y, width, height)

    def setAdaptor(self, adaptor):
        """
        @summary: Update the adaptor (used when reconnecting with existing widget)
        @param adaptor: {QAdaptor}
        """
        self._adaptor = adaptor

    def resize(self, width, height):
        """
        @summary: Programmatic resize (does not trigger RDP reconnection)
        @param width: {int} width of widget
        @param height: {int} height of widget
        """
        self._programmaticResize = True
        QtWidgets.QWidget.resize(self, width, height)

    def resizeEvent(self, event):
        """
        @summary: Called by Qt whenever the widget size changes
        @param event: QResizeEvent
        """
        w = event.size().width()
        h = event.size().height()
        self._buffer = QtGui.QImage(w, h, QtGui.QImage.Format.Format_RGB32)
        if self._programmaticResize:
            self._programmaticResize = False
            return
        if w > 0 and h > 0:
            self._pendingSize = (w, h)
            self._resizeTimer.start(500)

    def _handleResizeTimeout(self):
        """
        @summary: Called after resize debounce period to trigger RDP reconnection
        """
        if self._pendingSize:
            w, h = self._pendingSize
            self._pendingSize = None
            self._adaptor.onResizeRequest(w, h)

    def paintEvent(self, e):
        """
        @summary: Call when Qt renderer engine estimate that is needed
        @param e: QEvent
        """
        #draw only the dirty rect to avoid blitting the full buffer every frame
        rect = e.rect()
        with QtGui.QPainter(self) as qp:
            qp.drawImage(rect, self._buffer, rect)

    def mouseMoveEvent(self, event):
        """
        @summary: Call when mouse move
        @param event: QMouseEvent
        """
        self._adaptor.sendMouseEvent(event, False)

    def mousePressEvent(self, event):
        """
        @summary: Call when button mouse is pressed
        @param event: QMouseEvent
        """
        self._adaptor.sendMouseEvent(event, True)

    def mouseReleaseEvent(self, event):
        """
        @summary: Call when button mouse is released
        @param event: QMouseEvent
        """
        self._adaptor.sendMouseEvent(event, False)

    def keyPressEvent(self, event):
        """
        @summary: Call when button key is pressed
        @param event: QKeyEvent
        """
        self._adaptor.sendKeyEvent(event, True)

    def keyReleaseEvent(self, event):
        """
        @summary: Call when button key is released
        @param event: QKeyEvent
        """
        self._adaptor.sendKeyEvent(event, False)

    def wheelEvent(self, event):
        """
        @summary: Call on wheel event
        @param event:    QWheelEvent
        """
        self._adaptor.sendWheelEvent(event)

    def closeEvent(self, event):
        """
        @summary: Call when widget is closed
        @param event: QCloseEvent
        """
        self._adaptor.closeEvent(event)
