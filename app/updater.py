from __future__ import annotations

import datetime as _dt
import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests


APP_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = APP_ROOT.parent
BACKUP_ROOT = PACKAGE_ROOT / "backups"
UPDATE_CONFIG_PATH = APP_ROOT / "update_config.json"
VERSION_PATH = APP_ROOT / "VERSION"

PRESERVE_DIRS = {
    "data/private",
    "data/reports",
    "data/logs",
    "data/debug",
}
PRESERVE_FILES = {
    ".env",
    ".env.credentials",
}


def current_version() -> str:
    return VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "0.0.0"


def load_update_config() -> dict:
    if not UPDATE_CONFIG_PATH.exists():
        return {}
    return json.loads(UPDATE_CONFIG_PATH.read_text(encoding="utf-8"))


def check_update() -> dict:
    cfg = load_update_config()
    latest_url = str(cfg.get("latest_url") or "").strip()
    if not latest_url:
        return {"status": "no_update_source", "message": "当前未配置更新源。", "current_version": current_version()}
    response = requests.get(latest_url, timeout=20)
    response.raise_for_status()
    latest = response.json()
    latest["current_version"] = current_version()
    latest["update_available"] = str(latest.get("version") or "") != current_version()
    latest["status"] = "ok"
    return latest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find_extracted_app(root: Path) -> Path:
    candidates = [root / "app"]
    candidates.extend(path / "app" for path in root.iterdir() if path.is_dir())
    for candidate in candidates:
        if (candidate / "plugin_main.py").exists() and (candidate / "webui.py").exists():
            return candidate
    raise RuntimeError("更新包里没有找到 app 目录。")


def _backup_current_app() -> Path:
    stamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = BACKUP_ROOT / f"app_{current_version()}_{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for item in APP_ROOT.iterdir():
        rel = item.name
        if rel == "__pycache__":
            continue
        target = backup_dir / rel
        if item.is_dir():
            shutil.copytree(item, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, target)
    return backup_dir


def _copy_new_app(new_app: Path) -> None:
    for item in new_app.iterdir():
        rel_name = item.name
        rel_path = rel_name.replace("\\", "/")
        if rel_path in PRESERVE_DIRS or rel_path in PRESERVE_FILES:
            continue
        target = APP_ROOT / rel_name
        if item.is_dir():
            if rel_name == "data":
                target.mkdir(exist_ok=True)
                for sub in item.iterdir():
                    sub_rel = f"data/{sub.name}"
                    if sub_rel in PRESERVE_DIRS:
                        continue
                    sub_target = target / sub.name
                    if sub_target.exists():
                        shutil.rmtree(sub_target) if sub_target.is_dir() else sub_target.unlink()
                    if sub.is_dir():
                        shutil.copytree(sub, sub_target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    else:
                        shutil.copy2(sub, sub_target)
                continue
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, target)


def apply_update(latest: dict | None = None) -> dict:
    latest = latest or check_update()
    if latest.get("status") != "ok":
        return latest
    package_url = str(latest.get("package_url") or "").strip()
    expected_hash = str(latest.get("sha256") or "").strip().lower()
    if not package_url or not expected_hash:
        return {"status": "invalid_latest", "message": "latest.json 缺少 package_url 或 sha256。"}
    with tempfile.TemporaryDirectory(prefix="temu_dxm_update_") as tmp:
        tmp_dir = Path(tmp)
        zip_path = tmp_dir / "update.zip"
        with requests.get(package_url, stream=True, timeout=120) as response:
            response.raise_for_status()
            with zip_path.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
        actual_hash = _sha256(zip_path)
        if actual_hash.lower() != expected_hash:
            return {"status": "sha256_mismatch", "expected": expected_hash, "actual": actual_hash}
        extract_dir = tmp_dir / "extract"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)
        new_app = _find_extracted_app(extract_dir)
        backup_dir = _backup_current_app()
        _copy_new_app(new_app)
        return {
            "status": "updated",
            "version": latest.get("version"),
            "backup_dir": str(backup_dir),
            "message": "更新完成，请重启插件。",
        }


def rollback_latest() -> dict:
    if not BACKUP_ROOT.exists():
        return {"status": "no_backup", "message": "没有可回退备份。"}
    backups = sorted([p for p in BACKUP_ROOT.iterdir() if p.is_dir() and p.name.startswith("app_")], key=lambda p: p.stat().st_mtime, reverse=True)
    if not backups:
        return {"status": "no_backup", "message": "没有可回退备份。"}
    backup = backups[0]
    for item in backup.iterdir():
        target = APP_ROOT / item.name
        if item.is_dir():
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(item, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copy2(item, target)
    return {"status": "rolled_back", "backup_dir": str(backup), "message": "已回退上一版本，请重启插件。"}
