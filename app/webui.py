from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import requests

import updater
from secure_config import load_settings, mask_secret, save_settings


APP_ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 8765
DEFAULT_BASE_URL = "https://easyrouter.io/v1"
DEFAULT_MODEL = "deepseek-v4-pro"


def _version() -> str:
    path = APP_ROOT / "VERSION"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "0.0.0"


def _latest_excel() -> Path | None:
    reports = APP_ROOT / "data" / "reports"
    files = sorted(reports.glob("dxm_publish_twice_result_*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _merged_form(values: dict[str, str]) -> dict[str, str]:
    settings = load_settings()
    return {
        "count": str(values.get("count") or settings.get("edit_count") or 4),
        "api_key": values.get("api_key") or str(settings.get("api_key") or ""),
        "dxm_username": values.get("dxm_username") or str(settings.get("dxm_username") or ""),
        "dxm_password": values.get("dxm_password") or str(settings.get("dxm_password") or ""),
    }


def _save_from_form(values: dict[str, str], *, api_test: dict | None = None) -> None:
    merged = _merged_form(values)
    payload = {
        "edit_count": int(merged["count"] or 4),
        "api_key": merged["api_key"],
        "dxm_username": merged["dxm_username"],
        "dxm_password": merged["dxm_password"],
        "version": _version(),
    }
    if api_test is not None:
        payload.update(
            {
                "api_test_ok": bool(api_test.get("ok")),
                "api_test_at": dt.datetime.now().isoformat(timespec="seconds"),
                "api_test_message": str(api_test.get("message") or "")[:500],
            }
        )
    else:
        settings = load_settings()
        payload.update(
            {
                "api_test_ok": bool(settings.get("api_test_ok") or False),
                "api_test_at": str(settings.get("api_test_at") or ""),
                "api_test_message": str(settings.get("api_test_message") or ""),
            }
        )
    save_settings(payload)


def _test_easyrouter(api_key: str) -> dict:
    api_key = str(api_key or "").strip()
    if not api_key:
        return {"ok": False, "category": "missing_key", "message": "请先输入 EasyRouter API Key。"}
    base_url = os.environ.get("EASYROUTER_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
    model = os.environ.get("EASYROUTER_PRO_MODEL") or os.environ.get("EASYROUTER_TEXT_MODEL") or DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "你好，只回复 OK"}],
        "temperature": 0,
        "max_tokens": 16,
    }
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=35,
        )
    except requests.Timeout:
        return {"ok": False, "category": "network_timeout", "message": "网络超时，请检查网络或 EasyRouter 服务。", "model": model}
    except requests.RequestException as exc:
        return {"ok": False, "category": "network_error", "message": f"网络失败：{exc}", "model": model}

    text = response.text[:1000]
    if response.status_code in {401, 403}:
        return {"ok": False, "category": "invalid_key", "message": "Key 无效或无权限。", "status_code": response.status_code, "model": model}
    if response.status_code in {402, 429}:
        return {"ok": False, "category": "quota_or_rate_limit", "message": "额度不足或请求频率受限。", "status_code": response.status_code, "model": model}
    if response.status_code in {404, 400} and "model" in text.lower():
        return {"ok": False, "category": "model_unavailable", "message": f"模型不可用：{model}", "status_code": response.status_code, "model": model}
    if response.status_code >= 400:
        return {"ok": False, "category": "api_error", "message": f"API 返回错误 {response.status_code}: {text[:300]}", "status_code": response.status_code, "model": model}

    try:
        data = response.json()
        content = str(data.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    except Exception:
        content = text.strip()
    if "OK" in content.upper():
        return {"ok": True, "category": "ok", "message": f"API 连接成功：{content}", "model": model}
    return {"ok": True, "category": "ok_nonstandard", "message": f"API 已返回，但内容不是标准 OK：{content[:120]}", "model": model}


def _run_plugin(args: list[str], values: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    if values:
        merged = _merged_form(values)
        env["PLUGIN_EASYROUTER_API_KEY"] = merged["api_key"]
        env["PLUGIN_DXM_USERNAME"] = merged["dxm_username"]
        env["PLUGIN_DXM_PASSWORD"] = merged["dxm_password"]
    return subprocess.run([sys.executable, "plugin_main.py", *args], cwd=APP_ROOT, env=env, text=True, capture_output=True)


def _page(message: str = "", log: str = "") -> str:
    settings = load_settings()
    count = int(settings.get("edit_count") or 4)
    api_mask = mask_secret(str(settings.get("api_key") or ""))
    user_mask = mask_secret(str(settings.get("dxm_username") or ""))
    api_test_ok = bool(settings.get("api_test_ok") or False)
    api_test_message = str(settings.get("api_test_message") or "")
    latest = _latest_excel()
    safe_message = html.escape(message)
    safe_log = html.escape(log)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Temu 店小秘后台自动化</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif; margin: 32px; background: #f6f7f9; color: #1f2933; }}
    main {{ max-width: 960px; margin: auto; background: #fff; padding: 24px; border: 1px solid #d9dee7; border-radius: 8px; }}
    label {{ display:block; margin: 14px 0 6px; font-weight: 600; }}
    input {{ width: 100%; max-width: 560px; height: 34px; padding: 4px 8px; box-sizing: border-box; }}
    button {{ margin-top: 14px; margin-right: 8px; min-height: 34px; padding: 0 14px; cursor: pointer; }}
    pre {{ background: #111827; color: #e5e7eb; padding: 14px; white-space: pre-wrap; max-height: 360px; overflow: auto; border-radius: 6px; }}
    .muted {{ color: #697386; }}
    .notice {{ background:#eff6ff; border:1px solid #bfdbfe; padding:10px 12px; border-radius:6px; margin:12px 0; }}
    .ok {{ color:#047857; font-weight:700; }}
    .bad {{ color:#b91c1c; font-weight:700; }}
  </style>
</head>
<body><main>
  <h1>Temu 店小秘后台自动化</h1>
  <p>当前版本：<strong>{html.escape(_version())}</strong></p>
  <p class="muted">已保存 API Key：{html.escape(api_mask or "未保存")}；已保存店小秘账号：{html.escape(user_mask or "未保存")}</p>
  <p class="{'ok' if api_test_ok else 'bad'}">API 测试状态：{html.escape(api_test_message or ("已通过" if api_test_ok else "未通过"))}</p>
  <div class="notice">请先点击“打开店小秘登录页”，在打开的店小秘浏览器中完成登录。登录后回到本页面，点击“测试连接”，通过后再点击“开始运行”。</div>
  <form method="post" action="/run">
    <label>店小秘账号</label>
    <input name="dxm_username" autocomplete="username" placeholder="可留空使用已保存账号">
    <label>店小秘密码</label>
    <input name="dxm_password" type="password" autocomplete="current-password" placeholder="可留空使用已保存密码">
    <label>编辑数量</label>
    <input name="count" type="number" min="1" value="{count}">
    <label>EasyRouter API Key</label>
    <input name="api_key" type="password" placeholder="可留空使用已保存 Key">
    <br>
    <button type="submit" formaction="/save">保存配置</button>
    <button type="submit" formaction="/open_dxm_login">打开店小秘登录页</button>
    <button type="submit" formaction="/test_api">测试连接</button>
    <button type="submit">开始运行</button>
    <button type="submit" formaction="/check_update">检查更新</button>
    <button type="submit" formaction="/apply_update">立即更新</button>
    <button type="submit" formaction="/rollback">回退上一版本</button>
    <button type="submit" formaction="/open_excel">打开最新 Excel</button>
  </form>
  <p>{safe_message}</p>
  <p class="muted">最新 Excel：{html.escape(str(latest) if latest else "暂无")}</p>
  <h2>运行日志</h2>
  <pre>{safe_log}</pre>
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
        merged = _merged_form(values)

        if self.path == "/save":
            _save_from_form(values)
            self._send(_page("配置已保存。敏感字段使用本机 Windows DPAPI 加密保存。"))
            return

        if self.path == "/open_dxm_login":
            _save_from_form(values)
            proc = _run_plugin(["--open-browser-only"], values)
            log = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
            self._send(_page("已打开店小秘已采集产品页。请在浏览器中完成登录，登录后回到本页面点击开始运行。", log))
            return

        if self.path == "/test_api":
            api_result = _test_easyrouter(merged["api_key"])
            _save_from_form(values, api_test=api_result)
            self._send(_page(api_result["message"], json.dumps({k: v for k, v in api_result.items() if k != "api_key"}, ensure_ascii=False, indent=2)))
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

        if self.path not in {"/run", "/run_saved"}:
            self._send(_page("未知操作。"))
            return

        if self.path == "/run_saved":
            values = {
                "count": str(load_settings().get("edit_count") or 4),
                "api_key": str(load_settings().get("api_key") or ""),
                "dxm_username": str(load_settings().get("dxm_username") or ""),
                "dxm_password": str(load_settings().get("dxm_password") or ""),
            }
            merged = _merged_form(values)

        if not merged["api_key"]:
            self._send(_page("请先输入 EasyRouter API Key。"))
            return

        api_result = _test_easyrouter(merged["api_key"])
        _save_from_form(values, api_test=api_result)
        if not api_result.get("ok"):
            self._send(_page(f"API 测试失败，未开始运行：{api_result['message']}", json.dumps(api_result, ensure_ascii=False, indent=2)))
            return

        count = int(merged["count"] or 4)
        cmd = ["--count", str(count), "--save-config"]
        proc = _run_plugin(cmd, values)
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
