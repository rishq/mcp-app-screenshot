# MCP Application & IDE Screenshot Server

A lightweight, ultra-fast (<15ms latency) Model Context Protocol (MCP) server for Windows that captures screenshots of specific application windows—such as your IDE (`Code.exe`, `devenv.exe`, `idea64.exe`) and your compiled applications—directly into memory for AI visual debugging.

## Features

- **Background Window Capture (`Win32 PrintWindow`):** Captures windows directly from the Windows Desktop Window Manager (DWM) buffer using `PW_RENDERFULLCONTENT`, even if the window is behind other windows.
- **Hardware-Accelerated Fallback (`mss`):** Supports bringing DirectX/OpenGL/Vulkan windows to the foreground for sub-15ms bounding-box screen grabs.
- **Side-by-Side IDE + App Capture:** Includes `capture_ide_and_app` to grab both your source code in the IDE and the running compiled application in a single tool call.
- **High-DPI Aware:** Automatically handles Windows 10/11 DPI scaling without cropping or blurring.
- **Optional ShareX CLI Bridge:** Includes `trigger_sharex_workflow` if you still wish to invoke ShareX custom uploaders or annotation workflows.

## Tools Provided

1. `list_open_windows()` — Lists all visible open windows with titles, process names, PIDs, and coordinates.
2. `capture_application_window(target, mode="background", max_width=1920, save_path=None)` — Captures a window by title substring or executable name.
3. `capture_ide_and_app(ide_target="Code.exe", app_target="...", max_width=1600)` — Captures both the IDE and target application in one call.
4. `trigger_sharex_workflow(workflow_name="ActiveWindow", sharex_exe_path="...")` — Triggers ShareX CLI commands silently.

## Quick Start (with `uv`)

```powershell
git clone https://github.com/rishq/mcp-app-screenshot.git
cd mcp-app-screenshot
uv sync
```

Test with the MCP Inspector:
```powershell
uv run mcp dev server.py
```

## MCP Client Configuration

Add this to your `.cursor/mcp.json`, `.vscode/mcp.json`, or `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "app-screenshot": {
      "command": "uv",
      "args": [
        "--directory",
        "C:\\path\\to\\mcp-app-screenshot",
        "run",
        "server.py"
      ]
    }
  }
}
```

## License

MIT
