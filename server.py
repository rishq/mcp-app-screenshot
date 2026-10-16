import ctypes
import io
import json
import os
import subprocess
import time
from typing import List, Optional

import mss
import psutil
import win32con
import win32gui
import win32process
import win32ui
from PIL import Image as PILImage
from mcp.server.fastmcp import FastMCP, Image

# Ensure high-DPI awareness on Windows 10/11 so screenshots aren't cropped or scaled blurry
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

mcp = FastMCP("AppScreenshotServer")

# Win32 constant for capturing modern DWM/DirectComposition windows (like VS Code, Chrome, WPF)
PW_RENDERFULLCONTENT = 0x00000002
DWMWA_CLOAKED = 13
DWMWA_EXTENDED_FRAME_BOUNDS = 9

ALLOWED_SHAREX_WORKFLOWS = frozenset({"ActiveWindow", "FullScreen", "RectangleRegion"})
DEFAULT_BLOCKED_PROCESSES = frozenset(
    {"keepass.exe", "keepassxc.exe", "1password.exe", "bitwarden.exe", "lastpass.exe"}
)
ALLOWED_SAVE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})


def _parse_process_list(value: str) -> set[str]:
    """Normalizes a comma-separated process-name setting."""
    return {item.strip().casefold() for item in value.split(",") if item.strip()}


def _is_process_allowed(process_name: str) -> bool:
    """Returns whether a process can be exposed to MCP clients."""
    process = process_name.casefold()
    allowed_setting = os.environ.get("ALLOWED_PROCESSES")
    blocked_setting = os.environ.get("BLOCKED_PROCESSES")
    allowed = _parse_process_list(allowed_setting) if allowed_setting else set()
    blocked = (
        _parse_process_list(blocked_setting)
        if blocked_setting is not None
        else set(DEFAULT_BLOCKED_PROCESSES)
    )

    if process in blocked:
        return False
    return not allowed or process in allowed


def _is_cloaked_window(hwnd: int) -> bool:
    """Returns True when DWM reports that a window is cloaked."""
    cloaked = ctypes.c_int(0)
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
    except (AttributeError, OSError):
        return False
    return result == 0 and cloaked.value != 0


def _validate_save_path(save_path: str) -> str:
    """Resolves a requested save path and confines it to SCREENSHOT_DIR."""
    screenshot_dir = os.path.realpath(os.environ.get("SCREENSHOT_DIR", "./screenshots"))
    requested_path = (
        save_path if os.path.isabs(save_path) else os.path.join(screenshot_dir, save_path)
    )
    resolved_path = os.path.realpath(requested_path)
    extension = os.path.splitext(resolved_path)[1].casefold()
    if extension not in ALLOWED_SAVE_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_SAVE_EXTENSIONS))
        raise ValueError(f"save_path must use one of these extensions: {allowed}.")

    try:
        is_inside_directory = (
            os.path.normcase(os.path.commonpath([screenshot_dir, resolved_path]))
            == os.path.normcase(screenshot_dir)
        )
    except ValueError:
        is_inside_directory = False
    if not is_inside_directory:
        raise ValueError("save_path must be inside SCREENSHOT_DIR.")
    return resolved_path


class _RECT(ctypes.Structure):
    """Windows RECT structure for ctypes DWM calls."""

    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _get_extended_frame_bounds(hwnd: int) -> Optional[tuple[int, int, int, int]]:
    """Gets DWM's visible frame bounds, falling back to no result on failure."""
    rect = _RECT()
    try:
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect),
            ctypes.sizeof(rect),
        )
    except (AttributeError, OSError):
        return None
    if result != 0:
        return None
    return (rect.left, rect.top, rect.right, rect.bottom)


def _get_visible_windows() -> List[dict]:
    """Enumerates all visible application windows with non-zero dimensions."""
    windows = []

    def enum_handler(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd) or _is_cloaked_window(hwnd):
                return
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            if width <= 0 or height <= 0:
                return

            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = ""
            try:
                proc_name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                return

            if not _is_process_allowed(proc_name):
                return

            windows.append({
                "hwnd": hwnd,
                "title": title,
                "process": proc_name,
                "pid": pid,
                "rect": {"left": rect[0], "top": rect[1], "width": width, "height": height},
                "is_minimized": bool(win32gui.IsIconic(hwnd)),
            })
        except Exception:
            # A window can disappear or become inaccessible during enumeration.
            return

    try:
        win32gui.EnumWindows(enum_handler, None)
    except Exception as error:
        raise ValueError(f"Unable to enumerate visible windows: {error}") from None
    return windows


def _format_candidates(windows: List[dict]) -> str:
    """Formats windows for an actionable ambiguous-window error."""
    return "; ".join(
        f"hwnd={window['hwnd']}, title={window['title']!r}, process={window['process']}"
        for window in windows
    )


def _select_largest_window(candidates: List[dict], query: str) -> dict:
    """Selects the largest candidate, reporting a tie instead of guessing."""
    largest_area = max(
        candidate["rect"]["width"] * candidate["rect"]["height"]
        for candidate in candidates
    )
    largest = [
        candidate
        for candidate in candidates
        if candidate["rect"]["width"] * candidate["rect"]["height"] == largest_area
    ]
    if len(largest) > 1:
        raise ValueError(
            f"Ambiguous window match for '{query}'. Candidates: {_format_candidates(largest)}"
        )
    return largest[0]


def _find_target_window(
    query: str = "", hwnd: Optional[int] = None, pid: Optional[int] = None
) -> Optional[dict]:
    """Finds an allowed window by HWND, PID, title substring, or process name."""
    query_lower = query.lower()
    windows = _get_visible_windows()

    if hwnd is not None:
        return next((window for window in windows if window["hwnd"] == hwnd), None)

    if pid is not None:
        candidates = [window for window in windows if window["pid"] == pid]
        return _select_largest_window(candidates, f"pid={pid}") if candidates else None

    if not query_lower:
        return None

    # Priority 1: Exact process name match (e.g., "Code.exe" or "myapp.exe")
    exact_process_matches = [
        window
        for window in windows
        if window["process"].lower() == query_lower
        or window["process"].lower() == f"{query_lower}.exe"
    ]
    if exact_process_matches:
        return _select_largest_window(exact_process_matches, query)

    # Priority 2: Title substring match
    title_matches = [
        window for window in windows if query_lower in window["title"].lower()
    ]
    if title_matches:
        return _select_largest_window(title_matches, query)

    # Priority 3: Process substring match
    process_matches = [
        window for window in windows if query_lower in window["process"].lower()
    ]
    if process_matches:
        return _select_largest_window(process_matches, query)

    return None


def _is_blank_frame(img: PILImage.Image) -> bool:
    """Returns True for empty or fully black PrintWindow output."""
    if img.width <= 0 or img.height <= 0:
        return True
    extrema = img.convert("RGB").getextrema()
    return all(channel_maximum == 0 for _, channel_maximum in extrema)


def _get_client_screen_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Returns the client rectangle in screen coordinates."""
    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    screen_left, screen_top = win32gui.ClientToScreen(hwnd, (left, top))
    screen_right, screen_bottom = win32gui.ClientToScreen(hwnd, (right, bottom))
    if screen_right <= screen_left or screen_bottom <= screen_top:
        raise ValueError("The target window has an invalid client area.")
    return screen_left, screen_top, screen_right, screen_bottom


def _capture_hwnd_background(
    hwnd: int, client_area_only: bool = False
) -> Optional[PILImage.Image]:
    """
    Captures a window directly from its DWM buffer using PrintWindow.
    Works even if the window is occluded by other windows.
    """
    was_minimized = bool(win32gui.IsIconic(hwnd))
    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bitmap = None
    try:
        if was_minimized:
            # Minimized windows do not maintain an active render buffer; restore without focus.
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            time.sleep(0.15)

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        result = ctypes.windll.user32.PrintWindow(
            hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT
        )
        if result != 1:
            return None

        bmp_info = bitmap.GetInfo()
        bmp_str = bitmap.GetBitmapBits(True)
        img = PILImage.frombuffer(
            "RGB",
            (bmp_info["bmWidth"], bmp_info["bmHeight"]),
            bmp_str,
            "raw",
            "BGRX",
            0,
            1,
        )
        if _is_blank_frame(img):
            return None
        if client_area_only:
            client_left, client_top, client_right, client_bottom = _get_client_screen_rect(hwnd)
            crop_box = (
                client_left - left,
                client_top - top,
                client_right - left,
                client_bottom - top,
            )
            if (
                crop_box[0] < 0
                or crop_box[1] < 0
                or crop_box[2] > img.width
                or crop_box[3] > img.height
            ):
                raise ValueError("The target client area is outside its window bounds.")
            img = img.crop(crop_box)
            if _is_blank_frame(img):
                return None
        return img
    finally:
        # Delete in reverse acquisition order even when PrintWindow or PIL conversion fails.
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if save_dc is not None:
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if mfc_dc is not None:
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass
        if was_minimized:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            except Exception:
                pass


def _capture_hwnd_foreground_mss(
    hwnd: int, client_area_only: bool = False
) -> PILImage.Image:
    """
    Brings the window to the front and captures its screen bounding box via MSS.
    Best for OpenGL/Vulkan/DirectX compiled apps or when child popups need to be visible.
    """
    was_minimized = bool(win32gui.IsIconic(hwnd))
    input_attached = False
    user32 = ctypes.windll.user32
    try:
        if was_minimized:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        foreground_hwnd = win32gui.GetForegroundWindow()
        target_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
        foreground_thread = 0
        if foreground_hwnd:
            foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground_hwnd)
        if foreground_thread and target_thread and foreground_thread != target_thread:
            input_attached = bool(
                user32.AttachThreadInput(foreground_thread, target_thread, True)
            )

        win32gui.SetForegroundWindow(hwnd)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and win32gui.GetForegroundWindow() != hwnd:
            time.sleep(0.02)
        if win32gui.GetForegroundWindow() != hwnd:
            raise ValueError("Windows did not allow the target window to become foreground.")

        if client_area_only:
            left, top, right, bottom = _get_client_screen_rect(hwnd)
        else:
            left, top, right, bottom = (
                _get_extended_frame_bounds(hwnd) or win32gui.GetWindowRect(hwnd)
            )
        monitor_region = {
            "left": left,
            "top": top,
            "width": right - left,
            "height": bottom - top,
        }
        if monitor_region["width"] <= 0 or monitor_region["height"] <= 0:
            raise ValueError("The target window has invalid screen bounds.")

        with mss.mss() as sct:
            sct_img = sct.grab(monitor_region)
            return PILImage.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
    finally:
        if input_attached:
            try:
                user32.AttachThreadInput(foreground_thread, target_thread, False)
            except Exception:
                pass
        if was_minimized:
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            except Exception:
                pass


def _validate_image_options(
    max_width: int, max_height: int, format: str, quality: int
) -> str:
    """Validates image transport options and returns a normalized format name."""
    normalized_format = format.casefold()
    if normalized_format not in {"png", "jpeg", "webp"}:
        raise ValueError("format must be one of: png, jpeg, webp.")
    if max_width <= 0 or max_height <= 0:
        raise ValueError("max_width and max_height must be positive integers.")
    if not 1 <= quality <= 100:
        raise ValueError("quality must be between 1 and 100.")
    return normalized_format


def _scaled_size(
    width: int, height: int, max_width: int, max_height: int
) -> tuple[int, int]:
    """Calculates proportional dimensions constrained by both size limits."""
    scale = min(1.0, max_width / width, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _resize_to_fit(
    pil_img: PILImage.Image, max_width: int, max_height: int
) -> PILImage.Image:
    """Downscales an image proportionally when either size limit is exceeded."""
    final_size = _scaled_size(pil_img.width, pil_img.height, max_width, max_height)
    if final_size == pil_img.size:
        return pil_img
    return pil_img.resize(final_size, PILImage.Resampling.LANCZOS)


def _pil_to_mcp_image(
    pil_img: PILImage.Image,
    max_width: int = 1568,
    max_height: int = 1568,
    format: str = "png",
    quality: int = 80,
) -> Image:
    """Resizes and encodes a PIL image for a FastMCP image response."""
    normalized_format = _validate_image_options(max_width, max_height, format, quality)
    resized_img = _resize_to_fit(pil_img, max_width, max_height)
    buffer = io.BytesIO()
    if normalized_format == "png":
        resized_img.save(buffer, format="PNG")
    else:
        resized_img.convert("RGB").save(
            buffer,
            format=normalized_format.upper(),
            quality=quality,
        )
    return Image(data=buffer.getvalue(), format=normalized_format)


def _save_pil_image(pil_img: PILImage.Image, save_path: str, quality: int) -> None:
    """Writes a screenshot using the format selected by its validated extension."""
    extension = os.path.splitext(save_path)[1].casefold()
    format_by_extension = {
        ".png": "PNG",
        ".jpg": "JPEG",
        ".jpeg": "JPEG",
        ".webp": "WEBP",
    }
    image_format = format_by_extension[extension]
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    if image_format == "PNG":
        pil_img.save(save_path, format=image_format)
    else:
        pil_img.convert("RGB").save(save_path, format=image_format, quality=quality)


def _capture_metadata(
    window: dict,
    original_size: tuple[int, int],
    final_size: tuple[int, int],
    mode_used: str,
) -> str:
    """Serializes the metadata paired with an image response."""
    return json.dumps(
        {
            "title": window["title"],
            "process": window["process"],
            "pid": window["pid"],
            "hwnd": window["hwnd"],
            "original_size": {"width": original_size[0], "height": original_size[1]},
            "final_size": {"width": final_size[0], "height": final_size[1]},
            "mode_used": mode_used,
        }
    )


def _capture_application_window_data(
    target: str,
    mode: str,
    max_width: int,
    max_height: int,
    save_path: Optional[str],
    allow_foreground_fallback: bool,
    hwnd: Optional[int],
    pid: Optional[int],
    format: str,
    quality: int,
    client_area_only: bool,
    delay_ms: int,
) -> tuple[str, Image]:
    """Captures one selected window and returns its metadata and encoded image."""
    if mode not in {"background", "foreground"}:
        raise ValueError("mode must be either 'background' or 'foreground'.")
    if not target and hwnd is None and pid is None:
        raise ValueError("Provide target, hwnd, or pid to select a window.")
    if delay_ms < 0:
        raise ValueError("delay_ms must be zero or greater.")
    normalized_format = _validate_image_options(max_width, max_height, format, quality)
    validated_save_path = _validate_save_path(save_path) if save_path else None

    window = _find_target_window(target, hwnd=hwnd, pid=pid)
    if not window:
        available = [
            f"{item['process']} ({item['title']!r})" for item in _get_visible_windows()[:15]
        ]
        selector = (
            f"hwnd={hwnd}"
            if hwnd is not None
            else f"pid={pid}"
            if pid is not None
            else repr(target)
        )
        raise ValueError(
            f"No allowed visible window matching {selector}. "
            f"Top open windows: {', '.join(available)}"
        )

    if delay_ms:
        time.sleep(delay_ms / 1000)

    mode_used = mode
    try:
        if mode == "background":
            pil_img = _capture_hwnd_background(
                window["hwnd"], client_area_only=client_area_only
            )
            if pil_img is None and not allow_foreground_fallback:
                raise ValueError(
                    "Background capture failed. Use mode='foreground' or "
                    "allow_foreground_fallback=True to permit an activated screen capture."
                )
            if pil_img is None:
                mode_used = "foreground"
                pil_img = _capture_hwnd_foreground_mss(
                    window["hwnd"], client_area_only=client_area_only
                )
        else:
            pil_img = _capture_hwnd_foreground_mss(
                window["hwnd"], client_area_only=client_area_only
            )
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Unable to capture window '{window['title']}': {error}") from None

    original_size = pil_img.size
    resized_img = _resize_to_fit(pil_img, max_width, max_height)
    if validated_save_path:
        try:
            _save_pil_image(resized_img, validated_save_path, quality)
        except OSError as error:
            raise ValueError(f"Unable to save screenshot: {error}") from None

    metadata = _capture_metadata(window, original_size, resized_img.size, mode_used)
    image = _pil_to_mcp_image(
        resized_img,
        max_width=max_width,
        max_height=max_height,
        format=normalized_format,
        quality=quality,
    )
    return metadata, image


@mcp.tool()
def list_open_windows() -> List[dict]:
    """
    Lists all visible open windows with their window titles, process names (e.g. Code.exe),
    PIDs, and screen coordinates. Call this first to identify your IDE or compiled app.
    """
    return _get_visible_windows()


@mcp.tool()
def capture_application_window(
    target: str = "",
    mode: str = "background",
    max_width: int = 1568,
    save_path: Optional[str] = None,
    allow_foreground_fallback: bool = False,
    hwnd: Optional[int] = None,
    pid: Optional[int] = None,
    max_height: int = 1568,
    format: str = "png",
    quality: int = 80,
    client_area_only: bool = False,
    delay_ms: int = 0,
) -> List[object]:
    """
    Captures a screenshot of a specific application window (such as an IDE or compiled app).

    Args:
        target: Title substring or process name, such as "Visual Studio Code" or
            "Code.exe".
        mode: "background" (captures via Win32 PrintWindow without stealing focus) or
              "foreground" (brings the window forward for MSS screen capture).
        max_width: Maximum response width; images are proportionally downscaled.
        save_path: Optional file path if you also want to save the PNG to disk.
        allow_foreground_fallback: Permits an activated MSS fallback after a failed
            background capture.
        hwnd: Exact window handle, which takes priority over pid and target.
        pid: Process ID, which takes priority over target.
    """
    metadata, image = _capture_application_window_data(
        target=target,
        mode=mode,
        max_width=max_width,
        max_height=max_height,
        save_path=save_path,
        allow_foreground_fallback=allow_foreground_fallback,
        hwnd=hwnd,
        pid=pid,
        format=format,
        quality=quality,
        client_area_only=client_area_only,
        delay_ms=delay_ms,
    )
    return [metadata, image]


@mcp.tool()
def capture_ide_and_app(
    ide_target: str = "Code.exe",
    app_target: str = "",
    max_width: int = 1568,
    max_height: int = 1568,
    format: str = "png",
    quality: int = 80,
    allow_foreground_fallback: bool = False,
) -> List[object]:
    """
    Captures both the IDE window and the compiled application window in one call,
    returning both images so the AI can compare the source code against the rendered UI.

    Args:
        ide_target: IDE title or process name, such as "Code.exe" or "devenv.exe".
        app_target: Window title or process name of the compiled application.
        max_width: Maximum pixel width per image.
    """
    if not app_target:
        raise ValueError(
            "Please provide app_target (the title or executable name of your application)."
        )

    results: List[object] = []
    for label, target in (("IDE", ide_target), ("Application", app_target)):
        try:
            metadata, image = _capture_application_window_data(
                target=target,
                mode="background",
                max_width=max_width,
                max_height=max_height,
                save_path=None,
                allow_foreground_fallback=allow_foreground_fallback,
                hwnd=None,
                pid=None,
                format=format,
                quality=quality,
                client_area_only=False,
                delay_ms=0,
            )
            results.extend([metadata, image])
        except ValueError as error:
            results.append(f"{label} capture failed: {error}")
    return results


def _capture_mss_region(region: dict) -> PILImage.Image:
    """Captures an MSS region and returns it as a PIL RGB image."""
    try:
        with mss.mss() as sct:
            screenshot = sct.grab(region)
            return PILImage.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
    except Exception as error:
        raise ValueError(f"Unable to capture screen pixels: {error}") from None


def _screen_capture_result(
    image: PILImage.Image,
    title: str,
    max_width: int,
    max_height: int,
    format: str,
    quality: int,
) -> List[object]:
    """Creates a metadata/image response for a screen or region capture."""
    normalized_format = _validate_image_options(max_width, max_height, format, quality)
    resized = _resize_to_fit(image, max_width, max_height)
    metadata = json.dumps(
        {
            "title": title,
            "process": None,
            "pid": None,
            "hwnd": None,
            "original_size": {"width": image.width, "height": image.height},
            "final_size": {"width": resized.width, "height": resized.height},
            "mode_used": "screen",
        }
    )
    return [
        metadata,
        _pil_to_mcp_image(
            resized,
            max_width=max_width,
            max_height=max_height,
            format=normalized_format,
            quality=quality,
        ),
    ]


@mcp.tool()
def capture_screen(
    monitor: int = 0,
    max_width: int = 1568,
    max_height: int = 1568,
    format: str = "png",
    quality: int = 80,
    delay_ms: int = 0,
) -> List[object]:
    """Captures all screens (0) or a specific one-based monitor number."""
    if delay_ms < 0:
        raise ValueError("delay_ms must be zero or greater.")
    try:
        with mss.mss() as sct:
            if not isinstance(monitor, int) or monitor < 0 or monitor >= len(sct.monitors):
                raise ValueError("monitor must be 0 (all screens) or a valid monitor number.")
            if delay_ms:
                time.sleep(delay_ms / 1000)
            screenshot = sct.grab(sct.monitors[monitor])
            image = PILImage.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"Unable to capture screen pixels: {error}") from None
    title = "All screens" if monitor == 0 else f"Monitor {monitor}"
    return _screen_capture_result(
        image, title, max_width, max_height, format, quality
    )


@mcp.tool()
def capture_region(
    left: int,
    top: int,
    width: int,
    height: int,
    max_width: int = 1568,
    max_height: int = 1568,
    format: str = "png",
    quality: int = 80,
    delay_ms: int = 0,
) -> List[object]:
    """Captures an arbitrary rectangular region of the virtual desktop."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive integers.")
    if delay_ms < 0:
        raise ValueError("delay_ms must be zero or greater.")
    if delay_ms:
        time.sleep(delay_ms / 1000)
    image = _capture_mss_region(
        {"left": left, "top": top, "width": width, "height": height}
    )
    return _screen_capture_result(
        image, "Screen region", max_width, max_height, format, quality
    )


@mcp.tool()
def wait_for_window(target: str, timeout_s: float = 10) -> dict:
    """Waits for an allowed visible window and returns its description."""
    if not target:
        raise ValueError("target must not be empty.")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be greater than zero.")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        window = _find_target_window(target)
        if window:
            return window
        time.sleep(0.1)
    raise ValueError(f"Timed out after {timeout_s:g} seconds waiting for window '{target}'.")


@mcp.tool()
def trigger_sharex_workflow(
    workflow_name: str = "ActiveWindow",
) -> str:
    """
    Optional bridge to trigger ShareX CLI commands if you want ShareX to run custom post-capture
    uploaders or image effects. Use capture_application_window for direct image responses.
    """
    if workflow_name not in ALLOWED_SHAREX_WORKFLOWS:
        supported = ", ".join(sorted(ALLOWED_SHAREX_WORKFLOWS))
        raise ValueError(
            f"Unsupported ShareX workflow '{workflow_name}'. Allowed workflows: {supported}."
        )

    sharex_exe_path = os.environ.get(
        "SHAREX_PATH", r"C:\Program Files\ShareX\ShareX.exe"
    )
    if not os.path.isfile(sharex_exe_path):
        raise ValueError(f"ShareX executable not found at {sharex_exe_path}.")

    cmd = [sharex_exe_path, f"-{workflow_name}", "-silent"]
    try:
        subprocess.Popen(cmd)
    except OSError as error:
        raise ValueError(f"Unable to start ShareX: {error}.") from None
    return f"Triggered ShareX command: {' '.join(cmd)}"


def main() -> None:
    """Runs the MCP server using the installed console entry point."""
    mcp.run()


if __name__ == "__main__":
    main()
