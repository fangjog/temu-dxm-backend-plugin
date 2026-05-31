from __future__ import annotations

import base64
import ctypes
import json
from ctypes import wintypes
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
PRIVATE_DIR = APP_ROOT / "data" / "private"
CONFIG_PATH = PRIVATE_DIR / "settings.secure.json"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _to_blob(data: bytes) -> DATA_BLOB:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))


def _from_blob(blob: DATA_BLOB) -> bytes:
    if not blob.pbData:
        return b""
    data = ctypes.string_at(blob.pbData, blob.cbData)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return data


def _protect_dpapi(value: str) -> str:
    raw = value.encode("utf-8")
    in_blob = _to_blob(raw)
    out_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    return base64.b64encode(_from_blob(out_blob)).decode("ascii")


def _unprotect_dpapi(value: str) -> str:
    raw = base64.b64decode(value.encode("ascii"))
    in_blob = _to_blob(raw)
    out_blob = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed")
    return _from_blob(out_blob).decode("utf-8")


def mask_secret(value: str) -> str:
    value = value or ""
    if len(value) <= 8:
        return "****" if value else ""
    return f"{value[:4]}****{value[-4:]}"


def load_settings() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    settings = dict(payload.get("plain", {}))
    protected = payload.get("protected", {})
    for key, item in protected.items():
        if not isinstance(item, dict):
            continue
        try:
            if item.get("method") == "dpapi":
                settings[key] = _unprotect_dpapi(str(item.get("value") or ""))
        except Exception:
            settings[key] = ""
    return settings


def save_settings(settings: dict) -> None:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    plain = {
        "edit_count": int(settings.get("edit_count") or 4),
        "version": str(settings.get("version") or ""),
        "api_key_masked": mask_secret(str(settings.get("api_key") or "")),
        "dxm_username_masked": mask_secret(str(settings.get("dxm_username") or "")),
        "api_test_ok": bool(settings.get("api_test_ok") or False),
        "api_test_at": str(settings.get("api_test_at") or ""),
        "api_test_message": str(settings.get("api_test_message") or "")[:500],
    }
    protected = {}
    for key in ("api_key", "dxm_username", "dxm_password"):
        value = str(settings.get(key) or "")
        if not value:
            continue
        protected[key] = {"method": "dpapi", "value": _protect_dpapi(value)}
    CONFIG_PATH.write_text(
        json.dumps({"plain": plain, "protected": protected}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
