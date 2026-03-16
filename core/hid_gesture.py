"""
hid_gesture.py — Detect Logitech gesture-button events via HID++.

The gesture button on Bluetooth-connected Logitech mice often produces no
standard OS-level mouse event. This module uses the HID++ protocol to:

  1. Open a Logitech HID++ transport.
  2. Discover REPROG_CONTROLS_V4 (0x1B04) via IRoot.
  3. Divert the gesture button (CID 0x00C3) so we receive notifications.
  4. Fire callbacks on gesture press / release.

On macOS, IOKit is the preferred backend and hidapi is kept as a secondary
fallback/debug path. On other platforms this module continues to use hidapi.
"""

import os
import sys
import threading
import time

try:
    import hid as _hid
    HIDAPI_OK = True
    # On macOS, allow non-exclusive HID access so the mouse keeps working
    if sys.platform == "darwin" and hasattr(_hid, "hid_darwin_set_open_exclusive"):
        _hid.hid_darwin_set_open_exclusive(0)
except ImportError:
    HIDAPI_OK = False

_MAC_NATIVE_OK = False
if sys.platform == "darwin":
    try:
        import ctypes
        from ctypes import POINTER, byref, c_char_p, c_int, c_long, c_uint8, c_void_p

        _cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        _iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")

        _cf.CFNumberCreate.argtypes = [c_void_p, c_int, c_void_p]
        _cf.CFNumberCreate.restype = c_void_p
        _cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_int]
        _cf.CFStringCreateWithCString.restype = c_void_p
        _cf.CFDictionaryCreate.argtypes = [
            c_void_p, POINTER(c_void_p), POINTER(c_void_p), c_long, c_void_p, c_void_p,
        ]
        _cf.CFDictionaryCreate.restype = c_void_p
        _cf.CFSetGetCount.argtypes = [c_void_p]
        _cf.CFSetGetCount.restype = c_long
        _cf.CFSetGetValues.argtypes = [c_void_p, POINTER(c_void_p)]
        _cf.CFNumberGetValue.argtypes = [c_void_p, c_int, c_void_p]
        _cf.CFNumberGetValue.restype = c_int
        _cf.CFStringGetCString.argtypes = [c_void_p, c_void_p, c_long, c_int]
        _cf.CFStringGetCString.restype = c_int
        _cf.CFRelease.argtypes = [c_void_p]
        _cf.CFRetain.argtypes = [c_void_p]
        _cf.CFRetain.restype = c_void_p

        _iokit.IOHIDManagerCreate.argtypes = [c_void_p, c_int]
        _iokit.IOHIDManagerCreate.restype = c_void_p
        _iokit.IOHIDManagerSetDeviceMatching.argtypes = [c_void_p, c_void_p]
        _iokit.IOHIDManagerOpen.argtypes = [c_void_p, c_int]
        _iokit.IOHIDManagerOpen.restype = c_int
        _iokit.IOHIDManagerCopyDevices.argtypes = [c_void_p]
        _iokit.IOHIDManagerCopyDevices.restype = c_void_p

        _iokit.IOHIDDeviceOpen.argtypes = [c_void_p, c_int]
        _iokit.IOHIDDeviceOpen.restype = c_int
        _iokit.IOHIDDeviceClose.argtypes = [c_void_p, c_int]
        _iokit.IOHIDDeviceClose.restype = c_int
        _iokit.IOHIDDeviceGetProperty.argtypes = [c_void_p, c_void_p]
        _iokit.IOHIDDeviceGetProperty.restype = c_void_p
        _iokit.IOHIDDeviceSetReport.argtypes = [c_void_p, c_int, c_long, POINTER(c_uint8), c_long]
        _iokit.IOHIDDeviceSetReport.restype = c_int
        _iokit.IOHIDDeviceGetReport.argtypes = [c_void_p, c_int, c_long, POINTER(c_uint8), POINTER(c_long)]
        _iokit.IOHIDDeviceGetReport.restype = c_int

        _K_CF_NUMBER_SINT32 = 3
        _K_CF_STRING_ENCODING_UTF8 = 0x08000100
        _K_IOHID_REPORT_TYPE_INPUT = 0
        _K_IOHID_REPORT_TYPE_OUTPUT = 1

        _MAC_NATIVE_OK = True
    except Exception as exc:
        print(f"[HidGesture] macOS native HID unavailable: {exc}")


if _MAC_NATIVE_OK:
    class _MacNativeHidDevice:
        """Minimal IOHIDDevice wrapper for Logitech HID++ on macOS."""

        def __init__(self, device_ref, product_id=0, usage_page=None,
                     usage=None, transport="", product=""):
            self._device = device_ref
            self._product_id = int(product_id or 0)
            self._usage_page = usage_page
            self._usage = usage
            self._transport = transport or ""
            self._product = product or ""

        @staticmethod
        def _cfstring(text):
            return _cf.CFStringCreateWithCString(
                None, text.encode("utf-8"), _K_CF_STRING_ENCODING_UTF8
            )

        @staticmethod
        def _cfnumber(value):
            num = c_int(int(value))
            return _cf.CFNumberCreate(None, _K_CF_NUMBER_SINT32, byref(num))

        @classmethod
        def _cfdictionary(cls, **pairs):
            keys = [cls._cfstring(key) for key in pairs]
            values = [cls._cfnumber(value) for value in pairs.values()]
            key_array = (c_void_p * len(keys))(*keys)
            value_array = (c_void_p * len(values))(*values)
            ref = _cf.CFDictionaryCreate(
                None, key_array, value_array, len(keys), None, None
            )
            for item in keys + values:
                _cf.CFRelease(item)
            return ref

        @classmethod
        def _int_property(cls, device_ref, key, default=0):
            key_ref = cls._cfstring(key)
            try:
                value_ref = _iokit.IOHIDDeviceGetProperty(device_ref, key_ref)
            finally:
                _cf.CFRelease(key_ref)
            if not value_ref:
                return default
            value = c_int()
            if not _cf.CFNumberGetValue(
                value_ref, _K_CF_NUMBER_SINT32, byref(value)
            ):
                return default
            return int(value.value)

        @classmethod
        def _string_property(cls, device_ref, key, default=""):
            key_ref = cls._cfstring(key)
            try:
                value_ref = _iokit.IOHIDDeviceGetProperty(device_ref, key_ref)
            finally:
                _cf.CFRelease(key_ref)
            if not value_ref:
                return default
            buf = ctypes.create_string_buffer(256)
            if not _cf.CFStringGetCString(
                value_ref, buf, len(buf), _K_CF_STRING_ENCODING_UTF8
            ):
                return default
            return buf.value.decode("utf-8", errors="replace")

        @classmethod
        def enumerate_candidates(cls):
            manager = None
            matching = None
            devices = None
            out = []
            try:
                matching = cls._cfdictionary(VendorID=LOGI_VID)
                manager = _iokit.IOHIDManagerCreate(None, 0)
                if not manager:
                    raise OSError("IOHIDManagerCreate failed")
                _iokit.IOHIDManagerSetDeviceMatching(manager, matching)
                res = _iokit.IOHIDManagerOpen(manager, 0)
                if res != 0:
                    raise OSError(f"IOHIDManagerOpen failed: 0x{res:08X}")

                devices = _iokit.IOHIDManagerCopyDevices(manager)
                if not devices:
                    return out
                count = _cf.CFSetGetCount(devices)
                if count <= 0:
                    return out

                values_buf = (c_void_p * count)()
                _cf.CFSetGetValues(devices, values_buf)
                for i in range(count):
                    device_ref = values_buf[i]
                    if not device_ref:
                        continue
                    retained = _cf.CFRetain(device_ref)
                    product_id = cls._int_property(retained, "ProductID", 0)
                    if not product_id:
                        _cf.CFRelease(retained)
                        continue
                    out.append({
                        "backend": "iokit",
                        "product_id": product_id,
                        "usage_page": cls._int_property(retained, "PrimaryUsagePage", None),
                        "usage": cls._int_property(retained, "PrimaryUsage", None),
                        "transport": cls._string_property(retained, "Transport", ""),
                        "product": cls._string_property(retained, "Product", ""),
                        "device_ref": retained,
                    })
            finally:
                if devices:
                    _cf.CFRelease(devices)
                if matching:
                    _cf.CFRelease(matching)
                if manager:
                    _cf.CFRelease(manager)

            out.sort(key=cls._sort_key)
            return out

        @staticmethod
        def _sort_key(info):
            usage_page = info.get("usage_page")
            usage = info.get("usage")
            transport = (info.get("transport") or "").lower()
            return (
                0 if usage_page == 0xFF43 else 1,
                0 if isinstance(usage_page, int) and usage_page >= 0xFF00 else 1,
                0 if "bluetooth" in transport else 1,
                info.get("product_id", 0),
                usage_page if isinstance(usage_page, int) else 0xFFFFFFFF,
                usage if isinstance(usage, int) else 0xFFFFFFFF,
            )

        @staticmethod
        def release_candidate(info):
            device_ref = info.get("device_ref")
            if device_ref:
                _cf.CFRelease(device_ref)
                info["device_ref"] = None

        def open(self):
            if not self._device:
                raise OSError("IOHIDDevice ref missing")
            res = _iokit.IOHIDDeviceOpen(self._device, 0)
            if res != 0:
                raise OSError(f"IOHIDDeviceOpen failed: 0x{res:08X}")

        def close(self):
            if self._device:
                try:
                    _iokit.IOHIDDeviceClose(self._device, 0)
                except Exception:
                    pass
            if self._device:
                _cf.CFRelease(self._device)
                self._device = None

        def set_nonblocking(self, _enabled):
            return None

        def write(self, buf):
            arr = (c_uint8 * len(buf))(*buf)
            res = _iokit.IOHIDDeviceSetReport(
                self._device,
                _K_IOHID_REPORT_TYPE_OUTPUT,
                int(buf[0]),
                arr,
                len(buf),
            )
            if res != 0:
                raise OSError(f"IOHIDDeviceSetReport failed: 0x{res:08X}")
            return len(buf)

        def read(self, _size, timeout_ms=0):
            report = (c_uint8 * 64)()
            length = c_long(64)
            res = _iokit.IOHIDDeviceGetReport(
                self._device,
                _K_IOHID_REPORT_TYPE_INPUT,
                LONG_ID,
                report,
                byref(length),
            )
            if res != 0:
                raise OSError(f"IOHIDDeviceGetReport failed: 0x{res:08X}")
            if length.value <= 0:
                return b""
            return bytes(report[:length.value])

# ── Constants ─────────────────────────────────────────────────────
LOGI_VID       = 0x046D

SHORT_ID       = 0x10        # HID++ short report (7 bytes total)
LONG_ID        = 0x11        # HID++ long  report (20 bytes total)
SHORT_LEN      = 7
LONG_LEN       = 20

BT_DEV_IDX     = 0xFF        # device-index for direct Bluetooth
FEAT_IROOT     = 0x0000
FEAT_REPROG_V4 = 0x1B04      # Reprogrammable Controls V4
FEAT_ADJ_DPI   = 0x2201      # Adjustable DPI
CID_GESTURE    = 0x00C3      # "Mouse Gesture Button"

MY_SW          = 0x0A        # arbitrary software-id used in our requests

TRANSPORT_AUTO   = "auto"
TRANSPORT_IOKIT  = "iokit"
TRANSPORT_HIDAPI = "hidapi"
_TRANSPORT_CHOICES = {TRANSPORT_AUTO, TRANSPORT_IOKIT, TRANSPORT_HIDAPI}


# ── Helpers ───────────────────────────────────────────────────────

def _parse(raw):
    """Parse a read buffer → (dev_idx, feat_idx, func, sw, params) or None.

    On Windows the hidapi C backend strips the report-ID byte, so the
    first byte is device-index.  On other platforms / future versions
    the report-ID may be included.  We detect which layout we have by
    checking whether byte 0 looks like a valid HID++ report-ID.
    """
    if not raw or len(raw) < 4:
        return None
    off = 1 if raw[0] in (SHORT_ID, LONG_ID) else 0
    if off + 3 > len(raw):
        return None
    dev    = raw[off]
    feat   = raw[off + 1]
    fsw    = raw[off + 2]
    func   = (fsw >> 4) & 0x0F
    sw     = fsw & 0x0F
    params = raw[off + 3:]
    return dev, feat, func, sw, params


# ── Listener class ────────────────────────────────────────────────

class HidGestureListener:
    """Background thread: diverts the gesture button and listens via HID++."""

    def __init__(self, on_down=None, on_up=None, on_move=None,
                 on_connect=None, on_disconnect=None,
                 transport_preference=None):
        self._on_down       = on_down
        self._on_up         = on_up
        self._on_move       = on_move
        self._on_connect    = on_connect
        self._on_disconnect = on_disconnect
        self._transport_preference = self._resolve_transport_preference(
            transport_preference
        )
        self._dev       = None          # hid.device()
        self._thread    = None
        self._running   = False
        self._feat_idx  = None          # feature index of REPROG_V4
        self._dpi_idx   = None          # feature index of ADJUSTABLE_DPI
        self._dev_idx   = BT_DEV_IDX
        self._held      = False
        self._connected = False         # True while HID++ device is open
        self._rawxy_enabled = False
        self._pending_dpi = None        # set by set_dpi(), applied in loop
        self._dpi_result  = None        # True/False after apply

    # ── public API ────────────────────────────────────────────────

    @staticmethod
    def _resolve_transport_preference(value):
        if sys.platform != "darwin":
            return TRANSPORT_HIDAPI

        default = TRANSPORT_AUTO
        raw = value if value is not None else os.getenv("MOUSER_HID_TRANSPORT")
        if raw is None:
            return default

        pref = str(raw).strip().lower()
        if not pref:
            return default
        if pref not in _TRANSPORT_CHOICES:
            print(f"[HidGesture] Invalid MOUSER_HID_TRANSPORT={raw!r}; using auto")
            return default
        return pref

    def _has_transport_backend(self):
        if sys.platform != "darwin":
            return HIDAPI_OK
        if self._transport_preference == TRANSPORT_IOKIT:
            return _MAC_NATIVE_OK
        if self._transport_preference == TRANSPORT_HIDAPI:
            return HIDAPI_OK
        return _MAC_NATIVE_OK or HIDAPI_OK

    def start(self):
        if not self._has_transport_backend():
            if sys.platform == "darwin" and self._transport_preference == TRANSPORT_IOKIT:
                print("[HidGesture] macOS IOKit transport unavailable")
            elif sys.platform == "darwin" and self._transport_preference == TRANSPORT_AUTO:
                print("[HidGesture] No HID backend available on macOS")
            else:
                print("[HidGesture] 'hidapi' not installed — pip install hidapi")
            return False
        self._running = True
        self._thread = threading.Thread(
            target=self._main_loop, daemon=True, name="HidGesture")
        self._thread.start()
        return True

    def stop(self):
        self._running = False
        self._close_active_device()
        if self._thread:
            self._thread.join(timeout=3)

    # ── device discovery ──────────────────────────────────────────

    @staticmethod
    def _hidapi_candidates():
        """Return Logitech vendor-page hidapi candidates."""
        out = []
        if not HIDAPI_OK:
            return out
        try:
            for info in _hid.enumerate(LOGI_VID, 0):
                usage_page = info.get("usage_page", 0)
                if usage_page >= 0xFF00:
                    out.append({
                        "backend": TRANSPORT_HIDAPI,
                        "path": info["path"],
                        "product_id": info.get("product_id", 0),
                        "usage_page": usage_page,
                        "usage": info.get("usage"),
                        "transport": info.get("transport", ""),
                        "product": info.get("product_string", ""),
                    })
        except Exception as exc:
            print(f"[HidGesture] enumerate error: {exc}")
        out.sort(key=lambda info: (
            0 if info.get("usage_page") == 0xFF43 else 1,
            info.get("product_id", 0),
            info.get("usage_page", 0),
            info.get("usage") if isinstance(info.get("usage"), int) else 0xFFFFFFFF,
        ))
        return out

    @staticmethod
    def _native_candidates():
        if sys.platform != "darwin" or not _MAC_NATIVE_OK:
            return []
        try:
            return _MacNativeHidDevice.enumerate_candidates()
        except Exception as exc:
            print(f"[HidGesture] iokit enumerate error: {exc}")
            return []

    def _connect_candidates(self):
        if sys.platform != "darwin":
            return self._hidapi_candidates()

        if self._transport_preference == TRANSPORT_IOKIT:
            return self._native_candidates()
        if self._transport_preference == TRANSPORT_HIDAPI:
            return self._hidapi_candidates()
        return self._native_candidates() + self._hidapi_candidates()

    @staticmethod
    def _release_candidates(candidates):
        if not _MAC_NATIVE_OK:
            return
        for info in candidates:
            if info.get("backend") == TRANSPORT_IOKIT:
                _MacNativeHidDevice.release_candidate(info)

    @staticmethod
    def _candidate_label(info):
        parts = [f"backend={info.get('backend', 'unknown')}"]
        product_id = info.get("product_id")
        usage_page = info.get("usage_page")
        usage = info.get("usage")
        transport = info.get("transport")
        if isinstance(product_id, int):
            parts.append(f"pid=0x{product_id:04X}")
        if isinstance(usage_page, int):
            parts.append(f"up=0x{usage_page:04X}")
        if isinstance(usage, int):
            parts.append(f"usage=0x{usage:04X}")
        if transport:
            parts.append(f"transport={transport}")
        return " ".join(parts)

    # ── low-level HID++ I/O ───────────────────────────────────────

    def _tx(self, report_id, feat, func, params):
        """Transmit an HID++ message.  Always uses 20-byte long format
        because BLE HID collections typically only support long output reports."""
        buf = [0] * LONG_LEN
        buf[0] = LONG_ID                 # always long for BLE compat
        buf[1] = self._dev_idx
        buf[2] = feat
        buf[3] = ((func & 0x0F) << 4) | (MY_SW & 0x0F)
        for i, b in enumerate(params):
            if 4 + i < LONG_LEN:
                buf[4 + i] = b & 0xFF
        self._dev.write(buf)

    def _rx(self, timeout_ms=2000):
        """Read one HID input report (blocking with timeout).
        Raises on device error (e.g., disconnection) so callers
        can trigger reconnection."""
        dev = self._dev
        if dev is None:
            return None
        d = dev.read(64, timeout_ms)
        return list(d) if d else None

    def _request(self, feat, func, params, timeout_ms=2000, log_errors=True):
        """Send a long HID++ request, wait for matching response."""
        try:
            self._tx(LONG_ID, feat, func, params)
        except Exception:
            return None
        deadline = time.time() + timeout_ms / 1000
        while time.time() < deadline:
            try:
                raw = self._rx(min(500, timeout_ms))
            except Exception:
                return None
            if raw is None:
                continue
            msg = _parse(raw)
            if msg is None:
                continue
            _, r_feat, r_func, r_sw, r_params = msg

            # HID++ error (feature-index 0xFF)
            if r_feat == 0xFF:
                code = r_params[1] if len(r_params) > 1 else 0
                if log_errors:
                    print(f"[HidGesture] HID++ error 0x{code:02X} "
                          f"for feat=0x{feat:02X} func={func}")
                return None

            expected_funcs = {func, (func + 1) & 0x0F}
            if r_feat == feat and r_sw == MY_SW and r_func in expected_funcs:
                return msg
        return None

    # ── feature helpers ───────────────────────────────────────────

    def _find_feature(self, feature_id, log_errors=True):
        """Use IRoot (feature 0x0000) to discover a feature index."""
        hi = (feature_id >> 8) & 0xFF
        lo = feature_id & 0xFF
        resp = self._request(0x00, 0, [hi, lo, 0x00], log_errors=log_errors)
        if resp:
            _, _, _, _, p = resp
            if p and p[0] != 0:
                return p[0]
        return None

    def _set_cid_reporting(self, flags, log_errors=True):
        if self._feat_idx is None:
            return None
        hi = (CID_GESTURE >> 8) & 0xFF
        lo = CID_GESTURE & 0xFF
        return self._request(
            self._feat_idx, 3, [hi, lo, flags, 0x00, 0x00],
            log_errors=log_errors
        )

    def _divert(self, silent=False, log_errors=True):
        """Divert gesture button CID 0x00C3 and enable raw XY when supported."""
        if self._feat_idx is None:
            return False
        resp = self._set_cid_reporting(0x33, log_errors=log_errors)
        if resp is not None:
            self._rawxy_enabled = True
            if not silent:
                print(f"[HidGesture] Divert CID 0x{CID_GESTURE:04X} with RawXY: OK")
            return True
        self._rawxy_enabled = False
        resp = self._set_cid_reporting(0x03, log_errors=log_errors)
        ok = resp is not None
        if not silent:
            print(f"[HidGesture] Divert CID 0x{CID_GESTURE:04X}: "
                  f"{'OK' if ok else 'FAILED'}")
        return ok

    def _undivert(self):
        """Restore default button behaviour (best-effort)."""
        if self._feat_idx is None or self._dev is None:
            return
        hi = (CID_GESTURE >> 8) & 0xFF
        lo = CID_GESTURE & 0xFF
        flags = 0x22 if self._rawxy_enabled else 0x02
        try:
            self._tx(LONG_ID, self._feat_idx, 3,
                     [hi, lo, flags, 0x00, 0x00])
        except Exception:
            pass
        self._rawxy_enabled = False

    # ── DPI control ───────────────────────────────────────────────

    def set_dpi(self, dpi_value):
        """Queue a DPI change — will be applied on the listener thread.
        Can be called from any thread.  Returns True on success."""
        dpi = max(200, min(8200, int(dpi_value)))  # MX Master 3S max is 8000
        self._dpi_result = None
        self._pending_dpi = dpi
        # Wait up to 3s for the listener thread to apply it
        for _ in range(30):
            if self._pending_dpi is None:
                return self._dpi_result is True
            time.sleep(0.1)
        print("[HidGesture] DPI set timed out")
        return False

    def _apply_pending_dpi(self):
        """Called from the listener thread to actually send DPI."""
        dpi = self._pending_dpi
        if dpi is None:
            return
        if self._dpi_idx is None or self._dev is None:
            print("[HidGesture] Cannot set DPI — not connected")
            self._dpi_result = False
            self._pending_dpi = None
            return
        hi = (dpi >> 8) & 0xFF
        lo = dpi & 0xFF
        # setSensorDpi: function 3, params [sensorIdx=0, dpi_hi, dpi_lo]
        # (function 2 = getSensorDpi, function 3 = setSensorDpi)
        resp = self._request(self._dpi_idx, 3, [0x00, hi, lo])
        if resp:
            _, _, _, _, p = resp
            actual = (p[1] << 8 | p[2]) if len(p) >= 3 else dpi
            print(f"[HidGesture] DPI set to {actual}")
            self._dpi_result = True
        else:
            print("[HidGesture] DPI set FAILED")
            self._dpi_result = False
        self._pending_dpi = None

    def read_dpi(self):
        """Queue a DPI read — will be applied on the listener thread.
        Can be called from any thread.  Returns the DPI value or None."""
        self._dpi_result = None
        self._pending_dpi = "read"  # special sentinel
        for _ in range(30):
            if self._pending_dpi is None:
                return self._dpi_result
            time.sleep(0.1)
        print("[HidGesture] DPI read timed out")
        return None

    def _apply_pending_read_dpi(self):
        """Called from the listener thread to read current DPI."""
        if self._dpi_idx is None or self._dev is None:
            self._dpi_result = None
            self._pending_dpi = None
            return
        # getSensorDpi: function 2, params [sensorIdx=0]
        resp = self._request(self._dpi_idx, 2, [0x00])
        if resp:
            _, _, _, _, p = resp
            current = (p[1] << 8 | p[2]) if len(p) >= 3 else None
            print(f"[HidGesture] Current DPI = {current}")
            self._dpi_result = current
        else:
            print("[HidGesture] DPI read FAILED")
            self._dpi_result = None
        self._pending_dpi = None

    # ── notification handling ─────────────────────────────────────

    @staticmethod
    def _decode_s16(hi, lo):
        value = (hi << 8) | lo
        if value & 0x8000:
            value -= 0x10000
        return value

    def _on_report(self, raw):
        """Inspect an incoming HID++ report for diverted button / raw XY events."""
        msg = _parse(raw)
        if msg is None:
            return
        _, feat, func, _sw, params = msg

        if feat != self._feat_idx:
            return

        if func == 1:
            if len(params) < 4 or not self._held:
                return
            dx = self._decode_s16(params[0], params[1])
            dy = self._decode_s16(params[2], params[3])
            if (dx or dy) and self._on_move:
                try:
                    self._on_move(dx, dy)
                except Exception as e:
                    print(f"[HidGesture] move callback error: {e}")
            return

        if func != 0:
            return

        # Params: sequential CID pairs terminated by 0x0000
        cids = set()
        i = 0
        while i + 1 < len(params):
            c = (params[i] << 8) | params[i + 1]
            if c == 0:
                break
            cids.add(c)
            i += 2

        gesture_now = CID_GESTURE in cids

        if gesture_now and not self._held:
            self._held = True
            print("[HidGesture] Gesture DOWN")
            if self._on_down:
                try:
                    self._on_down()
                except Exception as e:
                    print(f"[HidGesture] down callback error: {e}")

        elif not gesture_now and self._held:
            self._held = False
            print("[HidGesture] Gesture UP")
            if self._on_up:
                try:
                    self._on_up()
                except Exception as e:
                    print(f"[HidGesture] up callback error: {e}")

    # ── connect / main loop ───────────────────────────────────────

    def _reset_transport_state(self):
        self._feat_idx = None
        self._dpi_idx = None
        self._dev_idx = BT_DEV_IDX
        self._held = False
        self._rawxy_enabled = False

    def _close_active_device(self):
        d = self._dev
        if d:
            try:
                d.close()
            except Exception:
                pass
            self._dev = None

    def _open_candidate(self, info):
        backend = info.get("backend")
        if backend == TRANSPORT_IOKIT:
            device_ref = info.get("device_ref")
            d = _MacNativeHidDevice(
                device_ref,
                product_id=info.get("product_id", 0),
                usage_page=info.get("usage_page"),
                usage=info.get("usage"),
                transport=info.get("transport", ""),
                product=info.get("product", ""),
            )
            info["device_ref"] = None
            try:
                d.open()
            except Exception:
                d.close()
                raise
            return d

        d = _hid.device()
        try:
            d.open_path(info["path"])
            d.set_nonblocking(False)
        except Exception:
            try:
                d.close()
            except Exception:
                pass
            raise
        return d

    def _try_candidate(self, info):
        label = self._candidate_label(info)
        print(f"[HidGesture] Try {label}")
        self._reset_transport_state()

        try:
            self._dev = self._open_candidate(info)
        except Exception as exc:
            print(f"[HidGesture] Reject {label} reason=open_failed error={exc}")
            return False

        reason = "missing_reprog_v4"
        detail = ""
        found_feature = False
        for idx in (0xFF, 1, 2, 3, 4, 5, 6):
            self._dev_idx = idx
            feat_idx = self._find_feature(FEAT_REPROG_V4, log_errors=False)
            if feat_idx is None:
                continue

            found_feature = True
            self._feat_idx = feat_idx
            self._dpi_idx = self._find_feature(FEAT_ADJ_DPI, log_errors=False)
            dpi_label = (
                f"0x{self._dpi_idx:02X}"
                if isinstance(self._dpi_idx, int) else "none"
            )
            if self._divert(silent=True, log_errors=False):
                print(
                    f"[HidGesture] Connected {label} "
                    f"devIdx=0x{idx:02X} reprog=0x{feat_idx:02X} "
                    f"dpi={dpi_label} rawxy={'yes' if self._rawxy_enabled else 'no'}"
                )
                return True

            reason = "divert_failed"
            detail = (
                f" devIdx=0x{idx:02X} reprog=0x{feat_idx:02X} dpi={dpi_label}"
            )
            self._feat_idx = None
            self._dpi_idx = None

        if not found_feature:
            detail = ""

        print(f"[HidGesture] Reject {label} reason={reason}{detail}")
        self._close_active_device()
        self._reset_transport_state()
        return False

    def _try_connect(self):
        """Open a HID++ candidate, discover features, and divert the gesture CID."""
        candidates = self._connect_candidates()
        if not candidates:
            return False

        try:
            for info in candidates:
                if self._try_candidate(info):
                    return True
            return False
        finally:
            self._release_candidates(candidates)

    def _main_loop(self):
        """Outer loop: connect → listen → reconnect on error/disconnect."""
        while self._running:
            if not self._try_connect():
                print("[HidGesture] No compatible device; retrying in 5 s…")
                for _ in range(50):
                    if not self._running:
                        return
                    time.sleep(0.1)
                continue

            self._connected = True
            if self._on_connect:
                try:
                    self._on_connect()
                except Exception:
                    pass
            print("[HidGesture] Listening for gesture events…")
            try:
                while self._running:
                    # Apply any queued DPI command
                    if self._pending_dpi is not None:
                        if self._pending_dpi == "read":
                            self._apply_pending_read_dpi()
                        else:
                            self._apply_pending_dpi()
                    raw = self._rx(1000)
                    if raw:
                        self._on_report(raw)
            except Exception as e:
                print(f"[HidGesture] read error: {e}")

            # Cleanup before potential reconnect
            self._undivert()
            self._close_active_device()
            self._reset_transport_state()
            if self._connected:
                self._connected = False
                if self._on_disconnect:
                    try:
                        self._on_disconnect()
                    except Exception:
                        pass

            if self._running:
                time.sleep(2)
