import atexit

from flask import Flask, jsonify, render_template, request

from analysis import (
    OPENAI_KEY_MISSING_ERROR as ANALYSIS_KEY_MISSING_ERROR,
    ask_aquarium_ai,
    build_summary,
    load_analysis_queries,
    save_analysis_queries,
)
from controller import controller, list_serial_ports


app = Flask(__name__)


def _close_telemetry() -> None:
    if controller.telemetry:
        controller.telemetry.close()


atexit.register(_close_telemetry)


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
                        "error": "Clé API OpenAI manquante.",
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
            jsonify({"ok": False, "error": "Résumé manquant pour l'analyse IA."}),
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
                        "error": "Clé API OpenAI manquante.",
                        "error_code": ANALYSIS_KEY_MISSING_ERROR,
                    }
                ),
                400,
            )
        app.logger.exception("AI analysis failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


if __name__ == "__main__":
    # Désactive le reloader Flask pour éviter de lancer deux instances du contrôleur
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
