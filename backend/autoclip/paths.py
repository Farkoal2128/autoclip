"""Filesystem layout for AutoClip control files and large artifacts.

The small control root defaults to ``~/.autoclip`` and contains the SQLite
database, settings, and a pointer to the data location. Media, work files, and
exports default to that same directory but can be moved to another drive from
Settings without moving a live database.

Layout::

    <control-root>/
      autoclip.db
      config.json
      storage-location.json   optional pointer to a custom data root

    <data-root>/              defaults to <control-root>
      media/                  source videos, one directory per source id
      work/<job_id>/          audio, transcript, crop path, captions
      exports/                finished clips
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

ENV_HOME = "AUTOCLIP_HOME"
ENV_STORAGE_HOME = "AUTOCLIP_STORAGE_HOME"

DEFAULT_DIR_NAME = ".autoclip"
STORAGE_LOCATION_FILE = "storage-location.json"

#: The project was called ClipForge before release. An install that predates the
#: rename has its media, database, and settings under the old name, and media
#: files are far too large to copy silently — so the old directory is adopted in
#: place when the new one doesn't exist yet.
LEGACY_DIR_NAME = ".clipforge"


def root() -> Path:
    """Return the AutoClip home directory.

    Resolved fresh on every call rather than cached at import time, so tests
    that patch ``AUTOCLIP_HOME`` take effect without reloading the module.
    """
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser().resolve()

    default = Path.home() / DEFAULT_DIR_NAME
    if not default.exists():
        legacy = Path.home() / LEGACY_DIR_NAME
        if legacy.is_dir():
            log.info(
                "Using the pre-rename data directory %s. Rename it to %s when "
                "convenient; AutoClip will use whichever it finds.",
                legacy,
                default,
            )
            return legacy.resolve()

    return default.resolve()


def storage_location_path() -> Path:
    """Small pointer file that keeps the bulky data location independent of config.json."""
    return root() / STORAGE_LOCATION_FILE


def data_root() -> Path:
    """Return the directory containing media, work, and exports.

    AUTOCLIP_HOME continues to own the small control files (SQLite database,
    config, storage pointer). The large artifacts can be redirected independently
    through the settings UI or AUTOCLIP_STORAGE_HOME.
    """
    override = os.environ.get(ENV_STORAGE_HOME)
    if override:
        return Path(override).expanduser().resolve()

    marker = storage_location_path()
    if marker.is_file():
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            configured = str(payload.get("path") or "").strip()
            if configured:
                return Path(configured).expanduser().resolve()
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log.warning("Could not read %s (%s); using the default data folder.", marker, exc)

    return root()


def set_data_root(path: Path | str | None) -> Path:
    """Persist the folder used for media/work/exports."""
    if os.environ.get(ENV_STORAGE_HOME):
        raise RuntimeError(
            f"{ENV_STORAGE_HOME} is set; remove that environment override before changing storage."
        )

    base = root()
    marker = storage_location_path()
    if path is None:
        marker.unlink(missing_ok=True)
        return base

    target = Path(path).expanduser().resolve()
    if target == base:
        marker.unlink(missing_ok=True)
        return base

    base.mkdir(parents=True, exist_ok=True)
    tmp = marker.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"path": str(target)}, indent=2), encoding="utf-8")
    os.replace(tmp, marker)
    return target


def media_dir() -> Path:
    return data_root() / "media"


def work_dir() -> Path:
    return data_root() / "work"


def exports_dir() -> Path:
    return data_root() / "exports"


DB_NAME = "autoclip.db"
LEGACY_DB_NAME = "clipforge.db"


def db_path() -> Path:
    """Return the SQLite database path.

    Adopts a pre-rename database in place rather than starting an empty one
    beside it, which would silently orphan every job the user already has.
    """
    base = root()
    current = base / DB_NAME
    if not current.exists():
        legacy = base / LEGACY_DB_NAME
        if legacy.is_file():
            return legacy
    return current


def config_path() -> Path:
    return root() / "config.json"


def install_dir() -> Path:
    """Return the source checkout root when available, otherwise the package folder.

    Editable installs resolve to the repo backend/autoclip package; walking up
    finds the directory that contains both backend and frontend. A wheel install
    has no frontend source tree, so opening the installed Python package is the
    most useful fallback.
    """
    package_dir = Path(__file__).resolve().parent
    candidates = (package_dir.parents[1], package_dir.parent, package_dir)
    for candidate in candidates:
        if (candidate / "backend").is_dir() and (candidate / "frontend").is_dir():
            return candidate
    return package_dir


def job_work_dir(job_id: str) -> Path:
    """Return the per-job scratch directory for pipeline stage artifacts."""
    return work_dir() / job_id


def source_media_dir(source_id: str) -> Path:
    """Return the directory holding a source's downloaded/uploaded media."""
    return media_dir() / source_id


def ensure_layout() -> Path:
    """Create the directory tree if absent and return the root.

    Safe to call repeatedly; used on startup and at the top of each CLI command.
    """
    base = root()
    for path in (base, data_root(), media_dir(), work_dir(), exports_dir()):
        path.mkdir(parents=True, exist_ok=True)
    return base
