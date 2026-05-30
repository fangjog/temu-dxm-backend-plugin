from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from .utils import take_screenshot


DEFAULT_EXPECTED_PROFILE_PATH = r"C:\Users\Administrator\AppData\Local\Google\Chrome\User Data\Profile 13"
START_SCRIPT_HINT = "请运行 scripts/start_chrome_profile13.ps1 或 scripts/start_chrome_auto_profile.ps1"


DEFAULT_EXPECTED_PROFILE_PATH = r"D:\Users\Administrator\Documents\KK temu工作流\temu_dxm_mvp\runtime\browser_user_data\Profile 13"
START_SCRIPT_HINT = "Please run scripts/setup_isolated_browser.ps1, then scripts/start_isolated_browser.ps1."


class ChromeProfileMismatchError(RuntimeError):
    def __init__(self, actual_path: str, expected_path: str, screenshot_path: str = ""):
        self.actual_path = actual_path
        self.expected_path = expected_path
        self.screenshot_path = screenshot_path
        super().__init__(
            "当前Chrome接管资料错误。\n"
            f"实际Profile Path: {actual_path or '(未读取到)'}\n"
            f"期望Profile Path: {expected_path or '(未配置)'}\n"
            f"{START_SCRIPT_HINT}"
        )


class BrowserManager:
    def __init__(self, config: dict[str, Any], logger: Any | None = None):
        self.config = config
        self.logger = logger
        self.playwright = None
        self.browser = None
        self.context = None
        self.connected_over_cdp = False
        self.last_version_info: dict[str, Any] = {}

    def start(self) -> tuple[Any, Any, Any]:
        return self.connect_cdp(verify_profile=True)

    def connect_cdp(self, verify_profile: bool = True) -> tuple[Any, Any, Any]:
        cdp_url = self._cdp_url()
        if not cdp_url:
            raise RuntimeError(
                "CHROME_CDP_URL 为空。当前阶段不会自动 launch_persistent_context，以免创建空白资料。\n"
                f"{START_SCRIPT_HINT}"
            )

        self._log("browser_cdp", "start", f"连接 Chrome CDP: {cdp_url}")
        self._ensure_playwright()
        try:
            self.browser = self.playwright.chromium.connect_over_cdp(cdp_url)
        except Exception as exc:
            raise RuntimeError(
                f"无法连接 CHROME_CDP_URL={cdp_url}。\n"
                "请先运行启动脚本，确认 http://127.0.0.1:9222/json/version 可以访问。\n"
                f"{START_SCRIPT_HINT}"
            ) from exc

        self.connected_over_cdp = True
        if not self.browser.contexts:
            raise RuntimeError("CDP 连接成功，但没有可用浏览器上下文。请确认连接的是正常 Chrome 用户窗口。")

        self.context = self.browser.contexts[0]
        page = self.context.pages[-1] if self.context.pages else self.context.new_page()
        self._apply_timeouts(page)
        self._log("browser_cdp", "connected", "CDP 连接成功，开始校验 Chrome Profile。")

        if verify_profile:
            verify_page = self.context.new_page()
            self._apply_timeouts(verify_page)
            try:
                self.verify_profile_path(verify_page)
            finally:
                try:
                    verify_page.close()
                except Exception:
                    pass

        return self.browser, self.context, page

    def close(self) -> None:
        try:
            if not self.connected_over_cdp and self.context:
                self.context.close()
        finally:
            if self.playwright:
                self.playwright.stop()

    def fetch_cdp_version(self) -> dict[str, Any]:
        cdp_url = self._cdp_url()
        if not cdp_url:
            raise RuntimeError("CHROME_CDP_URL 为空，无法检查 /json/version。")
        url = self._json_version_url(cdp_url)
        self._log("chrome_diagnose", "start", f"检查 CDP 版本接口: {url}")
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"无法访问 {url}。\n"
                "请先运行启动脚本，并确认 Chrome 没有被普通窗口占用错误资料。\n"
                f"{START_SCRIPT_HINT}"
            ) from exc
        data = json.loads(payload)
        self._log(
            "chrome_diagnose",
            "ok",
            f"CDP 可连接: Browser={data.get('Browser', '')}, User-Agent={data.get('User-Agent', '')}",
        )
        return data

    def collect_chrome_version_info(self, page: Any) -> dict[str, Any]:
        self._log("chrome_version", "start", "打开 chrome://version 读取 Profile Path。", page=page)
        page.goto("chrome://version/", wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        page.wait_for_timeout(500)

        dom_info = page.evaluate(
            """() => {
                const rows = Array.from(document.querySelectorAll('tr')).map((tr) =>
                    Array.from(tr.children).map((cell) => (cell.innerText || cell.textContent || '').trim())
                ).filter((cells) => cells.length > 0);
                return {
                    text: document.body ? document.body.innerText : '',
                    rows
                };
            }"""
        )
        text = dom_info.get("text", "") or ""
        rows = dom_info.get("rows", []) or []

        info = {
            "google_chrome_version": self._extract_version_field(text, rows, ["Google Chrome", "Google Chrome版本", "Google Chrome 版本"]),
            "command_line": self._extract_version_field(text, rows, ["Command Line", "命令行"]),
            "executable_path": self._extract_version_field(text, rows, ["Executable Path", "可执行文件路径"]),
            "profile_path": self._extract_version_field(text, rows, ["Profile Path", "个人资料路径", "個人資料路徑"]),
            "raw_text_preview": text[:2000],
        }
        self.last_version_info = info
        self._log(
            "chrome_version",
            "ok",
            "chrome://version 读取完成: "
            f"version={info['google_chrome_version']}, executable={info['executable_path']}, profile={info['profile_path']}",
            page=page,
        )
        return info

    def verify_profile_path(self, page: Any, version_info: dict[str, Any] | None = None) -> bool:
        info = version_info or self.collect_chrome_version_info(page)
        actual_path = str(info.get("profile_path") or "").strip()
        expected_path = self.expected_profile_path()

        if self._same_windows_path(actual_path, expected_path):
            self._log("chrome_profile_verify", "ok", f"Profile Path 校验通过: {actual_path}", page=page)
            return True

        screenshot_path = ""
        try:
            screenshot_path = take_screenshot(page, "chrome_profile_mismatch")
        except Exception:
            pass
        message = (
            "当前Chrome接管资料错误，不能继续执行店小秘任务。\n"
            f"实际Profile Path: {actual_path or '(未读取到)'}\n"
            f"期望Profile Path: {expected_path or '(未配置)'}\n"
            f"{START_SCRIPT_HINT}"
        )
        self._log("chrome_profile_verify", "error", message, page=page, screenshot_path=screenshot_path)
        raise ChromeProfileMismatchError(actual_path, expected_path, screenshot_path)

    def expected_profile_path(self) -> str:
        expected = os.getenv("EXPECTED_PROFILE_PATH", "").strip()
        if expected:
            return expected

        user_data_dir = os.getenv("CHROME_USER_DATA_DIR", "").strip()
        profile_dir = os.getenv("CHROME_PROFILE_DIR", "").strip()
        if user_data_dir and profile_dir:
            return str(Path(user_data_dir) / profile_dir)

        return DEFAULT_EXPECTED_PROFILE_PATH

    def detect_extension_presence(self) -> dict[str, Any]:
        if not self.context:
            return {"observed": False, "count": 0, "urls": [], "extensions_page_text": "", "manual_hint": True}
        urls: list[str] = []
        try:
            urls.extend(page.url for page in self.context.background_pages if page.url.startswith("chrome-extension://"))
        except Exception:
            pass
        try:
            urls.extend(worker.url for worker in self.context.service_workers if worker.url.startswith("chrome-extension://"))
        except Exception:
            pass

        extensions_text = ""
        try:
            page = self.context.new_page()
            page.goto("chrome://extensions/", wait_until="domcontentloaded")
            page.wait_for_timeout(1000)
            extensions_text = page.locator("body").inner_text(timeout=2000)
            page.close()
        except Exception:
            extensions_text = ""

        has_dxm_text = any(token in extensions_text.lower() for token in ["店小秘", "dianxiaomi", "dxm"])
        observed = bool(urls) or has_dxm_text
        return {
            "observed": observed,
            "count": len(urls),
            "urls": sorted(set(urls)),
            "extensions_page_text": extensions_text[:1000],
            "manual_hint": not has_dxm_text,
        }

    @staticmethod
    def resolve_profile_dir(user_data_dir: str | Path, profile_name: str, profile_dir: str = "") -> str:
        root = Path(user_data_dir).expanduser()
        if profile_dir:
            candidate = root / profile_dir
            if not candidate.exists():
                raise RuntimeError(f"CHROME_PROFILE_DIR={profile_dir} 不存在，请检查路径: {candidate}")
            return profile_dir

        local_state_path = root / "Local State"
        if not local_state_path.exists():
            raise RuntimeError(f"找不到 Chrome Local State 文件: {local_state_path}")

        with local_state_path.open("r", encoding="utf-8") as f:
            local_state = json.load(f)
        info_cache = local_state.get("profile", {}).get("info_cache", {})
        for folder_name, info in info_cache.items():
            if info.get("name") == profile_name:
                if not (root / folder_name).exists():
                    raise RuntimeError(f"Local State 中找到 {profile_name}={folder_name}，但目录不存在: {root / folder_name}")
                return folder_name
        raise RuntimeError(
            f"没有在 Chrome Local State 的 profile.info_cache 中找到显示名为“{profile_name}”的资料。"
            "请在 .env 手动填写 CHROME_PROFILE_DIR，例如 Profile 13。"
        )

    def _ensure_playwright(self) -> None:
        if not self.playwright:
            self.playwright = sync_playwright().start()

    def _apply_timeouts(self, page: Any) -> None:
        timeout = int(self.config.get("browser", {}).get("timeout", 60000))
        try:
            self.context.set_default_timeout(timeout)
            page.set_default_timeout(timeout)
        except Exception:
            pass

    @staticmethod
    def _json_version_url(cdp_url: str) -> str:
        base = cdp_url.rstrip("/")
        if base.endswith("/json/version"):
            return base
        return f"{base}/json/version"

    @staticmethod
    def _extract_version_field(text: str, rows: list[list[str]], aliases: list[str]) -> str:
        normalized_aliases = [BrowserManager._normalize_label(alias) for alias in aliases]
        for row in rows:
            if not row:
                continue
            label = BrowserManager._normalize_label(row[0])
            if label in normalized_aliases and len(row) > 1:
                return " ".join(cell.strip() for cell in row[1:] if cell.strip()).strip()

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for idx, line in enumerate(lines):
            line_label = BrowserManager._normalize_label(line)
            for alias, normalized_alias in zip(aliases, normalized_aliases):
                if line_label == normalized_alias and idx + 1 < len(lines):
                    return lines[idx + 1].strip()
                if line_label.startswith(normalized_alias):
                    candidate = line[len(alias) :].strip(" \t:：")
                    if candidate:
                        return candidate

        joined = "\n".join(lines)
        for alias in aliases:
            match = re.search(rf"{re.escape(alias)}\s*[:：]?\s*(.+)", joined, flags=re.IGNORECASE)
            if match:
                return match.group(1).splitlines()[0].strip()
        return ""

    @staticmethod
    def _normalize_label(value: str) -> str:
        return re.sub(r"\s+", " ", value.replace("：", ":").strip().rstrip(":")).lower()

    @staticmethod
    def _same_windows_path(actual: str, expected: str) -> bool:
        if not actual or not expected:
            return False
        return os.path.normcase(os.path.normpath(actual)) == os.path.normcase(os.path.normpath(expected))

    @staticmethod
    def _cdp_url() -> str:
        return os.getenv("CHROME_CDP_URL", "").strip()

    def _log(self, step: str, status: str, message: str, **kwargs: Any) -> None:
        if self.logger:
            self.logger.log_step(step, status, message, **kwargs)
        else:
            print(f"[{step}] {status}: {message}")
