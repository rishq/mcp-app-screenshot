# MCP Application & IDE Screenshot Server

A Windows-focused Model Context Protocol (MCP) server for capturing application windows, IDEs, monitors, and screen regions for visual debugging. It uses `PrintWindow` for background capture and MSS for foreground screen capture. Capture is fast—typically tens of milliseconds—but depends on the selected mode, window type, and image format.

## Install and run

The project targets Python 3.10+ and can be run directly with `uvx`:

```powershell
uvx --from . mcp-app-screenshot
```

For local development:

```powershell
uv sync
uv run mcp dev server.py
```

## MCP tools

- `list_open_windows()` lists permitted, visible application windows.
- `capture_application_window(target="", mode="background", max_width=1568, max_height=1568, format="png", quality=80, save_path=None, allow_foreground_fallback=False, hwnd=None, pid=None, client_area_only=False, delay_ms=0)` captures a chosen window. `hwnd`, then `pid`, take priority over `target`.
- `capture_ide_and_app(ide_target="Code.exe", app_target="...", max_width=1568, max_height=1568, format="png", quality=80, allow_foreground_fallback=False)` captures both independently, retaining a successful capture when the other fails.
- `capture_screen(monitor=0, max_width=1568, max_height=1568, format="png", quality=80, delay_ms=0)` captures all monitors (`0`) or one monitor (`1..N`).
- `capture_region(left, top, width, height, max_width=1568, max_height=1568, format="png", quality=80, delay_ms=0)` captures an arbitrary desktop rectangle.
- `wait_for_window(target, timeout_s=10)` waits for an allowed visible window and returns its description.
- `trigger_sharex_workflow(workflow_name="ActiveWindow")` triggers one approved ShareX workflow.

Capture tools return a JSON metadata string followed by an MCP image. Metadata includes the window title, process, PID, HWND, original and final dimensions, and the mode used. Supported response formats are `png`, `jpeg`, and `webp`; `quality` applies to JPEG and WebP.

## Security and configuration

The server does not expose windows belonging to password managers by default. These environment variables configure its boundaries:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SHAREX_PATH` | `C:\Program Files\ShareX\ShareX.exe` | The trusted ShareX executable used by `trigger_sharex_workflow`. Only `ActiveWindow`, `FullScreen`, and `RectangleRegion` can be invoked. |
| `SCREENSHOT_DIR` | `./screenshots` | The only directory where `capture_application_window(save_path=...)` may write. Paths are resolved to reject traversal and symlink escapes. Saved files must use `.png`, `.jpg`, `.jpeg`, or `.webp`. |
| `ALLOWED_PROCESSES` | unset | Optional comma-separated process allowlist, matched case-insensitively. When set, only listed processes are visible or capturable. |
| `BLOCKED_PROCESSES` | `KeePass.exe,KeePassXC.exe,1Password.exe,Bitwarden.exe,LastPass.exe` | Comma-separated process blocklist, matched case-insensitively. It takes precedence over the allowlist. |

For example, to expose only VS Code and a development application:

```powershell
$env:ALLOWED_PROCESSES = "Code.exe,MyApp.exe"
$env:SCREENSHOT_DIR = "D:\mcp-screenshots"
```

## Client configuration

Use `uvx` from an MCP client configuration:

```json
{
  "mcpServers": {
    "app-screenshot": {
      "command": "uvx",
      "args": [
        "--from",
        "C:\\path\\to\\mcp-app-screenshot",
        "mcp-app-screenshot"
      ],
      "env": {
        "SCREENSHOT_DIR": "C:\\path\\to\\screenshots",
        "ALLOWED_PROCESSES": "Code.exe,MyApp.exe"
      }
    }
  }
}
```

## Limitations

`PrintWindow` cannot reliably capture popup menus and some GPU-rendered windows. Use `mode="foreground"` for those applications, or set `allow_foreground_fallback=True` to opt into bringing the target window to the front. Foreground capture can include anything visually covering the window at capture time.

## Demo

<!-- Add a screenshot or short GIF demonstrating list_open_windows and capture_application_window here. -->

_Screenshot/GIF demo placeholder._

## Development

```powershell
uv run --with pytest --with ruff pytest
uv run --with ruff ruff check .
```

## License

MIT
