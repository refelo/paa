"""Project-local Voyage credential, encrypted for the current Windows user."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import uuid

from .cards import CardError
from .library import LOCAL

KEY_FILE = LOCAL / 'credentials/voyage.dpapi'


def _crypt(value: bytes, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise CardError('本机加密凭据仅支持Windows；其他系统使用进程VOYAGE_API_KEY。')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            raise CardError('Windows凭据加解密失败；请确认使用保存时的Windows账户。')
        return ctypes.string_at(output.data, output.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if output.data:
            ctypes.memset(output.data, 0, output.size)
            kernel32.LocalFree(output.data)


def save_key(value: str) -> None:
    value = value.strip()
    if not 10 <= len(value) <= 1024 or any(c.isspace() for c in value):
        raise CardError('凭据为空或格式不支持。')
    encrypted = _crypt(value.encode('utf-8'))
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = KEY_FILE.with_name(KEY_FILE.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(encrypted)
        os.replace(temporary, KEY_FILE)
    finally:
        temporary.unlink(missing_ok=True)


def load_key() -> str:
    supplied = os.environ.get('VOYAGE_API_KEY')
    if supplied:
        return supplied
    if not KEY_FILE.is_file():
        raise CardError('未配置Voyage凭据；使用项目credential-set或进程VOYAGE_API_KEY。')
    try:
        return _crypt(KEY_FILE.read_bytes(), decrypt=True).decode('utf-8')
    except (OSError, UnicodeError) as error:
        raise CardError('无法读取项目加密凭据。') from error
