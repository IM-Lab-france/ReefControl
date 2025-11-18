import json
import math
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import serial
import serial.tools.list_ports

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:
    GPIO = None

BAUDRATE = 115200
HANDSHAKE_TIMEOUT = 4.0
PUMP_CONFIG_PATH = Path("pump_config.json")
LIGHT_SCHEDULE_PATH = Path("light_schedule.json")
HEAT_CONFIG_PATH = Path("heat_config.json")
PUMP_GPIO_PIN = 22
FAN_GPIO_PIN = 23
HEAT_GPIO_PIN = 24  # relais chauffe eau
TEMP_NAMES_PATH = Path("temp_names.json")
LIGHT_GPIO_PIN = 27
LIGHT_DAY_KEYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]

logger = logging.getLogger("reef.controller")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)


def list_serial_ports() -> list[Dict[str, str]]:
    ports = []
    for port in serial.tools.list_ports.comports():
        ports.append({"device": port.device, "description": port.description})
    return ports


class SerialClient:
    def __init__(self, line_handler: Callable[[str], None]):
        self._ser: Optional[serial.Serial] = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._line_handler = line_handler
        self.port: Optional[str] = None

    def open(self, port: str) -> tuple[str, str]:
        self.close()
        self.port = port
        self._ser = serial.Serial(port, BAUDRATE, timeout=0.2)
        time.sleep(1.5)
        try:
            hello_line = self._handshake(
                "HELLO?", lambda l: l.startswith("HELLO OK"), "HELLO"
            )
            status_line = self._handshake(
                "STATUS?", lambda l: l.startswith("STATUS;"), "STATUS"
            )
        except Exception:
            self.close()
            raise
        self._stop.clear()
        self._reader = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader.start()
        return hello_line, status_line

    def _handshake(
        self, command: str, predicate: Callable[[str], bool], label: str
    ) -> str:
        assert self._ser is not None
        deadline = time.time() + HANDSHAKE_TIMEOUT
        self._write(command)
        while time.time() < deadline:
            line = self._ser.readline().decode(errors="ignore").strip()
            if not line:
                continue
            if predicate(line):
                return line
            logger.debug("[HANDSHAKE] ignoring %s", line)
        raise RuntimeError(f"Timeout {label}")

    def _write(self, command: str) -> None:
        if not self._ser:
            raise RuntimeError("Port fermé")
        payload = (command.strip() + "\r\n").encode()
        self._ser.write(payload)
        self._ser.flush()

    def write(self, command: str) -> None:
        self._write(command)

    def close(self) -> None:
        self._stop.set()
        if self._reader and self._reader.is_alive():
            self._reader.join(timeout=0.5)
        self._reader = None
        if self._ser:
            try:
                self._ser.close()
            except Exception:
                pass
        self._ser = None
        self.port = None

    def _reader_loop(self) -> None:
        assert self._ser is not None
        while not self._stop.is_set():
            try:
                line = self._ser.readline()
                if not line:
                    continue
                self._line_handler(line.decode(errors="ignore").strip())
            except Exception as exc:
                logger.error("[SER] reader error: %s", exc)
                self._stop.set()
                break


class ReefController:
    def __init__(self) -> None:
        self.serial = SerialClient(self._handle_line)
        self.connected = False
        self.status_text = "Déconnecté"
        self.last_error: Optional[Dict[str, Any]] = None
        self.response_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.state_lock = threading.Lock()
        self.state: Dict[str, Any] = {
            "tw": "--.-",
            "ta": "--.-",
            "tx": "--.-",
            "tset_water": 25.0,
            "tset_res": 30.0,
            "pidw": (12.0, 0.4, 60.0),
            "pidr": (12.0, 0.4, 60.0),
            "auto_fan": True,
            "auto_thresh": 28.0,
            "fan": 0,
            "lvl_low": "?",
            "lvl_high": "?",
            "lvl_alert": "?",
            "protect": True,
            "steps": 3200,
            "speed": 300,
            "mtr_auto_off": True,
            "servo_angle": 10,
            "motors_powered": False,
            "pump_config": {},
            "light_state": False,
            "light_auto": True,
            "light_schedule": {
                day: {"on": "08:00", "off": "20:00"} for day in LIGHT_DAY_KEYS
            },
            "heat_targets": {"water": 25.0, "reserve": 30.0},
            "heat_auto": True,
            "heat_enabled": True,
            "heat_state": {"water": True, "reserve": True},
            "ph_v": None,
            "ph_raw": None,
            "ph": None,
            "ty_min": "--.-",
            "ty_max": "--.-",
            "pump_state": False,
            "fan_on": False,
            "temp_names": {
                "water": "Eau",
                "air": "Air",
                "aux": "Aux",
                "ymin": "Y-Min",
                "ymax": "Y-Max",
            },
        }
        self.global_speed = 300
        self.steps_per_job = 1000
        self._load_configs()
        self._ensure_pump_defaults()
        self._ensure_light_schedule_defaults()
        self._load_heat_config()
        self._load_temp_names()
        self.light_gpio_ready = False
        self.pump_gpio_ready = False
        self.fan_gpio_ready = False
        self.heat_gpio_ready = False
        self._init_light_gpio()
        self._init_pump_gpio()
        self._init_fan_gpio()
        self._init_heat_gpio()
        self._drive_pump_gpio(self.state.get("pump_state", False))
        self._drive_fan_gpio(self.state.get("fan", 0) > 0)
        self._drive_heat_gpio(self.state.get("heat_enabled", False))
        self._last_temp_query = 0.0
        self._last_level_query = 0.0
        self.light_scheduler = threading.Thread(
            target=self._light_scheduler_loop, daemon=True
        )
        self.light_scheduler.start()
        self.telemetry_thread = threading.Thread(
            target=self._telemetry_loop, daemon=True
        )
        self.telemetry_thread.start()

    # ---------- Config ----------
    def _load_configs(self) -> None:
        if PUMP_CONFIG_PATH.exists():
            try:
                self.state["pump_config"] = json.loads(
                    PUMP_CONFIG_PATH.read_text("utf-8")
                )
            except Exception:
                self.state["pump_config"] = {}
        else:
            self.state["pump_config"] = {}

        if LIGHT_SCHEDULE_PATH.exists():
            try:
                self.state["light_schedule"] = json.loads(
                    LIGHT_SCHEDULE_PATH.read_text("utf-8")
                )
            except Exception:
                pass
        self._load_temp_names()
        # Fan state is GPIO-only now; ensure auto_fan defaults and fan_on coherence
        with self.state_lock:
            self.state["auto_fan"] = True
            self.state["fan_on"] = False

    def _save_pump_config(self) -> None:
        try:
            PUMP_CONFIG_PATH.write_text(
                json.dumps(self.state["pump_config"], indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error("Unable to save pump config: %s", exc)

    def _save_light_schedule(self) -> None:
        try:
            LIGHT_SCHEDULE_PATH.write_text(
                json.dumps(self.state["light_schedule"], indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error("Unable to save light schedule: %s", exc)

    def _load_temp_names(self) -> None:
        if TEMP_NAMES_PATH.exists():
            try:
                data = json.loads(TEMP_NAMES_PATH.read_text("utf-8"))
                if isinstance(data, dict):
                    self.state.setdefault("temp_names", {}).update(data)
            except Exception:
                pass

    def _save_temp_names(self) -> None:
        try:
            TEMP_NAMES_PATH.write_text(
                json.dumps(self.state.get("temp_names", {}), indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error("Unable to save temp names: %s", exc)

    def _load_heat_config(self) -> None:
        if HEAT_CONFIG_PATH.exists():
            try:
                data = json.loads(HEAT_CONFIG_PATH.read_text("utf-8"))
                with self.state_lock:
                    if "targets" in data:
                        self.state["heat_targets"].update(data["targets"])
                        self.state["tset_water"] = self.state["heat_targets"].get(
                            "water", self.state["tset_water"]
                        )
                        self.state["tset_res"] = self.state["heat_targets"].get(
                            "reserve", self.state["tset_res"]
                        )
                    if "auto" in data:
                        self.state["heat_auto"] = bool(data["auto"])
                    if "enabled" in data:
                        self.state["heat_enabled"] = bool(data["enabled"])
                    if "state" in data:
                        self.state["heat_state"].update(data["state"])
            except Exception as exc:
                logger.error("Unable to read heat config: %s", exc)

    def _save_heat_config(self) -> None:
        with self.state_lock:
            payload = {
                "targets": self.state.get("heat_targets", {}),
                "auto": self.state.get("heat_auto", True),
                "enabled": self.state.get("heat_enabled", True),
                "state": self.state.get("heat_state", {}),
            }
        try:
            HEAT_CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.error("Unable to save heat config: %s", exc)

    def _apply_heat_targets(self) -> None:
        """Reapplique les consignes de chauffe après connexion."""
        if self.state.get("heat_auto", True):
            self._evaluate_heat_needs()
        else:
            self._update_heater_outputs()

    def _ensure_pump_defaults(self) -> None:
        defaults = {
            "X": {"name": "Eau osmosée (X)", "volume_ml": 10.0, "direction": 1},
            "Y": {"name": "Vidange (Y)", "volume_ml": 10.0, "direction": 1},
            "Z": {"name": "Additifs (Z)", "volume_ml": 10.0, "direction": 1},
            "E": {"name": "Nourrisseur (E)", "volume_ml": 10.0, "direction": 1},
        }
        with self.state_lock:
            pump_cfg = self.state.setdefault("pump_config", {})
            for axis, cfg in defaults.items():
                pump_cfg.setdefault(axis, cfg.copy())

    def _ensure_light_schedule_defaults(self) -> None:
        with self.state_lock:
            sched = self.state.setdefault("light_schedule", {})
            if "workdays" in sched or "weekend" in sched:
                work = sched.get("workdays", {})
                week = sched.get("weekend", {})
                for idx, day in enumerate(LIGHT_DAY_KEYS):
                    source = week if idx >= 5 else work
                    if source:
                        sched[day] = {
                            "on": source.get("on", "08:00"),
                            "off": source.get("off", "20:00"),
                        }
                sched.pop("workdays", None)
                sched.pop("weekend", None)
            for day in LIGHT_DAY_KEYS:
                sched.setdefault(day, {"on": "08:00", "off": "20:00"})

    def _init_light_gpio(self) -> None:
        if GPIO is None:
            logger.debug("RPi.GPIO not available; light relay disabled")
            self.light_gpio_ready = False
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(LIGHT_GPIO_PIN, GPIO.OUT, initial=GPIO.HIGH)
            self.light_gpio_ready = True
            logger.info("Light relay configured on GPIO %s", LIGHT_GPIO_PIN)
        except Exception as exc:
            self.light_gpio_ready = False
            logger.warning("Unable to configure GPIO %s: %s", LIGHT_GPIO_PIN, exc)

    def _init_pump_gpio(self) -> None:
        if GPIO is None:
            logger.debug("RPi.GPIO not available; pump relay disabled")
            self.pump_gpio_ready = False
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(
                PUMP_GPIO_PIN, GPIO.OUT, initial=GPIO.HIGH
            )  # NC contact: HIGH = ouvert = pompe OFF
            self.pump_gpio_ready = True
            logger.info("Pump relay configured on GPIO %s", PUMP_GPIO_PIN)
        except Exception as exc:
            self.pump_gpio_ready = False
            logger.warning("Unable to configure pump GPIO %s: %s", PUMP_GPIO_PIN, exc)

    def _drive_pump_gpio(self, enabled: bool) -> None:
        if not self.pump_gpio_ready or GPIO is None:
            return
        try:
            # NC câblé : pompe ON quand relais relâché (niveau haut). On choisit enabled=True <=> pompe ON.
            GPIO.output(PUMP_GPIO_PIN, GPIO.LOW if enabled else GPIO.HIGH)
        except Exception as exc:
            logger.error("Pump relay write failed: %s", exc)
            self.pump_gpio_ready = False

    def _init_heat_gpio(self) -> None:
        if GPIO is None:
            logger.debug("RPi.GPIO not available; heat relay disabled")
            self.heat_gpio_ready = False
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(HEAT_GPIO_PIN, GPIO.OUT, initial=GPIO.HIGH)  # relais NC
            self.heat_gpio_ready = True
            logger.info("Heat relay configured on GPIO %s", HEAT_GPIO_PIN)
        except Exception as exc:
            self.heat_gpio_ready = False
            logger.warning("Unable to configure heat GPIO %s: %s", HEAT_GPIO_PIN, exc)

    def _drive_heat_gpio(self, enabled: bool) -> None:
        if not self.heat_gpio_ready or GPIO is None:
            return
        try:
            GPIO.output(HEAT_GPIO_PIN, GPIO.LOW if enabled else GPIO.HIGH)
        except Exception as exc:
            logger.error("Heat relay write failed: %s", exc)
            self.heat_gpio_ready = False

    def _init_fan_gpio(self) -> None:
        if GPIO is None:
            logger.debug("RPi.GPIO not available; fan relay disabled")
            self.fan_gpio_ready = False
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(
                FAN_GPIO_PIN, GPIO.OUT, initial=GPIO.HIGH
            )  # default OFF (inversé)
            self.fan_gpio_ready = True
            logger.info("Fan relay configured on GPIO %s", FAN_GPIO_PIN)
        except Exception as exc:
            self.fan_gpio_ready = False
            logger.warning("Unable to configure fan GPIO %s: %s", FAN_GPIO_PIN, exc)

    def _drive_fan_gpio(self, enabled: bool) -> None:
        if not self.fan_gpio_ready or GPIO is None:
            return
        try:
            # Inversé : LOW = ventilateur ON (relais NC), HIGH = OFF
            GPIO.output(FAN_GPIO_PIN, GPIO.LOW if enabled else GPIO.HIGH)
        except Exception as exc:
            logger.error("Fan relay write failed: %s", exc)
            self.fan_gpio_ready = False

    def _evaluate_fan(self) -> None:
        with self.state_lock:
            auto = self.state.get("auto_fan", True)
            thresh = float(self.state.get("auto_thresh", 28.0) or 28.0)
            current = self.state.get("fan_on", False)
            t_water = self.state.get("tw")
        if not auto:
            # Manual mode: do nothing here
            return
        temp_val = self._parse_temperature_value(t_water)
        desired = False
        if temp_val is not None and temp_val >= thresh:
            desired = True
        if desired != current:
            with self.state_lock:
                self.state["fan_on"] = desired
                self.state["fan"] = 255 if desired else 0
            self._drive_fan_gpio(desired)

    def toggle_pump(self, state: Optional[bool] = None) -> None:
        with self.state_lock:
            if state is None:
                new_state = not self.state.get("pump_state", False)
            else:
                new_state = bool(state)
            self.state["pump_state"] = new_state
        self._drive_pump_gpio(new_state)

    def _drive_light_gpio(self, enabled: bool) -> None:
        if not self.light_gpio_ready or GPIO is None:
            return
        try:
            GPIO.output(LIGHT_GPIO_PIN, GPIO.LOW if enabled else GPIO.HIGH)
        except Exception as exc:
            logger.error("Light relay write failed: %s", exc)
            self.light_gpio_ready = False

    def _light_scheduler_loop(self) -> None:
        while True:
            try:
                self._tick_light_schedule()
            except Exception as exc:
                logger.error("Light scheduler error: %s", exc)
            time.sleep(30)

    def _telemetry_loop(self) -> None:
        while True:
            try:
                if self.connected:
                    now = time.time()
                    if now - self._last_temp_query > 2.0:
                        self._last_temp_query = now
                        try:
                            self.read_temps_once()
                        except Exception as exc:
                            logger.debug("TEMP? query failed: %s", exc)
                    if now - self._last_level_query > 5.0:
                        self._last_level_query = now
                        try:
                            self.read_levels_once()
                        except Exception as exc:
                            logger.debug("LEVEL? query failed: %s", exc)
                    self._evaluate_fan()
                time.sleep(1.0)
            except Exception as exc:
                logger.error("Telemetry loop error: %s", exc)
                time.sleep(2.0)

    def _tick_light_schedule(self) -> None:
        with self.state_lock:
            auto = self.state.get("light_auto", True)
            schedule = self.state.get("light_schedule", {})
        if not auto:
            return

        now = time.localtime()
        day_key = LIGHT_DAY_KEYS[now.tm_wday % len(LIGHT_DAY_KEYS)]
        zone = schedule.get(day_key)
        if not zone:
            return

        on_time = zone.get("on")
        off_time = zone.get("off")
        if not on_time or not off_time:
            return

        def to_minutes(val: str) -> Optional[int]:
            try:
                hh, mm = val.split(":", 1)
                return int(hh) * 60 + int(mm)
            except Exception:
                return None

        now_min = now.tm_hour * 60 + now.tm_min
        on_min = to_minutes(on_time)
        off_min = to_minutes(off_time)
        if on_min is None or off_min is None:
            return

        if on_min <= off_min:
            should_on = on_min <= now_min < off_min
        else:
            should_on = now_min >= on_min or now_min < off_min

        with self.state_lock:
            current = self.state.get("light_state", False)
        if should_on != current:
            logger.info("Light schedule toggling to %s for %s", should_on, day_key)
            self.toggle_light(should_on)

    # ---------- Serial helpers ----------
    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        logger.debug("<< %s", line)
        if line == "OK":
            self.response_queue.put(("OK", None))
            return
        if line.startswith("ERR"):
            payload = self._parse_error(line)
            self.last_error = payload
            self.response_queue.put(("ERR", payload))
            return
        if line.startswith("HELLO OK"):
            self._apply_status_line(line.split(";", 1)[1] if ";" in line else "")
            return
        if line.startswith("STATUS;"):
            self._apply_status_line(line.split(";", 1)[1])
            return
        if line.startswith("T_WATER"):
            self._apply_temp_line(line)
            return
        if line.startswith("LEVEL"):
            self._apply_level_line(line)
            return

    def _parse_error(self, line: str) -> Dict[str, Any]:
        if line.startswith("ERR|"):
            parts = line.split("|", 2)
            code = parts[1] if len(parts) > 1 else "UNKNOWN"
            message = parts[2] if len(parts) > 2 else ""
        elif line.startswith("ERR:"):
            code = "MEGA"
            message = line.split(":", 1)[1]
        else:
            code = "UNKNOWN"
            message = line
        return {
            "code": code,
            "message": message.strip(),
            "raw": line,
            "ts": time.time(),
        }

    def _apply_status_line(self, payload: str) -> None:
        entries = payload.split(";") if payload else []
        with self.state_lock:
            for entry in entries:
                if "=" not in entry:
                    continue
                key, value = entry.split("=", 1)
                key = key.lower()
                if key == "mtr":
                    self.state["motors_powered"] = value in ("1", "ON", "TRUE")
                elif key == "fan_val":
                    try:
                        val = int(float(value))
                        self.state["fan"] = val
                        self.state["fan_on"] = val > 0
                    except ValueError:
                        pass
                elif key == "auto_thresh":
                    try:
                        self.state["auto_thresh"] = float(value)
                    except ValueError:
                        pass
                elif key == "pidw_tgt":
                    try:
                        self.state["tset_water"] = float(value)
                    except ValueError:
                        pass
                elif key == "pidr_tgt":
                    try:
                        self.state["tset_res"] = float(value)
                    except ValueError:
                        pass
                elif key == "level_low":
                    self.state["lvl_low"] = value
                elif key == "level_high":
                    self.state["lvl_high"] = value
                elif key == "level_alert":
                    self.state["lvl_alert"] = value
                elif key == "tempw":
                    self.state["tw"] = self._sanitize_temp_text(
                        value, self.state.get("tw", "--.-")
                    )
                elif key == "tempa":
                    self.state["ta"] = self._sanitize_temp_text(
                        value, self.state.get("ta", "--.-")
                    )
                elif key == "tempaux":
                    self.state["tx"] = self._sanitize_temp_text(
                        value, self.state.get("tx", "--.-")
                    )
                elif key == "tempymin":
                    self.state["ty_min"] = self._sanitize_temp_text(
                        value, self.state.get("ty_min", "--.-")
                    )
                elif key == "tempymax":
                    self.state["ty_max"] = self._sanitize_temp_text(
                        value, self.state.get("ty_max", "--.-")
                    )
                elif key == "ph_v":
                    try:
                        self.state["ph_v"] = float(value)
                        self.state["ph"] = self._ph_from_voltage(self.state["ph_v"])
                    except ValueError:
                        pass
                elif key == "ph_raw":
                    try:
                        self.state["ph_raw"] = int(float(value))
                    except ValueError:
                        pass
                elif key == "servo":
                    try:
                        self.state["servo_angle"] = int(float(value))
                    except ValueError:
                        pass

    def _apply_temp_line(self, line: str) -> None:
        payload = line.replace("C", "")
        parts = payload.split("|")
        vals = {}
        for part in parts:
            if ":" in part:
                k, v = part.split(":", 1)
                vals[k.strip().lower()] = v.strip()
        with self.state_lock:
            self.state["tw"] = self._sanitize_temp_text(
                vals.get("t_water"), self.state.get("tw", "--.-")
            )
            self.state["ta"] = self._sanitize_temp_text(
                vals.get("t_air"), self.state.get("ta", "--.-")
            )
            self.state["tx"] = self._sanitize_temp_text(
                vals.get("t_aux"), self.state.get("tx", "--.-")
            )
            self.state["ty_min"] = self._sanitize_temp_text(
                vals.get("t_ymin"), self.state.get("ty_min", "--.-")
            )
            self.state["ty_max"] = self._sanitize_temp_text(
                vals.get("t_ymax"), self.state.get("ty_max", "--.-")
            )
            try:
                self.state["ph_v"] = float(vals.get("ph_v", self.state.get("ph_v")))
                self.state["ph"] = self._ph_from_voltage(self.state["ph_v"])
            except Exception:
                pass
            try:
                self.state["ph_raw"] = int(
                    float(vals.get("ph_raw", self.state.get("ph_raw")))
                )
            except Exception:
                pass
        self._evaluate_heat_needs()
        self._evaluate_fan()

    def _apply_level_line(self, line: str) -> None:
        tokens = line.replace("|", " ").split()
        kv = {}
        for token in tokens:
            if "=" in token:
                k, v = token.split("=", 1)
                kv[k.strip().lower()] = v.strip()
        with self.state_lock:
            self.state["lvl_low"] = kv.get("low", self.state["lvl_low"])
            self.state["lvl_high"] = kv.get("high", self.state["lvl_high"])
            self.state["lvl_alert"] = kv.get("alert", self.state["lvl_alert"])

    def _send_command(self, command: str, timeout: float = 2.0) -> None:
        if not self.connected:
            raise RuntimeError("Non connecté")
        logger.debug(">> %s", command)
        self.serial.write(command)
        try:
            status, payload = self.response_queue.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError("Commande sans réponse")
        if status != "OK":
            raise RuntimeError(payload.get("message", "Erreur Mega"))
        self.last_error = None

    def _update_heater_outputs(self) -> None:
        if not self.connected:
            return
        with self.state_lock:
            targets = self.state.get("heat_targets", {}).copy()
            states = self.state.get("heat_state", {}).copy()
        cmd_water = targets.get("water", 0.0) if states.get("water") else 0.0
        cmd_res = targets.get("reserve", 0.0) if states.get("reserve") else 0.0
        # Pilotage via relais GPIO (NC) : ON si une zone chauffe
        heat_on = cmd_water > 0 or cmd_res > 0
        self._drive_heat_gpio(heat_on)

    def _parse_temperature_value(self, raw: Any) -> Optional[float]:
        if raw is None:
            return None
        try:
            text = str(raw).strip().replace("°C", "").replace(",", ".")
            if text == "--.-":
                return None
            return float(text)
        except Exception:
            return None

    def _evaluate_heat_needs(self) -> None:
        with self.state_lock:
            if not self.state.get("heat_auto", True):
                return
            targets = self.state.get("heat_targets", {}).copy()
            temps = {
                "water": self.state.get("tw"),
                "reserve": self.state.get("tx"),
            }
            states = self.state.get("heat_state", {}).copy()
        hysteresis = 0.2
        updated = False
        for zone, temp_raw in temps.items():
            target = float(targets.get(zone, 0) or 0)
            temp_val = self._parse_temperature_value(temp_raw)
            if target <= 0 or temp_val is None:
                if states.get(zone):
                    states[zone] = False
                    updated = True
                continue
            if temp_val < target - hysteresis:
                if not states.get(zone):
                    states[zone] = True
                    updated = True
            elif temp_val > target + hysteresis:
                if states.get(zone):
                    states[zone] = False
                    updated = True
        if updated:
            with self.state_lock:
                self.state["heat_state"] = states
                self.state["heat_enabled"] = any(states.values())
            self._save_heat_config()
            self._update_heater_outputs()

    def _sanitize_temp_text(self, raw: Any, fallback: str) -> str:
        try:
            val = float(str(raw).replace(",", "."))
            if math.isnan(val) or math.isinf(val):
                return fallback
            return f"{val:.1f}"
        except Exception:
            return fallback

    def _ph_from_voltage(self, v: Optional[float]) -> Optional[float]:
        """Approximate pH from PH-4502C voltage. Assumes 2.5 V at pH 7, ~0.18 V/pH."""
        if v is None:
            return None
        try:
            val = float(v)
            if math.isnan(val) or math.isinf(val):
                return None
            return round(7.0 + (2.5 - val) / 0.18, 2)
        except Exception:
            return None

    def _send_query(self, command: str) -> None:
        if not self.connected:
            raise RuntimeError("Non connecté")
        logger.debug(">> %s", command)
        self.serial.write(command)

    # ---------- Connection ----------
    def connect(self, port: str) -> None:
        hello, status = self.serial.open(port)
        self.connected = True
        self.status_text = f"Connecté : {port}"
        self.last_error = None
        if ";" in status:
            self._apply_status_line(status.split(";", 1)[1])
        logger.info("Mega connecté (%s)", hello)
        self._apply_heat_targets()

    def disconnect(self) -> None:
        self.serial.close()
        self.connected = False
        self._last_temp_query = 0.0
        self._last_level_query = 0.0
        self.status_text = "Déconnecté"
        self._drive_heat_gpio(False)
        self._drive_fan_gpio(False)

    # ---------- Actions exposed to API ----------
    def read_temps_once(self) -> None:
        self._send_query("TEMP?")

    def read_levels_once(self) -> None:
        self._send_query("LEVEL?")

    def set_water(self, value: float) -> None:
        with self.state_lock:
            self.state["tset_water"] = value
            self.state["heat_targets"]["water"] = value
        self._save_heat_config()
        if self.state.get("heat_auto", True):
            self._evaluate_heat_needs()
        else:
            self._update_heater_outputs()

    def set_reserve(self, value: float) -> None:
        with self.state_lock:
            self.state["tset_res"] = value
            self.state["heat_targets"]["reserve"] = value
        self._save_heat_config()
        if self.state.get("heat_auto", True):
            self._evaluate_heat_needs()
        else:
            self._update_heater_outputs()

    def apply_pid_water(self, p: float, i: float, d: float) -> None:
        with self.state_lock:
            self.state["pidw"] = (p, i, d)
        self._send_command(f"PIDW P{p}I{i}D{d}")

    def apply_pid_res(self, p: float, i: float, d: float) -> None:
        with self.state_lock:
            self.state["pidr"] = (p, i, d)
        self._send_command(f"PIDR P{p}I{i}D{d}")

    def set_autocool(self, thresh: float) -> None:
        with self.state_lock:
            self.state["auto_thresh"] = thresh
            self.state["auto_fan"] = True
        self._evaluate_fan()

    def set_fan_manual(self, value: int) -> None:
        with self.state_lock:
            self.state["auto_fan"] = False
            self.state["fan_on"] = bool(value)
            self.state["fan"] = 255 if value else 0
        self._drive_fan_gpio(bool(value))

    def set_auto_fan(self, enable: bool) -> None:
        if enable:
            with self.state_lock:
                self.state["auto_fan"] = True
            self._evaluate_fan()
        else:
            with self.state_lock:
                self.state["auto_fan"] = False
                self.state["fan_on"] = False
                self.state["fan"] = 0
            self._drive_fan_gpio(False)

    def update_temp_names(self, names: Dict[str, str]) -> None:
        if not isinstance(names, dict):
            return
        allowed = {"water", "air", "aux", "ymin", "ymax"}
        with self.state_lock:
            current = self.state.setdefault("temp_names", {})
            for key, val in names.items():
                if key in allowed and isinstance(val, str) and val.strip():
                    current[key] = val.strip()
        self._save_temp_names()

    def set_heat_mode(self, auto: bool) -> None:
        with self.state_lock:
            self.state["heat_auto"] = auto
        self._save_heat_config()
        if auto:
            self._evaluate_heat_needs()

    def set_heat_power(self, enable: bool) -> None:
        with self.state_lock:
            if self.state.get("heat_auto", True) and not enable:
                raise RuntimeError("Désactiver impossible en mode automatique")
            self.state["heat_enabled"] = enable
            self.state["heat_state"]["water"] = enable
            self.state["heat_state"]["reserve"] = enable
        self._save_heat_config()
        self._update_heater_outputs()

    def toggle_protect(self, enable: bool) -> None:
        with self.state_lock:
            self.state["protect"] = enable

    def set_servo(self, angle: int) -> None:
        with self.state_lock:
            self.state["servo_angle"] = angle
        self._send_command(f"SERVO {angle}")

    def dispense_macro(self) -> None:
        self._send_command("SERVOFEED")

    def set_mtr_auto_off(self, enable: bool) -> None:
        with self.state_lock:
            self.state["mtr_auto_off"] = enable

    def set_steps_speed(self, steps: int, speed: int) -> None:
        with self.state_lock:
            self.steps_per_job = steps
            self.state["steps"] = steps
            self.state["speed"] = speed

    def set_global_speed(self, speed: int) -> None:
        with self.state_lock:
            self.global_speed = speed
            self.state["speed"] = speed

    def pump(self, axis: str, backwards: bool = False) -> None:
        axis = axis.upper()
        with self.state_lock:
            steps = self.steps_per_job
            speed = self.state["speed"] or self.global_speed
            auto_off = self.state["mtr_auto_off"]
            protect = self.state["protect"]
            low = self.state.get("lvl_low")
        if protect and str(low) in ("1", "LOW", "true"):
            raise RuntimeError("Niveau bas - pompe bloquée")
        signed_steps = -steps if backwards else steps
        self._send_command(f"PUMP {axis} {signed_steps} {max(speed, 50)}")
        if auto_off:
            threading.Thread(
                target=self._auto_motor_off_delay,
                args=(abs(steps), max(speed, 50)),
                daemon=True,
            ).start()

    def _auto_motor_off_delay(self, steps: int, speed: int) -> None:
        duration = (steps * speed * 2) / 1_000_000.0
        time.sleep(duration + 0.5)
        try:
            self._send_command("MTR OFF", timeout=1.0)
        except Exception:
            pass

    def emergency_stop(self) -> None:
        self._send_command("MTR OFF")

    def update_pump_config(
        self,
        axis: str,
        name: Optional[str] = None,
        volume_ml: Optional[float] = None,
        direction: Optional[int] = None,
    ) -> None:
        axis = axis.upper()
        with self.state_lock:
            cfg = self.state.setdefault("pump_config", {}).setdefault(
                axis, {"name": axis, "volume_ml": 10.0, "direction": 1}
            )
            if name:
                cfg["name"] = name
            if volume_ml is not None:
                cfg["volume_ml"] = volume_ml
            if direction in (1, -1):
                cfg["direction"] = direction
        self._save_pump_config()

    def update_light_schedule(
        self, day: str, on_time: Optional[str], off_time: Optional[str]
    ) -> None:
        if not day:
            raise ValueError("Jour manquant")
        key = day.strip().lower()
        if key not in LIGHT_DAY_KEYS:
            raise ValueError(f"Jour inconnu: {day}")
        with self.state_lock:
            schedule = self.state.setdefault("light_schedule", {})
            entry = schedule.setdefault(key, {"on": "08:00", "off": "20:00"})
            if on_time is not None:
                entry["on"] = on_time
            if off_time is not None:
                entry["off"] = off_time
        self._save_light_schedule()

    def toggle_light(self, state: Optional[bool] = None) -> None:
        if isinstance(state, str):
            state = state.strip().lower() in ("1", "true", "on")
        with self.state_lock:
            if state is None:
                self.state["light_state"] = not self.state["light_state"]
            else:
                self.state["light_state"] = state
        self._drive_light_gpio(self.state["light_state"])

    def set_light_auto(self, enable: bool) -> None:
        with self.state_lock:
            self.state["light_auto"] = enable

    def raw(self, cmd: str) -> None:
        self._send_command(cmd)

    # ---------- State ----------
    def get_state(self) -> Dict[str, Any]:
        with self.state_lock:
            payload = {
                "status": self.status_text,
                "connected": self.connected,
                "mega_error": self.last_error,
            }
            payload.update(self.state)
            payload["global_speed"] = self.global_speed
            payload["heat_targets"] = self.state.get("heat_targets", {}).copy()
        return payload


controller = ReefController()
