"""
Бэкапы SQLite-БД WBnator.

Зачем: рабочая БД (data/wbnator.db) хранит пользователей, балансы и историю —
её потеря = потеря всех аккаунтов. Бэкапы делаются ОНЛАЙН (через sqlite3
Connection.backup() API), что безопасно при включённом WAL и при работающем
сервере.

Куда складываются (по приоритету):
  1) %USERPROFILE%/.wbnator/backups/   — основное место, ВНЕ папки проекта,
     чтобы случайный rm/перезапись каталога WBnator_site не убил архив.
  2) <project>/data/backups/           — локальная копия рядом с БД (запасная).

Имя файла: ``wbnator-<reason>-<UTC-ISO>.db``, где reason ∈
{startup, periodic, manual, pre-init, shutdown, pre-restore, …}.

Ротация: на каждый reason храним последние ROTATION_KEEP[reason] (по умолчанию
20), плюс ``manual`` — без лимита.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from . import db as _db

_BASE = Path(__file__).resolve().parent.parent
_PROJECT_BACKUP_DIR = _BASE / "data" / "backups"


def _home_backup_dir() -> Path:
    override = os.environ.get("WBNATOR_BACKUP_DIR")
    if override:
        return Path(override)
    return Path.home() / ".wbnator" / "backups"


ROTATION_KEEP = {
    "startup":   30,
    "periodic":  72,    # ~3 суток при шаге 1 час
    "pre-init":  20,
    "shutdown":  20,
    "pre-restore": 20,
}

_PERIODIC_THREAD: Optional[threading.Thread] = None
_PERIODIC_STOP = threading.Event()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _safe_reason(reason: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in (reason or "manual"))
    return out or "manual"


def _ensure_dirs() -> tuple[Path, Path]:
    home_dir = _home_backup_dir()
    home_dir.mkdir(parents=True, exist_ok=True)
    _PROJECT_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return home_dir, _PROJECT_BACKUP_DIR


def _online_copy(src_db_path: Path, dest_path: Path) -> None:
    """Атомарный online-снимок SQLite в dest_path (учитывает WAL и параллельные пишущие транзакции)."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_path.with_suffix(dest_path.suffix + ".part")
    src = sqlite3.connect(str(src_db_path))
    dst = sqlite3.connect(str(tmp))
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp, dest_path)


def backup_now(reason: str = "manual") -> list[Path]:
    """Сделать бэкап. Возвращает список путей, куда лёг файл (обычно 2 — home + project)."""
    reason = _safe_reason(reason)
    if not _db.DB_PATH.exists():
        return []

    home_dir, proj_dir = _ensure_dirs()
    name = f"wbnator-{reason}-{_ts()}.db"

    results: list[Path] = []
    primary = home_dir / name
    try:
        _online_copy(_db.DB_PATH, primary)
        results.append(primary)
    except Exception as e:
        print(f"[backup] WARN: home backup failed → {e}")

    secondary = proj_dir / name
    try:
        if results:
            shutil.copy2(results[0], secondary)
        else:
            _online_copy(_db.DB_PATH, secondary)
        results.append(secondary)
    except Exception as e:
        print(f"[backup] WARN: project backup failed → {e}")

    try:
        _prune(reason)
    except Exception as e:
        print(f"[backup] WARN: prune failed → {e}")

    return results


def _prune(reason: str) -> None:
    keep = ROTATION_KEEP.get(reason)
    if keep is None:
        return
    for d in (_home_backup_dir(), _PROJECT_BACKUP_DIR):
        if not d.is_dir():
            continue
        items = sorted(
            (p for p in d.glob(f"wbnator-{reason}-*.db") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in items[keep:]:
            try:
                stale.unlink()
            except Exception:
                pass


def list_backups() -> list[dict]:
    """Все найденные бэкапы (из обоих каталогов), новые сверху."""
    seen: dict[str, dict] = {}
    for d in (_home_backup_dir(), _PROJECT_BACKUP_DIR):
        if not d.is_dir():
            continue
        for p in d.glob("wbnator-*.db"):
            if not p.is_file():
                continue
            st = p.stat()
            row = seen.setdefault(p.name, {
                "name": p.name,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "paths": [],
            })
            row["paths"].append(str(p))
            row["mtime"] = max(row["mtime"], st.st_mtime)
    return sorted(seen.values(), key=lambda r: r["mtime"], reverse=True)


def find_backup(name_or_path: str) -> Optional[Path]:
    p = Path(name_or_path)
    if p.is_file():
        return p
    for d in (_home_backup_dir(), _PROJECT_BACKUP_DIR):
        candidate = d / name_or_path
        if candidate.is_file():
            return candidate
    return None


def restore(name_or_path: str) -> Path:
    """Восстановить БД из бэкапа. Перед этим — снимок текущей в reason=pre-restore."""
    src = find_backup(name_or_path)
    if not src:
        raise FileNotFoundError(f"Бэкап «{name_or_path}» не найден ни в одной из папок.")

    try:
        backup_now("pre-restore")
    except Exception as e:
        print(f"[backup] WARN: pre-restore snapshot failed → {e}")

    _db.close_conn()

    target = _db.DB_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    for sidecar in (target.with_suffix(target.suffix + "-wal"),
                    target.with_suffix(target.suffix + "-shm")):
        try:
            if sidecar.exists():
                sidecar.unlink()
        except Exception:
            pass

    shutil.copy2(src, target)
    print(f"[backup] восстановлено из {src} → {target}")
    return target


# ── periodic thread ──────────────────────────────────────────────────────────

def start_periodic(interval_seconds: int = 3600) -> None:
    """Запускает фоновый поток, делающий periodic-бэкап каждые N секунд (по умолчанию 1 час)."""
    global _PERIODIC_THREAD
    if _PERIODIC_THREAD is not None and _PERIODIC_THREAD.is_alive():
        return
    _PERIODIC_STOP.clear()

    def _loop():
        while not _PERIODIC_STOP.is_set():
            if _PERIODIC_STOP.wait(interval_seconds):
                return
            try:
                backup_now("periodic")
            except Exception as e:
                print(f"[backup] periodic failed → {e}")

    t = threading.Thread(target=_loop, name="wbnator-backup", daemon=True)
    t.start()
    _PERIODIC_THREAD = t


def stop_periodic() -> None:
    _PERIODIC_STOP.set()
