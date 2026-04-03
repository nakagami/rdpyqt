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

import sys, getopt, socket
import getpass
import threading

from PyQt6.QtWidgets import QApplication
from rdpy.ui.qt6 import RDPClientQt, QRemoteDesktop, _get_qt_invoker
from rdpy.protocol.rdp import rdp
from rdpy.core.error import RDPSecurityNegoFail
import rdpy.core.log as log
log._LOG_LEVEL = log.Level.INFO


class RDPClientQtRecorder(RDPClientQt):
    """
    @summary: Widget with record session
    """
    def __init__(self, controller, width, height):
        """
        @param controller: {RDPClientController} RDP controller
        @param width: {int} width of widget
        @param height: {int} height of widget
        """
        RDPClientQt.__init__(self, controller, width, height)
        self._screensize = width, height
        
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
        RDPClientQt.onUpdate(self, destLeft, destTop, destRight, destBottom, width, height, bitsPerPixel, isCompress, data)
    
    def onReady(self):
        """
        @summary: Call when stack is ready
        """
        log.debug("RDPClientRecorder.onReady()")
        RDPClientQt.onReady(self)
          
    def onClose(self):
        """
        @summary: Call when stack is close
        """
        log.debug("RDPClientRecorder.onClose()")
        RDPClientQt.onClose(self)
        
    def closeEvent(self, e):
        """
        @summary: Convert Qt close widget event into close stack event
        @param e: QCloseEvent
        """
        log.debug("RDPClientRecorder.onEvent()")
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
        self._resizing = False
        
    def buildObserver(self, controller, addr):
        """
        @summary:  Build RFB observer
                    We use a RDPClientQt as RDP observer
        @param controller: build factory and needed by observer
        @param addr: destination address
        @return: RDPClientQt
        """
        # Disconnect old clipboard signal before creating new observer
        if hasattr(self, '_client') and self._client is not None:
            self._client.disconnectClipboard()

        if self._w is not None:
            self._client = RDPClientQt(controller, self._width, self._height, self._swap_alt_meta, widget=self._w)
        else:
            self._client = RDPClientQt(controller, self._width, self._height, self._swap_alt_meta)
            self._w = self._client.getWidget()
            self._w.setWindowTitle('rdpyqt6')
            self._w.show()
        self._client.setResizeCallback(self._onResize)
        
        controller.setUsername(self._username)
        controller.setPassword(self._passwod)
        controller.setDomain(self._domain)
        controller.setKeyboardType(self._keyboardType)
        controller.setKeyboardLayout(self._keyboardLayout)
        controller.setHostname(socket.gethostname())
        controller.setSecurityLevel(self._security)
        
        #set routing token for server redirection reconnection
        if self._redirectRoutingToken:
            controller.setRoutingToken(self._redirectRoutingToken)
            self._redirectRoutingToken = None
        
        return self._client
    
    def _onResize(self, width, height):
        """
        @summary: Called when user resizes the window, triggers reconnection
        """
        self._width = width
        self._height = height
        self._resizing = True
        # Close must be called from the Twisted thread, not the Qt timer thread
        from twisted.internet import reactor
        reactor.callFromThread(self._client._controller.close)

    def clientConnectionLost(self, connector, reason):
        """
        @summary: Connection lost event
        @param connector: twisted connector use for rdp connection (use reconnect to restart connection)
        @param reason: str use to advertise reason of lost connection
        """
        if self._redirectRoutingToken:
            log.info("Reconnecting with routing token for server redirection")
            connector.connect()
            return

        if self._resizing:
            log.info("Reconnecting with new screen size %dx%d" % (self._width, self._height))
            self._resizing = False
            connector.connect()
            return

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
        # quit() is documented thread-safe in Qt; posts a quit event to the main loop
        self._app.quit()
        
    def clientConnectionFailed(self, connector, reason):
        """
        @summary: Connection failed event
        @param connector: twisted connector use for rdp connection (use reconnect to restart connection)
        @param reason: str use to advertise reason of lost connection
        """
        log.info("Connection failed : %s"%reason)
        from twisted.internet import reactor
        reactor.stop()
        self._app.quit()
        
def help():
    print ("""
    Usage: rdpy-rdpclient [options] ip[:port]"
    \t-u: user name
    \t-p: password
    \t-d: domain
    \t-w: width of screen [default : 1280]
    \t-h: height of screen [default : 1024]
    \t-kt: keyboard type (e.g. IBM_101_102_KEYS) [default : IBM_101_102_KEYS]
    \t-kl: keyboard layout (e.g. US, FRENCH) [default : US]
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
        opts, args = getopt.getopt(argv, "fou:p:d:w:h:r:", ["kt=", "kl=", "swap-alt-meta"])
    except getopt.GetoptError:
        help()
        sys.exit(1)
    for opt, arg in opts:
        if opt == "-u":
            username = arg
        elif opt == "-p":
            password = arg
        elif opt == "-d":
            domain = arg
        elif opt == "-w":
            width = int(arg)
        elif opt == "-h":
            height = int(arg)
        elif opt == "--kt":
            keyboardType = arg
        elif opt == "--kl":
            keyboardLayout = arg
        elif opt == "--swap-alt-meta":
            swap_alt_meta = True

    if not username:
        username = input("User: ")
    if not password:
        password = getpass.getpass()

    if ':' in args[0]:
        ip, port = args[0].split(':')
    else:
        ip, port = args[0], "3389"
    
    app = QApplication(sys.argv)
    
    from twisted.internet import reactor

    log.info("keyboard type set to %s" % keyboardType)
    log.info("keyboard layout set to %s" % keyboardLayout)

    factory = RDPClientQtFactory(app, width, height, username, password, domain, keyboardType, keyboardLayout, "nego", swap_alt_meta)

    # Pre-create the Qt invoker on the Qt main thread so the _QtInvoker QObject
    # lives on the Qt main thread.  If created lazily from the Twisted thread it
    # would be owned by that thread and QueuedConnection would not dispatch to Qt.
    _get_qt_invoker()

    # Pre-create the widget on the Qt main thread so buildObserver (which runs
    # on the Twisted thread) never has to create Qt objects directly.
    initial_widget = QRemoteDesktop(width, height, None)
    initial_widget.setWindowTitle('rdpyqt6')
    initial_widget.show()
    factory._w = initial_widget

    reactor.connectTCP(ip, int(port), factory)

    # Run the Twisted reactor on a background thread so it never blocks Qt's
    # event loop.  installSignalHandlers=False is required when the reactor
    # is not on the main thread.
    t = threading.Thread(
        target=reactor.run,
        kwargs={'installSignalHandlers': False},
        daemon=True,
        name='twisted-reactor',
    )
    t.start()

    app.exec()


if __name__ == '__main__':
    main()
