"""Windows generic credentials; secrets never enter configuration files."""
import ctypes
from ctypes import wintypes
import os


class Credential(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD), ('TargetName', wintypes.LPWSTR),
                ('Comment', wintypes.LPWSTR), ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
                ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
                ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
                ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def api():
    if os.name != 'nt':
        raise RuntimeError('保存授权码仅支持 Windows')
    dll = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    dll.CredWriteW.argtypes = [ctypes.POINTER(Credential), wintypes.DWORD]
    dll.CredWriteW.restype = wintypes.BOOL
    dll.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(Credential))]
    dll.CredReadW.restype = wintypes.BOOL
    dll.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    dll.CredDeleteW.restype = wintypes.BOOL
    dll.CredFree.argtypes = [ctypes.c_void_p]
    return dll


def target(account):
    return 'LocalInvoiceExtractor/' + account.strip().lower()


def read(account):
    dll = api()
    pointer = ctypes.POINTER(Credential)()
    if not dll.CredReadW(target(account), 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:
            return ''
        raise RuntimeError('无法读取 Windows 凭据管理器')
    try:
        return ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize).decode('utf-16-le')
    finally:
        dll.CredFree(pointer)


def save(account, secret):
    dll = api()
    data = secret.encode('utf-16-le')
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    credential = Credential(Type=1, TargetName=target(account), CredentialBlobSize=len(data),
                            CredentialBlob=buffer, Persist=2, UserName=account)
    if not dll.CredWriteW(ctypes.byref(credential), 0):
        raise RuntimeError('无法保存到 Windows 凭据管理器')


def delete(account):
    dll = api()
    if not dll.CredDeleteW(target(account), 1, 0) and ctypes.get_last_error() != 1168:
        raise RuntimeError('无法删除已保存的授权码')
