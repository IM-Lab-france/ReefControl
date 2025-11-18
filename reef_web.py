from flask import Flask, jsonify, render_template, request

from controller import controller, list_serial_ports


app = Flask(__name__)


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
            controller.set_steps_speed(int(params.get("steps", 0)), int(params.get("speed", 0)))
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
        elif action == "update_light_schedule":
            day = params.get("day") or params.get("zone")
            controller.update_light_schedule(day, params.get("on"), params.get("off"))
        elif action == "light_toggle":
            controller.toggle_light(params.get("state"))
        elif action == "light_auto":
            controller.set_light_auto(bool(params.get("enable", False)))
        elif action == "update_temp_names":
            controller.update_temp_names(params)
        elif action == "toggle_pump":
            controller.toggle_pump(params.get("state"))
        elif action == "raw":
            controller.raw(str(params.get("cmd", "")))
        elif action == "emergency_stop":
            controller.emergency_stop()
        else:
            return jsonify({"ok": False, "error": f"Action inconnue: {action}"}), 400
        return jsonify({"ok": True})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
