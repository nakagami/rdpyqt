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
@summary: Credential Security Support Provider
@see: https://msdn.microsoft.com/en-us/library/cc226764.aspx
"""

import binascii
import hashlib
import os
from pyasn1.type import namedtype, univ, tag
import pyasn1.codec.der.encoder as der_encoder
import pyasn1.codec.der.decoder as der_decoder
import pyasn1.codec.ber.encoder as ber_encoder

from rdpy.core.type import Stream
from twisted.internet import protocol
from OpenSSL import crypto
from rdpy.security import x509
from rdpy.core import error
from rdpy.core import log

class NegoToken(univ.Sequence):
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('negoToken', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0))),
    )

class NegoData(univ.SequenceOf):
    """
    @summary: contain spnego ntlm of kerberos data
    @see: https://msdn.microsoft.com/en-us/library/cc226781.aspx
    """
    componentType = NegoToken()

class TSRequest(univ.Sequence):
    """
    @summary: main structure
    @see: https://msdn.microsoft.com/en-us/library/cc226780.aspx
    """
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('version', univ.Integer().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0))),
        namedtype.OptionalNamedType('negoTokens', NegoData().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1))),
        namedtype.OptionalNamedType('authInfo', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 2))),
        namedtype.OptionalNamedType('pubKeyAuth', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 3))),
        namedtype.OptionalNamedType('errorCode', univ.Integer().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 4))),
        namedtype.OptionalNamedType('clientNonce', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 5))),
        )

class TSCredentials(univ.Sequence):
    """
    @summary: contain user information
    @see: https://msdn.microsoft.com/en-us/library/cc226782.aspx
    """
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('credType', univ.Integer().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0))),
        namedtype.NamedType('credentials', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1)))
        )
    
class TSPasswordCreds(univ.Sequence):
    """
    @summary: contain username and password
    @see: https://msdn.microsoft.com/en-us/library/cc226783.aspx
    """
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('domainName', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0))),
        namedtype.NamedType('userName', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1))),
        namedtype.NamedType('password', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 2)))
        )

class TSCspDataDetail(univ.Sequence):
    """
    @summary: smart card credentials
    @see: https://msdn.microsoft.com/en-us/library/cc226785.aspx
    """
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('keySpec', univ.Integer().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0))),
        namedtype.OptionalNamedType('cardName', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1))),
        namedtype.OptionalNamedType('readerName', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 2))),
        namedtype.OptionalNamedType('containerName', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 3))),
        namedtype.OptionalNamedType('cspName', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 4)))
        )

class TSSmartCardCreds(univ.Sequence):
    """
    @summary: smart card credentials
    @see: https://msdn.microsoft.com/en-us/library/cc226784.aspx
    """
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('pin', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0))),
        namedtype.NamedType('cspData', TSCspDataDetail().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1))),
        namedtype.OptionalNamedType('userHint', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 2))),
        namedtype.OptionalNamedType('domainHint', univ.OctetString().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 3)))
        )

class OpenSSLRSAPublicKey(univ.Sequence):
    """
    @summary: asn1 public rsa key
    @see: https://tools.ietf.org/html/rfc3447
    """
    componentType = namedtype.NamedTypes(
        namedtype.NamedType('unknow', univ.Integer()),
        namedtype.NamedType('modulus', univ.Integer()),
        namedtype.NamedType('publicExponent', univ.Integer()),
        )

def encodeDERTRequest(negoTypes = [], authInfo = None, pubKeyAuth = None, version = 2, clientNonce = None):
    """
    @summary: create TSRequest from list of Type
    @param negoTypes: {list(Type)}
    @param authInfo: {str} authentication info TSCredentials encrypted with authentication protocol
    @param pubKeyAuth: {str} public key encrypted with authentication protocol
    @param version: {int} CredSSP protocol version
    @param clientNonce: {bytes} 32-byte nonce for CredSSP v5+
    @return: {str} TRequest der encoded
    """
    negoData = NegoData().subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1))
    
    #fill nego data tokens
    i = 0
    for negoType in negoTypes:
        s = Stream()
        s.writeType(negoType)
        negoToken = NegoToken()
        negoToken.setComponentByPosition(0, s.getvalue())
        negoData.setComponentByPosition(i, negoToken)
        i += 1
        
    request = TSRequest()
    request.setComponentByName("version", univ.Integer(version).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0)))
    
    if i > 0:
        request.setComponentByName("negoTokens", negoData)
    
    if authInfo is not None:
        request.setComponentByName("authInfo", univ.OctetString(authInfo).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 2)))
    
    if pubKeyAuth is not None:
        request.setComponentByName("pubKeyAuth", univ.OctetString(pubKeyAuth).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 3))) 

    if clientNonce is not None:
        request.setComponentByName("clientNonce", univ.OctetString(clientNonce).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 5)))

    return der_encoder.encode(request)

def decodeDERTRequest(s):
    """
    @summary: Decode the stream as 
    @param s: {str}
    """
    return der_decoder.decode(s, asn1Spec=TSRequest())[0]

def getNegoTokens(tRequest):
    negoData = tRequest.getComponentByName("negoTokens")
    return [Stream(negoData.getComponentByPosition(i).getComponentByPosition(0).asOctets()) for i in range(len(negoData))]
    
def getPubKeyAuth(tRequest):
    return tRequest.getComponentByName("pubKeyAuth").asOctets()

def encodeDERTCredentials(domain, username, password):
    passwordCred = TSPasswordCreds()
    passwordCred.setComponentByName("domainName", univ.OctetString(domain).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0)))
    passwordCred.setComponentByName("userName", univ.OctetString(username).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1)))
    passwordCred.setComponentByName("password", univ.OctetString(password).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 2)))
    
    credentials = TSCredentials()
    credentials.setComponentByName("credType", univ.Integer(1).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 0)))
    credentials.setComponentByName("credentials", univ.OctetString(der_encoder.encode(passwordCred)).subtype(explicitTag=tag.Tag(tag.tagClassContext, tag.tagFormatConstructed, 1)))
    
    return der_encoder.encode(credentials)

class CSSP(protocol.Protocol):
    """
    @summary: Handle CSSP connection
    Proxy class for authentication
    """
    def __init__(self, layer, authenticationProtocol):
        """
        @param layer: {type.Layer.RawLayer}
        @param authenticationProtocol: {sspi.IAuthenticationProtocol}
        """
        log.debug(f"CSSP.__init__({layer}, {authenticationProtocol})")
        self._layer = layer
        self._authenticationProtocol = authenticationProtocol
        #IGenericSecurityService
        self._interface = None
        #function call at the end of nego
        self._callback = None
        self._recvBuffer = b''
        self._recvHandler = None
        
    def setFactory(self, factory):
        """
        @summary: Call by RawLayer Factory
        @param param: RawLayerClientFactory or RawLayerFactory
        """
        log.debug(f"CSSP.setFactory({factory})")
        self._layer.setFactory(factory)
        
    def dataReceived(self, data):
        """
        @summary:  Inherit from twisted.protocol class
                    main event of received data
        @param data: string data receive from twisted
        """
        # data: 4.1.2 Client X.224 Connection Request PDU
        log.debug(f"CSSP.dataRecievd() {len(data)=} {binascii.hexlify(data).decode('utf-8')}")
        self._layer.dataReceived(data)
    
    def connectionLost(self, reason):
        """
        @summary: Call from twisted engine when protocol is closed
        @param reason: str represent reason of close connection
        """
        log.debug(f"CSSP.connectionLost() {reason}")
        self._layer._factory.connectionLost(self, reason)
            
    def connectionMade(self):
        """
        @summary: install proxy
        """
        log.debug("CSSP.connectionMode()")
        self._layer.transport = self
        self._layer.getDescriptor = lambda:self.transport
        self._layer.connectionMade()
    
    def write(self, data):
        """
        @summary: write data on transport layer
        @param data: {str}
        """
        log.debug(f"CSSP.write() {len(data)=} {binascii.hexlify(data).decode('utf-8')}")
        self.transport.write(data)
    
    def startTLS(self, sslContext):
        """
        @summary: start TLS protocol
        @param sslContext: {ssl.ClientContextFactory | ssl.DefaultOpenSSLContextFactory} context use for TLS protocol
        """
        log.debug("CSSP.startTLS()")
        self.transport.startTLS(sslContext)

    @staticmethod
    def _getDERMessageLength(data):
        """
        @summary: Parse DER header to determine total message length.
        @param data: {bytes} raw data that may start with a DER-encoded message
        @return: {int|None} total message length, or None if not enough data for header
        """
        if len(data) < 2:
            return None
        pos = 1
        length_byte = data[pos]
        pos += 1
        if length_byte < 0x80:
            content_length = length_byte
        elif length_byte == 0x80:
            return None
        else:
            num_length_bytes = length_byte & 0x7f
            if len(data) < pos + num_length_bytes:
                return None
            content_length = int.from_bytes(data[pos:pos + num_length_bytes], 'big')
            pos += num_length_bytes
        return pos + content_length

    def _bufferedDataReceived(self, data):
        """
        @summary: Buffer incoming data and dispatch complete messages
                  to the current handler. Handles TCP fragmentation,
                  multiple messages in a single chunk, and TPKT wrapping.
        """
        self._recvBuffer += data
        while self._recvBuffer:
            if self._recvBuffer[0] == 0x03 and len(self._recvBuffer) >= 2 and self._recvBuffer[1] == 0x00:
                # TPKT header: version=0x03, reserved=0x00, length=2 bytes
                if len(self._recvBuffer) < 4:
                    log.debug(f"CSSP._bufferedDataReceived() buffering {len(self._recvBuffer)} bytes, need 4 for TPKT header")
                    break
                tpktLen = (self._recvBuffer[2] << 8) | self._recvBuffer[3]
                if len(self._recvBuffer) < tpktLen:
                    log.debug(f"CSSP._bufferedDataReceived() buffering {len(self._recvBuffer)} bytes, need {tpktLen} for TPKT")
                    break
                payload = self._recvBuffer[4:tpktLen]
                self._recvBuffer = self._recvBuffer[tpktLen:]
                # Strip X.224 Data header (02 F0 80) if present
                if len(payload) >= 3 and payload[0] == 0x02 and payload[1] == 0xF0 and payload[2] == 0x80:
                    payload = payload[3:]
                if self._recvHandler is not None and payload:
                    self._recvHandler(bytes(payload))
            else:
                msgLen = self._getDERMessageLength(self._recvBuffer)
                if msgLen is None or len(self._recvBuffer) < msgLen:
                    log.debug(f"CSSP._bufferedDataReceived() buffering {len(self._recvBuffer)} bytes, need {msgLen}")
                    break
                msgData = self._recvBuffer[:msgLen]
                self._recvBuffer = self._recvBuffer[msgLen:]
                if self._recvHandler is not None:
                    self._recvHandler(msgData)

    def startNLA(self, sslContext, callback = None):
        """
        @summary: start NLA authentication
        @param sslContext: {ssl.ClientContextFactory | ssl.DefaultOpenSSLContextFactory} context use for TLS protocol
        @param callback: {function} function call when cssp layer is read
        """
        log.debug("CSSP.startNLA()")
        self._callback = callback
        self._version = 6
        self._nonce = os.urandom(32)
        self._recvBuffer = b''
        self.startTLS(sslContext)
        #send negotiate message
        self.transport.write(encodeDERTRequest( negoTypes = [ self._authenticationProtocol.getNegotiateMessage() ], version = self._version ))
        #next state is receive a challenge
        self._recvHandler = self._processChallenge
        self.dataReceived = self._bufferedDataReceived

    def _processChallenge(self, data):
        """
        @summary: second state in cssp automata
        @param data : {bytes} one complete DER-encoded TSRequest
        """
        log.debug("CSSP._processChallenge()")
        request = decodeDERTRequest(data)
        #negotiate version with server
        serverVersion = int(request.getComponentByName("version"))
        self._version = min(self._version, serverVersion)
        log.debug(f"CSSP._processChallenge() negotiated CredSSP version {self._version}")

        message, self._interface = self._authenticationProtocol.getAuthenticateMessage(getNegoTokens(request)[0])
        #get back public key
        #convert from der to ber...
        pkey = self.transport.protocol._tlsConnection.get_peer_certificate().get_pubkey()

        log.debug(f"CSSP._processChallenge() PEM={crypto.dump_publickey(crypto.FILETYPE_PEM, pkey).decode('utf-8')}")

        public_numbers = pkey.to_cryptography_key().public_numbers()

        rsa = x509.RSAPublicKey()
        rsa.setComponentByName("modulus", univ.Integer(public_numbers.n))
        rsa.setComponentByName("publicExponent", univ.Integer(public_numbers.e))
        self._pubKeyBer = ber_encoder.encode(rsa)

        #send authenticate message with public key encoded
        if self._version >= 5:
            pubKeyData = hashlib.sha256(b"CredSSP Client-To-Server Binding Hash\0" + self._nonce + self._pubKeyBer).digest()
        else:
            pubKeyData = self._pubKeyBer

        b = encodeDERTRequest( negoTypes = [ message ], pubKeyAuth = self._interface.GSS_WrapEx(pubKeyData), version = self._version, clientNonce = self._nonce if self._version >= 5 else None)
        log.debug(f"CSSP._processChallenge() send {binascii.hexlify(b).decode('utf-8')} {len(b)=}")
        self.transport.write(b)
        #next step is received public key incremented by one
        self._recvHandler = self._processPubKeyInc

    def _processPubKeyInc(self, data):
        """
        @summary: the server send the pubKeyBer + 1 (v2-4) or SHA-256 hash (v5+)
        @param data : {bytes} one complete DER-encoded TSRequest
        """
        log.debug(f"CSSP._processPubKeyInc() {binascii.hexlify(data).decode('utf-8')} {len(data)}")
        request = decodeDERTRequest(data)
        pubKeyInc = self._interface.GSS_UnWrapEx(getPubKeyAuth(request))

        if self._version >= 5:
            expected = hashlib.sha256(b"CredSSP Server-To-Client Binding Hash\0" + self._nonce + self._pubKeyBer).digest()
            if pubKeyInc != expected:
                raise error.InvalidExpectedDataException("CSSP : Invalid public key hash")
        else:
            #check pubKeyInc = self._pubKeyBer + 1
            if not (self._pubKeyBer[1:] == pubKeyInc[1:] and self._pubKeyBer[0] + 1 == pubKeyInc[0]):
                raise error.InvalidExpectedDataException("CSSP : Invalid public key increment")
        domain, user, password = self._authenticationProtocol.getEncodedCredentials()
        log.debug(f"CSSP._processPubKeyInc() {binascii.hexlify(domain).decode('utf-8')} {binascii.hexlify(user).decode('utf-8')} {binascii.hexlify(password).decode('utf-8')}")
        #send credentials
        self.transport.write(encodeDERTRequest( authInfo = self._interface.GSS_WrapEx(encodeDERTCredentials(domain, user, password)), version = self._version))
        #reset state back to normal state
        self._recvHandler = None
        self._recvBuffer = b''
        self.dataReceived = lambda x: self.__class__.dataReceived(self, x)
        if self._callback is not None:
            from twisted.internet import reactor
            reactor.callLater(0, self._callback)
