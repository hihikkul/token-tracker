"""Windows taskbar pin metadata, separate from the live usage gauge."""
import ctypes
import os
import subprocess
import uuid
from ctypes import wintypes

APP_ID = 'hihikkul.token-tracker'


class GUID(ctypes.Structure):
    _fields_ = [('data', ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PropertyKey(ctypes.Structure):
    _fields_ = [('fmtid', GUID), ('pid', wintypes.DWORD)]


class PropVariant(ctypes.Structure):
    # PROPVARIANT's union is 16 bytes on Win64, 8 on Win32.
    _fields_ = [('vt', ctypes.c_ushort), ('reserved', ctypes.c_ushort * 3),
                ('value', ctypes.c_void_p), ('padding', ctypes.c_ubyte * ctypes.sizeof(ctypes.c_void_p))]


def key(pid):
    return PropertyKey(GUID.parse('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3'), pid)


def window_store(hwnd):
    iid = GUID.parse('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99')  # IPropertyStore
    store = ctypes.c_void_p()
    get = ctypes.windll.shell32.SHGetPropertyStoreForWindow
    get.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    get.restype = ctypes.c_long
    hr = get(hwnd, ctypes.byref(iid), ctypes.byref(store))
    if hr < 0:
        raise OSError(f'SHGetPropertyStoreForWindow: {hr:#x}')
    return store


def method(store, index, *args):
    table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *args)(table[index])


def configure_pin(hwnd, executable, app, icon):
    """Explorer otherwise pins the host interpreter and uses its Python icon."""
    if os.name != 'nt':
        return
    command = subprocess.list2cmdline([str(executable), str(app)])
    store = window_store(hwnd)
    try:
        set_value = method(store, 6, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))
        # Set relaunch properties before the explicit window ID (Microsoft requirement).
        for pid, text in ((2, command), (3, f'{icon},0'), (4, 'Token Tracker'), (5, APP_ID)):
            buffer = ctypes.create_unicode_buffer(text)
            value = PropVariant()
            value.vt = 31  # VT_LPWSTR; SetValue copies the string
            value.value = ctypes.cast(buffer, ctypes.c_void_p).value
            hr = set_value(store, ctypes.byref(key(pid)), ctypes.byref(value))
            if hr < 0:
                raise OSError(f'IPropertyStore.SetValue: {hr:#x}')
    finally:
        method(store, 2)(store)  # Release; window stores do not require Commit
