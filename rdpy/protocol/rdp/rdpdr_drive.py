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
RDPDR drive redirection device (MS-RDPEFS 2.2.3 File System Virtual Channel).
Exposes a local directory to the server as a redirected drive.

Ported from FreeRDP channels/drive/client (drive_main.c, drive_file.c).
Like FreeRDP's asynchronous mode, each drive processes its IRPs on its own
worker thread, so large file transfers do not block the Qt / Twisted
main loop. Completions are sent back from the reactor thread.

Protocol reference: [MS-RDPEFS], [MS-FSCC]
"""

import errno
import os
import queue
import re
import shutil
import stat
import struct
import threading
import zlib

import rdpy.core.log as log
from rdpy.core.filetimes import EPOCH_AS_FILETIME, HUNDREDS_OF_NANOSECONDS
from . import rdpdr
from .rdpdr import (
    RdpdrDevice, decodeUnicodeString, RDPDR_DTYP_FILESYSTEM,
    IRP_MJ_CREATE, IRP_MJ_CLOSE, IRP_MJ_READ, IRP_MJ_WRITE, IRP_MJ_QUERY_INFORMATION,
    IRP_MJ_SET_INFORMATION, IRP_MJ_QUERY_VOLUME_INFORMATION, IRP_MJ_DIRECTORY_CONTROL,
    IRP_MJ_DEVICE_CONTROL, IRP_MJ_LOCK_CONTROL, IRP_MN_QUERY_DIRECTORY,
    STATUS_SUCCESS, STATUS_UNSUCCESSFUL, STATUS_NOT_SUPPORTED, STATUS_ACCESS_DENIED,
    STATUS_NO_SUCH_FILE, STATUS_DEVICE_BUSY, STATUS_OBJECT_NAME_COLLISION,
    STATUS_INVALID_HANDLE, STATUS_NO_MORE_FILES, STATUS_NOT_A_DIRECTORY,
    STATUS_OBJECT_PATH_NOT_FOUND, STATUS_DIRECTORY_NOT_EMPTY, STATUS_DISK_FULL,
    STATUS_OBJECT_NAME_INVALID, STATUS_INVALID_PARAMETER,
)

_IS_WINDOWS = os.name == 'nt'
_O_BINARY = getattr(os, 'O_BINARY', 0)

# Access mask ([MS-SMB2] 2.2.13.1)
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_APPEND_DATA = 0x00000004
FILE_EXECUTE = 0x00000020
MAXIMUM_ALLOWED = 0x02000000
GENERIC_ALL = 0x10000000
GENERIC_EXECUTE = 0x20000000
GENERIC_WRITE = 0x40000000
GENERIC_READ = 0x80000000

_NEED_WRITE = FILE_WRITE_DATA | FILE_APPEND_DATA | GENERIC_WRITE | GENERIC_ALL
_NEED_READ = FILE_READ_DATA | FILE_EXECUTE | GENERIC_READ | GENERIC_EXECUTE | GENERIC_ALL | MAXIMUM_ALLOWED

# CreateDisposition
FILE_SUPERSEDE = 0x00000000
FILE_OPEN = 0x00000001
FILE_CREATE = 0x00000002
FILE_OPEN_IF = 0x00000003
FILE_OVERWRITE = 0x00000004
FILE_OVERWRITE_IF = 0x00000005

# CreateOptions
FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_DELETE_ON_CLOSE = 0x00001000

# Create response Information
FILE_SUPERSEDED = 0x00000000
FILE_OPENED = 0x00000001
FILE_OVERWRITTEN = 0x00000003

# File attributes ([MS-FSCC] 2.6)
FILE_ATTRIBUTE_READONLY = 0x00000001
FILE_ATTRIBUTE_HIDDEN = 0x00000002
FILE_ATTRIBUTE_SYSTEM = 0x00000004
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_ARCHIVE = 0x00000020
FILE_ATTRIBUTE_NORMAL = 0x00000080

# File information classes ([MS-FSCC] 2.4)
FileDirectoryInformation = 1
FileFullDirectoryInformation = 2
FileBothDirectoryInformation = 3
FileBasicInformation = 4
FileStandardInformation = 5
FileRenameInformation = 10
FileNamesInformation = 12
FileDispositionInformation = 13
FileAllocationInformation = 19
FileEndOfFileInformation = 20
FileAttributeTagInformation = 35

# File system information classes ([MS-FSCC] 2.5)
FileFsVolumeInformation = 1
FileFsSizeInformation = 3
FileFsDeviceInformation = 4
FileFsAttributeInformation = 5
FileFsFullSizeInformation = 7

FILE_DEVICE_DISK = 0x00000007
FILE_CASE_SENSITIVE_SEARCH = 0x00000001
FILE_CASE_PRESERVED_NAMES = 0x00000002
FILE_UNICODE_ON_DISK = 0x00000004
MAX_PATH = 260

# Values reported for the volume (FreeRDP reports "FAT32" with the same flags)
VOLUME_LABEL = "RDPYQT"
FILE_SYSTEM_NAME = "FAT32"
_BYTES_PER_SECTOR = 512
_SECTORS_PER_CLUSTER = 8

# Characters forbidden in the DEVICE_ANNOUNCE name (MS-RDPEFS 2.2.1.3)
_FORBIDDEN_NAME_CHARS = ':<>"/\\| '


def defaultDriveName(path, pathmod=os.path):
    """
    @summary: Name used when none is given: the directory name, or the
    drive letter for a Windows drive root ("C:\\" -> "C")
    @param path: {str} absolute path
    @return: {str}
    """
    drive, rest = pathmod.splitdrive(path)
    name = pathmod.basename(rest.rstrip('\\/'))
    if not name and drive:
        name = drive.rstrip(':\\/').split('\\')[-1]
    return name or "drive"


class _IrpError(Exception):
    """Abort IRP processing and answer with an NTSTATUS."""

    def __init__(self, status):
        Exception.__init__(self, "NTSTATUS 0x%08x" % status)
        self.status = status


def mapOSError(e):
    """
    @summary: Map a Python OSError to an NTSTATUS (FreeRDP drive_map_windows_err)
    @param e: {OSError}
    @return: {int} NTSTATUS
    """
    code = getattr(e, 'errno', None)
    if code in (errno.EACCES, errno.EPERM, errno.EROFS, errno.EISDIR):
        return STATUS_ACCESS_DENIED
    if code == errno.ENOENT:
        return STATUS_NO_SUCH_FILE
    if code == errno.EEXIST:
        return STATUS_OBJECT_NAME_COLLISION
    if code == errno.ENOTDIR:
        return STATUS_OBJECT_PATH_NOT_FOUND
    if code == errno.ENOTEMPTY:
        return STATUS_DIRECTORY_NOT_EMPTY
    if code in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)):
        return STATUS_DISK_FULL
    if code == errno.EBUSY:
        return STATUS_DEVICE_BUSY
    if code == errno.ENAMETOOLONG:
        return STATUS_OBJECT_NAME_INVALID
    if code == errno.EBADF:
        return STATUS_INVALID_HANDLE
    return STATUS_UNSUCCESSFUL


def toFiletime(seconds):
    """
    @summary: POSIX timestamp (float seconds) to Windows FILETIME
    """
    value = int(seconds * HUNDREDS_OF_NANOSECONDS) + EPOCH_AS_FILETIME
    return max(0, min(value, 0x7FFFFFFFFFFFFFFF))


def fromFiletime(filetime):
    """
    @summary: Windows FILETIME to POSIX timestamp (float seconds)
    """
    return (filetime - EPOCH_AS_FILETIME) / float(HUNDREDS_OF_NANOSECONDS)


def _creationTime(st):
    birth = getattr(st, 'st_birthtime', None)
    if birth is not None:
        return birth
    # st_ctime is the creation time on Windows but the change time on POSIX
    return st.st_ctime if _IS_WINDOWS else st.st_mtime


def fileAttributes(name, st):
    """
    @summary: Windows file attributes for a stat result (winpr FileAttributesFromStat)
    """
    winAttr = getattr(st, 'st_file_attributes', None)
    if winAttr is not None:
        attr = winAttr & (FILE_ATTRIBUTE_READONLY | FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM |
                          FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_ARCHIVE)
    else:
        attr = 0
        if stat.S_ISDIR(st.st_mode):
            attr |= FILE_ATTRIBUTE_DIRECTORY
        if not (st.st_mode & stat.S_IWUSR):
            attr |= FILE_ATTRIBUTE_READONLY
        if name.startswith('.') and name not in ('.', '..'):
            attr |= FILE_ATTRIBUTE_HIDDEN
    return attr or FILE_ATTRIBUTE_NORMAL


def _wildcardToRegex(pattern):
    """
    @summary: Compile a Windows search pattern (* ? and the DOS wildcards < > ")
    into a case-insensitive regular expression
    """
    if pattern in ('*', '*.*', '<.*'):
        return None  # match everything
    out = []
    for c in pattern:
        if c in ('*', '<'):
            out.append('.*')
        elif c == '?':
            out.append('.')
        elif c == '>':
            out.append('.?')
        elif c == '"':
            out.append(r'\.?')
        else:
            out.append(re.escape(c))
    return re.compile('^' + ''.join(out) + '$', re.IGNORECASE | re.DOTALL)


class _FindEntry(object):
    __slots__ = ('name', 'st')

    def __init__(self, name, st):
        self.name = name
        self.st = st


class DriveFile(object):
    """
    @summary: An open file or directory on a redirected drive (FreeRDP DRIVE_FILE)
    """

    def __init__(self, fileId, fullpath, isDir, fd, openFlags):
        self.id = fileId
        self.fullpath = fullpath
        self.isDir = isDir
        # OS file descriptor, None for directories and attribute-only opens
        self.fd = fd
        self.openFlags = openFlags
        self.deletePending = False
        self.findEntries = None
        self.findIndex = 0

    def stat(self):
        if self.fd is not None:
            return os.fstat(self.fd)
        return os.stat(self.fullpath)

    def closeHandle(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None


class DriveDevice(RdpdrDevice):
    """
    @summary: Redirect a local directory as a drive on the server
    """

    def __init__(self, name, path, asyncIo=True):
        """
        @param name: {str} name of the drive shown on the server
        @param path: {str} local directory to share
        @param asyncIo: {bool} process IRPs on a worker thread (default) or inline
        """
        basePath = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(basePath):
            raise ValueError("drive path is not a directory: %s" % path)
        if not name:
            name = defaultDriveName(basePath)

        # Filter 2.2.1.3 Device Announce Header forbidden symbols
        safeName = ''.join('_' if c in _FORBIDDEN_NAME_CHARS else c for c in name)
        RdpdrDevice.__init__(self, RDPDR_DTYP_FILESYSTEM, safeName,
                             safeName.encode('utf-8') + b'\x00')
        self.basePath = basePath
        self._files = {}
        self._asyncIo = asyncIo
        self._queue = None
        self._thread = None
        if asyncIo:
            self._queue = queue.Queue()
            self._thread = threading.Thread(target=self._threadFunc,
                                            name="rdpdr-drive-%s" % safeName)
            self._thread.daemon = True
            self._thread.start()

    # -------------------------------------------------------------------
    # RdpdrDevice interface
    # -------------------------------------------------------------------

    def irpRequest(self, irp):
        if self._asyncIo:
            self._queue.put(('irp', irp))
        else:
            self._processIrp(irp)

    def init(self):
        # The server forgot every handle when it restarts the channel
        if self._asyncIo:
            self._queue.put(('reset', None))
        else:
            self._closeAll()

    def free(self):
        if self._asyncIo:
            self._queue.put(('quit', None))
        else:
            self._closeAll()

    # -------------------------------------------------------------------
    # Worker thread (FreeRDP drive_thread_func)
    # -------------------------------------------------------------------

    def _threadFunc(self):
        while True:
            kind, irp = self._queue.get()
            if kind == 'quit':
                self._closeAll()
                break
            if kind == 'reset':
                self._closeAll()
                continue
            try:
                self._processIrp(irp)
            except Exception as e:  # never let the worker die
                log.error("RDPDR drive: unexpected error: %s" % e)

    def _closeAll(self):
        for f in list(self._files.values()):
            f.closeHandle()
        self._files.clear()

    # -------------------------------------------------------------------
    # IRP dispatch (FreeRDP drive_process_irp)
    # -------------------------------------------------------------------

    def _processIrp(self, irp):
        irp.ioStatus = STATUS_SUCCESS
        handlers = {
            IRP_MJ_CREATE: self._irpCreate,
            IRP_MJ_CLOSE: self._irpClose,
            IRP_MJ_READ: self._irpRead,
            IRP_MJ_WRITE: self._irpWrite,
            IRP_MJ_QUERY_INFORMATION: self._irpQueryInformation,
            IRP_MJ_SET_INFORMATION: self._irpSetInformation,
            IRP_MJ_QUERY_VOLUME_INFORMATION: self._irpQueryVolumeInformation,
            IRP_MJ_LOCK_CONTROL: self._irpSilentIgnore,
            IRP_MJ_DIRECTORY_CONTROL: self._irpDirectoryControl,
            IRP_MJ_DEVICE_CONTROL: self._irpDeviceControl,
        }
        handler = handlers.get(irp.majorFunction)
        try:
            if handler is None:
                irp.ioStatus = STATUS_NOT_SUPPORTED
            else:
                handler(irp)
        except ValueError as e:
            # Malformed request: FreeRDP drops it, which leaves the server
            # waiting forever. Answer it with an error instead.
            log.warning("RDPDR drive: malformed IRP 0x%x: %s" % (irp.majorFunction, e))
            irp.output = bytearray()
            irp.ioStatus = STATUS_INVALID_PARAMETER
        irp.complete()

    def _getFile(self, fileId):
        return self._files.get(fileId)

    def _resolvePath(self, rawPath):
        """
        @summary: Combine the drive base path with a server path
        (FreeRDP drive_file_combine_fullpath). Rejects '..' components and,
        on Windows clients, components that would escape the base directory
        (drive letters, alternate data streams).
        @param rawPath: {str} path relative to the drive root, '\\' separated
        @return: {str} local absolute path
        """
        parts = []
        for part in re.split(r'[\\/]', rawPath):
            if part in ('', '.'):
                continue
            if part == '..':
                log.warning("RDPDR drive: received invalid file path '%s' from server, aborting!" % rawPath)
                raise _IrpError(STATUS_ACCESS_DENIED)
            if _IS_WINDOWS and (':' in part or part.rstrip('. ') != part):
                raise _IrpError(STATUS_OBJECT_NAME_INVALID)
            if '\x00' in part:
                raise _IrpError(STATUS_OBJECT_NAME_INVALID)
            parts.append(part)
        if not parts:
            return self.basePath
        return os.path.join(self.basePath, *parts)

    # -------------------------------------------------------------------
    # IRP_MJ_CREATE (MS-RDPEFS 2.2.1.4.1 / 2.2.1.5.1)
    # -------------------------------------------------------------------

    def _irpCreate(self, irp):
        s = irp.input
        desiredAccess = s.u32()
        s.u64()  # AllocationSize: a hint only, it does not change the end of file
        fileAttributes = s.u32()
        s.u32()  # SharedAccess, not enforced on POSIX (like FreeRDP)
        createDisposition = s.u32()
        createOptions = s.u32()
        pathLength = s.u32()
        path = decodeUnicodeString(s.read(pathLength))

        fileId = 0
        information = 0
        try:
            fullpath = self._resolvePath(path)
            fileId = self.devman.nextId()
            f = self._openFile(fileId, fullpath, desiredAccess, createDisposition,
                               createOptions, fileAttributes)
            self._files[fileId] = f
            if createDisposition == FILE_OPEN_IF:
                information = FILE_OPENED
            elif createDisposition == FILE_OVERWRITE_IF:
                information = FILE_OVERWRITTEN
            else:
                information = FILE_SUPERSEDED
            log.debug("RDPDR drive: create '%s' -> fileId=%d dir=%s" % (path, fileId, f.isDir))
        except _IrpError as e:
            irp.ioStatus = e.status
            fileId = 0
        except OSError as e:
            irp.ioStatus = mapOSError(e)
            fileId = 0
            log.debug("RDPDR drive: create '%s' failed: %s" % (path, e))

        irp.output += struct.pack('<IB', fileId, information)

    def _openFile(self, fileId, fullpath, desiredAccess, createDisposition, createOptions,
                  fileAttributes):
        """
        @summary: FreeRDP drive_file_init
        """
        try:
            st = os.stat(fullpath)
            exists = True
        except FileNotFoundError:
            exists = False
        except NotADirectoryError:
            raise _IrpError(STATUS_OBJECT_PATH_NOT_FOUND)

        if exists:
            if stat.S_ISDIR(st.st_mode):
                if createDisposition == FILE_CREATE:
                    raise _IrpError(STATUS_OBJECT_NAME_COLLISION)
                if createOptions & FILE_NON_DIRECTORY_FILE:
                    raise _IrpError(STATUS_ACCESS_DENIED)
                f = DriveFile(fileId, fullpath, True, None, 0)
                f.deletePending = bool(createOptions & FILE_DELETE_ON_CLOSE)
                return f
            if createOptions & FILE_DIRECTORY_FILE:
                raise _IrpError(STATUS_NOT_A_DIRECTORY)
            if not stat.S_ISREG(st.st_mode):
                # FIFOs, sockets and devices could block the worker forever
                raise _IrpError(STATUS_ACCESS_DENIED)
        else:
            parent = os.path.dirname(fullpath)
            if not os.path.isdir(parent):
                raise _IrpError(STATUS_OBJECT_PATH_NOT_FOUND)
            if createOptions & FILE_DIRECTORY_FILE:
                # only create the directory if the disposition allows it
                if createDisposition in (FILE_OPEN_IF, FILE_CREATE):
                    os.mkdir(fullpath)
                    f = DriveFile(fileId, fullpath, True, None, 0)
                    f.deletePending = bool(createOptions & FILE_DELETE_ON_CLOSE)
                    return f
                raise _IrpError(STATUS_NO_SUCH_FILE)
            if createDisposition in (FILE_OPEN, FILE_OVERWRITE):
                raise _IrpError(STATUS_NO_SUCH_FILE)

        flags = _O_BINARY
        if createDisposition in (FILE_SUPERSEDE, FILE_OVERWRITE_IF):    # CREATE_ALWAYS
            flags |= os.O_CREAT | os.O_TRUNC
        elif createDisposition == FILE_CREATE:                          # CREATE_NEW
            flags |= os.O_CREAT | os.O_EXCL
        elif createDisposition == FILE_OPEN_IF:                         # OPEN_ALWAYS
            flags |= os.O_CREAT
        elif createDisposition == FILE_OVERWRITE:                       # TRUNCATE_EXISTING
            flags |= os.O_TRUNC

        mustWrite = bool(desiredAccess & _NEED_WRITE) or bool(flags & os.O_TRUNC)
        mode = 0o444 if (fileAttributes & FILE_ATTRIBUTE_READONLY) else 0o666

        writable = False
        if mustWrite:
            fd = os.open(fullpath, flags | os.O_RDWR, mode)
            writable = True
        elif desiredAccess & MAXIMUM_ALLOWED:
            try:
                fd = os.open(fullpath, flags | os.O_RDWR, mode)
                writable = True
            except PermissionError:
                fd = os.open(fullpath, flags | os.O_RDONLY, mode)
        else:
            try:
                fd = os.open(fullpath, flags | os.O_RDONLY, mode)
            except PermissionError:
                # Opening only for attributes / delete does not need read
                # permission on the content.
                if exists and not (desiredAccess & _NEED_READ):
                    fd = None
                else:
                    raise
        # flags used to reopen the file after a rename on Windows
        openFlags = (flags & ~(os.O_CREAT | os.O_EXCL | os.O_TRUNC)) | (
            os.O_RDWR if writable else os.O_RDONLY)
        f = DriveFile(fileId, fullpath, False, fd, openFlags)
        f.deletePending = bool(createOptions & FILE_DELETE_ON_CLOSE)
        return f

    # -------------------------------------------------------------------
    # IRP_MJ_CLOSE
    # -------------------------------------------------------------------

    def _irpClose(self, irp):
        f = self._files.pop(irp.fileId, None)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
        else:
            f.closeHandle()
            if f.deletePending:
                try:
                    if f.isDir:
                        os.rmdir(f.fullpath)
                    else:
                        os.remove(f.fullpath)
                except OSError as e:
                    irp.ioStatus = mapOSError(e)
        irp.output += b'\x00' * 4  # Padding

    # -------------------------------------------------------------------
    # IRP_MJ_READ / IRP_MJ_WRITE
    # -------------------------------------------------------------------

    def _irpRead(self, irp):
        length = irp.input.u32()
        offset = irp.input.u64()
        f = self._getFile(irp.fileId)
        data = b''
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
        elif f.fd is None:
            irp.ioStatus = STATUS_ACCESS_DENIED if not f.isDir else STATUS_UNSUCCESSFUL
        elif length > 0:
            try:
                os.lseek(f.fd, offset, os.SEEK_SET)
                chunks = []
                remaining = length
                while remaining > 0:
                    chunk = os.read(f.fd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b''.join(chunks)
            except OSError as e:
                irp.ioStatus = mapOSError(e)
                data = b''
        irp.output += struct.pack('<I', len(data)) + data

    def _irpWrite(self, irp):
        s = irp.input
        length = s.u32()
        offset = s.u64()
        s.skip(20)  # Padding
        data = s.read(length)
        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            length = 0
        elif f.fd is None or not (f.openFlags & os.O_RDWR):
            irp.ioStatus = STATUS_ACCESS_DENIED
            length = 0
        else:
            try:
                os.lseek(f.fd, offset, os.SEEK_SET)
                view = memoryview(data)
                while view:
                    written = os.write(f.fd, view)
                    view = view[written:]
            except OSError as e:
                irp.ioStatus = mapOSError(e)
                length = 0
        irp.output += struct.pack('<IB', length, 0)  # Length + Padding

    # -------------------------------------------------------------------
    # IRP_MJ_QUERY_INFORMATION
    # -------------------------------------------------------------------

    def _irpQueryInformation(self, irp):
        infoClass = irp.input.u32()
        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            irp.output += struct.pack('<I', 0)
            return
        try:
            st = f.stat()
        except OSError as e:
            irp.ioStatus = mapOSError(e)
            irp.output += struct.pack('<I', 0)
            return

        attr = fileAttributes(os.path.basename(f.fullpath), st)
        isDir = stat.S_ISDIR(st.st_mode)
        size = 0 if isDir else st.st_size
        if infoClass == FileBasicInformation:
            irp.output += struct.pack('<I', 36) + struct.pack(
                '<QQQQI',
                toFiletime(_creationTime(st)),   # CreationTime
                toFiletime(st.st_atime),         # LastAccessTime
                toFiletime(st.st_mtime),         # LastWriteTime
                toFiletime(st.st_mtime),         # ChangeTime
                attr)                            # FileAttributes (Reserved(4) MUST NOT be added)
        elif infoClass == FileStandardInformation:
            irp.output += struct.pack('<I', 22) + struct.pack(
                '<QQIBB',
                size,                            # AllocationSize
                size,                            # EndOfFile
                st.st_nlink,                     # NumberOfLinks
                1 if f.deletePending else 0,     # DeletePending
                1 if isDir else 0)               # Directory (Reserved(2) MUST NOT be added)
        elif infoClass == FileAttributeTagInformation:
            irp.output += struct.pack('<III', 8, attr, 0)  # FileAttributes + ReparseTag
        else:
            log.debug("RDPDR drive: unhandled FsInformationClass %d" % infoClass)
            irp.ioStatus = STATUS_NOT_SUPPORTED
            irp.output += struct.pack('<I', 0)

    # -------------------------------------------------------------------
    # IRP_MJ_SET_INFORMATION
    # -------------------------------------------------------------------

    def _irpSetInformation(self, irp):
        s = irp.input
        infoClass = s.u32()
        length = s.u32()
        s.skip(24)  # Padding
        body = rdpdr.StreamReader(s.read(length))
        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
        else:
            try:
                if infoClass == FileBasicInformation:
                    self._setBasicInformation(f, length, body)
                elif infoClass in (FileEndOfFileInformation, FileAllocationInformation):
                    self._setSize(f, infoClass, length, body)
                elif infoClass == FileDispositionInformation:
                    self._setDispositionInformation(f, length, body)
                elif infoClass == FileRenameInformation:
                    self._setRenameInformation(f, length, body)
                else:
                    log.debug("RDPDR drive: unhandled set FsInformationClass %d" % infoClass)
                    irp.ioStatus = STATUS_NOT_SUPPORTED
            except _IrpError as e:
                irp.ioStatus = e.status
            except OSError as e:
                irp.ioStatus = mapOSError(e)
        irp.output += struct.pack('<I', length)

    def _setBasicInformation(self, f, length, s):
        if length != 36:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        s.u64()  # CreationTime: cannot be set portably
        lastAccess = s.u64()
        lastWrite = s.u64()
        change = s.u64()
        attributes = s.u32()

        def isSet(value):
            # 0: do not change, -1/-2: stop/resume automatic updates
            return 0 < value < 0xFFFFFFFFFFFFFFFE

        if isSet(change) and change > lastWrite:
            lastWrite = change
        if isSet(lastAccess) or isSet(lastWrite):
            st = os.stat(f.fullpath)
            atime = fromFiletime(lastAccess) if isSet(lastAccess) else st.st_atime
            mtime = fromFiletime(lastWrite) if isSet(lastWrite) else st.st_mtime
            os.utime(f.fullpath, (atime, mtime))
        if attributes:
            st = os.stat(f.fullpath)
            if attributes & FILE_ATTRIBUTE_READONLY:
                mode = st.st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
            else:
                mode = st.st_mode | stat.S_IWUSR
            if mode != st.st_mode:
                os.chmod(f.fullpath, stat.S_IMODE(mode))

    def _setSize(self, f, infoClass, length, s):
        if length != 8:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        size = s.i64()
        if size < 0:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        if f.isDir:
            raise _IrpError(STATUS_ACCESS_DENIED)
        current = f.stat().st_size
        if infoClass == FileAllocationInformation and size >= current:
            return  # growing the allocation does not change the end of file
        if f.fd is not None and f.openFlags & os.O_RDWR:
            os.ftruncate(f.fd, size)
        else:
            os.truncate(f.fullpath, size)

    def _setDispositionInformation(self, f, length, s):
        if f.isDir and os.listdir(f.fullpath):
            raise _IrpError(STATUS_DIRECTORY_NOT_EMPTY)
        deletePending = s.u8() if length else 1
        if deletePending:
            st = os.stat(f.fullpath)
            if fileAttributes(os.path.basename(f.fullpath), st) & FILE_ATTRIBUTE_READONLY:
                raise _IrpError(STATUS_ACCESS_DENIED)
            if os.path.normcase(f.fullpath) == os.path.normcase(self.basePath):
                raise _IrpError(STATUS_ACCESS_DENIED)
        f.deletePending = bool(deletePending)

    def _setRenameInformation(self, f, length, s):
        if length < 6:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        replaceIfExists = s.u8()
        s.u8()  # RootDirectory
        fileNameLength = s.u32()
        if length != 6 + fileNameLength:
            raise _IrpError(STATUS_INVALID_PARAMETER)
        target = self._resolvePath(decodeUnicodeString(s.read(fileNameLength)))
        if os.path.normcase(f.fullpath) == os.path.normcase(self.basePath):
            raise _IrpError(STATUS_ACCESS_DENIED)

        if os.path.lexists(target):
            sameFile = False
            try:
                sameFile = os.path.samefile(f.fullpath, target)  # case-only rename
            except OSError:
                pass
            if not sameFile:
                if not replaceIfExists:
                    raise _IrpError(STATUS_OBJECT_NAME_COLLISION)
                if os.path.isdir(target):
                    raise _IrpError(STATUS_ACCESS_DENIED)

        reopen = _IS_WINDOWS and f.fd is not None
        if reopen:
            # Windows cannot rename an open file
            f.closeHandle()
        try:
            try:
                os.replace(f.fullpath, target)
            except OSError as e:
                if e.errno != errno.EXDEV:
                    raise
                shutil.move(f.fullpath, target)  # MOVEFILE_COPY_ALLOWED
            f.fullpath = target
        finally:
            if reopen:
                f.fd = os.open(f.fullpath, f.openFlags)

    # -------------------------------------------------------------------
    # IRP_MJ_QUERY_VOLUME_INFORMATION
    # -------------------------------------------------------------------

    def _irpQueryVolumeInformation(self, irp):
        infoClass = irp.input.u32()
        clusterSize = _BYTES_PER_SECTOR * _SECTORS_PER_CLUSTER
        try:
            usage = shutil.disk_usage(self.basePath)
            totalUnits = usage.total // clusterSize
            freeUnits = usage.free // clusterSize
        except OSError:
            totalUnits = freeUnits = 0

        out = irp.output
        if infoClass == FileFsVolumeInformation:
            label = (VOLUME_LABEL + '\0').encode('utf-16-le')
            try:
                created = toFiletime(_creationTime(os.stat(self.basePath)))
            except OSError:
                created = 0
            serial = zlib.crc32(self.basePath.encode('utf-8', 'replace')) & 0xFFFFFFFF
            out += struct.pack('<I', 17 + len(label))
            out += struct.pack('<QIIB', created, serial, len(label), 0)  # Reserved(1) MUST NOT be added
            out += label
        elif infoClass == FileFsSizeInformation:
            out += struct.pack('<I', 24)
            out += struct.pack('<QQII', totalUnits, freeUnits, _SECTORS_PER_CLUSTER, _BYTES_PER_SECTOR)
        elif infoClass == FileFsAttributeInformation:
            fsName = (FILE_SYSTEM_NAME + '\0').encode('utf-16-le')
            out += struct.pack('<I', 12 + len(fsName))
            out += struct.pack('<III', FILE_CASE_SENSITIVE_SEARCH | FILE_CASE_PRESERVED_NAMES |
                               FILE_UNICODE_ON_DISK, MAX_PATH, len(fsName))
            out += fsName
        elif infoClass == FileFsFullSizeInformation:
            out += struct.pack('<I', 32)
            out += struct.pack('<QQQII', totalUnits, freeUnits, freeUnits, _SECTORS_PER_CLUSTER,
                               _BYTES_PER_SECTOR)
        elif infoClass == FileFsDeviceInformation:
            out += struct.pack('<III', 8, FILE_DEVICE_DISK, 0)
        else:
            log.debug("RDPDR drive: unhandled FsInformationClass %d" % infoClass)
            irp.ioStatus = STATUS_UNSUCCESSFUL
            out += struct.pack('<I', 0)

    # -------------------------------------------------------------------
    # IRP_MJ_LOCK_CONTROL / IRP_MJ_DEVICE_CONTROL
    # -------------------------------------------------------------------

    def _irpSilentIgnore(self, irp):
        irp.input.u32()
        irp.output += struct.pack('<I', 0)

    def _irpDeviceControl(self, irp):
        irp.output += struct.pack('<I', 0)  # OutputBufferLength

    # -------------------------------------------------------------------
    # IRP_MJ_DIRECTORY_CONTROL
    # -------------------------------------------------------------------

    def _irpDirectoryControl(self, irp):
        if irp.minorFunction == IRP_MN_QUERY_DIRECTORY:
            self._irpQueryDirectory(irp)
        else:
            # IRP_MN_NOTIFY_CHANGE_DIRECTORY is not implemented (as in FreeRDP)
            irp.ioStatus = STATUS_NOT_SUPPORTED
            irp.output += struct.pack('<I', 0)

    def _irpQueryDirectory(self, irp):
        s = irp.input
        infoClass = s.u32()
        initialQuery = s.u8()
        pathLength = s.u32()
        s.skip(23)  # Padding
        path = decodeUnicodeString(s.read(pathLength))

        f = self._getFile(irp.fileId)
        if f is None:
            irp.ioStatus = STATUS_UNSUCCESSFUL
            irp.output += struct.pack('<I', 0)
            return

        try:
            if initialQuery:
                f.findEntries = self._findFirst(path)
                f.findIndex = 0
            if not f.findEntries or f.findIndex >= len(f.findEntries):
                if initialQuery or f.findEntries is None:
                    raise _IrpError(STATUS_NO_SUCH_FILE)
                raise _IrpError(STATUS_NO_MORE_FILES)
            entry = f.findEntries[f.findIndex]
            f.findIndex += 1
            irp.output += self._encodeDirEntry(infoClass, entry)
        except _IrpError as e:
            irp.ioStatus = e.status
            irp.output += struct.pack('<IB', 0, 0)  # Length + Padding
        except OSError as e:
            irp.ioStatus = mapOSError(e)
            irp.output += struct.pack('<IB', 0, 0)

    def _findFirst(self, path):
        """
        @summary: Snapshot the entries matching a search path like '\\dir\\*'
        (FindFirstFileW + FindNextFileW)
        """
        sep = max(path.rfind('\\'), path.rfind('/'))
        dirPart, pattern = path[:sep + 1], path[sep + 1:]
        directory = self._resolvePath(dirPart)
        if not pattern:
            pattern = '*'

        if not re.search(r'[*?<>"]', pattern):
            # no wildcard: a single, exact entry
            fullpath = self._resolvePath(path)
            st = os.lstat(fullpath)
            if stat.S_ISLNK(st.st_mode):
                st = os.stat(fullpath)
            return [_FindEntry(os.path.basename(fullpath), st)]

        regex = _wildcardToRegex(pattern)
        dirStat = os.stat(directory)
        if not stat.S_ISDIR(dirStat.st_mode):
            raise _IrpError(STATUS_NOT_A_DIRECTORY)

        entries = []
        # '.' and '..' are reported like FindFirstFile does on a real file system;
        # '..' uses the directory itself so nothing outside the share is touched
        for dot in ('.', '..'):
            if regex is None or regex.match(dot):
                entries.append(_FindEntry(dot, dirStat))
        for name in sorted(os.listdir(directory)):
            if regex is not None and not regex.match(name):
                continue
            full = os.path.join(directory, name)
            try:
                st = os.stat(full)
            except OSError:
                try:
                    st = os.lstat(full)  # dangling symlink
                except OSError:
                    continue
            entries.append(_FindEntry(name, st))
        return entries

    def _encodeDirEntry(self, infoClass, entry):
        st = entry.st
        nameW = entry.name.encode('utf-16-le', 'surrogatepass')
        isDir = stat.S_ISDIR(st.st_mode)
        size = 0 if isDir else st.st_size
        attr = fileAttributes(entry.name, st)
        times = struct.pack('<QQQQ',
                            toFiletime(_creationTime(st)),  # CreationTime
                            toFiletime(st.st_atime),        # LastAccessTime
                            toFiletime(st.st_mtime),        # LastWriteTime
                            toFiletime(st.st_mtime))        # ChangeTime
        sizes = struct.pack('<QQI', size, size, attr)       # EndOfFile, AllocationSize, FileAttributes
        header = struct.pack('<II', 0, 0)                   # NextEntryOffset, FileIndex

        if infoClass == FileDirectoryInformation:
            body = header + times + sizes + struct.pack('<I', len(nameW)) + nameW
        elif infoClass == FileFullDirectoryInformation:
            body = header + times + sizes + struct.pack('<II', len(nameW), 0) + nameW  # + EaSize
        elif infoClass == FileBothDirectoryInformation:
            body = (header + times + sizes + struct.pack('<II', len(nameW), 0) +
                    struct.pack('<B', 0) +    # ShortNameLength (Reserved(1) MUST NOT be added)
                    b'\x00' * 24 + nameW)     # ShortName
        elif infoClass == FileNamesInformation:
            body = header + struct.pack('<I', len(nameW)) + nameW
        else:
            log.debug("RDPDR drive: unhandled directory FsInformationClass %d" % infoClass)
            raise _IrpError(STATUS_NOT_SUPPORTED)
        return struct.pack('<I', len(body)) + body
