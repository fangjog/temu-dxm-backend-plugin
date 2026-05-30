from __future__ import annotations

import json
import os
import time
from typing import Any

from openai import OpenAI


class EasyRouterClient:
    MODEL_ENV_KEYS = {
        "text": "EASYROUTER_TEXT_MODEL",
        "default": "EASYROUTER_TEXT_MODEL",
        "fast": "EASYROUTER_FAST_MODEL",
        "pro": "EASYROUTER_PRO_MODEL",
        "backup": "EASYROUTER_BACKUP_MODEL",
    }

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        model_tier: str = "text",
        temperature: float = 0.3,
        max_tokens: int = 800,
        logger: Any | None = None,
    ):
        self.api_key = (api_key or os.getenv("EASYROUTER_API_KEY", "")).strip()
        self.base_url = (base_url or os.getenv("EASYROUTER_BASE_URL", "https://easyrouter.io/v1")).strip()
        self.model = (model or self.model_from_env(model_tier)).strip()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.logger = logger

        if not self.api_key:
            raise RuntimeError("EASYROUTER_API_KEY 为空，请在 .env 中配置后再运行。")
        if not self.model:
            raise RuntimeError("EASYROUTER_TEXT_MODEL 为空，请在 .env 中配置模型名后再运行。")

        self.client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self._log("easyrouter_init", "ok", f"EasyRouter 初始化完成，base_url={self.base_url}, model={self.model}, key={self._masked_key()}")

    @classmethod
    def model_from_env(cls, tier: str = "text") -> str:
        key = cls.MODEL_ENV_KEYS.get((tier or "text").lower(), "EASYROUTER_TEXT_MODEL")
        fallback = os.getenv("EASYROUTER_TEXT_MODEL", "").strip()
        return (os.getenv(key, "") or fallback).strip()

    @classmethod
    def runtime_models_from_env(cls) -> dict[str, str]:
        text = cls.model_from_env("text")
        return {
            "text": text,
            "fast": cls.model_from_env("fast") or text,
            "pro": cls.model_from_env("pro") or text,
            "backup": cls.model_from_env("backup") or text,
        }

    @staticmethod
    def mask_key(api_key: str) -> str:
        api_key = str(api_key or "")
        if len(api_key) <= 8:
            return "***"
        return f"{api_key[:4]}...{api_key[-4:]}"

    @property
    def key_masked(self) -> str:
        return self._masked_key()

    def chat_text(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> str:
        response = self._create_completion(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            retries=retries,
        )
        content = response.choices[0].message.content or ""
        return content.strip()

    def chat_json(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> dict[str, Any]:
        response = self._create_completion(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            retries=retries,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            cleaned = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(cleaned)

    def list_models(self) -> list[str]:
        try:
            models = self.client.models.list()
        except Exception as exc:
            self._log("easyrouter_models", "error", f"EasyRouter 模型列表获取失败: {exc}")
            raise RuntimeError(f"EasyRouter 模型列表获取失败: {exc}") from exc

        ids: list[str] = []
        for model in getattr(models, "data", []) or []:
            model_id = getattr(model, "id", "")
            if model_id:
                ids.append(str(model_id))
        ids = sorted(set(ids))
        self._log("easyrouter_models", "ok", f"获取到 {len(ids)} 个模型。")
        return ids

    def _create_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None,
        max_tokens: int | None,
        retries: int,
        response_format: dict[str, str] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": self.temperature if temperature is None else temperature,
                    "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
                }
                if response_format:
                    kwargs["response_format"] = response_format
                return self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                last_error = exc
                self._log("easyrouter_call", "retry" if attempt < retries else "error", f"EasyRouter 调用失败，第 {attempt + 1} 次: {exc}")
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"EasyRouter 调用失败，已重试 {retries} 次: {last_error}") from last_error

    def _masked_key(self) -> str:
        return self.mask_key(self.api_key)

    def _log(self, step: str, status: str, message: str) -> None:
        if self.logger:
            self.logger.log_step(step, status, message)
        else:
            print(f"[{step}] {status}: {message}")
