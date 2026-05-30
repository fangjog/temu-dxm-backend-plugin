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
DRAFT_URL = "https://www.dianxiaomi.com/web/temu/choiceTemuList/draft"
DEFAULT_PORT = 9333


def _find_chrome() -> Path | None:
    candidates = [
        BROWSER_ROOT / "chrome" / "chrome.exe",
        BROWSER_ROOT / "Chrome" / "chrome.exe",
        BROWSER_ROOT / "GoogleChromePortable" / "App" / "Chrome-bin" / "chrome.exe",
        Path(os.environ.get("PLUGIN_CHROME_EXE", "")),
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ]
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


def start_browser_if_needed(port: int = DEFAULT_PORT) -> dict:
    user_data = BROWSER_ROOT / "user_data"
    profile_dir = "Profile 13"
    expected_profile = user_data / profile_dir
    if _port_open(port):
        return {"started": False, "cdp_url": f"http://127.0.0.1:{port}", "user_data_dir": str(user_data), "expected_profile_path": str(expected_profile)}
    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome executable found. Put portable Chrome under browser/chrome/chrome.exe or install Google Chrome.")
    user_data.mkdir(parents=True, exist_ok=True)
    args = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        f"--profile-directory={profile_dir}",
        "--new-window",
        DRAFT_URL,
    ]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(25):
        if _port_open(port):
            break
        time.sleep(0.4)
    return {"started": True, "cdp_url": f"http://127.0.0.1:{port}", "user_data_dir": str(user_data), "expected_profile_path": str(expected_profile)}


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
    saved = load_settings()
    api_key = args.api_key or os.environ.get("PLUGIN_EASYROUTER_API_KEY") or saved.get("api_key") or ""
    dxm_username = args.dxm_username or os.environ.get("PLUGIN_DXM_USERNAME") or saved.get("dxm_username") or ""
    dxm_password = args.dxm_password or os.environ.get("PLUGIN_DXM_PASSWORD") or saved.get("dxm_password") or ""
    if not api_key:
        print("EasyRouter API Key is required.")
        return 2

    update_runtime_config(args.count)
    if args.save_config:
        save_settings({
            "edit_count": args.count,
            "api_key": api_key,
            "dxm_username": dxm_username,
            "dxm_password": dxm_password,
            "version": (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip() if (APP_ROOT / "VERSION").exists() else "",
        })

    browser_info = {"cdp_url": os.environ.get("CHROME_CDP_URL", "http://127.0.0.1:9333")}
    if not args.no_start_browser:
        browser_info = start_browser_if_needed(args.port)

    env = os.environ.copy()
    env.update({
        "EASYROUTER_API_KEY": api_key,
        "EASYROUTER_BASE_URL": env.get("EASYROUTER_BASE_URL", "https://easyrouter.io/v1"),
        "EASYROUTER_TEXT_MODEL": env.get("EASYROUTER_TEXT_MODEL", "deepseek-v4-pro"),
        "EASYROUTER_FAST_MODEL": env.get("EASYROUTER_FAST_MODEL", "deepseek-v4-flash"),
        "EASYROUTER_PRO_MODEL": env.get("EASYROUTER_PRO_MODEL", "deepseek-v4-pro"),
        "EASYROUTER_BACKUP_MODEL": env.get("EASYROUTER_BACKUP_MODEL", "qwen3.6-plus"),
        "DXM_USERNAME": dxm_username,
        "DXM_PASSWORD": dxm_password,
        "CHROME_CDP_URL": browser_info.get("cdp_url", f"http://127.0.0.1:{args.port}"),
        "CHROME_USER_DATA_DIR": browser_info.get("user_data_dir", str(BROWSER_ROOT / "user_data")),
        "CHROME_PROFILE_DIR": "Profile 13",
        "EXPECTED_PROFILE_PATH": browser_info.get("expected_profile_path", str(BROWSER_ROOT / "user_data" / "Profile 13")),
    })
    print(f"Version: {(APP_ROOT / 'VERSION').read_text(encoding='utf-8').strip()}")
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
    parser.add_argument("--open-excel", action="store_true", default=True, help="Open the latest Excel after the run.")
    parser.add_argument("--no-open-excel", dest="open_excel", action="store_false")
    parser.add_argument("--save-config", action="store_true", help="Save credentials/API key with local Windows encryption.")
    parser.add_argument("--dry-run", action="store_true", help="Validate configuration without starting automation.")
    args = parser.parse_args()
    if args.open_browser_only:
        info = start_browser_if_needed(args.port)
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    return run_automation(args)


if __name__ == "__main__":
    raise SystemExit(main())
