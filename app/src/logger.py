from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import LOG_DIR, ensure_dirs, now_iso


class WorkflowLogger:
    def __init__(self, log_dir: Path | None = None):
        ensure_dirs()
        self.log_dir = log_dir or LOG_DIR
        self.log_file = self.log_dir / f"run_{datetime.now().strftime('%Y%m%d')}.log"

    def log_step(
        self,
        step: str,
        status: str,
        message: str,
        *,
        page: Any | None = None,
        url: str = "",
        screenshot_path: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "timestamp": now_iso(),
            "step": step,
            "status": status,
            "message": message,
            "url": url or self._page_url(page),
            "screenshot_path": screenshot_path,
        }
        if extra:
            record.update(extra)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[{record['timestamp']}] [{step}] {status}: {message}")
        if screenshot_path:
            print(f"截图: {screenshot_path}")
        return record

    @staticmethod
    def _page_url(page: Any | None) -> str:
        if not page:
            return ""
        try:
            return page.url
        except Exception:
            return ""
