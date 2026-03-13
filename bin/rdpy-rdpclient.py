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
example of use rdpy as rdp client
"""

import sys, os, getopt, socket

from PyQt6.QtWidgets import QApplication
from rdpy.ui.qt6 import RDPClientQt
from rdpy.protocol.rdp import rdp
from rdpy.core.error import RDPSecurityNegoFail
from rdpy.core import rss

import rdpy.core.log as log
log._LOG_LEVEL = log.Level.INFO


class RDPClientQtRecorder(RDPClientQt):
    """
    @summary: Widget with record session
    """
    def __init__(self, controller, width, height, rssRecorder):
        """
        @param controller: {RDPClientController} RDP controller
        @param width: {int} width of widget
        @param height: {int} height of widget
        @param rssRecorder: {rss.FileRecorder}
        """
        RDPClientQt.__init__(self, controller, width, height)
        self._screensize = width, height
        self._rssRecorder = rssRecorder
        
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
        log.debug(f"RDPClientRecorder.onUpdate() {destLeft}, {destTop}, {destRight}, {destBottom}, {width}, {height}, {bitsPerPixel}, {isCompress}, {data}")
        self._rssRecorder.update(destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, rss.UpdateFormat.BMP if isCompress else rss.UpdateFormat.RAW, data)
        RDPClientQt.onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data)
    
    def onReady(self):
        """
        @summary: Call when stack is ready
        """
        log.debug("RDPClientRecorder.onReady()")
        self._rssRecorder.screen(self._screensize[0], self._screensize[1], self._controller.getColorDepth())
        RDPClientQt.onReady(self)
          
    def onClose(self):
        """
        @summary: Call when stack is close
        """
        log.debug("RDPClientRecorder.onClose()")
        self._rssRecorder.close()
        RDPClientQt.onClose(self)
        
    def closeEvent(self, e):
        """
        @summary: Convert Qt close widget event into close stack event
        @param e: QCloseEvent
        """
        log.debug("RDPClientRecorder.onEvent()")
        self._rssRecorder.close()
        RDPClientQt.closeEvent(self, e)

class RDPClientQtFactory(rdp.ClientFactory):
    """
    @summary: Factory create a RDP GUI client
    """
    def __init__(self, app, width, height, username, password, domain, keyboardType, keyboardLayout, security, swap_alt_meta=False):
        """
        @param app: {QApplication} Qt application instance
        @param width: {integer} width of client
        @param heigth: {integer} heigth of client
        @param username: {str} username present to the server
        @param password: {str} password present to the server
        @param domain: {str} microsoft domain
        @param keyboardType: {str} name of gcc.KeyboardType attribute (e.g. IBM_101_102_KEYS)
        @param keyboardLayout: {str} name of gcc.KeyboardLayout attribute (e.g. US, FRENCH)
        @param security: {str} (ssl | rdp | nego)
        @param swap_alt_meta: {bool} swap Alt and Meta (Windows) keys
        """
        self._app = app
        self._width = width
        self._height = height
        self._username = username
        self._passwod = password
        self._domain = domain
        self._keyboardType = keyboardType
        self._keyboardLayout = keyboardLayout
        self._nego = security == "nego"
        if self._nego:
            if username != "" and password != "":
                self._security = rdp.SecurityLevel.RDP_LEVEL_NLA
            else:
                self._security = rdp.SecurityLevel.RDP_LEVEL_SSL
        else:
            self._security = security
        self._swap_alt_meta = swap_alt_meta
        self._w = None
        
    def buildObserver(self, controller, addr):
        """
        @summary:  Build RFB observer
                    We use a RDPClientQt as RDP observer
        @param controller: build factory and needed by observer
        @param addr: destination address
        @return: RDPClientQt
        """
        self._client = RDPClientQt(controller, self._width, self._height, self._swap_alt_meta)
        self._w = self._client.getWidget()
        self._w.setWindowTitle('rdpyqt6')
        self._w.show()
        
        controller.setUsername(self._username)
        controller.setPassword(self._passwod)
        controller.setDomain(self._domain)
        controller.setKeyboardType(self._keyboardType)
        controller.setKeyboardLayout(self._keyboardLayout)
        controller.setHostname(socket.gethostname())
        controller.setSecurityLevel(self._security)
        
        return self._client
    
    def clientConnectionLost(self, connector, reason):
        """
        @summary: Connection lost event
        @param connector: twisted connector use for rdp connection (use reconnect to restart connection)
        @param reason: str use to advertise reason of lost connection
        """
        if reason.type == RDPSecurityNegoFail and self._nego:
            log.info("due to security nego error back to standard RDP security layer")
            self._nego = False
            self._security = rdp.SecurityLevel.RDP_LEVEL_RDP
            self._client._widget.hide()
            connector.connect()
            return
        
        log.info("Lost connection : %s"%reason)
        from twisted.internet import reactor
        reactor.stop()
        self._app.exit()
        
    def clientConnectionFailed(self, connector, reason):
        """
        @summary: Connection failed event
        @param connector: twisted connector use for rdp connection (use reconnect to restart connection)
        @param reason: str use to advertise reason of lost connection
        """
        log.info("Connection failed : %s"%reason)
        from twisted.internet import reactor
        reactor.stop()
        self._app.exit()
        
def help():
    print ("""
    Usage: rdpy-rdpclient [options] ip[:port]"
    \t-u: user name
    \t-p: password
    \t-d: domain
    \t-w: width of screen [default : 1280]
    \t-l: height of screen [default : 1024]
    \t-f: enable full screen mode [default : False]
    \t-kt: keyboard type (e.g. IBM_101_102_KEYS) [default : IBM_101_102_KEYS]
    \t-kl: keyboard layout (e.g. US, FRENCH) [default : US]
    \t-o: optimized session (disable costly effect) [default : False]
    \t-r: rss_filepath Recorded Session Scenario [default : None]
    \t--swap-alt-meta: swap Alt and Meta (Windows/Super/Command) keys [default : False]
    """)


def main():
    username = ""
    password = ""
    domain = ""
    width = 1280
    height = 1024
    keyboardType = "IBM_101_102_KEYS"
    keyboardLayout = "US"
    swap_alt_meta = False
    
    argv = []
    for a in sys.argv[1:]:
        if a.startswith("-kt"):
            argv.append("--kt" + a[3:])
        elif a.startswith("-kl"):
            argv.append("--kl" + a[3:])
        else:
            argv.append(a)

    try:
        opts, args = getopt.getopt(argv, "hfou:p:d:w:l:r:", ["kt=", "kl=", "swap-alt-meta"])
    except getopt.GetoptError:
        help()
        sys.exit(1)
    for opt, arg in opts:
        if opt == "-h":
            help()
            sys.exit()
        elif opt == "-u":
            username = arg
        elif opt == "-p":
            password = arg
        elif opt == "-d":
            domain = arg
        elif opt == "-w":
            width = int(arg)
        elif opt == "-l":
            height = int(arg)
        elif opt == "--kt":
            keyboardType = arg
        elif opt == "--kl":
            keyboardLayout = arg
        elif opt == "--swap-alt-meta":
            swap_alt_meta = True

    if ':' in args[0]:
        ip, port = args[0].split(':')
    else:
        ip, port = args[0], "3389"
    
    app = QApplication(sys.argv)
    
    import qreactor
    qreactor.install()
    
    log.info("keyboard type set to %s" % keyboardType)
    log.info("keyboard layout set to %s" % keyboardLayout)

    from twisted.internet import reactor
    reactor.connectTCP(ip, int(port), RDPClientQtFactory(app, width, height, username, password, domain, keyboardType, keyboardLayout, "nego", swap_alt_meta))
    reactor.runReturn()
    app.exec()


if __name__ == '__main__':
    main()
