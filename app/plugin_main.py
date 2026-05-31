from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import yaml

from secure_config import load_settings, mask_secret, save_settings


APP_ROOT = Path(__file__).resolve().parent
PACKAGE_ROOT = APP_ROOT.parent
BROWSER_ROOT = PACKAGE_ROOT / "browser"
USER_DATA_ROOT = Path(os.environ.get("TEMU_DXM_USER_DATA_DIR") or (PACKAGE_ROOT / "user_data"))
DRAFT_URL = "https://www.dianxiaomi.com/web/temu/choiceTemuList/draft"
DEFAULT_PORT = 9333


FLOATING_CONTROL_SCRIPT = r"""
(() => {
  const ROOT_ID = 'dxm-auto-uploader-floating';
  const STYLE_ID = 'dxm-auto-uploader-floating-style';
  const ensurePanel = () => {
    if (!document.body) return false;
    let style = document.getElementById(STYLE_ID);
    if (!style) {
      style = document.createElement('style');
      style.id = STYLE_ID;
      style.textContent = `
        #${ROOT_ID}{position:fixed;right:18px;top:45%;z-index:2147483647;font-family:Arial,"Microsoft YaHei",sans-serif;color:#111827}
        #${ROOT_ID} .dxm-auto-btn{width:70px;height:70px;border-radius:999px;background:#2563eb;color:#fff;border:0;box-shadow:0 10px 24px rgba(0,0,0,.24);cursor:pointer;font-size:14px;font-weight:700;line-height:1.2}
        #${ROOT_ID} .dxm-auto-panel{display:none;width:224px;margin-top:10px;background:#fff;border:1px solid #d1d5db;border-radius:8px;box-shadow:0 10px 28px rgba(0,0,0,.2);padding:10px}
        #${ROOT_ID} .dxm-auto-panel button{width:100%;height:31px;margin:4px 0;border:1px solid #cbd5e1;background:#f8fafc;border-radius:6px;cursor:pointer;font-size:13px}
        #${ROOT_ID} .dxm-auto-panel button.primary{background:#2563eb;color:#fff;border-color:#2563eb}
        #${ROOT_ID} .dxm-auto-status{font-size:12px;color:#374151;line-height:1.55;margin-top:6px;word-break:break-all}
      `;
      document.documentElement.appendChild(style);
    }
    let root = document.getElementById(ROOT_ID);
    if (!root) {
      root = document.createElement('div');
      root.id = ROOT_ID;
      root.innerHTML = `
        <button class="dxm-auto-btn" type="button">自动<br>上架</button>
        <div class="dxm-auto-panel">
          <button class="primary" data-action="start">开始运行</button>
          <button data-action="pause">暂停运行</button>
          <button data-action="resume">继续运行</button>
          <button data-action="stop">停止运行</button>
          <div class="dxm-auto-status">
            <div>当前状态：<span data-role="status">待命</span></div>
            <div>当前进度：<span data-role="progress">-</span></div>
          </div>
        </div>
      `;
      document.body.appendChild(root);
    }
    const panel = root.querySelector('.dxm-auto-panel');
    const status = root.querySelector('[data-role="status"]');
    const progress = root.querySelector('[data-role="progress"]');
    const refresh = () => {
      if (status) status.textContent = localStorage.getItem('temuDxmAutoStatus') || '待命';
      if (progress) progress.textContent = localStorage.getItem('temuDxmAutoProgress') || '-';
    };
    if (!root.dataset.bound) {
      root.dataset.bound = '1';
      root.querySelector('.dxm-auto-btn')?.addEventListener('click', () => {
        panel.style.display = panel.style.display === 'block' ? 'none' : 'block';
        refresh();
      });
      panel?.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-action]');
        if (!button) return;
        const action = button.getAttribute('data-action');
        if (action === 'start') {
          localStorage.setItem('temuDxmAutoStatus', '启动中');
          localStorage.setItem('temuDxmAutoPause', '0');
          localStorage.setItem('temuDxmAutoStop', '0');
          fetch('http://127.0.0.1:8765/run_saved', {method:'POST', mode:'no-cors'}).catch(() => {});
        } else if (action === 'pause') {
          localStorage.setItem('temuDxmAutoPause', '1');
          localStorage.setItem('temuDxmAutoStatus', '已暂停');
        } else if (action === 'resume') {
          localStorage.setItem('temuDxmAutoPause', '0');
          localStorage.setItem('temuDxmAutoStatus', '运行中');
        } else if (action === 'stop') {
          localStorage.setItem('temuDxmAutoStop', '1');
          localStorage.setItem('temuDxmAutoPause', '0');
          localStorage.setItem('temuDxmAutoStatus', '停止中');
        }
        refresh();
      });
    }
    refresh();
    return true;
  };
  ensurePanel();
  if (!window.__dxmAutoUploaderReinjectBound) {
    window.__dxmAutoUploaderReinjectBound = true;
    document.addEventListener('DOMContentLoaded', ensurePanel);
    setInterval(ensurePanel, 2000);
  }
})();
"""


def _plugin_log(message: str) -> None:
    try:
        log_dir = USER_DATA_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with (log_dir / "plugin_main.log").open("a", encoding="utf-8") as handle:
            handle.write(f"[{stamp}] {message}\n")
    except Exception:
        pass


def _ensure_utf8_console() -> None:
    for stream in (getattr(sys, "stdout", None), getattr(sys, "stderr", None)):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _version_text() -> str:
    try:
        return (APP_ROOT / "VERSION").read_text(encoding="utf-8-sig").strip()
    except Exception:
        return "unknown"


def _find_chrome() -> Path | None:
    candidates = [
        BROWSER_ROOT / "chrome" / "chrome.exe",
        BROWSER_ROOT / "Chrome" / "chrome.exe",
        BROWSER_ROOT / "GoogleChromePortable" / "App" / "Chrome-bin" / "chrome.exe",
        Path(os.environ.get("PLUGIN_CHROME_EXE", "")),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
    try:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            candidates.append(Path(pw.chromium.executable_path))
        finally:
            pw.stop()
    except Exception:
        pass
    for candidate in candidates:
        if candidate and str(candidate) != "." and candidate.exists():
            return candidate
    found = shutil.which("chrome") or shutil.which("chrome.exe")
    return Path(found) if found else None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def _connect_cdp(cdp_url: str):
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(cdp_url)
    return pw, browser


def _open_draft_in_existing_browser(cdp_url: str, inject_panel: bool = True) -> None:
    pw = None
    browser = None
    try:
        pw, browser = _connect_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = None
        for candidate in context.pages:
            url = (candidate.url or "").lower()
            if "dianxiaomi.com" in url and "choicetumulist" in url:
                page = candidate
                break
        if page is None:
            page = context.new_page()
        page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=15000)
        try:
            page.bring_to_front()
        except Exception:
            pass
        if inject_panel:
            inject_control_panel(cdp_url)
    except Exception:
        pass
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass


def inject_control_panel(cdp_url: str = "http://127.0.0.1:9333") -> bool:
    pw = None
    browser = None
    ok = False
    try:
        pw, browser = _connect_cdp(cdp_url)
        context = browser.contexts[0] if browser.contexts else None
        if not context:
            return False
        try:
            context.add_init_script(FLOATING_CONTROL_SCRIPT)
        except Exception:
            pass
        for page in context.pages:
            try:
                if "dianxiaomi.com" in (page.url or "").lower():
                    page.evaluate(FLOATING_CONTROL_SCRIPT)
                    exists = page.evaluate("() => !!document.querySelector('#dxm-auto-uploader-floating')")
                    ok = bool(exists)
                    _plugin_log("floating_control_injected ok" if ok else "floating_control_injected failed: selector missing")
            except Exception:
                _plugin_log("floating_control_injected failed: evaluate exception")
                continue
        if not ok:
            _plugin_log("floating_control_injected failed: no dianxiaomi page injected")
        return ok
    except Exception:
        _plugin_log("floating_control_injected failed: cdp connection exception")
        return False
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass


def start_browser_if_needed(port: int = DEFAULT_PORT, open_draft: bool = True) -> dict:
    user_data = USER_DATA_ROOT / "browser_profile"
    profile_dir = "Profile"
    expected_profile = user_data / profile_dir
    cdp_url = f"http://127.0.0.1:{port}"
    if _port_open(port):
        if open_draft:
            _open_draft_in_existing_browser(cdp_url, inject_panel=True)
        return {"started": False, "cdp_url": cdp_url, "user_data_dir": str(user_data), "expected_profile_path": str(expected_profile)}
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome executable found in the package runtime.")
    user_data.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile_dir}",
        "--new-window",
        DRAFT_URL if open_draft else "about:blank",
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(25):
        if _port_open(port):
            break
        time.sleep(0.4)
    if open_draft:
        time.sleep(0.8)
        inject_control_panel(cdp_url)
    return {"started": True, "cdp_url": cdp_url, "user_data_dir": str(user_data), "expected_profile_path": str(expected_profile)}


def update_runtime_config(count: int) -> None:
    config_path = APP_ROOT / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config.setdefault("dxm_publish_twice", {})
    config["dxm_publish_twice"]["source_product_count"] = max(1, int(count))
    config["dxm_publish_twice"]["target_title"] = ""
    config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")


def latest_excel() -> Path | None:
    reports = APP_ROOT / "data" / "reports"
    files = sorted(reports.glob("dxm_publish_twice_result_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def run_automation(args: argparse.Namespace) -> int:
    _ensure_utf8_console()
    saved = load_settings()
    api_key = args.api_key or os.environ.get("PLUGIN_EASYROUTER_API_KEY") or saved.get("api_key") or ""
    dxm_username = args.dxm_username or os.environ.get("PLUGIN_DXM_USERNAME") or saved.get("dxm_username") or ""
    dxm_password = args.dxm_password or os.environ.get("PLUGIN_DXM_PASSWORD") or saved.get("dxm_password") or ""
    if not api_key:
        print("EasyRouter API Key is required.")
        return 2

    update_runtime_config(args.count)
    if args.save_config:
        save_settings(
            {
                "edit_count": args.count,
                "api_key": api_key,
                "dxm_username": dxm_username,
                "dxm_password": dxm_password,
                "version": _version_text(),
                "api_test_ok": saved.get("api_test_ok", False),
                "api_test_at": saved.get("api_test_at", ""),
                "api_test_message": saved.get("api_test_message", ""),
            }
        )

    browser_info = {"cdp_url": os.environ.get("CHROME_CDP_URL", f"http://127.0.0.1:{args.port}")}
    if not args.no_start_browser:
        browser_info = start_browser_if_needed(args.port, open_draft=True)

    env = os.environ.copy()
    env.update(
        {
            "EASYROUTER_API_KEY": api_key,
            "EASYROUTER_BASE_URL": env.get("EASYROUTER_BASE_URL", "https://easyrouter.io/v1"),
            "EASYROUTER_TEXT_MODEL": env.get("EASYROUTER_TEXT_MODEL", "deepseek-v4-pro"),
            "EASYROUTER_FAST_MODEL": env.get("EASYROUTER_FAST_MODEL", "deepseek-v4-flash"),
            "EASYROUTER_PRO_MODEL": env.get("EASYROUTER_PRO_MODEL", "deepseek-v4-pro"),
            "EASYROUTER_BACKUP_MODEL": env.get("EASYROUTER_BACKUP_MODEL", "qwen3.6-plus"),
            "DXM_USERNAME": dxm_username,
            "DXM_PASSWORD": dxm_password,
            "CHROME_CDP_URL": browser_info.get("cdp_url", f"http://127.0.0.1:{args.port}"),
            "CHROME_USER_DATA_DIR": browser_info.get("user_data_dir", str(USER_DATA_ROOT / "browser_profile")),
            "CHROME_PROFILE_DIR": "Profile",
            "EXPECTED_PROFILE_PATH": browser_info.get("expected_profile_path", str(USER_DATA_ROOT / "browser_profile" / "Profile")),
        }
    )
    print(f"Version: {_version_text()}")
    print(f"Edit count: {args.count}")
    print(f"EasyRouter key: {mask_secret(api_key)}")
    print(f"CDP URL: {env['CHROME_CDP_URL']}")
    if args.dry_run:
        print("Dry-run OK. No automation started.")
        return 0

    proc = subprocess.run([sys.executable, "main.py", "dxm-publish-twice"], cwd=APP_ROOT, env=env)
    excel = latest_excel()
    if excel:
        print(f"Latest Excel: {excel}")
        if args.open_excel:
            try:
                os.startfile(excel)  # type: ignore[attr-defined]
            except Exception as exc:
                print(f"Could not open Excel automatically: {exc}")
    return proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Temu DXM backend automation plugin launcher")
    parser.add_argument("--count", type=int, default=4, help="Number of collected products to process.")
    parser.add_argument("--api-key", default="", help="EasyRouter API Key. Prefer the web UI for safer entry.")
    parser.add_argument("--dxm-username", default="", help="Dianxiaomi username.")
    parser.add_argument("--dxm-password", default="", help="Dianxiaomi password.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Chrome CDP port.")
    parser.add_argument("--no-start-browser", action="store_true", help="Do not start Chrome; connect to existing CDP only.")
    parser.add_argument("--open-browser-only", action="store_true", help="Only start/open the browser and exit.")
    parser.add_argument("--inject-control-only", action="store_true", help="Only inject the floating DXM page control panel.")
    parser.add_argument("--open-excel", action="store_true", default=True, help="Open the latest Excel after the run.")
    parser.add_argument("--no-open-excel", dest="open_excel", action="store_false")
    parser.add_argument("--save-config", action="store_true", help="Save credentials/API key with local Windows encryption.")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration without starting automation.")
    args = parser.parse_args()
    if args.inject_control_only:
        return 0 if inject_control_panel(f"http://127.0.0.1:{args.port}") else 1
    if args.open_browser_only:
        info = start_browser_if_needed(args.port, open_draft=True)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    return run_automation(args)


if __name__ == "__main__":
    raise SystemExit(main())
