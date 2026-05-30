from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.browser_manager import BrowserManager, ChromeProfileMismatchError
from src.captcha_guard import check_and_wait_if_captcha
from src.dianxiaomi_pages import open_draft_list, run_edit_one_product
from src.dxm_publish_twice_flow import run_dxm_publish_once
from src.dxm_publish_twice_v2 import run_dxm_field_fill_test, run_dxm_publish_twice
from src.easyrouter_client import EasyRouterClient
from src.full_one_link_flow import run_full_one_link_product
from src.full_one_pool_flow import prepare_browser_by_index, prepare_browser_pool, run_full_one_pool
from src.full_one_flow import run_full_one_product
from src.logger import WorkflowLogger
from src.publish_pages import run_publish_one_product
from src.sku_cleaner import sanitize_sku
from src.state_manager import StateManager
from src.text_ai import optimize_product_title
from src.utils import ensure_dirs, load_config, take_screenshot


PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    load_dotenv(PROJECT_ROOT / ".env.credentials", override=False)
    ensure_dirs()

    parser = argparse.ArgumentParser(description="Temu / 店小秘铺货自动化 MVP")
    parser.add_argument(
        "command",
        choices=[
            "check",
            "edit-one",
            "publish-one",
            "dxm-publish-once",
            "dxm-publish-twice",
            "dxm-field-fill-test",
            "full-one",
            "full-one-link",
            "full-one-pool",
            "full-one-record",
            "prepare-isolated-browser",
            "prepare-browser",
            "prepare-browser-pool",
            "test-ai",
            "diagnose-chrome",
            "list-models",
        ],
    )
    parser.add_argument("--index", type=int, help="Browser pool index for prepare-browser (1-5).")
    parser.add_argument("--edit-url", default="", help="指定店小秘编辑页 URL，用于 dxm-field-fill-test。")
    args = parser.parse_args()

    config = load_config(PROJECT_ROOT / "config.yaml")
    if args.edit_url:
        config.setdefault("dxm_field_fill_test", {})["edit_url"] = args.edit_url
    logger = WorkflowLogger()
    state = StateManager()

    if args.command == "test-ai":
        return command_test_ai(logger)
    if args.command == "list-models":
        return command_list_models(logger)
    if args.command == "diagnose-chrome":
        return command_diagnose_chrome(config, logger, state)
    if args.command == "prepare-browser-pool":
        result = prepare_browser_pool(config, logger=logger, state=state)
        print("\nprepare-browser-pool result:")
        _print_result(result)
        return 0 if result.get("status") in {"ok", "manual_required"} else 1
    if args.command == "prepare-browser":
        if not args.index:
            print("请使用 --index 指定浏览器，例如：python main.py prepare-browser --index 1")
            return 2
        result = prepare_browser_by_index(args.index, config, logger=logger, state=state)
        print("\nprepare-browser result:")
        _print_result(result)
        return 0 if result.get("status") in {"ok", "manual_required"} else 1

    if args.command in {"edit-one", "publish-one", "dxm-publish-once", "dxm-publish-twice", "full-one", "full-one-link", "full-one-pool", "full-one-record"}:
        missing = _missing_ai_config()
        if missing:
            print(f"EasyRouter 配置不完整，无法执行 {args.command}: " + ", ".join(missing))
            return 2
    if args.command == "full-one-pool":
        result = run_full_one_pool(config, logger=logger, state=state)
        print("\nfull-one-pool result:")
        _print_result(result)
        return 0 if result.get("status") == "success" else 1

    manager = BrowserManager(config, logger=logger)
    try:
        _, context, page = manager.start()
        if args.command == "check":
            return command_check(page, context, manager, logger, state)
        if args.command == "prepare-isolated-browser":
            return command_prepare_isolated_browser(page, context, manager, logger, state)
        if args.command == "edit-one":
            result = run_edit_one_product(page, config, logger=logger, state=state)
            print("\n执行结果:")
            _print_result(result)
            return 0 if result.get("status") in {"saved_draft", "manual_required"} else 1
        if args.command == "publish-one":
            result = run_publish_one_product(page, config, logger=logger, state=state)
            print("\n发布执行结果:")
            _print_result(result)
            return 0 if result.get("status") in {"success", "unknown", "manual_required"} else 1
        if args.command == "dxm-publish-once":
            page = _select_existing_dxm_page(context, page)
            result = run_dxm_publish_once(page, config, logger=logger, state=state)
            print("\ndxm-publish-once result:")
            _print_result(result)
            return 0 if result.get("status") == "success" else 1
        if args.command == "dxm-publish-twice":
            page = _select_existing_dxm_page(context, page)
            result = run_dxm_publish_twice(page, config, logger=logger, state=state)
            print("\ndxm-publish-twice result:")
            _print_result(result)
            return 0 if result.get("status") == "success" else 1
        if args.command == "dxm-field-fill-test":
            page = _select_existing_dxm_page(context, page)
            result = run_dxm_field_fill_test(page, config, logger=logger, state=state)
            print("\ndxm-field-fill-test result:")
            _print_result(result)
            return 0 if result.get("status") in {"success", "manual_required"} else 1
        if args.command == "full-one":
            result = run_full_one_product(page, config, logger=logger, state=state)
            print("\nfull-one result:")
            _print_result(result)
            return 0 if result.get("status") == "success" else 1
        if args.command == "full-one-link":
            result = run_full_one_link_product(page, config, logger=logger, state=state)
            print("\nfull-one-link result:")
            _print_result(result)
            return 0 if result.get("status") == "success" else 1
        if args.command == "full-one-record":
            print("full-one-record 已禁用录屏，现在直接按 full-one 执行，不触发 Alt+F9/Alt+F10。")
            result = run_full_one_product(page, config, logger=logger, state=state)
            print("\nfull-one result:")
            _print_result(result)
            return 0 if result.get("status") == "success" else 1
        return 0
    except ChromeProfileMismatchError as exc:
        print("\nFAIL: 当前接管资料错误，不能继续执行店小秘任务。")
        print(f"实际Profile Path: {exc.actual_path or '(未读取到)'}")
        print(f"期望Profile Path: {exc.expected_path or '(未配置)'}")
        print("请运行 scripts/start_chrome_profile13.ps1 或 scripts/start_chrome_auto_profile.ps1")
        state.update(status="chrome_profile_mismatch", actual_profile_path=exc.actual_path, expected_profile_path=exc.expected_path)
        return 3
    except RuntimeError as exc:
        print(f"\n启动/接管 Chrome 失败:\n{exc}")
        state.update(status="chrome_error", error=str(exc))
        return 3
    finally:
        manager.close()


def _close_extra_pages(context, keep_page, logger: WorkflowLogger) -> None:
    closed = 0
    try:
        for candidate in list(context.pages):
            if candidate == keep_page:
                continue
            try:
                candidate.close()
                closed += 1
            except Exception:
                continue
        logger.log_step("recording_prepare", "ok", f"Closed {closed} extra Chrome tab(s) before recording.")
    except Exception as exc:
        logger.log_step("recording_prepare", "warning", f"Could not close extra Chrome tabs before recording: {exc}")


def command_diagnose_chrome(config: dict, logger: WorkflowLogger, state: StateManager) -> int:
    manager = BrowserManager(config, logger=logger)
    version_json: dict[str, object] = {}
    try:
        version_json = manager.fetch_cdp_version()
    except RuntimeError as exc:
        print("\nFAIL: 9222 不可连接。")
        print(exc)
        state.update(status="chrome_cdp_unreachable", error=str(exc))
        return 2

    print("\nCDP /json/version:")
    print(f"- Browser: {version_json.get('Browser', '')}")
    print(f"- User-Agent: {version_json.get('User-Agent', '')}")
    print(f"- webSocketDebuggerUrl: {version_json.get('webSocketDebuggerUrl', '')}")

    try:
        _, context, page = manager.connect_cdp(verify_profile=False)
        version_page = context.new_page()
        try:
            chrome_info = manager.collect_chrome_version_info(version_page)
        finally:
            try:
                version_page.close()
            except Exception:
                pass
        expected_path = manager.expected_profile_path()

        print("\nchrome://version:")
        print(f"- Google Chrome版本: {chrome_info.get('google_chrome_version', '')}")
        print(f"- 命令行: {chrome_info.get('command_line', '')}")
        print(f"- 可执行文件路径: {chrome_info.get('executable_path', '')}")
        print(f"- 个人资料路径/Profile Path: {chrome_info.get('profile_path', '')}")
        print(f"- 期望Profile Path: {expected_path}")

        try:
            manager.verify_profile_path(page, chrome_info)
            profile_ok = True
        except ChromeProfileMismatchError as exc:
            profile_ok = False
            print("\nFAIL: 当前不是期望 Profile，不允许继续。")
            print(f"实际Profile Path: {exc.actual_path or '(未读取到)'}")
            print(f"期望Profile Path: {exc.expected_path or '(未配置)'}")
            print("请运行 scripts/start_chrome_profile13.ps1 或 scripts/start_chrome_auto_profile.ps1")

        extension_info = manager.detect_extension_presence()
        print("\n扩展检查:")
        print(f"- chrome-extension 后台/ServiceWorker 数量: {extension_info.get('count', 0)}")
        print(f"- 自动观察到扩展: {extension_info.get('observed', False)}")
        if extension_info.get("urls"):
            for url in extension_info["urls"]:
                print(f"  - {url}")
        if extension_info.get("manual_hint"):
            print("- 请人工确认右上角扩展中是否存在店小秘助手。")

        status = "OK" if profile_ok else "FAIL"
        print(f"\n诊断结论: {status}")
        if profile_ok:
            print("当前接管的是期望 Profile，可以继续执行 check/edit-one。")
        else:
            print("当前Chrome接管资料错误，不能继续执行店小秘任务。")

        state.update(
            status="chrome_diagnose_ok" if profile_ok else "chrome_profile_mismatch",
            cdp_version=version_json,
            chrome_version_info=chrome_info,
            extension_info=extension_info,
        )
        return 0 if profile_ok else 3
    except RuntimeError as exc:
        print(f"\nFAIL: Chrome 诊断失败:\n{exc}")
        state.update(status="chrome_diagnose_error", error=str(exc), cdp_version=version_json)
        return 3
    finally:
        manager.close()


def command_check(page, context, manager: BrowserManager, logger: WorkflowLogger, state: StateManager) -> int:
    report: dict[str, object] = {
        "chrome_connected": True,
        "expected_profile_path": manager.expected_profile_path(),
        "actual_profile_path": manager.last_version_info.get("profile_path", ""),
        "yunqi_opened": False,
        "dxm_draft_opened": False,
        "captcha_detected_or_handled": False,
        "extension_observed": False,
        "extension_count": 0,
    }

    yunqi_url = "https://www.yunqishuju.com/"
    logger.log_step("check_yunqi", "start", f"打开云启数据页面: {yunqi_url}", page=page)
    page.goto(yunqi_url, wait_until="domcontentloaded")
    report["captcha_detected_or_handled"] = check_and_wait_if_captcha(page, logger=logger)
    report["yunqi_opened"] = True
    logger.log_step("check_yunqi", "ok", "云启数据页面已打开。", page=page)

    open_draft_list(page, logger=logger, state=state)
    report["dxm_draft_opened"] = True

    extension_info = manager.detect_extension_presence()
    report["extension_observed"] = extension_info["observed"]
    report["extension_count"] = extension_info["count"]
    report["extension_urls"] = extension_info["urls"]
    logger.log_step(
        "check_extension",
        "ok" if extension_info["observed"] else "unknown",
        f"检测到 chrome-extension 后台/ServiceWorker 数量: {extension_info['count']}。如果为 0，请人工确认右上角扩展中是否存在店小秘助手。",
        page=page,
        extra={"extension_urls": extension_info["urls"]},
    )

    state.update(last_check_report=report)
    print("\n环境检查报告:")
    for key, value in report.items():
        print(f"- {key}: {value}")
    return 0


def command_prepare_isolated_browser(page, context, manager: BrowserManager, logger: WorkflowLogger, state: StateManager) -> int:
    """Open login/setup pages in the isolated browser without collecting or publishing."""
    expected = manager.expected_profile_path()
    actual = manager.last_version_info.get("profile_path", "")
    report: dict[str, object] = {
        "expected_profile_path": expected,
        "actual_profile_path": actual,
        "opened_pages": [],
        "extension_observed": False,
        "extension_count": 0,
        "continued": False,
        "post_login_check": "not_run",
    }

    urls = [
        ("yunqi", "https://www.yunqishuju.com/"),
        ("temu", "https://www.temu.com/"),
        ("dianxiaomi", "https://www.dianxiaomi.com/"),
        ("dxm_draft", os.getenv("DXM_DRAFT_URL", "https://www.dianxiaomi.com/web/temu/choiceTemuList/draft")),
    ]
    for index, (name, url) in enumerate(urls):
        target = page if index == 0 else context.new_page()
        try:
            logger.log_step("prepare_isolated_browser", "start", f"Opening {name}: {url}", page=target)
            target.goto(url, wait_until="domcontentloaded")
            try:
                target.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            report["opened_pages"].append({"name": name, "url": target.url, "status": "ok"})
            logger.log_step("prepare_isolated_browser", "ok", f"Opened {name}.", page=target)
        except Exception as exc:
            screenshot_path = ""
            try:
                screenshot_path = take_screenshot(target, f"prepare_{name}_error")
            except Exception:
                pass
            report["opened_pages"].append({"name": name, "url": url, "status": "error", "message": str(exc), "screenshot_path": screenshot_path})
            logger.log_step("prepare_isolated_browser", "warning", f"Could not open {name}: {exc}", page=target, screenshot_path=screenshot_path)

    extension_info = manager.detect_extension_presence()
    report["extension_observed"] = extension_info.get("observed", False)
    report["extension_count"] = extension_info.get("count", 0)
    report["extension_urls"] = extension_info.get("urls", [])
    if not extension_info.get("observed", False):
        logger.log_step(
            "prepare_isolated_browser",
            "warning",
            "DXM extension was not observed automatically. Please confirm the Dianxiaomi assistant exists in the isolated browser extensions.",
            extra={"extension_info": extension_info},
        )
    else:
        logger.log_step("prepare_isolated_browser", "ok", "Extension presence check completed.", extra={"extension_info": extension_info})

    print("\n请在打开的隔离浏览器中完成 Google / Temu / 云启数据 / 店小秘 登录，并确认店小秘插件存在。")
    print("完成后回到终端输入 continue。此命令不会采集、认领或发布。")
    if _wait_for_continue():
        report["continued"] = True
        try:
            manager.verify_profile_path(page)
            command_check(page, context, manager, logger, state)
            report["post_login_check"] = "ok"
        except Exception as exc:
            report["post_login_check"] = f"failed: {exc}"
            logger.log_step("prepare_isolated_browser", "warning", f"Post-login check failed: {exc}", page=page)
    else:
        report["post_login_check"] = "skipped_noninteractive"

    state.update(status="prepare_isolated_browser_done", prepare_isolated_browser=report)
    print("\nprepare-isolated-browser report:")
    for key, value in report.items():
        print(f"- {key}: {value}")
    return 0


def _wait_for_continue() -> bool:
    if not sys.stdin.isatty():
        print("当前不是交互式终端，已打开登录页并跳过等待。请在浏览器完成登录后手动运行 python main.py diagnose-chrome 和 python main.py check。")
        return False
    while True:
        try:
            if input("> ").strip().lower() == "continue":
                return True
        except EOFError:
            return False
        print("请输入 continue 继续。")


def _select_existing_dxm_edit_page(context, fallback_page):
    """Pick an already-open DXM edit page without navigating or selecting list rows."""
    try:
        pages = list(context.pages)
    except Exception:
        pages = []
    for candidate in reversed(pages):
        try:
            url = candidate.url.lower()
        except Exception:
            continue
        if "dianxiaomi.com" in url and "edit" in url:
            try:
                candidate.bring_to_front()
            except Exception:
                pass
            return candidate
    return fallback_page


def _select_existing_dxm_page(context, fallback_page):
    """Pick the last already-open Dianxiaomi backend page without navigating."""
    try:
        pages = list(context.pages)
    except Exception:
        pages = []
    for candidate in reversed(pages):
        try:
            url = candidate.url.lower()
        except Exception:
            continue
        if "dianxiaomi.com" in url:
            try:
                candidate.bring_to_front()
            except Exception:
                pass
            return candidate
    return fallback_page


def command_test_ai(logger: WorkflowLogger) -> int:
    missing = _missing_ai_config()
    if missing:
        print("EasyRouter 配置不完整: " + ", ".join(missing))
        return 2

    client = EasyRouterClient(logger=logger)
    runtime_models = EasyRouterClient.runtime_models_from_env()
    pong = client.chat_text(
        [
            {"role": "system", "content": "Return exactly: OK"},
            {"role": "user", "content": "API health check"},
        ],
        max_tokens=20,
        temperature=0,
    )
    title = optimize_product_title("黑色 6pcs Type-C 手机支架 多场景使用")
    sku = sanitize_sku("黑色 6pcs - Type-C", product_id="TEST", index=1)
    print("\nEasyRouter 测试结果:")
    print(f"- base_url: {client.base_url}")
    print(f"- model: {client.model}")
    print(f"- key_masked: {client.key_masked}")
    print(f"- fast_model: {runtime_models['fast']}")
    print(f"- pro_model: {runtime_models['pro']}")
    print(f"- backup_model: {runtime_models['backup']}")
    print(f"- API: {pong}")
    print(f"- 优化标题: {title}")
    print(f"- 清洗 SKU: {sku}")
    return 0


def command_list_models(logger: WorkflowLogger) -> int:
    missing = []
    if not os.getenv("EASYROUTER_API_KEY", "").strip():
        missing.append("EASYROUTER_API_KEY")
    if missing:
        print("EasyRouter 配置不完整: " + ", ".join(missing))
        return 2

    client = EasyRouterClient(logger=logger, model=os.getenv("EASYROUTER_TEXT_MODEL", "model-list-placeholder") or "model-list-placeholder")
    models = client.list_models()
    current_model = os.getenv("EASYROUTER_TEXT_MODEL", "").strip()
    selected_model = current_model if current_model in models else _choose_text_model(models)

    print("\nEasyRouter 模型列表:")
    for model_id in models:
        marker = " (current)" if model_id == current_model else ""
        print(f"- {model_id}{marker}")

    if current_model and current_model in models:
        print(f"\n当前模型可用: {current_model}")
        return 0

    if selected_model:
        print(f"\n当前模型不可用或为空，未自动修改 .env。可用候选模型: {selected_model}")
        return 0

    print("\n未找到可用文本模型，请手动在 .env 设置 EASYROUTER_TEXT_MODEL。")
    return 1


def _choose_text_model(models: list[str]) -> str:
    blocked = ("embed", "embedding", "image", "vision", "tts", "audio", "whisper", "dall", "stable", "sdxl")
    preferred = ("deepseek", "qwen", "gpt", "gemini", "claude", "llama", "mistral", "yi", "moonshot")
    candidates = [model for model in models if not any(token in model.lower() for token in blocked)]
    for token in preferred:
        for model in candidates:
            if token in model.lower():
                return model
    return candidates[0] if candidates else ""


def _update_env_value(name: str, value: str) -> None:
    env_path = PROJECT_ROOT / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replaced = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{name}="):
            new_lines.append(f"{name}={value}")
            replaced = True
        else:
            new_lines.append(line)
    if not replaced:
        new_lines.append(f"{name}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.environ[name] = value


def _missing_ai_config() -> list[str]:
    missing = []
    if not os.getenv("EASYROUTER_API_KEY", "").strip():
        missing.append("EASYROUTER_API_KEY")
    if not os.getenv("EASYROUTER_TEXT_MODEL", "").strip():
        missing.append("EASYROUTER_TEXT_MODEL")
    return missing


def _print_result(result: object) -> None:
    try:
        print(json.dumps(result, ensure_ascii=True, default=str, indent=2))
    except Exception:
        print(str(result).encode("ascii", errors="backslashreplace").decode("ascii"))


if __name__ == "__main__":
    sys.exit(main())
