"""在服务端所在桌面弹出系统目录选择框。

Windows 用 PowerShell 的 FolderBrowserDialog，macOS 用 osascript，Linux 用
zenity/kdialog。服务端没有图形会话或缺少对话框程序时抛 RuntimeError，
前端退回手动输入路径。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_WINDOWS_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = $args[0]
$dialog.ShowNewFolderButton = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.SelectedPath
}
"""

_MACOS_SCRIPT = (
    "on run argv\n"
    "POSIX path of (choose folder with prompt (item 1 of argv))\n"
    "end run"
)


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _result(proc: subprocess.CompletedProcess) -> str | None:
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "无法打开目录选择框")
    path = proc.stdout.strip().splitlines()
    return path[-1] if path and path[-1] else None


def _pick_windows(title: str, timeout: int) -> str | None:
    return _result(
        _run(
            ["powershell", "-NoProfile", "-STA", "-Command", _WINDOWS_SCRIPT, title],
            timeout,
        )
    )


def _pick_macos(title: str, timeout: int) -> str | None:
    proc = _run(["osascript", "-e", _MACOS_SCRIPT, title], timeout)
    if proc.returncode != 0 and "User canceled" in (proc.stderr or ""):
        return None
    return _result(proc)


def _pick_linux(title: str, timeout: int) -> str | None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("当前服务没有图形会话")
    if shutil.which("zenity"):
        proc = _run(["zenity", "--file-selection", "--directory", "--title", title], timeout)
    elif shutil.which("kdialog"):
        proc = _run(
            ["kdialog", "--getexistingdirectory", str(Path.home()), "--title", title],
            timeout,
        )
    else:
        raise RuntimeError("系统缺少 zenity 或 kdialog")
    if proc.returncode == 1:
        return None
    return _result(proc)


def pick_directory(title: str = "选择导出目录", timeout: int = 300) -> str | None:
    try:
        if sys.platform == "win32":
            return _pick_windows(title, timeout)
        if sys.platform == "darwin":
            return _pick_macos(title, timeout)
        return _pick_linux(title, timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("目录选择超时") from exc
