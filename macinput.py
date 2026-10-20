"""Real keyboard / mouse events on macOS via CoreGraphics, using only ctypes.

The process running this needs Accessibility permission
(System Settings > Privacy & Security > Accessibility), otherwise events are silently dropped.
"""

import ctypes
import ctypes.util
import time

cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
ax = ctypes.cdll.LoadLibrary(ctypes.util.find_library("ApplicationServices"))


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("w", ctypes.c_double), ("h", ctypes.c_double)]


vp = ctypes.c_void_p
cg.CGEventSourceCreate.restype = vp
cg.CGEventSourceCreate.argtypes = [ctypes.c_int32]
cg.CGEventCreateKeyboardEvent.restype = vp
cg.CGEventCreateKeyboardEvent.argtypes = [vp, ctypes.c_uint16, ctypes.c_bool]
cg.CGEventSetFlags.argtypes = [vp, ctypes.c_uint64]
cg.CGEventPost.argtypes = [ctypes.c_uint32, vp]
cg.CGEventCreate.restype = vp
cg.CGEventCreate.argtypes = [vp]
cg.CGEventGetLocation.restype = CGPoint
cg.CGEventGetLocation.argtypes = [vp]
cg.CGEventCreateMouseEvent.restype = vp
cg.CGEventCreateMouseEvent.argtypes = [vp, ctypes.c_uint32, CGPoint, ctypes.c_uint32]
cg.CGEventCreateScrollWheelEvent2.restype = vp
cg.CGEventCreateScrollWheelEvent2.argtypes = [vp, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_int32, ctypes.c_int32, ctypes.c_int32]
cg.CGEventKeyboardSetUnicodeString.argtypes = [vp, ctypes.c_ulong, ctypes.POINTER(ctypes.c_uint16)]
cg.CGMainDisplayID.restype = ctypes.c_uint32
cg.CGDisplayBounds.restype = CGRect
cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
cg.CGEventSetIntegerValueField.argtypes = [vp, ctypes.c_uint32, ctypes.c_int64]
cf.CFRelease.argtypes = [vp]
ax.AXIsProcessTrusted.restype = ctypes.c_bool

HID_TAP = 0
SOURCE = cg.CGEventSourceCreate(1)  # kCGEventSourceStateHIDSystemState

KEYS = {
    "up": 126, "down": 125, "left": 123, "right": 124,
    "enter": 36, "esc": 53, "space": 49, "tab": 48, "backspace": 51,
    "f": 3, "j": 38, "k": 40, "l": 37, "m": 46, "q": 12, "w": 13, "[": 33, "]": 30,
}
MODS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}


def _post(event):
    if event:
        cg.CGEventPost(HID_TAP, event)
        cf.CFRelease(event)


def trusted():
    return bool(ax.AXIsProcessTrusted())


def key(name, mods=()):
    code = KEYS[name]
    flags = sum(MODS[m] for m in mods)
    for down in (True, False):
        e = cg.CGEventCreateKeyboardEvent(SOURCE, code, down)
        cg.CGEventSetFlags(e, flags)
        _post(e)


def cursor():
    e = cg.CGEventCreate(None)
    p = cg.CGEventGetLocation(e)
    cf.CFRelease(e)
    return p


def move(dx, dy):
    b = cg.CGDisplayBounds(cg.CGMainDisplayID())
    p = cursor()
    p.x = min(max(p.x + dx, b.origin.x), b.origin.x + b.w - 1)
    p.y = min(max(p.y + dy, b.origin.y), b.origin.y + b.h - 1)
    _post(cg.CGEventCreateMouseEvent(SOURCE, 5, p, 0))  # kCGEventMouseMoved


def click(right=False):
    p = cursor()
    down, up, button = (3, 4, 1) if right else (1, 2, 0)
    _post(cg.CGEventCreateMouseEvent(SOURCE, 5, p, 0))  # settle the pointer first
    for kind in (down, up):
        e = cg.CGEventCreateMouseEvent(SOURCE, kind, p, button)
        cg.CGEventSetIntegerValueField(e, 1, 1)  # kCGMouseEventClickState: browsers ignore clicks without it
        _post(e)
        time.sleep(0.03)


def scroll(dy):
    _post(cg.CGEventCreateScrollWheelEvent2(SOURCE, 0, 1, -int(dy), 0, 0))  # pixel units


def type_text(text):
    units = text.encode("utf-16-le")
    units = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
    for i in range(0, len(units), 16):
        chunk = (ctypes.c_uint16 * len(units[i:i + 16]))(*units[i:i + 16])
        for down in (True, False):
            e = cg.CGEventCreateKeyboardEvent(SOURCE, 0, down)
            cg.CGEventKeyboardSetUnicodeString(e, len(chunk), chunk)
            _post(e)
