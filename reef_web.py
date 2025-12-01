import atexit
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Set
from uuid import uuid4

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

from analysis import (
    OPENAI_KEY_MISSING_ERROR as ANALYSIS_KEY_MISSING_ERROR,
    ask_aquarium_ai,
    build_summary,
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
PHOTO_LABELS_PATH = BASE_DIR / "photo_labels.json"
DEFAULT_PHOTO_CATEGORIES = ["Plante", "Produit", "Poisson"]

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





if __name__ == "__main__":

    # DÃ©sactive le reloader Flask pour Ã©viter de lancer deux instances du contrÃ´leur

    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

