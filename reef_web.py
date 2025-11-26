import atexit



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



from analysis import (

    OPENAI_KEY_MISSING_ERROR as ANALYSIS_KEY_MISSING_ERROR,

    ask_aquarium_ai,

    build_summary,

    load_analysis_queries,

    save_analysis_queries,

)

from controller import controller, list_serial_ports

from camera_manager import CameraUnavailable, camera_manager





app = Flask(__name__)





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

    for item in listing["items"]:

        filename = item["filename"]

        thumb_name = item.get("thumbnail") or filename

        items_payload.append(

            {

                "filename": filename,

                "url": url_for("camera_media", filename=filename),

                "thumbnail_url": url_for("camera_media", filename=thumb_name),

            }

        )

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

    return jsonify({"ok": True, "deleted": deleted})



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

