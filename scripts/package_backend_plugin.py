from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
REPO_NAME = "temu-dxm-backend-plugin"


def _version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _owner() -> str:
    owner = os.getenv("GITHUB_OWNER", "").strip()
    if owner:
        return owner
    return "<owner>"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _sync_update_config(owner: str, version: str) -> None:
    latest_url = f"https://github.com/{owner}/{REPO_NAME}/releases/latest/download/latest.json"
    payload = {"latest_url": latest_url, "version": version}
    _write_json(ROOT / "update_config.json", payload)
    app_cfg = ROOT / "app" / "update_config.json"
    app_payload = {"version": version, "latest_url": latest_url, "update_url": "", "notes": ""}
    _write_json(app_cfg, app_payload)
    (ROOT / "app" / "VERSION").write_text(version + "\n", encoding="utf-8", newline="\n")


def _should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    parts = set(rel.parts)
    rel_text = rel.as_posix()
    if ".git" in parts or "__pycache__" in parts or "dist" in parts or "backups" in parts:
        return True
    if rel_text.startswith("browser/user_data/") or rel_text.startswith("browser/User Data/"):
        return True
    if rel_text.startswith("app/data/private/") or rel_text.startswith("app/data/reports/") or rel_text.startswith("app/data/logs/") or rel_text.startswith("app/data/debug/"):
        return True
    if path.name in {".env", ".env.credentials"}:
        return True
    if path.suffix.lower() in {".pyc", ".log"}:
        return True
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    version = _version()
    owner = _owner()
    _sync_update_config(owner, version)
    DIST.mkdir(exist_ok=True)
    zip_path = DIST / f"temu_dxm_backend_plugin_v{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    prefix = "temu_dxm_backend_plugin"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file() or _should_skip(path):
                continue
            zf.write(path, Path(prefix) / path.relative_to(ROOT))
    sha = _sha256(zip_path)
    latest = {
        "version": version,
        "package_url": f"https://github.com/{owner}/{REPO_NAME}/releases/download/v{version}/{zip_path.name}",
        "sha256": sha,
        "notes": "初始可迁移版本" if version == "0.1.0" else "版本更新",
        "force_update": False,
    }
    latest_path = DIST / "latest.json"
    _write_json(latest_path, latest)
    _write_json(ROOT / "latest.json", latest)
    print(json.dumps({"zip_path": str(zip_path), "latest_json_path": str(latest_path), "sha256": sha}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
