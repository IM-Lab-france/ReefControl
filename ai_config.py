import json
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
AI_CONFIG_PATH = BASE_DIR / "ai_config.json"
LEGACY_OPENAI_KEY_PATH = BASE_DIR / ".openai_api_key"

DEFAULT_AI_CONFIG: Dict[str, Any] = {
    "ai_mode": "cloud",
    "local_ai_base_url": "http://127.0.0.1:1234/v1",
    "local_ai_model": "lmstudio-community/gpt4all",
    "local_ai_api_key": "",
    "cloud_ai_base_url": "https://api.openai.com/v1",
    "cloud_ai_model": "gpt-4o-mini",
    "cloud_ai_api_key": "",
}


def _read_config_file() -> Dict[str, Any]:
    if not AI_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(AI_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_config_file(config: Dict[str, Any]) -> None:
    AI_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    AI_CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def _load_legacy_openai_key() -> Optional[str]:
    if not LEGACY_OPENAI_KEY_PATH.exists():
        return None
    try:
        key = LEGACY_OPENAI_KEY_PATH.read_text(encoding="utf-8").strip()
        return key or None
    except OSError:
        return None


def _merge_with_defaults(raw: Dict[str, Any], *, include_secrets: bool) -> Dict[str, Any]:
    config = dict(DEFAULT_AI_CONFIG)
    for key, value in raw.items():
        if key not in config:
            continue
        if isinstance(value, str):
            config[key] = value.strip()
        else:
            config[key] = value
    legacy_key = _load_legacy_openai_key()
    if not config.get("cloud_ai_api_key") and legacy_key:
        config["cloud_ai_api_key"] = legacy_key
    ai_mode = str(config.get("ai_mode") or "").lower()
    if ai_mode not in {"local", "cloud"}:
        ai_mode = "cloud"
    config["ai_mode"] = ai_mode
    if not include_secrets:
        config["cloud_ai_api_key"] = ""
        config["local_ai_api_key"] = ""
    config["cloud_ai_has_key"] = bool(raw.get("cloud_ai_api_key") or legacy_key)
    config["local_ai_has_key"] = bool(raw.get("local_ai_api_key"))
    return config


def load_ai_config(*, include_secrets: bool = True) -> Dict[str, Any]:
    raw = _read_config_file()
    return _merge_with_defaults(raw, include_secrets=include_secrets)


def save_ai_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Configuration IA invalide.")
    current = _read_config_file()
    merged = dict(current)

    def _normalize_str(value: Any) -> str:
        return str(value or "").strip()

    if "ai_mode" in payload:
        mode = _normalize_str(payload["ai_mode"]).lower()
        if mode not in {"local", "cloud"}:
            raise ValueError("Mode IA invalide (local ou cloud).")
        merged["ai_mode"] = mode

    for key in (
        "local_ai_base_url",
        "local_ai_model",
        "cloud_ai_base_url",
        "cloud_ai_model",
    ):
        if key in payload:
            merged[key] = _normalize_str(payload[key])

    for key in ("local_ai_api_key", "cloud_ai_api_key"):
        if key not in payload:
            continue
        value = payload[key]
        if value is None:
            continue  # conserver la valeur existante
        merged[key] = _normalize_str(value)

    _write_config_file(merged)
    return load_ai_config(include_secrets=False)


def load_ai_config_for_client() -> Dict[str, Any]:
    return load_ai_config(include_secrets=False)
