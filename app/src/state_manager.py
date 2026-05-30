from __future__ import annotations

from pathlib import Path
from typing import Any

from .utils import DATA_DIR, ensure_dirs, json_dump, json_load, now_iso


class StateManager:
    def __init__(self, state_path: Path | None = None):
        ensure_dirs()
        self.state_path = state_path or DATA_DIR / "run_state.json"

    def load(self) -> dict[str, Any]:
        return json_load(self.state_path, {})

    def update(self, **kwargs: Any) -> dict[str, Any]:
        state = self.load()
        state.update(kwargs)
        state["updated_at"] = now_iso()
        json_dump(self.state_path, state)
        return state
