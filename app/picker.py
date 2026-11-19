"""在服务端所在桌面弹出系统“另存为”对话框。

Windows 用 PowerShell 的 SaveFileDialog，macOS 用 osascript，Linux 用
zenity/kdialog。服务端没有图形会话或缺少对话框程序时抛 RuntimeError。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_WINDOWS_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.SaveFileDialog
$dialog.Title = $args[0]
$dialog.FileName = $args[1]
$dialog.Filter = $args[2]
$dialog.InitialDirectory = $args[3]
$dialog.OverwritePrompt = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    Write-Output $dialog.FileName
}
"""

_MACOS_SCRIPT = (
    "on run argv\n"
    "set target to POSIX file (item 3 of argv) as alias\n"
    "POSIX path of (choose file name with prompt (item 1 of argv) "
    "default name (item 2 of argv) default location target)\n"
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
        raise RuntimeError(detail[-1] if detail else "无法打开保存对话框")
    path = proc.stdout.strip().splitlines()
    return path[-1] if path and path[-1] else None


def _full_path(default_name: str, initial_dir: str) -> Path:
    base = Path(initial_dir).expanduser() if initial_dir else Path.home()
    if not base.is_dir():
        base = Path.home()
    return base / default_name


def _save_windows(default_name: str, initial_dir: str, label: str,
                  pattern: str, title: str, timeout: int) -> str | None:
    target = _full_path(default_name, initial_dir)
    file_filter = f"{label} ({pattern})|{pattern}|所有文件 (*.*)|*.*"
    return _result(
        _run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-Command",
                _WINDOWS_SCRIPT,
                title,
                default_name,
                file_filter,
                str(target.parent),
            ],
            timeout,
        )
    )


def _save_macos(default_name: str, initial_dir: str, title: str, timeout: int) -> str | None:
    target = _full_path(default_name, initial_dir)
    proc = _run(["osascript", "-e", _MACOS_SCRIPT, title, default_name, str(target.parent)], timeout)
    if proc.returncode != 0 and "User canceled" in (proc.stderr or ""):
        return None
    return _result(proc)


def _save_linux(default_name: str, initial_dir: str, label: str,
                pattern: str, title: str, timeout: int) -> str | None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        raise RuntimeError("当前服务没有图形会话")
    target = _full_path(default_name, initial_dir)
    if shutil.which("zenity"):
        proc = _run(
            [
                "zenity",
                "--file-selection",
                "--save",
                "--confirm-overwrite",
                "--title",
                title,
                "--filename",
                str(target),
                f"--file-filter={label} | {pattern}",
            ],
            timeout,
        )
    elif shutil.which("kdialog"):
        proc = _run(
            [
                "kdialog",
                "--getsavefilename",
                str(target),
                f"{label} ({pattern})",
                "--title",
                title,
            ],
            timeout,
        )
    else:
        raise RuntimeError("系统缺少 zenity 或 kdialog")
    if proc.returncode == 1:
        return None
    return _result(proc)


def save_file(
    default_name: str,
    suffix: str,
    initial_dir: str = "",
    title: str = "导出文件",
    timeout: int = 300,
) -> str | None:
    """弹出另存为对话框，返回用户选择的完整路径；取消时返回 None。"""
    pattern = f"*{suffix}"
    label = {
        ".html": "HTML 文件",
        ".pdf": "PDF 文件",
        ".md": "Markdown 文件",
    }.get(suffix, "文件")
    try:
        if sys.platform == "win32":
            return _save_windows(default_name, initial_dir, label, pattern, title, timeout)
        if sys.platform == "darwin":
            return _save_macos(default_name, initial_dir, title, timeout)
        return _save_linux(default_name, initial_dir, label, pattern, title, timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("保存对话框超时") from exc
