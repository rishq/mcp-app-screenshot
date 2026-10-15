import ctypes
import io
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


def _get_visible_windows() -> List[dict]:
    """Enumerates all visible application windows with non-zero dimensions."""
    windows = []

    def enum_handler(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if not title:
                return
            rect = win32gui.GetWindowRect(hwnd)
            width = rect[2] - rect[0]
            height = rect[3] - rect[1]
            if width <= 0 or height <= 0:
                return

            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc_name = "unknown"
            try:
                proc_name = psutil.Process(pid).name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            windows.append({
                "hwnd": hwnd,
                "title": title,
                "process": proc_name,
                "pid": pid,
                "rect": {"left": rect[0], "top": rect[1], "width": width, "height": height},
                "is_minimized": bool(win32gui.IsIconic(hwnd)),
            })

    win32gui.EnumWindows(enum_handler, None)
    return windows


def _find_target_window(query: str) -> Optional[dict]:
    """Finds the best matching window by title substring or process name."""
    query_lower = query.lower()
    windows = _get_visible_windows()

    # Priority 1: Exact process name match (e.g., "Code.exe" or "myapp.exe")
    for w in windows:
        if w["process"].lower() == query_lower or w["process"].lower() == f"{query_lower}.exe":
            return w

    # Priority 2: Title substring match
    for w in windows:
        if query_lower in w["title"].lower():
            return w

    # Priority 3: Process substring match
    for w in windows:
        if query_lower in w["process"].lower():
            return w

    return None


def _capture_hwnd_background(hwnd: int) -> Optional[PILImage.Image]:
    """
    Captures a window directly from its DWM buffer using PrintWindow.
    Works even if the window is occluded by other windows.
    """
    if win32gui.IsIconic(hwnd):
        # Minimized windows do not maintain an active render buffer; restore briefly without stealing focus
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

    # Call PrintWindow with PW_RENDERFULLCONTENT
    result = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)

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

    # Clean up GDI handles to prevent memory leaks
    win32gui.DeleteObject(bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)

    if result != 1:
        return None
    return img


def _capture_hwnd_foreground_mss(hwnd: int) -> PILImage.Image:
    """
    Brings the window to the front and captures its screen bounding box via MSS.
    Best for OpenGL/Vulkan/DirectX compiled apps or when child popups need to be visible.
    """
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    # Trick Windows into allowing SetForegroundWindow from a background script
    shell = ctypes.windll.user32
    shell.keybd_event(0x12, 0, 0, 0)  # ALT down
    win32gui.SetForegroundWindow(hwnd)
    shell.keybd_event(0x12, 0, 2, 0)  # ALT up
    time.sleep(0.15)  # Wait for DWM transition

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    monitor_region = {
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }

    with mss.mss() as sct:
        sct_img = sct.grab(monitor_region)
        return PILImage.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")


def _pil_to_mcp_image(pil_img: PILImage.Image, max_width: int = 1920) -> Image:
    """Optimizes and encodes a PIL Image into a FastMCP Image object."""
    if pil_img.width > max_width:
        ratio = max_width / float(pil_img.width)
        new_height = int(float(pil_img.height) * ratio)
        pil_img = pil_img.resize((max_width, new_height), PILImage.Resampling.LANCZOS)

    buffer = io.BytesIO()
    pil_img.save(buffer, format="PNG", optimize=True)
    return Image(data=buffer.getvalue(), format="png")


@mcp.tool()
def list_open_windows() -> List[dict]:
    """
    Lists all visible open windows with their window titles, process names (e.g. Code.exe),
    PIDs, and screen coordinates. Call this first to identify your IDE or compiled app.
    """
    return _get_visible_windows()


@mcp.tool()
def capture_application_window(
    target: str,
    mode: str = "background",
    max_width: int = 1920,
    save_path: Optional[str] = None
) -> Image:
    """
    Captures a screenshot of a specific application window (such as an IDE or compiled app).

    Args:
        target: Substring of the window title (e.g., "Visual Studio Code") or process name (e.g., "Code.exe", "myapp.exe").
        mode: "background" (captures via Win32 PrintWindow without stealing focus) or
              "foreground" (brings window to front and uses ultra-fast MSS screen grab—use for DirectX/OpenGL apps).
        max_width: Downscales wide 4K screenshots to save LLM vision tokens while preserving code legibility.
        save_path: Optional file path if you also want to save the PNG to disk.
    """
    win = _find_target_window(target)
    if not win:
        available = [f"{w['process']} ('{w['title']}')" for w in _get_visible_windows()[:15]]
        raise ValueError(f"No window matching '{target}'. Top open windows: {', '.join(available)}")

    hwnd = win["hwnd"]
    pil_img = None

    if mode == "background":
        pil_img = _capture_hwnd_background(hwnd)

    # Fallback to foreground MSS if background PrintWindow fails or mode is explicitly foreground
    if pil_img is None:
        pil_img = _capture_hwnd_foreground_mss(hwnd)

    if save_path:
        os.makedirs(os.path.dirname(os.path.abspath(save_path)), exist_ok=True)
        pil_img.save(save_path, format="PNG")

    return _pil_to_mcp_image(pil_img, max_width=max_width)


@mcp.tool()
def capture_ide_and_app(
    ide_target: str = "Code.exe",
    app_target: str = "",
    max_width: int = 1600
) -> List[Image]:
    """
    Captures both the IDE window and the compiled application window in one call,
    returning both images so the AI can compare the source code against the rendered UI.

    Args:
        ide_target: Window title or process name of the IDE (default: "Code.exe" for VS Code, or use "devenv.exe", "idea64.exe").
        app_target: Window title or process name of the compiled application.
        max_width: Maximum pixel width per image.
    """
    if not app_target:
        raise ValueError("Please provide 'app_target' (the window title or executable name of your compiled app).")

    ide_img = capture_application_window(target=ide_target, mode="background", max_width=max_width)
    app_img = capture_application_window(target=app_target, mode="background", max_width=max_width)
    return [ide_img, app_img]


@mcp.tool()
def trigger_sharex_workflow(
    workflow_name: str = "ActiveWindow",
    sharex_exe_path: str = r"C:\Program Files\ShareX\ShareX.exe"
) -> str:
    """
    Optional bridge to trigger ShareX CLI commands if you want ShareX to run custom post-capture
    uploaders or image effects. Note: Use capture_application_window to return images directly to the AI.
    """
    if not os.path.exists(sharex_exe_path):
        raise FileNotFoundError(f"ShareX executable not found at {sharex_exe_path}")

    cmd = [sharex_exe_path, f"-{workflow_name}", "-silent"]
    subprocess.Popen(cmd)
    return f"Triggered ShareX command: {' '.join(cmd)}"


if __name__ == "__main__":
    mcp.run()
