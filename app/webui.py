from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import updater
from secure_config import load_settings, mask_secret, save_settings


APP_ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765


def _version() -> str:
    path = APP_ROOT / "VERSION"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "0.0.0"


def _latest_excel() -> Path | None:
    reports = APP_ROOT / "data" / "reports"
    files = sorted(reports.glob("dxm_publish_twice_result_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _page(message: str = "", log: str = "") -> str:
    settings = load_settings()
    count = int(settings.get("edit_count") or 4)
    api_mask = mask_secret(str(settings.get("api_key") or ""))
    user_mask = mask_secret(str(settings.get("dxm_username") or ""))
    latest = _latest_excel()
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Temu 店小秘后台自动化</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 32px; background: #f6f7f9; color: #1f2933; }}
    main {{ max-width: 920px; margin: auto; background: #fff; padding: 24px; border: 1px solid #d9dee7; border-radius: 8px; }}
    label {{ display:block; margin: 14px 0 6px; font-weight: 600; }}
    input {{ width: 100%; max-width: 520px; height: 34px; padding: 4px 8px; }}
    button {{ margin-top: 16px; margin-right: 8px; height: 34px; padding: 0 14px; }}
    pre {{ background: #111827; color: #e5e7eb; padding: 14px; white-space: pre-wrap; max-height: 360px; overflow: auto; }}
    .muted {{ color: #697386; }}
  </style>
</head>
<body><main>
  <h1>Temu 店小秘后台自动化</h1>
  <p>版本号：<strong>{html.escape(_version())}</strong></p>
  <p class="muted">已保存 API Key：{html.escape(api_mask or '未保存')}；已保存店小秘账号：{html.escape(user_mask or '未保存')}</p>
  <form method="post" action="/run">
    <label>店小秘账号</label>
    <input name="dxm_username" autocomplete="username" placeholder="可留空使用已保存值">
    <label>店小秘密码</label>
    <input name="dxm_password" type="password" autocomplete="current-password" placeholder="可留空使用已保存值">
    <label>编辑数量</label>
    <input name="count" type="number" min="1" value="{count}">
    <label>EasyRouter API Key</label>
    <input name="api_key" type="password" placeholder="可留空使用已保存值">
    <br>
    <button type="submit">开始运行</button>
    <button type="submit" formaction="/save">保存配置</button>
    <button type="submit" formaction="/check_update">检查更新</button>
    <button type="submit" formaction="/apply_update">立即更新</button>
    <button type="submit" formaction="/rollback">回退上一版本</button>
    <button type="submit" formaction="/open_excel">打开最新 Excel</button>
  </form>
  <p>{html.escape(message)}</p>
  <p class="muted">最新 Excel：{html.escape(str(latest) if latest else '暂无')}</p>
  <h2>日志</h2>
  <pre>{html.escape(log)}</pre>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._send(_page())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        values = {key: (items[0] if items else "") for key, items in form.items()}
        settings = load_settings()
        count = int(values.get("count") or settings.get("edit_count") or 4)
        api_key = values.get("api_key") or settings.get("api_key") or ""
        dxm_username = values.get("dxm_username") or settings.get("dxm_username") or ""
        dxm_password = values.get("dxm_password") or settings.get("dxm_password") or ""
        if self.path == "/save":
            save_settings({"edit_count": count, "api_key": api_key, "dxm_username": dxm_username, "dxm_password": dxm_password, "version": _version()})
            self._send(_page("配置已保存，本地使用 Windows DPAPI 加密敏感字段。"))
            return
        if self.path == "/open_excel":
            latest = _latest_excel()
            if latest:
                os.startfile(latest)  # type: ignore[attr-defined]
                self._send(_page(f"已打开最新 Excel：{latest}"))
            else:
                self._send(_page("暂无 Excel 报告。"))
            return
        if self.path == "/check_update":
            try:
                result = updater.check_update()
                self._send(_page("检查更新完成。", json.dumps(result, ensure_ascii=False, indent=2)))
            except Exception as exc:
                self._send(_page(f"检查更新失败：{exc}"))
            return
        if self.path == "/apply_update":
            try:
                latest = updater.check_update()
                result = updater.apply_update(latest)
                self._send(_page(str(result.get("message") or result.get("status")), json.dumps(result, ensure_ascii=False, indent=2)))
            except Exception as exc:
                self._send(_page(f"更新失败：{exc}"))
            return
        if self.path == "/rollback":
            try:
                result = updater.rollback_latest()
                self._send(_page(str(result.get("message") or result.get("status")), json.dumps(result, ensure_ascii=False, indent=2)))
            except Exception as exc:
                self._send(_page(f"回退失败：{exc}"))
            return
        if not api_key:
            self._send(_page("请先输入 EasyRouter API Key。"))
            return
        save_settings({"edit_count": count, "api_key": api_key, "dxm_username": dxm_username, "dxm_password": dxm_password, "version": _version()})
        env = os.environ.copy()
        env["PLUGIN_EASYROUTER_API_KEY"] = api_key
        env["PLUGIN_DXM_USERNAME"] = dxm_username
        env["PLUGIN_DXM_PASSWORD"] = dxm_password
        cmd = [sys.executable, "plugin_main.py", "--count", str(count), "--save-config"]
        proc = subprocess.run(cmd, cwd=APP_ROOT, env=env, text=True, capture_output=True)
        log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
        self._send(_page(f"运行完成，退出码 {proc.returncode}。", log))


def main() -> int:
    parser = argparse.ArgumentParser(description="Temu DXM backend plugin local web UI")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()
    print(f"Open http://{args.host}:{args.port} in your browser.")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
