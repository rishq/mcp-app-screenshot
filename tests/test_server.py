"""Unit tests for selection and security helpers without real application windows."""

import os

import pytest
from PIL import Image

import server


def _window(
    hwnd: int,
    title: str,
    process: str,
    pid: int,
    width: int = 100,
    height: int = 100,
) -> dict:
    return {
        "hwnd": hwnd,
        "title": title,
        "process": process,
        "pid": pid,
        "rect": {"left": 0, "top": 0, "width": width, "height": height},
        "is_minimized": False,
    }


def test_find_target_window_keeps_match_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    title_match = _window(1, "Code workspace", "Other.exe", 10, 400, 400)
    exact_process_match = _window(2, "Untitled", "Code.exe", 20, 50, 50)
    monkeypatch.setattr(server, "_get_visible_windows", lambda: [title_match, exact_process_match])

    assert server._find_target_window("code") == exact_process_match


def test_find_target_window_selects_largest_match(monkeypatch: pytest.MonkeyPatch) -> None:
    smaller = _window(1, "First", "Viewer.exe", 11, 100, 100)
    larger = _window(2, "Second", "Viewer.exe", 12, 300, 200)
    monkeypatch.setattr(server, "_get_visible_windows", lambda: [smaller, larger])

    assert server._find_target_window("viewer") == larger


def test_find_target_window_hwnd_and_pid_override_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _window(1, "Alpha", "Alpha.exe", 50, 100, 100)
    second = _window(2, "Beta", "Beta.exe", 50, 300, 300)
    monkeypatch.setattr(server, "_get_visible_windows", lambda: [first, second])

    assert server._find_target_window("does not match", hwnd=1, pid=50) == first
    assert server._find_target_window("does not match", pid=50) == second


def test_process_allow_and_block_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALLOWED_PROCESSES", raising=False)
    monkeypatch.delenv("BLOCKED_PROCESSES", raising=False)
    assert not server._is_process_allowed("KeePass.exe")
    assert server._is_process_allowed("Code.exe")

    monkeypatch.setenv("ALLOWED_PROCESSES", "Code.exe, MyApp.EXE")
    assert server._is_process_allowed("code.EXE")
    assert not server._is_process_allowed("chrome.exe")

    monkeypatch.setenv("BLOCKED_PROCESSES", "Code.exe")
    assert not server._is_process_allowed("CODE.exe")


def test_visible_window_filter_excludes_disallowed_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOWED_PROCESSES", "Code.exe")
    monkeypatch.delenv("BLOCKED_PROCESSES", raising=False)
    monkeypatch.setattr(server, "_is_cloaked_window", lambda hwnd: False)
    monkeypatch.setattr(server.win32gui, "IsWindowVisible", lambda hwnd: True)
    monkeypatch.setattr(server.win32gui, "GetWindowText", lambda hwnd: f"Window {hwnd}")
    monkeypatch.setattr(server.win32gui, "GetWindowRect", lambda hwnd: (0, 0, 100, 100))
    monkeypatch.setattr(server.win32gui, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(
        server.win32process, "GetWindowThreadProcessId", lambda hwnd: (1, hwnd)
    )

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid

        def name(self) -> str:
            return "Code.exe" if self.pid == 1 else "KeePass.exe"

    def enumerate_windows(callback, unused) -> None:
        callback(1, unused)
        callback(2, unused)

    monkeypatch.setattr(server.psutil, "Process", FakeProcess)
    monkeypatch.setattr(server.win32gui, "EnumWindows", enumerate_windows)

    assert [window["process"] for window in server._get_visible_windows()] == ["Code.exe"]


def test_validate_save_path_rejects_escaping_and_bad_extensions(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    screenshot_dir = tmp_path / "screenshots"
    monkeypatch.setenv("SCREENSHOT_DIR", str(screenshot_dir))

    assert server._validate_save_path("nested/capture.png") == os.path.realpath(
        screenshot_dir / "nested" / "capture.png"
    )
    with pytest.raises(ValueError, match="inside SCREENSHOT_DIR"):
        server._validate_save_path(str(tmp_path / "outside.png"))
    with pytest.raises(ValueError, match="extensions"):
        server._validate_save_path("capture.bmp")


def test_validate_save_path_rejects_symlink_escape(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    screenshot_dir = tmp_path / "screenshots"
    outside_dir = tmp_path / "outside"
    screenshot_dir.mkdir()
    outside_dir.mkdir()
    link = screenshot_dir / "link"
    try:
        link.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        realpath = os.path.realpath

        def simulated_realpath(path) -> str:
            path_text = os.fspath(path)
            if os.path.normcase(path_text).startswith(os.path.normcase(str(link))):
                suffix = path_text[len(str(link)) :].lstrip("\\/")
                return str(outside_dir / suffix)
            return realpath(path_text)

        monkeypatch.setattr(server.os.path, "realpath", simulated_realpath)
    monkeypatch.setenv("SCREENSHOT_DIR", str(screenshot_dir))

    with pytest.raises(ValueError, match="inside SCREENSHOT_DIR"):
        server._validate_save_path(str(link / "escape.png"))


def test_blank_frame_detection() -> None:
    assert server._is_blank_frame(Image.new("RGB", (8, 8), "black"))
    assert not server._is_blank_frame(Image.new("RGB", (8, 8), "white"))
