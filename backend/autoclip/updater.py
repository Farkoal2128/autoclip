"""Self-update helper for source-checkout AutoClip installs.

The web server starts this module in a detached process, then shuts itself down.
The updater waits for that server process to exit before touching the checkout,
fast-forwards main, refreshes the editable Python install and frontend bundle,
then starts AutoClip again. Update results are persisted under the control root
so the restarted UI can show a one-time success or failure message.
"""

from __future__ import annotations

import argparse
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
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from . import paths

APP_URL = "http://127.0.0.1:8000"
UPDATE_RESULT_NAME = "update-result.json"
UPDATE_LOG_NAME = "update.log"


class UpdateError(RuntimeError):
    """AutoClip cannot safely perform an in-app update."""


def result_path() -> Path:
    return paths.root() / UPDATE_RESULT_NAME


def preflight() -> None:
    """Validate everything needed before the server agrees to shut down."""
    install = paths.install_dir()
    if not (install / ".git").exists():
        raise UpdateError(
            "In-app update requires a Git checkout. This AutoClip install was not "
            "found inside a Git repository."
        )

    for command, label in (("git", "Git"), ("uv", "uv"), ("npm", "Node/npm")):
        if shutil.which(command) is None:
            raise UpdateError(f"{label} is not available on PATH.")

    status = _capture(["git", "status", "--porcelain"], cwd=install)
    if status.strip():
        raise UpdateError(
            "AutoClip has local file changes. Commit, stash, or discard them before "
            "using in-app update."
        )


def launch_detached(parent_pid: int) -> str:
    """Start the updater independently of the running web server."""
    preflight()
    paths.ensure_layout()
    token = uuid4().hex
    result_path().unlink(missing_ok=True)

    python = _python_path()
    command = [
        str(python),
        "-m",
        "autoclip.updater",
        "--parent-pid",
        str(parent_pid),
        "--token",
        token,
        "--install-dir",
        str(paths.install_dir()),
    ]

    log_path = paths.root() / UPDATE_LOG_NAME
    with log_path.open("a", encoding="utf-8") as log_file:
        kwargs: dict = {
            "cwd": str(paths.install_dir()),
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True

        try:
            subprocess.Popen(command, **kwargs)
        except OSError as exc:
            raise UpdateError(f"Could not start the AutoClip updater: {exc}") from exc

    return token


def read_result(token: str) -> dict | None:
    path = result_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if payload.get("token") == token else None


def clear_result(token: str) -> None:
    path = result_path()
    payload = read_result(token)
    if payload is not None:
        path.unlink(missing_ok=True)


def _write_result(
    token: str,
    *,
    status: str,
    message: str,
    from_revision: str | None = None,
    to_revision: str | None = None,
) -> None:
    paths.ensure_layout()
    payload = {
        "token": token,
        "status": status,
        "message": message,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    target = result_path()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def _capture(command: list[str], *, cwd: Path) -> str:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise UpdateError(f"Could not run {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()
        raise UpdateError(f"{' '.join(command)} failed: {detail}")
    return result.stdout.strip()


def _run(command: list[str], *, cwd: Path) -> None:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(
        command,
        cwd=str(cwd),
        env=env,
        check=False,
    )
    if result.returncode != 0:
        raise UpdateError(
            f"{' '.join(command)} exited with code {result.returncode}. "
            f"See {paths.root() / UPDATE_LOG_NAME} for details."
        )


def _wait_for_parent_exit(pid: int) -> None:
    if pid <= 0:
        return

    if sys.platform == "win32":
        # SYNCHRONIZE is enough to wait on a process handle without inspecting it.
        synchronize = 0x00100000
        infinite = 0xFFFFFFFF
        windll = getattr(ctypes, "windll", None)
        kernel32 = getattr(windll, "kernel32", None) if windll is not None else None
        if kernel32 is not None:
            handle = kernel32.OpenProcess(synchronize, False, pid)
            if handle:
                try:
                    kernel32.WaitForSingleObject(handle, infinite)
                    return
                finally:
                    kernel32.CloseHandle(handle)

    while True:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return
        time.sleep(0.2)


def _python_path() -> Path:
    current = Path(sys.executable).resolve()
    candidate = current.with_name("python.exe")
    return candidate if candidate.is_file() else current


def _server_command() -> list[str]:
    python = _python_path()
    return [str(python), "-m", "autoclip.cli", "serve", "--no-open"]


def _start_server(install: Path) -> None:
    log_path = paths.root() / "launcher.log"
    with log_path.open("a", encoding="utf-8") as log_file:
        kwargs: dict = {
            "cwd": str(install),
            "stdin": subprocess.DEVNULL,
            "stdout": log_file,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(_server_command(), **kwargs)


def _server_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{APP_URL}/api/health", timeout=0.7) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _restore_static(backup: Path | None, static_dir: Path) -> None:
    if backup is None or not backup.is_dir():
        return
    shutil.rmtree(static_dir, ignore_errors=True)
    shutil.copytree(backup, static_dir)


def perform_update(token: str, parent_pid: int, install: Path) -> None:
    """Wait for shutdown, update the checkout, and always attempt to relaunch."""
    install = install.resolve()
    frontend = install / "frontend"
    static_dir = install / "backend" / "autoclip" / "static"
    uv = shutil.which("uv")
    npm = shutil.which("npm")
    git = shutil.which("git")
    if not uv or not npm or not git:
        _write_result(
            token,
            status="error",
            message="Update tools disappeared from PATH before the update could start.",
        )
        _start_server(install)
        return

    _wait_for_parent_exit(parent_pid)

    original_revision: str | None = None
    original_branch: str | None = None
    updated_revision: str | None = None
    backup_root: Path | None = None

    try:
        original_revision = _capture([git, "rev-parse", "HEAD"], cwd=install)
        try:
            original_branch = _capture([git, "branch", "--show-current"], cwd=install) or None
        except UpdateError:
            original_branch = None

        if _capture([git, "status", "--porcelain"], cwd=install).strip():
            raise UpdateError(
                "Local file changes appeared before the update started; update was cancelled."
            )

        if static_dir.is_dir():
            backup_root = Path(tempfile.mkdtemp(prefix="autoclip-update-static-"))
            shutil.copytree(static_dir, backup_root / "static")

        _run([git, "fetch", "origin", "main"], cwd=install)
        _run([git, "switch", "main"], cwd=install)
        _run([git, "pull", "--ff-only", "origin", "main"], cwd=install)
        updated_revision = _capture([git, "rev-parse", "HEAD"], cwd=install)

        _run([uv, "pip", "install", "--python", str(_python_path()), "-e", "."], cwd=install)
        _run([npm, "ci"], cwd=frontend)
        _run([npm, "run", "build"], cwd=frontend)

        changed = original_revision != updated_revision
        message = (
            "AutoClip updated successfully."
            if changed
            else "AutoClip is already up to date."
        )
        _write_result(
            token,
            status="success",
            message=message,
            from_revision=original_revision,
            to_revision=updated_revision,
        )
    except Exception as exc:
        # The checkout was clean before the update, so restoring its previous
        # revision is safe and prevents a half-updated app from being relaunched.
        try:
            if original_revision is not None:
                _run([git, "reset", "--hard", original_revision], cwd=install)
                if original_branch and original_branch != "main":
                    _run([git, "switch", original_branch], cwd=install)
            _restore_static(backup_root / "static" if backup_root else None, static_dir)
            _run([uv, "pip", "install", "--python", str(_python_path()), "-e", "."], cwd=install)
        except Exception:
            pass

        _write_result(
            token,
            status="error",
            message=f"AutoClip update failed: {exc}",
            from_revision=original_revision,
            to_revision=updated_revision,
        )
    finally:
        if backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)

    _start_server(install)

    # Give the replacement server a chance to bind before this helper exits.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _server_ready():
            return
        time.sleep(0.35)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    args = parser.parse_args()
    perform_update(args.token, args.parent_pid, args.install_dir)


if __name__ == "__main__":
    main()
