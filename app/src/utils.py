from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
LOG_DIR = DATA_DIR / "logs"
SCREENSHOT_DIR = PROJECT_ROOT / "screenshots"


class ManualRequiredError(RuntimeError):
    """Raised when the workflow needs a human selector/login/form decision."""

    def __init__(self, step: str, message: str, screenshot_path: str = ""):
        super().__init__(message)
        self.step = step
        self.message = message
        self.screenshot_path = screenshot_path


def ensure_dirs() -> None:
    for path in (DATA_DIR, LOG_DIR, SCREENSHOT_DIR):
        path.mkdir(parents=True, exist_ok=True)


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    config_path = config_path or PROJECT_ROOT / "config.yaml"
    with config_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def safe_filename(value: str) -> str:
    value = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("_")
    return value[:120] or "snapshot"


def json_load(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return default


def json_dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def take_screenshot(page: Any, step: str, product_id: str = "") -> str:
    ensure_dirs()
    suffix = f"_{safe_filename(product_id)}" if product_id else ""
    path = SCREENSHOT_DIR / f"{safe_filename(step)}{suffix}_{now_ts()}.png"
    try:
        page.screenshot(path=str(path), full_page=True, timeout=15000)
    except Exception:
        page.screenshot(path=str(path), full_page=False, timeout=15000)
    return str(path)


def body_text(page: Any, timeout: int = 3000) -> str:
    try:
        return page.locator("body").inner_text(timeout=timeout)
    except Exception:
        return ""
