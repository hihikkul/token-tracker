"""Windows taskbar pin metadata, separate from the live usage gauge."""
import ctypes
import os
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



def shortcut_store(filename):
    """Load a ShellLink and its property store; caller releases both interfaces."""
    clsid = GUID.parse('00021401-0000-0000-c000-000000000046')
    iid = GUID.parse('0000010b-0000-0000-c000-000000000046')  # IPersistFile
    link = ctypes.c_void_p()
    create = ctypes.windll.ole32.CoCreateInstance
    create.argtypes = [ctypes.POINTER(GUID), ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    create.restype = ctypes.c_long
    hr = create(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(link))
    if hr < 0:
        raise OSError(f'CoCreateInstance(ShellLink): {hr:#x}')
    try:
        hr = method(link, 5, wintypes.LPCWSTR, wintypes.DWORD)(link, str(filename), 2)
        if hr < 0:
            raise OSError(f'IPersistFile.Load: {hr:#x}')
        iid = GUID.parse('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99')
        store = ctypes.c_void_p()
        hr = method(link, 0, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
            link, ctypes.byref(iid), ctypes.byref(store))
        if hr < 0:
            raise OSError(f'QueryInterface(IPropertyStore): {hr:#x}')
        return link, store
    except Exception:
        method(link, 2)(link)
        raise


def set_shortcut_id(filename):
    """Pin static shortcut, use process identity plus WM_SETICON for the running app.

    Window RelaunchIconResource would force the static icon even while running.
    """
    ole = ctypes.windll.ole32
    initialized = ole.CoInitialize(None)
    link = store = None
    try:
        link, store = shortcut_store(filename)
        buffer = ctypes.create_unicode_buffer(APP_ID)
        value = PropVariant()
        value.vt = 31
        value.value = ctypes.cast(buffer, ctypes.c_void_p).value
        hr = method(store, 6, ctypes.POINTER(PropertyKey), ctypes.POINTER(PropVariant))(
            store, ctypes.byref(key(5)), ctypes.byref(value))
        if hr >= 0:
            hr = method(store, 7)(store)  # Commit
        if hr >= 0:
            hr = method(link, 6, wintypes.LPCWSTR, wintypes.BOOL)(link, str(filename), True)
        if hr < 0:
            raise OSError(f'Save shortcut AppUserModelID: {hr:#x}')
    finally:
        if store:
            method(store, 2)(store)
        if link:
            method(link, 2)(link)
        if initialized >= 0:
            ole.CoUninitialize()
