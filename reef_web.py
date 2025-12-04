import atexit
import base64
import json
import math
import logging
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set
from uuid import uuid4
from logging.handlers import RotatingFileHandler

import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
    url_for,
)
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from ai_config import load_ai_config_for_client, save_ai_config
from analysis import (
    OPENAI_KEY_MISSING_ERROR as ANALYSIS_KEY_MISSING_ERROR,
    ask_aquarium_ai,
    build_ai_summary_payload,
    build_summary,
    call_llm,
    load_analysis_queries,
    save_analysis_queries,
)
from controller import controller, list_serial_ports
from camera_manager import (
    CAMERA_CONFIG_PATH,
    PHOTO_EXTENSIONS,
    CameraUnavailable,
    camera_manager,
)





BASE_DIR = Path(__file__).resolve().parent
LOGBOOK_PATH = BASE_DIR / "logbook_entries.json"
LOGBOOK_ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png"}
MAX_LOGBOOK_PHOTOS = 8
LIVESTOCK_CATALOG_PATH = BASE_DIR / "livestock_catalog.json"
LIVESTOCK_VALID_CATEGORIES = {"animal": "Animal", "plant": "Vegetal"}
LIVESTOCK_WATER_FIELDS = ("ph", "kh", "gh", "temperature")
LIVESTOCK_POPULATION_MEASUREMENT = "livestock_population"
PHOTO_LABELS_PATH = BASE_DIR / "photo_labels.json"
DEFAULT_PHOTO_CATEGORIES = ["Plante", "Produit", "Poisson"]
AI_INSIGHTS_PATH = BASE_DIR / "ai_insights.json"
MAX_AI_INSIGHTS = 100
AI_IMAGE_SELECTION_LIMIT = 5
AI_MAX_IMAGE_BYTES = 4 * 1024 * 1024
AI_WORKER_SCRIPT = BASE_DIR / "llm" / "ai_worker_local.py"
AI_WORKER_LOG = BASE_DIR / "ai_worker.log"
AI_LOG_DIR = BASE_DIR / "logs"
AI_COMFORT_LOG = AI_LOG_DIR / "ai_comfort.log"
WATER_METRICS_PATH = BASE_DIR / "last_water_metrics.json"
WATER_TARGET_METRICS = {
    "ph": {
        "label": "pH",
        "unit": "",
        "decimals": 1,
        "default_min": 1.0,
        "default_max": 14.0,
        "scale_min": 1.0,
        "scale_max": 14.0,
    },
    "temperature": {
        "label": "Temperature",
        "unit": "C",
        "unit_prefix": "deg",
        "decimals": 1,
        "default_min": 0.0,
        "default_max": 35.0,
        "scale_min": 0.0,
        "scale_max": 35.0,
    },
    "gh": {
        "label": "GH",
        "unit": "dH",
        "unit_prefix": "deg",
        "decimals": 1,
        "default_min": 0.0,
        "default_max": 30.0,
        "scale_min": 0.0,
        "scale_max": 30.0,
    },
}

app = Flask(__name__)

ESP32_CONFIG_KEY = "esp32_cam_url"
ESP32_SETTINGS_TIMEOUT = 5
ESP32_CAPTURE_TIMEOUT = 10


def _load_camera_config_file() -> Dict[str, Any]:
    if not CAMERA_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CAMERA_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        app.logger.warning("camera_config.json invalide, reinitialisation temporaire.")
        return {}


def _save_camera_config_file(data: Dict[str, Any]) -> None:
    CAMERA_CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _get_esp32_cam_url() -> str:
    config = _load_camera_config_file()
    return str(config.get(ESP32_CONFIG_KEY) or "").strip()


def _set_esp32_cam_url(url: str) -> str:
    config = _load_camera_config_file()
    config[ESP32_CONFIG_KEY] = url
    _save_camera_config_file(config)
    return url


def _require_esp32_url() -> str:
    url = _get_esp32_cam_url()
    if not url:
        raise RuntimeError("ESP32_URL_NOT_SET")
    return url


def _build_esp32_url(path: str) -> str:
    base = _require_esp32_url().rstrip("/")
    segment = path.lstrip("/")
    return f"{base}/{segment}"


def _esp32_error(message: str, status: int = 502, code: str = "ESP32_UNREACHABLE"):
    return jsonify({"ok": False, "error": message, "error_code": code}), status


def _load_logbook_entries() -> List[Dict[str, object]]:
    if not LOGBOOK_PATH.exists():
        return []
    try:
        data = json.loads(LOGBOOK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        app.logger.warning("Fichier journal corrompu, reinitialisation.")
        return []
    if not isinstance(data, list):
        return []
    cleaned: List[Dict[str, object]] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cleaned.append(entry)
    return cleaned


def _save_logbook_entries(entries: List[Dict[str, object]]) -> None:
    LOGBOOK_PATH.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def _store_logbook_photo(file_obj: FileStorage) -> str:
    filename = file_obj.filename or ""
    ext = Path(filename).suffix.lower()
    if ext not in LOGBOOK_ALLOWED_EXTENSIONS:
        raise ValueError("Format d'image non supporte.")
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug = (secure_filename(Path(filename).stem) or "photo")[:20]
    unique_id = uuid4().hex[:6]
    final_name = f"journal-{timestamp}-{slug}-{unique_id}{ext}"
    target = camera_manager.save_directory / final_name
    target.parent.mkdir(parents=True, exist_ok=True)
    file_obj.save(target)
    return final_name


def _serialize_log_entry(entry: Dict[str, object]) -> Dict[str, object]:
    photos = []
    for name in entry.get("photos") or []:
        if not isinstance(name, str):
            continue
        photos.append(
            {
                "filename": name,
                "url": url_for("camera_media", filename=name),
                "thumbnail_url": url_for("camera_media", filename=name),
            }
        )
    return {
        "id": entry.get("id"),
        "text": entry.get("text") or "",
        "created_at": entry.get("created_at"),
        "photos": photos,
    }


def _load_livestock_entries() -> List[Dict[str, Any]]:
    if not LIVESTOCK_CATALOG_PATH.exists():
        return []
    try:
        raw = json.loads(LIVESTOCK_CATALOG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        app.logger.warning("Catalogue vivant corrompu, reinitialisation.")
        return []
    entries: List[Dict[str, Any]]
    if isinstance(raw, dict):
        entries = raw.get("entries") or []
    else:
        entries = raw
    cleaned: List[Dict[str, Any]] = []
    if not isinstance(entries, list):
        return cleaned
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("category") or "").lower()
        if category not in LIVESTOCK_VALID_CATEGORIES:
            continue
        cleaned.append(entry)
    return cleaned


def _save_livestock_entries(entries: List[Dict[str, Any]]) -> None:
    payload: Dict[str, Any] = {"entries": entries}
    LIVESTOCK_CATALOG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _serialize_livestock_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    photo_name = entry.get("photo")
    photo = None
    if isinstance(photo_name, str) and photo_name:
        photo = {
            "filename": photo_name,
            "url": url_for("camera_media", filename=photo_name),
            "thumbnail_url": url_for("camera_media", filename=photo_name),
        }
    return {
        "id": entry.get("id"),
        "category": entry.get("category"),
        "name": entry.get("name") or "",
        "introduced_at": entry.get("introduced_at") or "",
        "removed_at": entry.get("removed_at") or "",
        "count": entry.get("count") or 0,
        "photo": photo,
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "ph_min": _serialize_float_field(entry.get("ph_min")),
        "ph_max": _serialize_float_field(entry.get("ph_max")),
        "kh_min": _serialize_float_field(entry.get("kh_min")),
        "kh_max": _serialize_float_field(entry.get("kh_max")),
        "gh_min": _serialize_float_field(entry.get("gh_min")),
        "gh_max": _serialize_float_field(entry.get("gh_max")),
        "temperature_min": _serialize_float_field(entry.get("temperature_min")),
        "temperature_max": _serialize_float_field(entry.get("temperature_max")),
        "resistance": (entry.get("resistance") or "").strip(),
    }


def _parse_livestock_count(raw_value: Any) -> int:
    try:
        value = int(str(raw_value).strip())
    except (AttributeError, ValueError, TypeError):
        value = 0
    return max(0, value)


def _parse_livestock_float(raw_value: Any) -> Optional[float]:
    if raw_value is None:
        return None
    try:
        if isinstance(raw_value, (int, float)):
            value = float(raw_value)
        else:
            text = str(raw_value).strip()
            if not text:
                return None
            text = text.replace(",", ".")
            value = float(text)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _serialize_float_field(value: Any) -> Optional[float]:
    try:
        if isinstance(value, (int, float)):
            num = float(value)
        else:
            num = float(str(value))
    except (ValueError, TypeError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _apply_livestock_water_params(entry: Dict[str, Any], form: Dict[str, Any]) -> None:
    if entry.get("category") != "animal":
        return
    for field in LIVESTOCK_WATER_FIELDS:
        entry[f"{field}_min"] = _parse_livestock_float(form.get(f"{field}_min"))
        entry[f"{field}_max"] = _parse_livestock_float(form.get(f"{field}_max"))
    entry["resistance"] = (form.get("resistance") or "").strip()


def _coerce_entry_animal_count(entry: Optional[Dict[str, Any]]) -> int:
    if not entry:
        return 0
    try:
        return _parse_livestock_count(entry.get("count"))
    except Exception:
        return 0


def _build_population_fields(action: str, entry: Optional[Dict[str, Any]]) -> Dict[str, int]:
    if action == "delete":
        return {
            "active_count": 0,
            "archived_count": 0,
            "total_count": 0,
            "entry_count": 0,
        }
    if not entry or entry.get("category") != "animal":
        return {
            "active_count": 0,
            "archived_count": 0,
            "total_count": 0,
            "entry_count": 0,
        }
    count = _coerce_entry_animal_count(entry)
    # For deletions, consider the fishes archived even if removed_at was unset.
    is_archived = bool(entry.get("removed_at")) or action == "delete"
    active_count = 0 if is_archived else count
    archived_count = count if is_archived else 0
    return {
        "active_count": active_count,
        "archived_count": archived_count,
        "total_count": count,
        "entry_count": count,
    }


def _publish_animal_population(action: str, entry: Optional[Dict[str, Any]] = None) -> None:
    telemetry = getattr(controller, "telemetry", None)
    if not telemetry:
        return
    entry_name = ""
    if entry:
        try:
            entry_name = str(entry.get("name") or "").strip()
        except Exception:
            entry_name = ""
    try:
        telemetry.emit(
            measurement=LIVESTOCK_POPULATION_MEASUREMENT,
            tags={
                "event": action,
                "category": "animal",
                "entry_id": (entry or {}).get("id"),
                "entry_name": entry_name or "inconnu",
            },
            fields=_build_population_fields(action, entry),
        )
    except Exception as exc:
        app.logger.error("Impossible d'envoyer la population InfluxDB: %s", exc)


def _find_livestock_entry(
    entries: List[Dict[str, Any]], entry_id: str
) -> Optional[Dict[str, Any]]:
    for entry in entries:
        if entry.get("id") == entry_id:
            return entry
    return None


def _normalize_photo_categories(candidates: Any) -> List[str]:
    merged: List[str] = list(DEFAULT_PHOTO_CATEGORIES)
    if isinstance(candidates, list):
        merged.extend(str(item) for item in candidates)
    seen: Set[str] = set()
    normalized: List[str] = []
    for name in merged:
        clean = str(name or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        normalized.append(clean)
        seen.add(key)
    return normalized


def _normalize_photo_labels(raw_labels: Any, categories: List[str]) -> Dict[str, List[str]]:
    if not isinstance(raw_labels, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    lookup = {cat.lower(): cat for cat in categories}
    for filename, labels in raw_labels.items():
        if not isinstance(filename, str):
            continue
        clean_name = filename.strip()
        if not clean_name:
            continue
        if not isinstance(labels, list):
            continue
        cleaned: List[str] = []
        seen: Set[str] = set()
        for label in labels:
            if not isinstance(label, str):
                continue
            clean_label = label.strip()
            if not clean_label:
                continue
            key = clean_label.lower()
            canonical = lookup.get(key)
            if not canonical or key in seen:
                continue
            cleaned.append(canonical)
            seen.add(key)
        if cleaned:
            normalized[clean_name] = cleaned
    return normalized


def _load_photo_label_data() -> Dict[str, Any]:
    payload = {"categories": list(DEFAULT_PHOTO_CATEGORIES), "labels": {}}
    if not PHOTO_LABELS_PATH.exists():
        return payload
    try:
        data = json.loads(PHOTO_LABELS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        app.logger.warning("photo_labels.json invalide, retour aux valeurs par defaut.")
        return payload
    if not isinstance(data, dict):
        return payload
    categories = _normalize_photo_categories(data.get("categories"))
    labels = _normalize_photo_labels(data.get("labels"), categories)
    return {"categories": categories, "labels": labels}


def _save_photo_label_data(data: Dict[str, Any]) -> None:
    safe = {
        "categories": list(data.get("categories", [])),
        "labels": data.get("labels", {}),
    }
    PHOTO_LABELS_PATH.write_text(json.dumps(safe, indent=2), encoding="utf-8")


def _ensure_photo_media_file(filename: str) -> str:
    clean = str(filename or "").replace("\\", "/").strip()
    if not clean:
        raise ValueError("Nom de fichier requis.")
    candidate = (camera_manager.save_directory / clean).resolve()
    base = camera_manager.save_directory.resolve()
    if not str(candidate).startswith(str(base)):
        raise ValueError("Chemin de fichier invalide.")
    if candidate.suffix.lower() not in PHOTO_EXTENSIONS:
        raise ValueError("Seules les photos peuvent être etiquetees.")
    if not candidate.exists():
        raise FileNotFoundError(f"Fichier introuvable: {clean}")
    return clean


def _remove_photo_labels_for_files(filenames: Iterable[str]) -> None:
    data = _load_photo_label_data()
    removed = False
    for name in filenames:
        if name in data["labels"]:
            data["labels"].pop(name, None)
            removed = True
    if removed:
        _save_photo_label_data(data)


def _delete_livestock_photo_file(filename: Optional[str]) -> None:
    if not filename:
        return
    clean_name = str(filename or "").replace("\\", "/").strip()
    if not clean_name:
        return
    base_dir = camera_manager.save_directory.resolve()
    candidate = (camera_manager.save_directory / clean_name).resolve()
    if not str(candidate).startswith(str(base_dir)):
        return
    try:
        candidate.unlink(missing_ok=True)
    except Exception as exc:
        app.logger.warning("Suppression photo vivant impossible (%s): %s", clean_name, exc)
        return
    _remove_photo_labels_for_files([clean_name])


def _load_last_water_metrics() -> Dict[str, Any]:
    if not WATER_METRICS_PATH.exists():
        return {}
    try:
        data = json.loads(WATER_METRICS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if not isinstance(data.get("values"), dict):
        data["values"] = {}
    return data


def _save_last_water_metrics(data: Dict[str, Any]) -> None:
    safe = {
        "recorded_at": data.get("recorded_at"),
        "values": data.get("values", {}),
    }
    WATER_METRICS_PATH.write_text(json.dumps(safe, indent=2), encoding="utf-8")


def _record_last_water_metrics(values: Dict[str, Any]) -> None:
    allowed = ("no3", "no2", "gh", "kh", "cl2", "po4", "ph", "temperature")
    sanitized: Dict[str, float] = {}
    for key in allowed:
        parsed = _parse_livestock_float(values.get(key))
        if parsed is not None:
            sanitized[key] = parsed
    if not sanitized:
        return
    payload = {
        "recorded_at": datetime.utcnow().isoformat() + "Z",
        "values": sanitized,
    }
    try:
        _save_last_water_metrics(payload)
    except OSError as exc:
        app.logger.warning("Enregistrement des mesures eau impossible: %s", exc)


def _get_last_water_metric(key: str) -> Optional[float]:
    data = _load_last_water_metrics()
    values = data.get("values") or {}
    return _parse_livestock_float(values.get(key))


def _load_ai_insights() -> List[Dict[str, Any]]:
    if not AI_INSIGHTS_PATH.exists():
        return []
    try:
        data = json.loads(AI_INSIGHTS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    for entry in data:
        if isinstance(entry, dict):
            cleaned.append(entry)
    return cleaned


def _save_ai_insights(insights: List[Dict[str, Any]]) -> None:
    AI_INSIGHTS_PATH.write_text(json.dumps(insights, indent=2), encoding="utf-8")


def _append_ai_insight(entry: Dict[str, Any]) -> Dict[str, Any]:
    insights = _load_ai_insights()
    insights.insert(0, entry)
    if len(insights) > MAX_AI_INSIGHTS:
        insights = insights[:MAX_AI_INSIGHTS]
    _save_ai_insights(insights)
    return entry


def _active_livestock_animals(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    active: List[Dict[str, Any]] = []
    for entry in entries:
        if entry.get("category") != "animal":
            continue
        if entry.get("removed_at"):
            continue
        if _coerce_entry_animal_count(entry) <= 0:
            continue
        active.append(entry)
    return active


def _compute_metric_payload(entries: List[Dict[str, Any]], metric: str, config: Dict[str, Any]) -> Dict[str, Any]:
    min_values: List[float] = []
    max_values: List[float] = []
    comfort_min_values: List[float] = []
    comfort_max_values: List[float] = []
    for entry in entries:
        min_val = _serialize_float_field(entry.get(f"{metric}_min"))
        max_val = _serialize_float_field(entry.get(f"{metric}_max"))
        if min_val is not None:
            min_values.append(min_val)
        if max_val is not None:
            max_values.append(max_val)
        if min_val is not None and max_val is not None:
            comfort_min_values.append(min_val)
            comfort_max_values.append(max_val)
    global_min = min(min_values) if min_values else None
    global_max = max(max_values) if max_values else None
    comfort_min = max(comfort_min_values) if comfort_min_values else None
    comfort_max = min(comfort_max_values) if comfort_max_values else None
    if comfort_min is not None and comfort_max is not None and comfort_min > comfort_max:
        comfort_min = None
        comfort_max = None
    scale_min = global_min if global_min is not None else config.get("default_min", 0.0)
    scale_max = global_max if global_max is not None else config.get("default_max", 1.0)
    scale_min = config.get("scale_min", scale_min)
    scale_max = config.get("scale_max", scale_max)
    if scale_max <= scale_min:
        span = max(abs(scale_min) * 0.2, 1.0)
        scale_min -= span
        scale_max += span
    return {
        "key": metric,
        "label": config.get("label", metric),
        "unit": config.get("unit", ""),
        "unit_prefix": config.get("unit_prefix"),
        "decimals": config.get("decimals", 1),
        "min": global_min,
        "max": global_max,
        "comfort_min": comfort_min,
        "comfort_max": comfort_max,
        "scale_min": scale_min,
        "scale_max": scale_max,
        "current": None,
    }


def _pick_temperature_value(state: Dict[str, Any]) -> Optional[float]:
    candidates = ("temp_2", "temp_1", "temp_3", "temp_4")
    for key in candidates:
        parsed = _parse_livestock_float(state.get(key))
        if parsed is not None:
            return parsed
    return None


def _build_water_targets_payload() -> Dict[str, Any]:
    all_entries = _load_livestock_entries()
    entries = [entry for entry in all_entries if entry.get("category") == "animal"]
    active_entries = _active_livestock_animals(entries)
    metrics: List[Dict[str, Any]] = []
    for key, config in WATER_TARGET_METRICS.items():
        metrics.append(_compute_metric_payload(entries, key, config))
    state = controller.get_state()
    last_metrics = _load_last_water_metrics()
    last_values = last_metrics.get("values", {})
    state_values = {
        "ph": _parse_livestock_float(state.get("ph")),
        "temperature": _pick_temperature_value(state),
        "gh": _parse_livestock_float(last_values.get("gh")),
    }
    for metric in metrics:
        current = state_values.get(metric["key"])
        if current is not None:
            metric["current"] = current
    return {
        "ok": True,
        "fish_count": len(active_entries),
        "metrics": metrics,
        "last_manual_record": last_metrics.get("recorded_at"),
    }


def _list_recent_photos(limit: int = 6) -> List[Dict[str, str]]:
    try:
        listing = camera_manager.list_media("photos", "desc", 1, max(limit, 1))
    except Exception:
        return []
    items: List[Dict[str, str]] = []
    for item in listing.get("items", []):
        filename = item.get("filename")
        if not filename:
            continue
        items.append(
            {
                "filename": filename,
                "url": url_for("camera_media", filename=filename),
                "thumbnail_url": url_for(
                    "camera_media", filename=item.get("thumbnail") or filename
                ),
            }
        )
        if len(items) >= limit:
            break
    return items


def _encode_photo_to_data_url(filename: str) -> str:
    clean_name = _ensure_photo_media_file(filename)
    path = (camera_manager.save_directory / clean_name).resolve()
    if not path.exists():
        raise FileNotFoundError(clean_name)
    if path.stat().st_size > AI_MAX_IMAGE_BYTES:
        raise ValueError(f"Image trop volumineuse (> {AI_MAX_IMAGE_BYTES // (1024 * 1024)} Mo)")
    suffix = path.suffix.lower()
    mime = "image/jpeg"
    if suffix == ".png":
        mime = "image/png"
    elif suffix in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


_ai_worker_lock = threading.Lock()
_ai_worker_process: Optional[subprocess.Popen] = None
_ai_worker_log_handle: Optional[Any] = None
_ai_worker_last_start: Optional[datetime] = None
_ai_worker_last_stop: Optional[datetime] = None
_ai_worker_last_exit: Optional[int] = None
_ai_comfort_logger: Optional[logging.Logger] = None


def _get_ai_comfort_logger() -> logging.Logger:
    global _ai_comfort_logger
    if _ai_comfort_logger:
        return _ai_comfort_logger
    AI_LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("reef.ai_comfort")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(
        AI_COMFORT_LOG, maxBytes=512 * 1024, backupCount=5, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    _ai_comfort_logger = logger
    return logger


def _ai_worker_script_path() -> Path:
    if not AI_WORKER_SCRIPT.exists():
        raise RuntimeError("Script ai_worker_local.py introuvable.")
    return AI_WORKER_SCRIPT


def _finalize_ai_worker_locked() -> None:
    global _ai_worker_process, _ai_worker_log_handle, _ai_worker_last_exit, _ai_worker_last_stop
    if _ai_worker_process and _ai_worker_process.poll() is not None:
        _ai_worker_last_exit = _ai_worker_process.returncode
        _ai_worker_last_stop = datetime.utcnow()
        _ai_worker_process = None
        if _ai_worker_log_handle:
            try:
                _ai_worker_log_handle.close()
            except Exception:
                pass
            _ai_worker_log_handle = None


def _ai_worker_running_locked() -> bool:
    _finalize_ai_worker_locked()
    return _ai_worker_process is not None


def _start_ai_worker_locked() -> Dict[str, Any]:
    global _ai_worker_process, _ai_worker_log_handle, _ai_worker_last_start
    if _ai_worker_running_locked():
        raise RuntimeError("Le worker IA tourne déjà.")
    script = _ai_worker_script_path()
    AI_WORKER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_handle = open(AI_WORKER_LOG, "ab", buffering=0)
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        cwd=str(BASE_DIR),
    )
    _ai_worker_process = process
    _ai_worker_log_handle = log_handle
    _ai_worker_last_start = datetime.utcnow()
    return {
        "pid": process.pid,
        "started_at": _ai_worker_last_start.isoformat() + "Z",
    }


def _stop_ai_worker_locked() -> bool:
    global _ai_worker_process, _ai_worker_log_handle, _ai_worker_last_stop, _ai_worker_last_exit
    if not _ai_worker_process:
        return False
    proc = _ai_worker_process
    _ai_worker_process = None
    if _ai_worker_log_handle:
        try:
            _ai_worker_log_handle.flush()
        except Exception:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    _ai_worker_last_stop = datetime.utcnow()
    _ai_worker_last_exit = proc.returncode
    if _ai_worker_log_handle:
        try:
            _ai_worker_log_handle.close()
        except Exception:
            pass
        _ai_worker_log_handle = None
    return True


def _ai_worker_status_locked() -> Dict[str, Any]:
    running = _ai_worker_running_locked()
    pid = _ai_worker_process.pid if running and _ai_worker_process else None
    return {
        "running": running,
        "pid": pid,
        "started_at": _ai_worker_last_start.isoformat() + "Z" if _ai_worker_last_start else None,
        "last_exit_code": _ai_worker_last_exit,
        "stopped_at": _ai_worker_last_stop.isoformat() + "Z" if _ai_worker_last_stop else None,
        "log_path": str(AI_WORKER_LOG),
    }




def _close_telemetry() -> None:

    if controller.telemetry:

        controller.telemetry.close()





atexit.register(_close_telemetry)

atexit.register(camera_manager.shutdown)





@app.route("/")

def index():

    return render_template("index.html")





@app.get("/api/ports")

def api_ports():

    return jsonify(list_serial_ports())





@app.get("/api/state")

def api_state():

    return jsonify(controller.get_state())





@app.get("/api/water/targets")

def api_water_targets():

    try:

        payload = _build_water_targets_payload()

    except Exception as exc:

        app.logger.exception("Impossible de calculer les cibles d'eau")

        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify(payload)





@app.post("/api/action")

def api_action():

    payload = request.get_json(force=True)

    action = payload.get("action")

    params = payload.get("params") or {}

    try:

        if action == "connect":

            controller.connect(params["port"])

        elif action == "disconnect":

            controller.disconnect()

        elif action == "read_temps":

            controller.read_temps_once()

        elif action == "read_levels":

            controller.read_levels_once()

        elif action == "set_water":

            controller.set_water(float(params["t"]))

        elif action == "set_reserve":

            controller.set_reserve(float(params["t"]))

        elif action == "auto_fan":

            controller.set_auto_fan(bool(params.get("auto")))

        elif action == "fan_manual":

            controller.set_fan_manual(int(params.get("value", 0)))

        elif action == "set_autocool":

            controller.set_autocool(float(params.get("thresh", 28)))

        elif action == "set_heat_hyst":

            controller.set_heat_hyst(float(params.get("value", 0.3)))

        elif action == "protect":

            controller.toggle_protect(bool(params.get("enable", False)))

        elif action == "servo":

            controller.set_servo(int(params.get("angle", 0)))

        elif action == "dispense":

            controller.dispense_macro()

        elif action == "heat_mode":

            controller.set_heat_mode(bool(params.get("auto", False)))

        elif action == "heat_power":

            controller.set_heat_power(bool(params.get("enable", False)))

        elif action == "mtr_auto_off":

            controller.set_mtr_auto_off(bool(params.get("enable", False)))

        elif action == "set_steps_speed":

            controller.set_steps_speed(

                int(params.get("steps", 0)), int(params.get("speed", 0))

            )

        elif action == "pump":

            controller.pump(params["axis"], bool(params.get("backwards", False)))

        elif action == "set_global_speed":

            controller.set_global_speed(int(params.get("speed", 0)))

        elif action == "update_pump_config":

            controller.update_pump_config(

                params["axis"],

                name=params.get("name"),

                volume_ml=params.get("volume_ml"),

                direction=params.get("direction"),

            )

        elif action == "set_peristaltic_auto":

            controller.set_peristaltic_auto(bool(params.get("enable", False)))

        elif action == "set_peristaltic_schedule":

            controller.update_peristaltic_schedule(params["axis"], params.get("time"))

        elif action == "peristaltic_cycle":

            controller.run_peristaltic_cycle(

                params["axis"],

                reason=str(params.get("reason") or "manual_cycle"),

            )

        elif action == "update_light_schedule":

            day = params.get("day") or params.get("zone")

            controller.update_light_schedule(day, params.get("on"), params.get("off"))

        elif action == "light_toggle":

            controller.toggle_light(

                params.get("state"), event_type="light_manual_toggle"

            )

        elif action == "light_auto":

            controller.set_light_auto(bool(params.get("enable", False)))

        elif action == "update_temp_names":

            controller.update_temp_names(params)

        elif action == "toggle_pump":

            controller.toggle_pump(params.get("state"))

        elif action == "set_feeder_auto":

            controller.set_feeder_auto(bool(params.get("enable", False)))

        elif action == "set_feeder_schedule":

            controller.update_feeder_schedule(params.get("entries", []))

        elif action == "trigger_feeder_url":

            controller.trigger_feeder_url(

                params["url"],

                params.get("method", "GET"),

                params.get("stop_pump"),

                params.get("pump_stop_duration_min"),

            )

        elif action == "submit_water_quality":

            controller.submit_water_quality(params)
            _record_last_water_metrics(params)

        elif action == "ph_calibrate":

            cal_state = controller.calibrate_ph_reference(
                params.get("reference") or params.get("ref")
            )
            return jsonify({"ok": True, "calibration": cal_state})

        elif action == "raw":

            controller.raw(str(params.get("cmd", "")))

        elif action == "emergency_stop":

            controller.emergency_stop()

        elif action == "restart_service":

            controller.restart_service()

        else:

            return jsonify({"ok": False, "error": f"Action inconnue: {action}"}), 400

        return jsonify({"ok": True})

    except Exception as exc:

        return jsonify({"ok": False, "error": str(exc)}), 400





@app.post("/api/analyze")

def api_analyze():

    try:

        analysis_response = controller.get_ai_analysis()

        if isinstance(analysis_response, dict):

            return jsonify(analysis_response)

        return jsonify({"analysis": analysis_response})

    except RuntimeError as exc:

        if str(exc) == controller.OPENAI_KEY_MISSING_ERROR:

            return (

                jsonify(

                    {

                        "ok": False,

                        "error": "ClÃ© API OpenAI manquante.",

                        "error_code": controller.OPENAI_KEY_MISSING_ERROR,

                    }

                ),

                400,

            )

        app.logger.exception("AI analysis failed")

        return jsonify({"ok": False, "error": str(exc)}), 500

    except Exception as exc:

        app.logger.exception("AI analysis failed")

        return jsonify({"ok": False, "error": str(exc)}), 500





@app.post("/api/openai-key")

def api_openai_key():

    payload = request.get_json(force=True) or {}

    api_key = (payload.get("api_key") or "").strip()

    try:

        controller.set_openai_api_key(api_key)

        return jsonify({"ok": True})

    except Exception as exc:

        return jsonify({"ok": False, "error": str(exc)}), 400



@app.get("/camera/settings")

def get_camera_settings():

    try:

        return jsonify({"ok": True, "settings": camera_manager.get_settings()})

    except Exception as exc:

        return jsonify({"ok": False, "error": str(exc)}), 500





@app.post("/camera/settings")

def update_camera_settings():

    payload = request.get_json(force=True) or {}

    try:

        updated = camera_manager.update_settings(payload)

        return jsonify({"ok": True, "settings": updated})

    except ValueError as exc:

        return jsonify({"ok": False, "error": str(exc)}), 400

    except Exception as exc:

        app.logger.exception("Camera settings update failed")

        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/camera/devices")

def camera_devices():

    try:

        return jsonify({"ok": True, "cameras": camera_manager.list_cameras()})

    except Exception as exc:

        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/camera/select")

def camera_select():

    payload = request.get_json(force=True) or {}

    camera_id = (payload.get("camera_id") or "").strip()

    if not camera_id:

        return jsonify({"ok": False, "error": "Identifiant caméra requis."}), 400

    try:

        settings = camera_manager.set_active_camera(camera_id)

        return jsonify({"ok": True, "settings": settings})

    except ValueError as exc:

        return jsonify({"ok": False, "error": str(exc)}), 400

    except Exception as exc:

        app.logger.exception("Camera select failed")

        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/camera_feed")

def camera_feed():

    if not camera_manager.available:

        return Response("Caméra indisponible", status=503)



    def generate():

        try:

            for frame in camera_manager.frame_generator():

                yield (

                    b"--frame\r\n"

                    b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"

                )

        except CameraUnavailable:

            app.logger.warning("Camera feed interrompue (non disponible)")



    return Response(

        stream_with_context(generate()),

        mimetype="multipart/x-mixed-replace; boundary=frame",

    )





@app.get("/camera/media/<path:filename>")

def camera_media(filename: str):

    return send_from_directory(str(camera_manager.save_directory), filename)





@app.post("/camera/capture_photo")

def camera_capture_photo():

    try:

        path = camera_manager.capture_photo()

    except CameraUnavailable as exc:

        return jsonify({"ok": False, "error": str(exc)}), 503

    except Exception as exc:

        app.logger.exception("Photo capture failed")

        return jsonify({"ok": False, "error": str(exc)}), 500

    url = url_for("camera_media", filename=path.name)

    return jsonify(

        {

            "ok": True,

            "filename": path.name,

            "url": url,

            "thumbnail_url": url,

        }

    )





@app.post("/camera/capture_video")

def camera_capture_video():

    payload = request.get_json(force=True) or {}

    try:

        duration = int(payload.get("duration_seconds", 0))

    except (TypeError, ValueError):

        return jsonify({"ok": False, "error": "Durée invalide."}), 400

    try:

        path = camera_manager.capture_video(duration)

    except ValueError as exc:

        return jsonify({"ok": False, "error": str(exc)}), 400

    except CameraUnavailable as exc:

        return jsonify({"ok": False, "error": str(exc)}), 503

    except Exception as exc:

        app.logger.exception("Video capture failed")

        return jsonify({"ok": False, "error": str(exc)}), 500

    thumb = camera_manager.generate_video_thumbnail(path)

    url = url_for("camera_media", filename=path.name)

    thumbnail_url = url_for(

        "camera_media", filename=(thumb.name if thumb else path.name)

    )

    return jsonify(

        {

            "ok": True,

            "filename": path.name,

            "url": url,

            "thumbnail_url": thumbnail_url,

        }

    )





@app.get("/esp32cam/config")

def esp32cam_get_config():

    return jsonify({"ok": True, "url": _get_esp32_cam_url()})



@app.post("/esp32cam/config")

def esp32cam_set_config():

    payload = request.get_json(force=True) or {}

    url = (payload.get("url") or "").strip()

    if not url:

        return (

            jsonify(

                {

                    "ok": False,

                    "error": "URL ESP32-CAM requise.",

                    "error_code": "ESP32_URL_NOT_SET",

                }

            ),

            400,

        )

    try:

        saved = _set_esp32_cam_url(url)

    except OSError as exc:

        app.logger.exception("Impossible de sauvegarder la configuration ESP32-CAM")

        return jsonify({"ok": False, "error": str(exc)}), 500

    return jsonify({"ok": True, "url": saved})



@app.get("/esp32cam/settings")

def esp32cam_get_settings():

    try:

        target = _build_esp32_url("/api/settings")

    except RuntimeError:

        return _esp32_error("URL ESP32-CAM non definie.", 400, "ESP32_URL_NOT_SET")

    try:

        resp = requests.get(target, timeout=ESP32_SETTINGS_TIMEOUT)

        resp.raise_for_status()

        data = resp.json()

    except requests.exceptions.RequestException as exc:

        app.logger.warning("ESP32 settings unreachable: %s", exc)

        return _esp32_error("ESP32-CAM injoignable.", 502, "ESP32_UNREACHABLE")

    except ValueError:

        return _esp32_error(

            "Reponse JSON invalide de l'ESP32-CAM.",

            502,

            "ESP32_INVALID_RESPONSE",

        )

    return jsonify({"ok": True, "settings": data})



@app.post("/esp32cam/settings")

def esp32cam_update_settings():

    payload = request.get_json(force=True) or {}

    try:

        target = _build_esp32_url("/api/settings")

    except RuntimeError:

        return _esp32_error("URL ESP32-CAM non definie.", 400, "ESP32_URL_NOT_SET")

    try:

        resp = requests.post(

            target, json=payload, timeout=ESP32_SETTINGS_TIMEOUT

        )

        resp.raise_for_status()

        data = resp.json()

    except requests.exceptions.RequestException as exc:

        app.logger.warning("ESP32 settings update failed: %s", exc)

        return _esp32_error("ESP32-CAM injoignable.", 502, "ESP32_UNREACHABLE")

    except ValueError:

        return _esp32_error(

            "Reponse JSON invalide de l'ESP32-CAM.",

            502,

            "ESP32_INVALID_RESPONSE",

        )

    return jsonify({"ok": True, "settings": data})



@app.get("/esp32cam/capture")

def esp32cam_capture():

    try:

        target = _build_esp32_url("/capture")

    except RuntimeError:

        return _esp32_error("URL ESP32-CAM non definie.", 400, "ESP32_URL_NOT_SET")

    try:

        resp = requests.get(target, timeout=ESP32_CAPTURE_TIMEOUT, stream=True)

        resp.raise_for_status()

    except requests.exceptions.RequestException as exc:

        app.logger.warning("ESP32 capture failed: %s", exc)

        return _esp32_error("ESP32-CAM injoignable.", 502, "ESP32_UNREACHABLE")

    content_type = resp.headers.get("Content-Type", "image/jpeg")

    content_length = resp.headers.get("Content-Length")

    def generate():

        with resp:

            for chunk in resp.iter_content(chunk_size=8192):

                if chunk:

                    yield chunk

    headers = {}

    if content_length:

        headers["Content-Length"] = content_length

    return Response(

        stream_with_context(generate()),

        content_type=content_type,

        headers=headers,

    )






@app.get("/gallery/media")

def gallery_media():

    media_type = request.args.get("type", "photos")

    page = request.args.get("page", 1, type=int)

    sort = request.args.get("sort", "desc")

    per_page = request.args.get("per_page", 30, type=int)

    if media_type not in ("photos", "videos"):

        return jsonify({"ok": False, "error": "Type invalide."}), 400

    try:

        listing = camera_manager.list_media(media_type, sort, page, per_page)

    except Exception as exc:

        app.logger.exception("Gallery listing failed")

        return jsonify({"ok": False, "error": str(exc)}), 500

    items_payload = []
    labels_map = {}
    if media_type == "photos":
        labels_map = _load_photo_label_data().get("labels", {})

    for item in listing["items"]:

        filename = item["filename"]

        thumb_name = item.get("thumbnail") or filename

        payload = {

            "filename": filename,

            "url": url_for("camera_media", filename=filename),

            "thumbnail_url": url_for("camera_media", filename=thumb_name),

        }
        if media_type == "photos":

            payload["labels"] = labels_map.get(filename, [])

        items_payload.append(payload)

    return jsonify(

        {

            "ok": True,

            "items": items_payload,

            "total_pages": listing["total_pages"],

            "current_page": listing["current_page"],

        }

    )





@app.post("/gallery/delete")

def gallery_delete():

    payload = request.get_json(force=True) or {}

    filenames = payload.get("filenames") or []

    if not isinstance(filenames, list):

        return jsonify({"ok": False, "error": "Liste de fichiers requise."}), 400

    clean_names = [str(name) for name in filenames if isinstance(name, str)]

    deleted = camera_manager.delete_media(clean_names)

    if deleted:

        _remove_photo_labels_for_files(deleted)

    return jsonify({"ok": True, "deleted": deleted})



@app.get("/gallery/labels")

def gallery_get_labels():

    data = _load_photo_label_data()

    return jsonify(

        {"ok": True, "categories": data["categories"], "labels": data["labels"]}

    )



@app.post("/gallery/categories")

def gallery_add_category():

    payload = request.get_json(force=True) or {}

    name = str(payload.get("name") or "").strip()

    if not name:

        return jsonify({"ok": False, "error": "Nom de categorie requis."}), 400

    data = _load_photo_label_data()

    lower = name.lower()

    if any(cat.lower() == lower for cat in data["categories"]):

        return (

            jsonify({"ok": False, "error": "Categorie deja existante."}),

            400,

        )

    data["categories"].append(name)

    data["categories"] = _normalize_photo_categories(data["categories"])

    _save_photo_label_data(data)

    return jsonify({"ok": True, "categories": data["categories"]})



@app.post("/gallery/labels")

def gallery_update_labels():

    payload = request.get_json(force=True) or {}

    filename_value = payload.get("filename")

    try:

        filename = _ensure_photo_media_file(filename_value)

    except FileNotFoundError:

        return (

            jsonify({"ok": False, "error": "Fichier a etiqueter introuvable."}),

            404,

        )

    except ValueError as exc:

        return jsonify({"ok": False, "error": str(exc)}), 400

    labels_value = payload.get("labels") or []

    if not isinstance(labels_value, list):

        return jsonify({"ok": False, "error": "Labels invalides."}), 400

    data = _load_photo_label_data()

    lookup = {cat.lower(): cat for cat in data["categories"]}

    cleaned: List[str] = []

    seen: Set[str] = set()

    for label in labels_value:

        if not isinstance(label, str):

            continue

        clean_label = label.strip()

        if not clean_label:

            continue

        key = clean_label.lower()

        canonical = lookup.get(key)

        if not canonical or key in seen:

            continue

        cleaned.append(canonical)

        seen.add(key)

    if cleaned:

        data["labels"][filename] = cleaned

    else:

        data["labels"].pop(filename, None)

    _save_photo_label_data(data)

    return jsonify({"ok": True, "filename": filename, "labels": data["labels"].get(filename, [])})



@app.get("/logbook/entries")
def logbook_entries():
    entries = _load_logbook_entries()
    entries.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    payload = [_serialize_log_entry(entry) for entry in entries]
    return jsonify({"ok": True, "entries": payload})


@app.post("/logbook/entries")
def logbook_add_entry():
    text = (request.form.get("text") or "").strip()
    photos: List[str] = []
    files = request.files.getlist("photos")
    if files and len(files) > MAX_LOGBOOK_PHOTOS:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": f"Maximum {MAX_LOGBOOK_PHOTOS} photos par entree.",
                }
            ),
            400,
        )
    if not text and not any(file.filename for file in files):
        return jsonify({"ok": False, "error": "Texte ou photo requis."}), 400
    try:
        for file_obj in files:
            if not file_obj or not file_obj.filename:
                continue
            saved_name = _store_logbook_photo(file_obj)
            photos.append(saved_name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("Sauvegarde photo journal impossible")
        return jsonify({"ok": False, "error": str(exc)}), 500
    entry = {
        "id": uuid4().hex,
        "text": text,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "photos": photos,
    }
    entries = _load_logbook_entries()
    entries.append(entry)
    entries.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    try:
        _save_logbook_entries(entries)
    except OSError as exc:
        app.logger.error("Logbook save failed: %s", exc)
        return jsonify({"ok": False, "error": "Sauvegarde du journal impossible."}), 500
    return jsonify({"ok": True, "entry": _serialize_log_entry(entry)})


def _sort_livestock_payload(items: List[Dict[str, Any]]) -> None:
    def _key(item: Dict[str, Any]) -> str:
        introduced = item.get("introduced_at") or ""
        created = item.get("created_at") or ""
        return f"{introduced}|{created}"

    items.sort(key=_key, reverse=True)


@app.get("/logbook/catalog")
def logbook_catalog():
    entries = _load_livestock_entries()
    animals: List[Dict[str, Any]] = []
    plants: List[Dict[str, Any]] = []
    for entry in entries:
        serialized = _serialize_livestock_entry(entry)
        target = animals if entry.get("category") == "animal" else plants
        target.append(serialized)
    _sort_livestock_payload(animals)
    _sort_livestock_payload(plants)
    return jsonify({"ok": True, "animals": animals, "plants": plants})


@app.post("/logbook/catalog")
def logbook_catalog_add():
    category = (request.form.get("category") or "").strip().lower()
    if category not in LIVESTOCK_VALID_CATEGORIES:
        return jsonify({"ok": False, "error": "Categorie invalide."}), 400
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Nom de l'espece requis."}), 400
    introduced_at = (request.form.get("introduced_at") or "").strip()
    removed_at = (request.form.get("removed_at") or "").strip()
    count = _parse_livestock_count(request.form.get("count"))
    photo_name: Optional[str] = None
    photo_file = request.files.get("photo")
    if photo_file and photo_file.filename:
        try:
            photo_name = _store_logbook_photo(photo_file)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - unexpected IO failure
            app.logger.exception("Enregistrement photo vivant impossible")
            return jsonify({"ok": False, "error": str(exc)}), 500
    now = datetime.utcnow().isoformat() + "Z"
    entry = {
        "id": uuid4().hex,
        "category": category,
        "name": name,
        "introduced_at": introduced_at,
        "removed_at": removed_at,
        "count": count,
        "photo": photo_name,
        "created_at": now,
        "updated_at": now,
    }
    _apply_livestock_water_params(entry, request.form)
    entries = _load_livestock_entries()
    entries.append(entry)
    try:
        _save_livestock_entries(entries)
    except OSError as exc:
        app.logger.error("Sauvegarde catalogue vivant impossible: %s", exc)
        return (
            jsonify({"ok": False, "error": "Ecriture sur disque impossible."}),
            500,
        )
    _publish_animal_population("create", entry)
    return jsonify(
        {"ok": True, "entry": _serialize_livestock_entry(entry), "category": category}
    )


@app.put("/logbook/catalog/<entry_id>")
def logbook_catalog_update(entry_id: str):
    entries = _load_livestock_entries()
    entry = _find_livestock_entry(entries, entry_id)
    if not entry:
        return jsonify({"ok": False, "error": "Entree introuvable."}), 404
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Nom de l'espece requis."}), 400
    introduced_at = (request.form.get("introduced_at") or "").strip()
    removed_at = (request.form.get("removed_at") or "").strip()
    count = _parse_livestock_count(request.form.get("count"))
    photo_file = request.files.get("photo")
    if photo_file and photo_file.filename:
        try:
            entry["photo"] = _store_logbook_photo(photo_file)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - unexpected IO failure
            app.logger.exception("Mise a jour photo vivant impossible")
            return jsonify({"ok": False, "error": str(exc)}), 500
    entry["name"] = name
    entry["introduced_at"] = introduced_at
    entry["removed_at"] = removed_at
    entry["count"] = count
    entry["updated_at"] = datetime.utcnow().isoformat() + "Z"
    _apply_livestock_water_params(entry, request.form)
    try:
        _save_livestock_entries(entries)
    except OSError as exc:
        app.logger.error("Sauvegarde catalogue vivant impossible: %s", exc)
        return (
            jsonify({"ok": False, "error": "Ecriture sur disque impossible."}),
            500,
        )
    _publish_animal_population("update", entry)
    return jsonify(
        {
            "ok": True,
            "entry": _serialize_livestock_entry(entry),
            "category": entry.get("category"),
        }
    )


@app.delete("/logbook/catalog/<entry_id>")
def logbook_catalog_delete(entry_id: str):
    entries = _load_livestock_entries()
    entry = _find_livestock_entry(entries, entry_id)
    if not entry:
        return jsonify({"ok": False, "error": "Entree introuvable."}), 404
    updated_entries = [item for item in entries if item.get("id") != entry_id]
    photo_name = entry.get("photo")
    try:
        _save_livestock_entries(updated_entries)
    except OSError as exc:
        app.logger.error("Suppression catalogue vivant impossible: %s", exc)
        return (
            jsonify({"ok": False, "error": "Ecriture sur disque impossible."}),
            500,
        )
    _publish_animal_population("delete", entry)
    _delete_livestock_photo_file(photo_name)
    return jsonify(
        {"ok": True, "entry_id": entry_id, "category": entry.get("category")}
    )


def _build_animal_comfort_prompt(species_name: str) -> List[Dict[str, str]]:
    instructions = (
        "Tu es un expert en aquariophilie. Utilise exclusivement les informations figurant sur le site https://fr.aqua-fish.net/poissons/ "
        "pour repondre. Fournis uniquement un JSON compact avec ce schema:\n"
        '{\n'
        '  "ph": {"min": nombre ou null, "max": nombre ou null},\n'
        '  "kh": {"min": nombre ou null, "max": nombre ou null},\n'
        '  "gh": {"min": nombre ou null, "max": nombre ou null},\n'
        '  "temperature": {"min": nombre ou null, "max": nombre ou null},\n'
        '  "resistance": "texte court (par ex. Faible/Moyenne/Elevee)"\n'
        "}\n"
        "Utilise un point comme separateur decimal et indique les valeurs habituelles"
        " observees en captivite. Si l'information est inconnue, utilise null."
    )
    user_prompt = (
        f"Espece concernee: {species_name}.\n"
        "Releve les informations exactes presentes sur la fiche correspondante du site https://fr.aqua-fish.net/poissons/ "
        "pour donner les plages de confort en aquarium communautaire tropical."
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": user_prompt},
    ]


def _parse_comfort_json(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Payload IA invalide.")
    flattened: Dict[str, Any] = {}
    for field in LIVESTOCK_WATER_FIELDS:
        raw = payload.get(field)
        if isinstance(raw, dict):
            flattened[f"{field}_min"] = _parse_livestock_float(raw.get("min"))
            flattened[f"{field}_max"] = _parse_livestock_float(raw.get("max"))
        else:
            flattened[f"{field}_min"] = None
            flattened[f"{field}_max"] = None
    resistance = payload.get("resistance")
    flattened["resistance"] = str(resistance).strip() if isinstance(resistance, str) else ""
    return flattened


def _strip_code_fences(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    content = raw.strip()
    if content.startswith("```"):
        first_newline = content.find("\n")
        if first_newline != -1:
            content = content[first_newline + 1 :]
        content = content.strip()
        if content.endswith("```"):
            content = content[: -3].strip()
    return content


@app.post("/logbook/catalog/comfort")
def logbook_catalog_comfort():
    payload = request.get_json(force=True) or {}
    name = (payload.get("name") or "").strip()
    category = (payload.get("category") or "").strip().lower() or "animal"
    if category != "animal":
        return jsonify({"ok": False, "error": "Support limite aux fiches animales."}), 400
    if not name:
        return jsonify({"ok": False, "error": "Nom de l'espece requis."}), 400
    logger = _get_ai_comfort_logger()
    try:
        messages = _build_animal_comfort_prompt(name)
        logger.info("[INFO] espece=%s request=%s", name, json.dumps(messages, ensure_ascii=False))
        result = call_llm(messages, temperature=0.2, max_tokens=400)
        content = result.get("content") or ""
        logger.info("[INFO] espece=%s response=%s", name, content)
        cleaned_content = _strip_code_fences(content)
        data = json.loads(cleaned_content)
        flattened = _parse_comfort_json(data)
        logger.info("[SUCCES] espece=%s ranges=%s", name, json.dumps(flattened, ensure_ascii=False))
    except RuntimeError as exc:
        if str(exc) == ANALYSIS_KEY_MISSING_ERROR:
            logger.error("[ERROR] espece=%s code=%s message=%s", name, ANALYSIS_KEY_MISSING_ERROR, exc)
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "Cle API OpenAI manquante.",
                        "error_code": ANALYSIS_KEY_MISSING_ERROR,
                    }
                ),
                400,
            )
        logger.error("[ERROR] espece=%s type=runtime message=%s", name, exc)
        app.logger.error("AI comfort lookup failed: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 500
    except json.JSONDecodeError as exc:
        logger.error("[ERROR] espece=%s type=json message=%s", name, exc)
        app.logger.error("AI comfort payload invalide: %s", exc)
        return jsonify({"ok": False, "error": "Reponse IA invalide."}), 502
    except ValueError as exc:
        logger.error("[ERROR] espece=%s type=parse message=%s", name, exc)
        app.logger.error("AI comfort parsing failed: %s", exc)
        return jsonify({"ok": False, "error": "Reponse IA invalide."}), 502
    return jsonify({"ok": True, "ranges": flattened})


PERIOD_ALIASES = {

    "3d": "last_3_days",

    "week": "last_week",

    "month": "last_month",

    "year": "last_year",

}





@app.get("/analysis/queries")

def get_analysis_queries():

    try:

        return jsonify(load_analysis_queries())

    except Exception as exc:

        return jsonify({"ok": False, "error": str(exc)}), 500





@app.put("/analysis/queries")

def put_analysis_queries():

    payload = request.get_json(force=True) or {}

    try:

        updated = save_analysis_queries(payload)

        return jsonify(updated)

    except Exception as exc:

        return jsonify({"ok": False, "error": str(exc)}), 400





@app.get("/analysis/run")

def run_analysis():

    periods_param = request.args.get("periods", "last_3_days")

    requested = [

        PERIOD_ALIASES.get(item.strip(), item.strip())

        for item in periods_param.split(",")

        if item.strip()

    ]

    if not requested:

        requested = ["last_3_days"]

    try:

        summary = build_summary(requested)

        return jsonify({"summary": summary})

    except Exception as exc:

        app.logger.exception("Analysis build failed")

        return jsonify({"ok": False, "error": str(exc)}), 500





@app.post("/analysis/ask")

def ask_analysis():

    payload = request.get_json(force=True) or {}

    summary = payload.get("summary")

    user_context = payload.get("context", "")

    client_time = payload.get("client_time")

    if not isinstance(summary, dict):

        return (

            jsonify({"ok": False, "error": "RÃ©sumÃ© manquant pour l'analyse IA."}),

            400,

        )

    try:

        ai_response = ask_aquarium_ai(

            summary,

            user_context=user_context or "",

            client_timestamp=client_time,

        )

        return jsonify(ai_response)

    except Exception as exc:

        if isinstance(exc, RuntimeError) and str(exc) == ANALYSIS_KEY_MISSING_ERROR:

            return (

                jsonify(

                    {

                        "ok": False,

                        "error": "ClÃ© API OpenAI manquante.",

                        "error_code": ANALYSIS_KEY_MISSING_ERROR,

                    }

                ),

                400,

            )

        app.logger.exception("AI analysis failed")

        return jsonify({"ok": False, "error": str(exc)}), 500




@app.get("/api/ai/config")
def api_ai_config_get():
    return jsonify({"ok": True, "config": load_ai_config_for_client()})


@app.post("/api/ai/config")
def api_ai_config_save():
    payload = request.get_json(force=True) or {}
    try:
        updated = save_ai_config(payload)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("AI config save failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "config": updated})


@app.post("/api/ai/test")
def api_ai_test():
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode")
    try:
        start = time.monotonic()
        result = call_llm(
            [
                {"role": "system", "content": "Tu es un service IA de diagnostic."},
                {"role": "user", "content": "Reponds par 'pong' pour confirmer que tu es disponible."},
            ],
            force_mode=mode if isinstance(mode, str) else None,
            allow_fallback=False,
            request_timeout=20,
        )
        latency = (time.monotonic() - start) * 1000.0
        return jsonify(
            {
                "ok": True,
                "mode_used": result.get("mode_used"),
                "latency_ms": round(latency, 2),
                "reply": result.get("content"),
            }
        )
    except RuntimeError as exc:
        status = 400 if str(exc) == ANALYSIS_KEY_MISSING_ERROR else 502
        return jsonify({"ok": False, "error": str(exc)}), status
    except Exception as exc:
        app.logger.exception("AI test failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.get("/api/ai/summary")
def api_ai_summary():
    period = request.args.get("period", "last_3_days")
    try:
        summary = build_ai_summary_payload(period)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("AI summary build failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    summary["images"] = _list_recent_photos(limit=6)
    return jsonify({"ok": True, "summary": summary})


@app.post("/api/ai/analyze_with_images")
def api_ai_analyze_with_images():
    payload = request.get_json(force=True) or {}
    prompt = (payload.get("prompt") or "").strip()
    image_filenames = payload.get("image_filenames") or []
    if not prompt and not image_filenames:
        return jsonify({"ok": False, "error": "Prompt ou images requis."}), 400
    if not isinstance(image_filenames, list):
        return jsonify({"ok": False, "error": "Liste d'images invalide."}), 400
    if len(image_filenames) > AI_IMAGE_SELECTION_LIMIT:
        return jsonify({"ok": False, "error": f"Maximum {AI_IMAGE_SELECTION_LIMIT} images."}), 400
    encoded_chunks = []
    attached_images = []
    for raw_name in image_filenames:
        if not isinstance(raw_name, str):
            continue
        try:
            data_url = _encode_photo_to_data_url(raw_name)
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Image encoding failed")
            return jsonify({"ok": False, "error": str(exc)}), 500
        encoded_chunks.append({"type": "image_url", "image_url": {"url": data_url}})
        attached_images.append(
            {
                "filename": raw_name,
                "url": url_for("camera_media", filename=raw_name),
            }
        )
    if encoded_chunks:
        user_content = [{"type": "text", "text": prompt or "Analyse ces photos."}] + encoded_chunks
    else:
        user_content = prompt
    messages = [
        {
            "role": "system",
            "content": "Tu es une IA experte en aquariophilie. Analyse les observations et photos fournies pour conseiller sur l'etat de l'aquarium.",
        },
        {"role": "user", "content": user_content},
    ]
    try:
        result = call_llm(messages, temperature=0.3, allow_fallback=True)
    except RuntimeError as exc:
        status = 400 if str(exc) == ANALYSIS_KEY_MISSING_ERROR else 502
        return jsonify({"ok": False, "error": str(exc)}), status
    except Exception as exc:
        app.logger.exception("AI vision analysis failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify(
        {
            "ok": True,
            "analysis": result.get("content") or "Pas de reponse IA.",
            "mode_used": result.get("mode_used"),
            "images": attached_images,
        }
    )


@app.get("/api/ai/insights")
def api_ai_insights():
    return jsonify({"ok": True, "insights": _load_ai_insights()})


@app.post("/api/ai/insight")
def api_ai_insight_post():
    payload = request.get_json(force=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Texte requis."}), 400
    source = (payload.get("source") or "manual").strip() or "manual"
    risk_level = (payload.get("risk_level") or "info").strip() or "info"
    entry = {
        "id": uuid4().hex,
        "text": text,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "source": source,
        "risk_level": risk_level,
        "mode": payload.get("mode") or "",
        "metadata": payload.get("metadata") or {},
    }
    try:
        stored = _append_ai_insight(entry)
    except Exception as exc:
        app.logger.exception("AI insight save failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
    return jsonify({"ok": True, "insight": stored})


@app.get("/api/ai/worker/status")
def api_ai_worker_status():
    with _ai_worker_lock:
        status = _ai_worker_status_locked()
    return jsonify({"ok": True, "status": status})


@app.post("/api/ai/worker/start")
def api_ai_worker_start():
    try:
        with _ai_worker_lock:
            data = _start_ai_worker_locked()
        return jsonify({"ok": True, "started": data})
    except RuntimeError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        app.logger.exception("AI worker start failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/ai/worker/stop")
def api_ai_worker_stop():
    with _ai_worker_lock:
        stopped = _stop_ai_worker_locked()
        status = _ai_worker_status_locked()
    return jsonify({"ok": True, "stopped": stopped, "status": status})





if __name__ == "__main__":

    # DÃ©sactive le reloader Flask pour Ã©viter de lancer deux instances du contrÃ´leur

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

