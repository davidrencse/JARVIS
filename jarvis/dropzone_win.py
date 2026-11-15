"""The Windows edge drop zone: a thin, faint, always-on-top strip on the edge Lan Mouse
crosses to the other laptop. Dragging files from Explorer (or the desktop) onto
it hands their file paths to the clipboard watcher, which sends them to the peer's Desktop, the
same as the Wayland zone in dropzone.py.

Lan Mouse never crosses while a mouse button is held, so a drag stops at the edge, right
over this strip. Pure ctypes; runs as a thread of the clipboard watcher, no extra process.
"""
import ctypes
import os
import sys
import threading

WIDTH = 6        # physical px: thin enough to sit on the screen edge unnoticed
ALPHA = 70       # 0-255; faint blue so the edge is findable mid-drag
COLOR = 0x00E08A3C  # COLORREF 0x00BBGGRR: a JARVIS blue

WM_DESTROY = 0x0002
WM_DISPLAYCHANGE = 0x007E
WM_TIMER = 0x0113
WM_DROPFILES = 0x0233
WM_COPYGLOBALDATA = 0x0049
WM_COPYDATA = 0x004A
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
WS_EX_TOPMOST = 0x00000008
WS_EX_ACCEPTFILES = 0x00000010
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x2
MSGFLT_ALLOW = 1
SWP_NOACTIVATE = 0x0010
HWND_TOPMOST = -1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79


def geometry(edge, virtual, width=WIDTH):
    """(x, y, w, h) of the strip on `edge` of the virtual screen (x, y, w, h)."""
    vx, vy, vw, vh = virtual
    if edge == "right":
        return vx + vw - width, vy, width, vh
    if edge == "top":
        return vx, vy, vw, width
    if edge == "bottom":
        return vx, vy + vh - width, vw, width
    return vx, vy, width, vh  # left, and anything unreadable


def run(edge, on_drop, log=print):
    """Show the strip and call on_drop([paths]) for each drop. Blocks (message loop)."""
    from ctypes import wintypes as w
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [("style", w.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                    ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]

    user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND,
                                       w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.CreateWindowExW.restype = w.HWND
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
    user32.RegisterClassW.restype = w.ATOM
    user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, w.UINT]
    user32.SetLayeredWindowAttributes.argtypes = [w.HWND, w.COLORREF, w.BYTE, w.DWORD]
    user32.ChangeWindowMessageFilterEx.argtypes = [w.HWND, w.UINT, w.DWORD, w.LPVOID]
    user32.SetTimer.argtypes = [w.HWND, ctypes.c_size_t, w.UINT, w.LPVOID]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(w.MSG)]
    user32.DispatchMessageW.restype = LRESULT
    shell32.DragQueryFileW.argtypes = [w.HANDLE, w.UINT, w.LPWSTR, w.UINT]
    shell32.DragQueryFileW.restype = w.UINT
    shell32.DragFinish.argtypes = [w.HANDLE]
    shell32.DragAcceptFiles.argtypes = [w.HWND, w.BOOL]
    gdi32.CreateSolidBrush.argtypes = [w.COLORREF]
    gdi32.CreateSolidBrush.restype = w.HBRUSH
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel32.GetModuleHandleW.restype = w.HMODULE
    try:  # physical pixels, so the strip sits exactly on the edge at any display scale
        user32.SetThreadDpiAwarenessContext.argtypes = [w.HANDLE]
        user32.SetThreadDpiAwarenessContext(w.HANDLE(-4))  # per-monitor v2
    except (AttributeError, OSError):
        pass

    def place(hwnd):
        virtual = tuple(user32.GetSystemMetrics(i) for i in (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN,
                                                             SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN))
        x, y, cx, cy = geometry(edge, virtual)
        user32.SetWindowPos(hwnd, w.HWND(HWND_TOPMOST), x, y, cx, cy, SWP_NOACTIVATE)

    def dropped(hdrop):
        paths = []
        try:
            for i in range(shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)):
                size = shell32.DragQueryFileW(hdrop, i, None, 0) + 1
                buf = ctypes.create_unicode_buffer(size)
                shell32.DragQueryFileW(hdrop, i, buf, size)
                paths.append(buf.value)
        finally:
            shell32.DragFinish(hdrop)
        return paths

    def proc(hwnd, msg, wparam, lparam):
        if msg == WM_DROPFILES:
            paths = dropped(w.HANDLE(wparam))
            if paths:  # off the message loop: sending can take a while
                threading.Thread(target=on_drop, args=(paths,), daemon=True).start()
            return 0
        if msg in (WM_DISPLAYCHANGE, WM_TIMER):  # monitors changed; keep above new windows
            place(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(proc)  # keep a reference for the window's lifetime
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSW(lpfnWndProc=wndproc, hInstance=hinst, lpszClassName="JarvisDropZone",
                   hbrBackground=gdi32.CreateSolidBrush(COLOR),
                   hCursor=user32.LoadCursorW(None, ctypes.c_void_p(32512)))
    if not user32.RegisterClassW(ctypes.byref(wc)):
        raise OSError(f"RegisterClassW failed: {ctypes.get_last_error()}")
    hwnd = user32.CreateWindowExW(
        WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_ACCEPTFILES | WS_EX_LAYERED | WS_EX_NOACTIVATE,
        "JarvisDropZone", "JARVIS drop zone", WS_POPUP | WS_VISIBLE, 0, 0, 1, 1,
        None, None, hinst, None)
    if not hwnd:
        raise OSError(f"CreateWindowExW failed: {ctypes.get_last_error()}")
    user32.SetLayeredWindowAttributes(hwnd, 0, ALPHA, LWA_ALPHA)
    for message in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):  # drops from an unelevated
        user32.ChangeWindowMessageFilterEx(hwnd, message, MSGFLT_ALLOW, None)  # Explorer
    shell32.DragAcceptFiles(hwnd, True)
    place(hwnd)
    user32.SetTimer(hwnd, 1, 5000, None)
    log(f"drop zone on the {edge} edge")
    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":  # manual check on Windows: python -m jarvis.dropzone_win left
    run(sys.argv[1] if len(sys.argv) > 1 else "left",
        lambda paths: print({"files": paths}, flush=True))
