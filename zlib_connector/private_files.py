"""Private file permissions through native Windows ACLs or POSIX mode bits.

Use create_private_file() to create a file with private permissions from its
first instant. protect_private_file() repairs an existing file's permissions,
but cannot revoke handles opened earlier. Path checks do not make directory
entries safe from replacement by a directory's owner.
No function reads file contents or exposes account identifiers in its errors.
"""
import ctypes
import os
from pathlib import Path
import stat
from typing import BinaryIO


_DWORD = ctypes.c_uint32
_BOOL = ctypes.c_int
_POINTER = ctypes.c_void_p
_OWNER_INFORMATION = 0x00000001
_DACL_INFORMATION = 0x00000004
_PROTECTED_DACL_INFORMATION = 0x80000000
_DACL_PROTECTED = 0x1000
_FILE_OBJECT = 1
_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"


class _PermissionFailure(Exception):
    pass


class _AceHeader(ctypes.Structure):
    _fields_ = [("kind", ctypes.c_ubyte), ("flags", ctypes.c_ubyte), ("size", ctypes.c_uint16)]


class _AclSize(ctypes.Structure):
    _fields_ = [("count", _DWORD), ("bytes_used", _DWORD), ("bytes_free", _DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("sid", _POINTER), ("attributes", _DWORD)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", _DWORD), ("descriptor", _POINTER), ("inherit_handle", _BOOL)]


def _checked(value):
    if not value:
        raise _PermissionFailure
    return value


class _WindowsSecurity:
    """Small native boundary; every Windows-owned allocation has a finally block."""

    def __init__(self):
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        pointer_out = ctypes.POINTER(_POINTER)
        dword_out = ctypes.POINTER(_DWORD)
        bool_out = ctypes.POINTER(_BOOL)

        def bind(library, name, arguments, result):
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
            return function

        self._get_process = bind(kernel, "GetCurrentProcess", [], _POINTER)
        self._create_file = bind(kernel, "CreateFileW",
                                 [ctypes.c_wchar_p, _DWORD, _DWORD, ctypes.POINTER(_SecurityAttributes),
                                  _DWORD, _DWORD, _POINTER], _POINTER)
        self._close = bind(kernel, "CloseHandle", [_POINTER], _BOOL)
        self._free = bind(kernel, "LocalFree", [_POINTER], _POINTER)
        self._open_token = bind(security, "OpenProcessToken", [_POINTER, _DWORD, pointer_out], _BOOL)
        self._token_information = bind(security, "GetTokenInformation",
                                       [_POINTER, ctypes.c_int, _POINTER, _DWORD, dword_out], _BOOL)
        self._valid_sid = bind(security, "IsValidSid", [_POINTER], _BOOL)
        self._sid_string = bind(security, "ConvertSidToStringSidW", [_POINTER, pointer_out], _BOOL)
        self._get_security = bind(security, "GetNamedSecurityInfoW",
                                  [ctypes.c_wchar_p, ctypes.c_int, _DWORD, pointer_out, pointer_out,
                                   pointer_out, pointer_out, pointer_out], _DWORD)
        self._set_security = bind(security, "SetNamedSecurityInfoW",
                                  [ctypes.c_wchar_p, ctypes.c_int, _DWORD, _POINTER, _POINTER,
                                   _POINTER, _POINTER], _DWORD)
        self._valid_acl = bind(security, "IsValidAcl", [_POINTER], _BOOL)
        self._acl_information = bind(security, "GetAclInformation",
                                     [_POINTER, _POINTER, _DWORD, ctypes.c_int], _BOOL)
        self._get_ace = bind(security, "GetAce", [_POINTER, _DWORD, pointer_out], _BOOL)
        self._descriptor_control = bind(security, "GetSecurityDescriptorControl",
                                        [_POINTER, ctypes.POINTER(ctypes.c_uint16), dword_out], _BOOL)
        self._descriptor_from_string = bind(security, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
                                            [ctypes.c_wchar_p, _DWORD, pointer_out, dword_out], _BOOL)
        self._descriptor_dacl = bind(security, "GetSecurityDescriptorDacl",
                                     [_POINTER, bool_out, pointer_out, bool_out], _BOOL)

    def _sid(self, pointer) -> str:
        _checked(pointer)
        _checked(self._valid_sid(pointer))
        value = _POINTER()
        try:
            _checked(self._sid_string(pointer, ctypes.byref(value)))
            _checked(value.value)
            return ctypes.wstring_at(value.value)
        finally:
            if value.value:
                self._free(value)

    def current_user(self) -> str:
        token = _POINTER()
        try:
            _checked(self._open_token(self._get_process(), 0x0008, ctypes.byref(token)))  # TOKEN_QUERY
            size = _DWORD()
            self._token_information(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            _checked(ctypes.sizeof(_TokenUser) <= size.value <= 65536)
            buffer = ctypes.create_string_buffer(size.value)
            _checked(self._token_information(token, 1, buffer, size.value, ctypes.byref(size)))
            return self._sid(_TokenUser.from_buffer(buffer).sid)
        finally:
            if token.value:
                self._close(token)

    def require(self, path: Path, current_user: str, *, protected: bool = False) -> None:
        descriptor, owner, dacl = _POINTER(), _POINTER(), _POINTER()
        try:
            result = self._get_security(str(path), _FILE_OBJECT, _OWNER_INFORMATION | _DACL_INFORMATION,
                                        ctypes.byref(owner), None, ctypes.byref(dacl), None,
                                        ctypes.byref(descriptor))
            _checked(result == 0 and descriptor.value and dacl.value)
            trusted = {current_user, _SYSTEM_SID, _ADMINISTRATORS_SID}
            # An owner outside this set could give itself access by replacing the DACL.
            _checked(self._sid(owner) in trusted)
            control, revision = ctypes.c_uint16(), _DWORD()
            _checked(self._descriptor_control(descriptor, ctypes.byref(control), ctypes.byref(revision)))
            if protected:
                _checked(control.value & _DACL_PROTECTED)
            _checked(self._valid_acl(dacl))
            information = _AclSize()
            _checked(self._acl_information(dacl, ctypes.byref(information), ctypes.sizeof(information), 2))
            user_can_read = False
            for index in range(information.count):
                ace = _POINTER()
                _checked(self._get_ace(dacl, index, ctypes.byref(ace)))
                _checked(ace.value)
                header = _AceHeader.from_address(ace.value)
                # Deny, conditional, callback and object ACEs are deliberately rejected.
                # Do not try to subtract a deny from a broad allow or infer group membership.
                _checked(header.kind == 0 and header.size >= 16)  # ACCESS_ALLOWED_ACE_TYPE
                mask = _DWORD.from_address(ace.value + 4).value
                sid_pointer = ace.value + 8
                subauthorities = ctypes.c_ubyte.from_address(sid_pointer + 1).value
                _checked(subauthorities <= 15 and 8 + subauthorities * 4 <= header.size - 8)
                sid = self._sid(sid_pointer)
                _checked(not mask or sid in trusted)
                if sid == current_user and not header.flags & 0x08:  # INHERIT_ONLY_ACE
                    user_can_read |= bool(mask & (0x00000001 | 0x80000000 | 0x10000000))
            _checked(user_can_read)
        finally:
            if descriptor.value:
                self._free(descriptor)

    def _apply_dacl(self, path: Path, sddl: str) -> None:
        descriptor, dacl = _POINTER(), _POINTER()
        present, defaulted = _BOOL(), _BOOL()
        try:
            _checked(self._descriptor_from_string(sddl, 1, ctypes.byref(descriptor), None))
            _checked(self._descriptor_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl),
                                           ctypes.byref(defaulted)))
            _checked(present.value and dacl.value)
            result = self._set_security(str(path), _FILE_OBJECT,
                                        _DACL_INFORMATION | _PROTECTED_DACL_INFORMATION,
                                        None, None, dacl, None)
            _checked(result == 0)
        finally:
            if descriptor.value:
                self._free(descriptor)

    def protect(self, path: Path) -> None:
        user = self.current_user()
        self._apply_dacl(path, f"D:P(A;;FA;;;{user})(A;;FA;;;SY)")
        self.require(path, user, protected=True)

    def create(self, path: Path) -> BinaryIO:
        import msvcrt

        user = self.current_user()
        descriptor = _POINTER()
        handle = None
        try:
            _checked(self._descriptor_from_string(f"D:P(A;;FA;;;{user})(A;;FA;;;SY)", 1,
                                                  ctypes.byref(descriptor), None))
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            created = self._create_file(str(path), 0xC0000000, 0x00000005, ctypes.byref(attributes),
                                         1, 0x00000080, None)  # READ|WRITE, share READ|DELETE, CREATE_NEW
            if created == ctypes.c_void_p(-1).value:
                if ctypes.get_last_error() in (80, 183):  # FILE_EXISTS / ALREADY_EXISTS
                    raise FileExistsError
                raise _PermissionFailure
            _checked(created)
            handle = created
            self.require(path, user, protected=True)
            file_descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            _checked(file_descriptor >= 0)
            handle = None  # The C runtime descriptor now owns the native handle.
            return _binary_file(file_descriptor)
        finally:
            if handle is not None:
                self._close(handle)
            if descriptor.value:
                self._free(descriptor)


def _regular_file(path) -> tuple[Path, os.stat_result]:
    path = Path(path).absolute()
    info = path.lstat()
    _checked(stat.S_ISREG(info.st_mode) and not getattr(info, "st_file_attributes", 0) & 0x400)
    return path, info


def require_private_file(path) -> None:
    """Reject a file whose privacy cannot be established without reading its contents.

    Windows accepts only simple allow ACEs for the process user, SYSTEM and built-in
    Administrators, with an explicit user read grant. Complex/deny ACLs fail closed.
    POSIX requires current-user ownership and no group/other permission bits.
    """
    try:
        path, info = _regular_file(path)
        if os.name == "nt":
            security = _WindowsSecurity()
            security.require(path, security.current_user())
        else:
            _checked(info.st_uid == os.getuid() and not stat.S_IMODE(info.st_mode) & 0o077
                     and info.st_mode & stat.S_IRUSR)
    except Exception:
        raise ValueError("The file permissions could not be verified as private.") from None


def protect_private_file(path) -> None:
    """Restrict an existing empty file before secret writes; never reads file contents."""
    try:
        path, _ = _regular_file(path)
        if os.name == "nt":
            _WindowsSecurity().protect(path)
        else:
            os.chmod(path, 0o600, follow_symlinks=False)
            require_private_file(path)
    except Exception:
        raise ValueError("The file permissions could not be made private.") from None


def _binary_file(descriptor: int) -> BinaryIO:
    try:
        return os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        raise


def create_private_file(path) -> BinaryIO:
    """Create an exclusive binary file, private before any handle can read its data.

    The caller owns the returned context-manageable stream and publishes/removes
    the file as needed. An existing file is never opened, truncated or chmodded.
    """
    try:
        path = Path(path).absolute()
        if os.name == "nt":
            return _WindowsSecurity().create(path)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        return _binary_file(descriptor)
    except FileExistsError:
        raise FileExistsError("The private file already exists.") from None
    except Exception:
        raise ValueError("The private file could not be created.") from None
