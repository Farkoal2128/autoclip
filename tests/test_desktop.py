"""Windows desktop launcher helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from autoclip import desktop


def test_create_shortcut_uses_pythonw_and_creates_link(
    monkeypatch, tmp_path: Path
) -> None:
    shortcut = tmp_path / desktop.SHORTCUT_NAME
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(b"pythonw")

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_desktop_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop, "_pythonw_path", lambda: pythonw)
    monkeypatch.setattr(desktop, "_autoclip_executable", lambda: tmp_path / "autoclip.exe")
    monkeypatch.setattr(desktop.paths, "install_dir", lambda: tmp_path)
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "cscript.exe")

    captured: dict[str, str] = {}

    def fake_run(command, **kwargs):
        script_path = Path(command[-1])
        captured["script"] = script_path.read_text(encoding="utf-8")
        shortcut.write_text("shortcut", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(desktop.subprocess, "run", fake_run)

    status = desktop.create_shortcut()

    assert status.supported is True
    assert status.exists is True
    assert status.path == shortcut
    assert str(pythonw) in captured["script"]
    assert "-m autoclip.desktop" in captured["script"]


def test_shortcut_status_is_unsupported_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(desktop.sys, "platform", "linux")

    status = desktop.shortcut_status()

    assert status.supported is False
    assert status.exists is False
    assert status.path is None
