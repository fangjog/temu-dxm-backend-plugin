from __future__ import annotations

import sys
from typing import Any


class UserChoseSkip(RuntimeError):
    """Raised when the operator asks the pool flow to skip the current browser."""


class UserChoseStop(RuntimeError):
    """Raised when the operator asks the pool flow to stop completely."""


def show_manual_action_popup(title: str, message: str, logger: Any | None = None) -> bool:
    """Log a manual-action message without interrupting this backend batch flow."""
    _log(logger, "windows_prompt_disabled", "skipped", f"{title}: {message}")
    return True


def wait_user_decision(prompt_text: str, logger: Any | None = None, *, default_noninteractive: str = "stop") -> str:
    """Block for an operator decision.

    Returns one of: continue, skip, stop. In non-interactive runs we never
    auto-continue because that could switch browsers or publish after a manual
    verification point without the operator's consent.
    """
    print(prompt_text)
    print("请输入 continue 继续当前浏览器，skip 跳过当前浏览器，stop 停止全部流程。")

    if not sys.stdin.isatty():
        decision = default_noninteractive if default_noninteractive in {"continue", "skip", "stop"} else "stop"
        _log(
            logger,
            "manual_decision",
            "manual_required",
            f"Terminal is non-interactive; returning {decision} instead of auto-continuing.",
        )
        return decision

    while True:
        try:
            value = input("> ").strip().lower()
        except EOFError:
            _log(logger, "manual_decision", "manual_required", "stdin closed; returning stop.")
            return "stop"
        if value in {"continue", "skip", "stop"}:
            _log(logger, "manual_decision", "ok", f"Operator selected: {value}")
            return value
        print("请输入 continue / skip / stop。")


def require_continue_or_raise(prompt_text: str, logger: Any | None = None) -> None:
    decision = wait_user_decision(prompt_text, logger=logger)
    if decision == "skip":
        raise UserChoseSkip(prompt_text)
    if decision == "stop":
        raise UserChoseStop(prompt_text)


def _log(logger: Any | None, step: str, status: str, message: str) -> None:
    if logger:
        logger.log_step(step, status, message)
    else:
        print(f"[{step}] {status}: {message}")
