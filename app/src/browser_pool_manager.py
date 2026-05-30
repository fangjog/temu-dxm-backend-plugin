from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .browser_manager import BrowserManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class BrowserPoolManager:
    def __init__(self, config: dict[str, Any], logger: Any | None = None):
        self.config = config
        self.logger = logger
        self.browsers = list(config.get("browser_pool", {}).get("browsers", []))
        self.failures: list[dict[str, Any]] = []

    def setup_browser_pool(self) -> dict[str, Any]:
        script = PROJECT_ROOT / "scripts" / "setup_browser_pool.ps1"
        return _run_powershell(script, logger=self.logger)

    def start_browser_by_index(self, index: int) -> dict[str, Any]:
        browser = self._browser(index)
        script = PROJECT_ROOT / "scripts" / f"start_browser_{index}.ps1"
        if not script.exists():
            raise RuntimeError(f"Start script missing: {script}")
        result = _run_powershell(script, logger=self.logger)
        result.update({"browser_name": browser["name"], "cdp_url": browser["cdp_url"]})
        return result

    def connect_browser_by_index(self, index: int) -> tuple[BrowserManager, Any, Any, Any]:
        browser = self._browser(index)
        with self._browser_env(browser):
            manager = BrowserManager(self.config, logger=self.logger)
            playwright_browser, context, page = manager.start()
            return manager, playwright_browser, context, page

    def diagnose_browser_by_index(self, index: int) -> dict[str, Any]:
        browser = self._browser(index)
        with self._browser_env(browser):
            manager = BrowserManager(self.config, logger=self.logger)
            try:
                version = manager.fetch_cdp_version()
                _, _context, page = manager.connect_cdp(verify_profile=False)
                chrome_info = manager.collect_chrome_version_info(page)
                manager.verify_profile_path(page, chrome_info)
                extension_info = manager.detect_extension_presence()
                return {
                    "status": "ok",
                    "browser_name": browser["name"],
                    "cdp_url": browser["cdp_url"],
                    "expected_profile_path": browser["expected_profile_path"],
                    "actual_profile_path": chrome_info.get("profile_path", ""),
                    "cdp_version": version,
                    "extension_observed": extension_info.get("observed", False),
                    "extension_count": extension_info.get("count", 0),
                }
            finally:
                manager.close()

    def close_browser_by_index(self, index: int) -> dict[str, Any]:
        browser = self._browser(index)
        port = str(browser["cdp_url"]).rstrip("/").split(":")[-1]
        ps = (
            "$port = '" + port + "'; "
            "$items = Get-CimInstance Win32_Process -Filter \"name = 'chrome.exe'\" | "
            "Where-Object { $_.CommandLine -like \"*--remote-debugging-port=$port*\" }; "
            "$items | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; "
            "Write-Output ($items.Count)"
        )
        completed = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30)
        count = (completed.stdout or "").strip()
        result = {"status": "ok" if completed.returncode == 0 else "warning", "browser_name": browser["name"], "closed_processes": count}
        self._log("browser_pool_close", result["status"], f"Closed {browser['name']} chrome processes: {count}")
        return result

    def close_all_pool_browsers(self) -> dict[str, Any]:
        results = []
        for index in range(1, self.browser_count() + 1):
            try:
                results.append(self.close_browser_by_index(index))
            except Exception as exc:
                results.append({"status": "warning", "index": index, "message": str(exc)})
        return {"status": "ok", "results": results}

    def validate_pool_assets(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        ok = True
        for index, browser in enumerate(self.browsers, start=1):
            user_data_dir = Path(str(browser.get("user_data_dir", "")))
            profile_path = Path(str(browser.get("expected_profile_path", "")))
            script = PROJECT_ROOT / "scripts" / f"start_browser_{index}.ps1"
            item = {
                "index": index,
                "browser_name": browser.get("name", f"browser_{index}"),
                "cdp_url": browser.get("cdp_url", ""),
                "user_data_dir": str(user_data_dir),
                "expected_profile_path": str(profile_path),
                "user_data_exists": user_data_dir.exists(),
                "profile_exists": profile_path.exists(),
                "start_script": str(script),
                "start_script_exists": script.exists(),
            }
            item["status"] = "ok" if item["user_data_exists"] and item["profile_exists"] and item["start_script_exists"] else "missing"
            ok = ok and item["status"] == "ok"
            items.append(item)
        result = {"status": "ok" if ok and items else "missing", "browsers": items}
        self._log("browser_pool_validate", result["status"], "Validated browser pool assets.", extra=result)
        return result

    def get_next_browser(self, current_index: int) -> int | None:
        next_index = current_index + 1
        return next_index if next_index <= len(self.browsers) else None

    def record_failure(self, index: int, reason: str, screenshot_path: str = "", extra: dict[str, Any] | None = None) -> None:
        browser = self._browser(index)
        failure = {
            "browser_name": browser["name"],
            "index": index,
            "reason": reason,
            "screenshot_path": screenshot_path,
        }
        if extra:
            failure.update(extra)
        self.failures.append(failure)
        self._log("browser_pool_failure", "warning", f"{browser['name']} failed: {reason}", extra=failure)

    def browser_count(self) -> int:
        return len(self.browsers)

    def browser_config(self, index: int) -> dict[str, Any]:
        return dict(self._browser(index))

    def _browser(self, index: int) -> dict[str, Any]:
        if index < 1 or index > len(self.browsers):
            raise IndexError(f"Browser index out of range: {index}")
        return self.browsers[index - 1]

    @contextmanager
    def _browser_env(self, browser: dict[str, Any]) -> Iterator[None]:
        keys = ["CHROME_CDP_URL", "CHROME_USER_DATA_DIR", "CHROME_PROFILE_DIR", "EXPECTED_PROFILE_PATH"]
        old = {key: os.environ.get(key) for key in keys}
        os.environ["CHROME_CDP_URL"] = str(browser["cdp_url"])
        os.environ["CHROME_USER_DATA_DIR"] = str(browser["user_data_dir"])
        os.environ["CHROME_PROFILE_DIR"] = str(browser.get("profile_dir", "Profile 13"))
        os.environ["EXPECTED_PROFILE_PATH"] = str(browser["expected_profile_path"])
        try:
            yield
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _log(self, step: str, status: str, message: str, **kwargs: Any) -> None:
        if self.logger:
            self.logger.log_step(step, status, message, **kwargs)
        else:
            print(f"[{step}] {status}: {message}")


def is_cdp_reachable(cdp_url: str) -> bool:
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=2):
            return True
    except (urllib.error.URLError, TimeoutError):
        return False


def _run_powershell(script: Path, logger: Any | None = None) -> dict[str, Any]:
    completed = subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=600,
    )
    result = {
        "status": "ok" if completed.returncode == 0 else "error",
        "script": str(script),
        "returncode": completed.returncode,
        "stdout": (completed.stdout or "").strip(),
        "stderr": (completed.stderr or "").strip(),
    }
    if logger:
        logger.log_step("browser_pool_script", result["status"], f"Ran {script.name}", extra=result)
    if completed.returncode != 0:
        raise RuntimeError(f"{script.name} failed: {result['stderr'] or result['stdout']}")
    return result
