"""Windows desktop shortcut and no-console launcher for AutoClip."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path

from . import paths

APP_URL = "http://127.0.0.1:8000"
SHORTCUT_NAME = "AutoClip.lnk"
LAUNCH_LOG_NAME = "launcher.log"

# SHSTOCKICONID.SIID_VIDEOFILES. Asking Windows for the stock icon location
# avoids hard-coding a resource index that can change between Windows versions.
_SIID_VIDEOFILES = 73
_MAX_PATH = 260


class _SHSTOCKICONINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("hIcon", ctypes.c_void_p),
        ("iSysImageIndex", ctypes.c_int),
        ("iIcon", ctypes.c_int),
        ("szPath", ctypes.c_wchar * _MAX_PATH),
    ]


class DesktopShortcutError(RuntimeError):
    """Desktop shortcut setup failed."""


@dataclass(frozen=True)
class DesktopShortcutStatus:
    supported: bool
    exists: bool
    path: Path | None = None


def shortcut_status() -> DesktopShortcutStatus:
    if sys.platform != "win32":
        return DesktopShortcutStatus(supported=False, exists=False)

    path = _desktop_dir() / SHORTCUT_NAME
    return DesktopShortcutStatus(supported=True, exists=path.is_file(), path=path)


def create_shortcut() -> DesktopShortcutStatus:
    """Create a Windows .lnk that starts AutoClip without PowerShell."""
    if sys.platform != "win32":
        raise DesktopShortcutError("Desktop shortcuts are currently supported on Windows only.")

    desktop = _desktop_dir()
    desktop.mkdir(parents=True, exist_ok=True)
    shortcut = desktop / SHORTCUT_NAME

    pythonw = _pythonw_path()
    if not pythonw.is_file():
        raise DesktopShortcutError(f"Could not find the AutoClip Python launcher at {pythonw}.")

    cscript = shutil.which("cscript.exe") or shutil.which("cscript")
    if not cscript:
        raise DesktopShortcutError("Windows Script Host (cscript.exe) is unavailable.")

    icon_location = _shortcut_icon_location()
    script = "\n".join(
        [
            'Set shell = CreateObject("WScript.Shell")',
            f"Set link = shell.CreateShortcut({_vbs_string(str(shortcut))})",
            f"link.TargetPath = {_vbs_string(str(pythonw))}",
            f"link.Arguments = {_vbs_string('-m autoclip.desktop')}",
            f"link.WorkingDirectory = {_vbs_string(str(paths.install_dir()))}",
            f"link.Description = {_vbs_string('Start AutoClip')}",
            f"link.IconLocation = {_vbs_string(icon_location)}",
            "link.WindowStyle = 7",
            "link.Save",
        ]
    )

    try:
        with tempfile.TemporaryDirectory(prefix="autoclip-shortcut-") as temp_dir:
            script_path = Path(temp_dir) / "create-shortcut.vbs"
            script_path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                [cscript, "//nologo", str(script_path)],
                capture_output=True,
                text=True,
                check=False,
            )
    except OSError as exc:
        raise DesktopShortcutError(f"Could not create the desktop shortcut: {exc}") from exc

    if result.returncode != 0 or not shortcut.is_file():
        detail = (result.stderr or result.stdout or "Windows did not create the shortcut.").strip()
        raise DesktopShortcutError(f"Could not create the desktop shortcut: {detail}")

    return shortcut_status()


def remove_shortcut() -> DesktopShortcutStatus:
    if sys.platform != "win32":
        raise DesktopShortcutError("Desktop shortcuts are currently supported on Windows only.")

    shortcut = _desktop_dir() / SHORTCUT_NAME
    try:
        shortcut.unlink(missing_ok=True)
    except OSError as exc:
        raise DesktopShortcutError(f"Could not remove {shortcut}: {exc}") from exc
    return shortcut_status()


def main() -> None:
    """Entry point used by the Windows desktop shortcut."""
    if sys.platform != "win32":
        webbrowser.open(APP_URL)
        return

    if _server_is_ready():
        webbrowser.open(APP_URL)
        return

    paths.ensure_layout()
    log_path = paths.root() / LAUNCH_LOG_NAME
    command = _server_command()

    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            subprocess.Popen(
                command,
                cwd=str(paths.install_dir()),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    except OSError as exc:
        _message_box(
            "AutoClip could not start",
            f"Could not start AutoClip.\n\n{exc}\n\nLauncher log: {log_path}",
        )
        return

    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _server_is_ready():
            webbrowser.open(APP_URL)
            return
        time.sleep(0.35)

    _message_box(
        "AutoClip is taking too long to start",
        (
            "AutoClip did not become ready within 30 seconds. "
            f"Check the launcher log for details:\n\n{log_path}"
        ),
    )


def _server_is_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{APP_URL}/api/health", timeout=0.7) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def _server_command() -> list[str]:
    executable = _autoclip_executable()
    if executable.is_file():
        return [str(executable), "serve", "--no-open"]

    python = _python_path()
    return [str(python), "-m", "autoclip.cli", "serve", "--no-open"]


def _shortcut_icon_location() -> str:
    """Return Windows' stock video-file icon location for the desktop shortcut."""
    if sys.platform == "win32":
        try:
            windll = getattr(ctypes, "windll", None)
            shell32 = getattr(windll, "shell32", None) if windll is not None else None
            get_stock_icon = (
                getattr(shell32, "SHGetStockIconInfo", None) if shell32 is not None else None
            )
            if get_stock_icon is not None:
                info = _SHSTOCKICONINFO()
                info.cbSize = ctypes.sizeof(_SHSTOCKICONINFO)
                result = get_stock_icon(
                    _SIID_VIDEOFILES,
                    0,  # SHGSI_ICONLOCATION
                    ctypes.byref(info),
                )
                if result == 0 and info.szPath:
                    return f"{info.szPath},{info.iIcon}"
        except (AttributeError, OSError, ValueError):
            pass

    # Keep shortcut creation working even if the stock-icon API is unavailable.
    return f"{_autoclip_executable()},0"


def _autoclip_executable() -> Path:
    return Path(sys.executable).resolve().with_name("autoclip.exe")


def _python_path() -> Path:
    current = Path(sys.executable).resolve()
    candidate = current.with_name("python.exe")
    return candidate if candidate.is_file() else current


def _pythonw_path() -> Path:
    current = Path(sys.executable).resolve()
    candidate = current.with_name("pythonw.exe")
    return candidate if candidate.is_file() else current


def _desktop_dir() -> Path:
    """Resolve the user's real Windows Desktop, including redirected profiles."""
    if sys.platform != "win32":
        raise DesktopShortcutError("Windows desktop lookup is unavailable on this platform.")

    # CSIDL_DESKTOPDIRECTORY. SHGetFolderPath follows Windows known-folder
    # redirection (for example OneDrive Desktop), unlike USERPROFILE/Desktop.
    windll = getattr(ctypes, "windll", None)
    shell32 = getattr(windll, "shell32", None) if windll is not None else None
    if shell32 is not None:
        buffer = ctypes.create_unicode_buffer(32768)
        result = shell32.SHGetFolderPathW(None, 0x0010, None, 0, buffer)
        if result == 0 and buffer.value:
            return Path(buffer.value)

    fallback = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    return fallback


def _vbs_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _message_box(title: str, message: str) -> None:
    try:
        windll = getattr(ctypes, "windll", None)
        user32 = getattr(windll, "user32", None) if windll is not None else None
        if user32 is not None:
            user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    main()
