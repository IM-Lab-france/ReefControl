import json
import math
import logging
import logging.handlers
import os
import queue
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS, WriteApi
import serial
import serial.tools.list_ports
import requests

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:
    GPIO = None

BAUDRATE = 115200
HANDSHAKE_TIMEOUT = 4.0
BASE_DIR = Path(__file__).resolve().parent
PUMP_CONFIG_PATH = BASE_DIR / "pump_config.json"
LIGHT_SCHEDULE_PATH = BASE_DIR / "light_schedule.json"
HEAT_CONFIG_PATH = BASE_DIR / "heat_config.json"
FEEDER_CONFIG_PATH = BASE_DIR / "feeder_config.json"
VALUES_POST_PERIOD = 1.0
REQUEST_TIMEOUT = 3.0
VALUES_LOG_PATH = BASE_DIR / "telemetry_values.log"
EVENTS_LOG_PATH = BASE_DIR / "telemetry_events.log"
INFLUX_LOG_PATH = BASE_DIR / "telemetry_influx.log"
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


def _build_rotating_file_logger(name: str, path: str) -> logging.Logger:
    lgr = logging.getLogger(name)
    lgr.setLevel(logging.INFO)
    if not lgr.handlers:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=50 * 1024 * 1024, backupCount=1, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        lgr.addHandler(handler)
        lgr.propagate = False
    return lgr


telemetry_values_logger = _build_rotating_file_logger(
    "reef.telemetry.values", VALUES_LOG_PATH
)
telemetry_events_logger = _build_rotating_file_logger(
    "reef.telemetry.events", EVENTS_LOG_PATH
)
telemetry_influx_logger = _build_rotating_file_logger(
    "reef.telemetry.influx", INFLUX_LOG_PATH
)


INFLUXDB_URL = os.environ.get("INFLUXDB_URL")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET")
INFLUXDB_MEASUREMENT = os.environ.get("INFLUXDB_MEASUREMENT", "reef_controller")


class TelemetryPublisher:
    def __init__(self) -> None:
        self._client: Optional[InfluxDBClient] = None
        self._write_api: Optional[WriteApi] = None
        self.bucket = INFLUXDB_BUCKET
        self.org = INFLUXDB_ORG
        self.measurement = INFLUXDB_MEASUREMENT
        missing = [
            name
            for name, value in {
                "INFLUXDB_URL": INFLUXDB_URL,
                "INFLUXDB_TOKEN": INFLUXDB_TOKEN,
                "INFLUXDB_ORG": INFLUXDB_ORG,
                "INFLUXDB_BUCKET": INFLUXDB_BUCKET,
            }.items()
            if not value
        ]
        if missing:
            logger.error(
                "Variables d'environnement InfluxDB manquantes: %s", ", ".join(missing)
            )
            return
        try:
            self._client = InfluxDBClient(
                url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG
            )
            self._write_api = self._client.write_api(write_options=SYNCHRONOUS)
        except Exception as exc:
            logger.error("Impossible d'initialiser le client InfluxDB: %s", exc)
            self._client = None
            self._write_api = None

    def emit(
        self,
        metric: str,
        value: Optional[Any],
        *,
        category: str = "value",
        tags: Optional[Dict[str, Any]] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if (
            not metric
            or self._write_api is None
            or self.bucket is None
            or self.org is None
        ):
            return
        numeric_value = self._coerce_value(value)
        if numeric_value is None:
            return
        point = Point(self.measurement).tag("metric", metric).tag("category", category)
        if tags:
            for key, tag_value in tags.items():
                if tag_value is None:
                    continue
                point.tag(key, str(tag_value))
        if details:
            try:
                details_str = json.dumps(details, ensure_ascii=False)
            except Exception:
                details_str = str(details)
            point.field("details", details_str)
        point.field("value", numeric_value)
        try:
            self._write_api.write(bucket=self.bucket, org=self.org, record=point)
            telemetry_influx_logger.info(
                "INFLUX measurement=%s metric=%s category=%s tags=%s value=%s",
                self.measurement,
                metric,
                category,
                tags or {},
                numeric_value,
            )
        except Exception as exc:
            telemetry_influx_logger.error(
                "INFLUX measurement=%s metric=%s category=%s error=%s value=%s tags=%s",
                self.measurement,
                metric,
                category,
                exc,
                value,
                tags or {},
            )

    @staticmethod
    def _coerce_value(value: Optional[Any]) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return None


telemetry_publisher = TelemetryPublisher()


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
        self.telemetry = telemetry_publisher
        self.connected = False
        self.status_text = "Déconnecté"
        self.last_error: Optional[Dict[str, Any]] = None
        self.response_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.state_lock = threading.RLock()
        self.state: Dict[str, Any] = {
            "temp_1": "--.-",
            "temp_2": "--.-",
            "temp_3": "--.-",
            "temp_4": "--.-",
            "tset_water": 25.0,
            "tset_res": 30.0,
            "heat_hyst": 0.3,
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
            "peristaltic_state": {"X": False, "Y": False, "Z": False, "E": False},
            "light_state": False,
            "light_auto": True,
            "light_schedule": {
                day: {"on": "08:00", "off": "20:00"} for day in LIGHT_DAY_KEYS
            },
            "heat_targets": {"temp_1": 25.0, "temp_2": 30.0},
            "heat_auto": True,
            "heat_enabled": True,
            "heat_state": {"temp_1": True, "temp_2": True},
            "ph_v": None,
            "ph_raw": None,
            "ph": None,
            "pump_state": False,
            "fan_on": False,
            "temp_names": {
                "temp_1": "Temp 1",
                "temp_2": "Temp 2",
                "temp_3": "Temp 3",
                "temp_4": "Temp 4",
            },
            "feeder_auto": True,
            "feeder_schedule": [],
        }
        self.global_speed = 300
        self.steps_per_job = 1000
        self._load_configs()
        self._ensure_pump_defaults()
        self._ensure_light_schedule_defaults()
        self._load_heat_config()
        self._load_temp_names()
        self._load_feeder_config()
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
        self._last_status_query = 0.0
        self._last_values_push = 0.0
        self._last_auto_connect_attempt = 0.0
        self._last_feeder_runs: Dict[str, float] = {}
        self.light_scheduler = threading.Thread(
            target=self._light_scheduler_loop, daemon=True
        )
        self.light_scheduler.start()
        self.telemetry_thread = threading.Thread(
            target=self._telemetry_loop, daemon=True
        )
        self.telemetry_thread.start()
        self.feeder_scheduler = threading.Thread(
            target=self._feeder_scheduler_loop, daemon=True
        )
        self.feeder_scheduler.start()
        self._auto_connect_serial()

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

    def _load_feeder_config(self) -> None:
        if FEEDER_CONFIG_PATH.exists():
            try:
                data = json.loads(FEEDER_CONFIG_PATH.read_text("utf-8"))
                if isinstance(data, dict):
                    if isinstance(data.get("schedule"), list):
                        # Enrich with default method if absent to keep compat
                        schedule: list[Dict[str, Any]] = []
                        for entry in data["schedule"]:
                            if not isinstance(entry, dict):
                                continue
                            method = str(entry.get("method", "GET")).upper()
                            if method not in ("GET", "POST"):
                                method = "GET"
                            schedule.append(
                                {
                                    "time": entry.get("time", ""),
                                    "url": entry.get("url", ""),
                                    "method": method,
                                }
                            )
                        self.state["feeder_schedule"] = schedule
                    if "auto" in data:
                        self.state["feeder_auto"] = bool(data.get("auto", True))
            except Exception as exc:
                logger.error("Unable to load feeder config: %s", exc)

    def _save_feeder_config(self) -> None:
        try:
            FEEDER_CONFIG_PATH.write_text(
                json.dumps(
                    {
                        "auto": self.state.get("feeder_auto", True),
                        "schedule": self.state.get("feeder_schedule", []),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Unable to save feeder config: %s", exc)

    def _load_heat_config(self) -> None:
        if HEAT_CONFIG_PATH.exists():
            try:
                data = json.loads(HEAT_CONFIG_PATH.read_text("utf-8"))
                with self.state_lock:
                    if "targets" in data:
                        t = data["targets"]
                        if "water" in t:
                            t["temp_1"] = t.pop("water")
                        if "reserve" in t:
                            t["temp_2"] = t.pop("reserve")
                    if "targets" in data:
                        self.state["heat_targets"].update(data["targets"])
                        self.state["tset_water"] = self.state["heat_targets"].get(
                            "temp_1", self.state["tset_water"]
                        )
                        self.state["tset_res"] = self.state["heat_targets"].get(
                            "temp_2", self.state["tset_res"]
                        )
                    if "auto" in data:
                        self.state["heat_auto"] = bool(data["auto"])
                    if "enabled" in data:
                        self.state["heat_enabled"] = bool(data["enabled"])
                    if "state" in data:
                        st = data["state"]
                        if "water" in st:
                            st["temp_1"] = st.pop("water")
                        if "reserve" in st:
                            st["temp_2"] = st.pop("reserve")
                        self.state["heat_state"].update(st)
                    if "hyst" in data:
                        try:
                            self.state["heat_hyst"] = float(data["hyst"])
                        except Exception:
                            pass
            except Exception as exc:
                logger.error("Unable to read heat config: %s", exc)

    def _save_heat_config(self) -> None:
        with self.state_lock:
            payload = {
                "targets": self.state.get("heat_targets", {}),
                "auto": self.state.get("heat_auto", True),
                "enabled": self.state.get("heat_enabled", True),
                "state": self.state.get("heat_state", {}),
                "hyst": self.state.get("heat_hyst", 0.3),
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

    def _publish_value(
        self, metric: str, value: Optional[Any], tags: Optional[Dict[str, Any]] = None
    ) -> None:
        if value is None:
            return
        telemetry_values_logger.info(
            "VALUE metric=%s value=%s tags=%s", metric, value, tags or {}
        )
        if self.telemetry:
            self.telemetry.emit(metric, value, category="value", tags=tags)

    def _publish_event(
        self, name: str, details: Optional[Dict[str, Any]], category: str
    ) -> None:
        payload = {"ts": time.time(), "details": details or {}}
        try:
            payload_str = json.dumps(payload["details"], ensure_ascii=False)
        except Exception:
            payload_str = str(payload["details"])
        telemetry_events_logger.info(
            "EVENT category=%s name=%s details=%s", category, name, payload_str
        )
        if self.telemetry:
            self.telemetry.emit(name, 1.0, category=category, details=payload)

    def _publish_user_action(
        self, name: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        self._publish_event(name, details, "user_action")

    def _publish_automation(
        self, name: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        self._publish_event(name, details, "automation")

    def _publish_system_event(
        self, name: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        self._publish_event(name, details, "system")

    def _get_peristaltic_profile(self, axis: str) -> tuple[str, float]:
        axis_key = axis.upper()
        with self.state_lock:
            pump_cfg = {}
            cfg_root = self.state.get("pump_config", {})
            if isinstance(cfg_root, dict):
                pump_cfg = cfg_root.get(axis_key, {}) or {}
        name = str(pump_cfg.get("name", axis_key))
        volume_raw = pump_cfg.get("volume_ml", 0.0)
        try:
            volume = float(volume_raw or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        return name, volume

    def _record_peristaltic_dose(
        self,
        axis: str,
        *,
        source: str,
        backwards: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        name, volume = self._get_peristaltic_profile(axis)
        signed_volume = -volume if backwards else volume
        details: Dict[str, Any] = {
            "axis": axis.upper(),
            "name": name,
            "volume_ml": signed_volume,
        }
        if metadata:
            details.update(metadata)
        self._publish_value(
            "peristaltic_dose_ml",
            signed_volume,
            {"axis": axis.upper(), "name": name, "source": source},
        )
        return details

    def _evaluate_fan(self) -> None:
        with self.state_lock:
            auto = self.state.get("auto_fan", True)
            thresh = float(self.state.get("auto_thresh", 28.0) or 28.0)
            current = self.state.get("fan_on", False)
            t_water = self.state.get("temp_1")
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
            self._publish_automation(
                "fan_auto_toggle",
                {"from": current, "to": desired, "threshold": thresh},
            )

    def toggle_pump(self, state: Optional[bool] = None) -> None:
        with self.state_lock:
            prev_state = bool(self.state.get("pump_state", False))
            if state is None:
                new_state = not prev_state
            else:
                new_state = bool(state)
            self.state["pump_state"] = new_state
        self._drive_pump_gpio(new_state)
        self._publish_user_action(
            "pump_manual_toggle", {"from": prev_state, "to": new_state}
        )

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
                now = time.time()
                if self.connected:
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
                    if now - self._last_status_query > 5.0:
                        self._last_status_query = now
                        try:
                            self._send_query("STATUS?")
                        except Exception as exc:
                            logger.debug("STATUS? query failed: %s", exc)
                    self._evaluate_fan()
                else:
                    if now - self._last_auto_connect_attempt > 10.0:
                        self._last_auto_connect_attempt = now
                        self._auto_connect_serial()
                if now - self._last_values_push >= VALUES_POST_PERIOD:
                    self._last_values_push = now
                    self._post_values()
                time.sleep(1.0)
            except Exception as exc:
                logger.error("Telemetry loop error: %s", exc)
                time.sleep(2.0)

    def _build_temperature_payload(self) -> list[Dict[str, Any]]:
        with self.state_lock:
            temps = {
                "temp_1": self.state.get("temp_1"),
                "temp_2": self.state.get("temp_2"),
                "temp_3": self.state.get("temp_3"),
                "temp_4": self.state.get("temp_4"),
            }
            temp_names = self.state.get("temp_names", {}).copy()
        payload = []
        for key, raw in temps.items():
            payload.append(
                {
                    "key": key,
                    "name": temp_names.get(key, key),
                    "value": self._parse_temperature_value(raw),
                }
            )
        return payload

    def _feeder_scheduler_loop(self) -> None:
        while True:
            try:
                with self.state_lock:
                    auto = self.state.get("feeder_auto", True)
                    schedule = list(self.state.get("feeder_schedule", []))
                if auto and schedule:
                    now = time.localtime()
                    for entry in schedule:
                        if not isinstance(entry, dict):
                            continue
                        time_text = str(entry.get("time", "")).strip()
                        url = entry.get("url", "")
                        method = str(entry.get("method", "GET")).upper()
                        if method not in ("GET", "POST"):
                            method = "GET"
                        try:
                            hh, mm = time_text.split(":", 1)
                            hh_i = int(hh)
                            mm_i = int(mm)
                        except Exception:
                            continue
                        if not (0 <= hh_i < 24 and 0 <= mm_i < 60):
                            continue
                        if now.tm_hour != hh_i or now.tm_min != mm_i:
                            continue
                        key = f"{hh_i:02d}:{mm_i:02d}|{method}|{url}"
                        last_run = self._last_feeder_runs.get(key, 0)
                        # avoid double fire within same minute (loop runs every 10s)
                        if time.time() - last_run < 70:
                            continue
                        self._last_feeder_runs[key] = time.time()
                        if url:
                            url_norm = self._normalize_url(url)
                            telemetry_events_logger.info(
                                "Feeder scheduled trigger %s %s key=%s",
                                method,
                                url_norm,
                                key,
                            )
                            threading.Thread(
                                target=self._trigger_feeder_url,
                                args=(url_norm, key, method),
                                daemon=True,
                            ).start()
                time.sleep(10)
            except Exception as exc:
                logger.error("Feeder scheduler error: %s", exc)
                time.sleep(5)

    def _trigger_feeder_url(self, url: str, key: str, method: str = "GET") -> None:
        method_norm = method.upper() if isinstance(method, str) else "GET"
        if method_norm not in ("GET", "POST"):
            method_norm = "GET"
        origin = "user_action" if isinstance(key, str) and key.startswith("manual|") else "automation"
        try:
            if method_norm == "POST":
                resp = requests.post(url, timeout=REQUEST_TIMEOUT)
            else:
                resp = requests.get(url, timeout=REQUEST_TIMEOUT)
            telemetry_events_logger.info(
                "Feeder trigger %s %s status=%s key=%s",
                method_norm,
                url,
                resp.status_code,
                key,
            )
            details = {
                "url": url,
                "status": resp.status_code,
                "key": key,
                "method": method_norm,
            }
            if origin == "user_action":
                self._publish_user_action("feeder_trigger", details)
            else:
                self._publish_automation("feeder_trigger", details)
        except Exception as exc:
            telemetry_events_logger.error(
                "Feeder trigger error %s %s key=%s: %s", method_norm, url, key, exc
            )
            details = {
                "url": url,
                "key": key,
                "method": method_norm,
                "error": str(exc),
            }
            if origin == "user_action":
                self._publish_user_action("feeder_trigger_error", details)
            else:
                self._publish_automation("feeder_trigger_error", details)

    def _normalize_url(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme:
            return url
        return f"http://{url.lstrip('/')}"

    def _build_values_payload(self) -> Dict[str, Any]:
        with self.state_lock:
            levels = {
                "low": self.state.get("lvl_low"),
                "high": self.state.get("lvl_high"),
                "alert": self.state.get("lvl_alert"),
            }
            pump_active = bool(self.state.get("pump_state", False))
            motors_powered = bool(self.state.get("motors_powered", False))
            light_on = bool(self.state.get("light_state", False))
            heat_on = bool(self.state.get("heat_enabled", False))
            heat_state = self.state.get("heat_state", {}).copy()
            fan_on = bool(self.state.get("fan_on", False))
            fan_value = self.state.get("fan", 0)
            ph = self.state.get("ph")
            ph_v = self.state.get("ph_v")
            ph_raw = self.state.get("ph_raw")
            pump_cfg_raw = self.state.get("pump_config", {})
            pump_cfg = pump_cfg_raw.copy() if isinstance(pump_cfg_raw, dict) else {}
            peristaltic_state = self.state.get("peristaltic_state", {})
            peristaltic = []
            for axis_key in ("X", "Y", "Z", "E"):
                cfg = pump_cfg.get(axis_key, {}) if isinstance(pump_cfg, dict) else {}
                if not isinstance(cfg, dict):
                    cfg = {}
                powered = bool(peristaltic_state.get(axis_key, motors_powered))
                peristaltic.append(
                    {
                        "axis": axis_key,
                        "name": cfg.get("name", axis_key),
                        "powered": powered,
                    }
                )
        return {
            "ts": time.time(),
            "temperatures": self._build_temperature_payload(),
            "levels": levels,
            "pumps": {"main": pump_active, "motors_powered": motors_powered},
            "peristaltic": peristaltic,
            "relays": {"light": light_on, "heat": heat_on, "fan": fan_on},
            "heat_state": heat_state,
            "fan": {"on": fan_on, "value": fan_value},
            "ph": {"value": ph, "voltage": ph_v, "raw": ph_raw},
        }

    def _post_values(self) -> None:
        payload = self._build_values_payload()
        try:
            temperatures = payload.get("temperatures", [])
            if isinstance(temperatures, list):
                for entry in temperatures:
                    if not isinstance(entry, dict):
                        continue
                    self._publish_value(
                        "temperature_celsius",
                        entry.get("value"),
                        {
                            "sensor": str(entry.get("key", "")),
                            "name": str(entry.get("name", "")),
                        },
                    )

            ph_data = payload.get("ph", {})
            if isinstance(ph_data, dict):
                self._publish_value("ph_value", ph_data.get("value"))
                self._publish_value("ph_voltage", ph_data.get("voltage"))
                self._publish_value("ph_raw", ph_data.get("raw"))

            levels = payload.get("levels", {})
            if isinstance(levels, dict):
                for level_name, level_value in levels.items():
                    self._publish_value(
                        "water_level_state",
                        level_value,
                        {"sensor": str(level_name)},
                    )

            fan_data = payload.get("fan", {})
            if isinstance(fan_data, dict):
                self._publish_value("fan_pwm_value", fan_data.get("value"))
                self._publish_value("fan_state", 1 if fan_data.get("on") else 0)

            pumps = payload.get("pumps", {})
            if isinstance(pumps, dict):
                for pump_name, pump_state in pumps.items():
                    self._publish_value(
                        "pump_state",
                        1 if pump_state else 0,
                        {"pump": str(pump_name)},
                    )

            peristaltic = payload.get("peristaltic", [])
            if isinstance(peristaltic, list):
                for pump_entry in peristaltic:
                    if not isinstance(pump_entry, dict):
                        continue
                    self._publish_value(
                        "peristaltic_power_state",
                        1 if pump_entry.get("powered") else 0,
                        {
                            "axis": str(pump_entry.get("axis", "")),
                            "name": str(pump_entry.get("name", "")),
                        },
                    )

            relays = payload.get("relays", {})
            if isinstance(relays, dict):
                for relay_name, relay_state in relays.items():
                    self._publish_value(
                        "relay_state",
                        1 if relay_state else 0,
                        {"relay": str(relay_name)},
                    )

            heat_state = payload.get("heat_state", {})
            if isinstance(heat_state, dict):
                for zone, state in heat_state.items():
                    self._publish_value(
                        "heat_zone_state",
                        1 if state else 0,
                        {"zone": str(zone)},
                    )
        except Exception as exc:
            logger.error("Erreur lors de la préparation des mesures InfluxDB: %s", exc)

    def _auto_connect_serial(self) -> None:
        """Auto-connect to the first available Mega on common ACM ports."""
        if self.connected:
            return
        for port in ("/dev/ttyACM0", "/dev/ttyACM1"):
            try:
                if not Path(port).exists():
                    continue
                logger.info("Auto-connecting to %s", port)
                self.connect(port)
                logger.info("Auto-connected to %s", port)
                return
            except Exception as exc:
                logger.info("Auto-connect failed on %s: %s", port, exc)
        logger.info(
            "Auto-connect skipped: no /dev/ttyACM[0-1] detected or connection failed"
        )

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
            self._publish_automation(
                "light_schedule_toggle",
                {"from": current, "to": should_on, "day": day_key},
            )

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
                    prev = bool(self.state.get("motors_powered", False))
                    new_state = value in ("1", "ON", "TRUE")
                    self.state["motors_powered"] = new_state
                    if new_state != prev:
                        self._publish_system_event(
                            "pump_motor_state",
                            {"from": prev, "to": new_state, "source": "status_line"},
                        )
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
                    self.state["temp_1"] = self._sanitize_temp_text(
                        value, self.state.get("temp_1", "--.-")
                    )
                elif key == "tempa":
                    self.state["temp_3"] = self._sanitize_temp_text(
                        value, self.state.get("temp_3", "--.-")
                    )
                elif key == "tempymin":
                    self.state["temp_4"] = self._sanitize_temp_text(
                        value, self.state.get("temp_4", "--.-")
                    )
                elif key == "tempymax":
                    self.state["temp_2"] = self._sanitize_temp_text(
                        value, self.state.get("temp_2", "--.-")
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
                elif key in ("mtrx", "mtry", "mtrx", "mtre", "mtrz"):
                    axis_map = {"mtrx": "X", "mtry": "Y", "mtrz": "Z", "mtre": "E"}
                    axis_key = axis_map.get(key)
                    if axis_key:
                        prev = bool(self.state.get("peristaltic_state", {}).get(axis_key, False))
                        new_state = value in ("1", "ON", "TRUE", "true", "on")
                        self.state.setdefault("peristaltic_state", {})[axis_key] = new_state
                        if new_state != prev:
                            self._publish_system_event(
                                "peristaltic_state",
                                {"axis": axis_key, "from": prev, "to": new_state, "source": "status_line"},
                            )
                            if new_state:
                                details = self._record_peristaltic_dose(
                                    axis_key,
                                    source="automation",
                                    backwards=False,
                                    metadata={"reason": "status_line"},
                                )
                                self._publish_automation("peristaltic_auto_run", details)

    def _apply_temp_line(self, line: str) -> None:
        payload = line.replace("C", "")
        parts = payload.split("|")
        vals = {}
        for part in parts:
            if ":" in part:
                k, v = part.split(":", 1)
                vals[k.strip().lower()] = v.strip()
        with self.state_lock:
            self.state["temp_1"] = self._sanitize_temp_text(
                vals.get("t_water"), self.state.get("temp_1", "--.-")
            )
            self.state["temp_3"] = self._sanitize_temp_text(
                vals.get("t_air"), self.state.get("temp_3", "--.-")
            )
            self.state["temp_4"] = self._sanitize_temp_text(
                vals.get("t_ymin"), self.state.get("temp_4", "--.-")
            )
            self.state["temp_2"] = self._sanitize_temp_text(
                vals.get("t_ymax"), self.state.get("temp_2", "--.-")
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
        cmd_water = targets.get("temp_1", 0.0) if states.get("temp_1") else 0.0
        cmd_res = targets.get("temp_2", 0.0) if states.get("temp_2") else 0.0
        # Pilotage via relais GPIO (NC) : ON si temp_1 chauffe
        heat_on = cmd_water > 0
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
                "temp_1": self.state.get("temp_1"),
                "temp_2": self.state.get("temp_2"),
            }
            states = self.state.get("heat_state", {}).copy()
        hysteresis = float(self.state.get("heat_hyst", 0.3) or 0.3)
        updated = False
        prev_states = states.copy()
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
            self._publish_automation(
                "heat_auto_toggle",
                {
                    "from": prev_states,
                    "to": states,
                    "heat_enabled": any(states.values()),
                    "hysteresis": hysteresis,
                },
            )

    def set_heat_hyst(self, value: float) -> None:
        with self.state_lock:
            self.state["heat_hyst"] = value
        self._save_heat_config()
        self._evaluate_heat_needs()
        self._publish_user_action("heat_hysteresis_update", {"value": value})

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
        self._publish_system_event("serial_connect", {"port": port})

    def disconnect(self) -> None:
        port = self.serial.port
        self.serial.close()
        self.connected = False
        self._last_temp_query = 0.0
        self._last_level_query = 0.0
        self.status_text = "Déconnecté"
        self._drive_heat_gpio(False)
        self._drive_fan_gpio(False)
        self._publish_system_event("serial_disconnect", {"port": port})

    # ---------- Actions exposed to API ----------
    def read_temps_once(self) -> None:
        self._send_query("TEMP?")

    def read_levels_once(self) -> None:
        self._send_query("LEVEL?")

    def set_water(self, value: float) -> None:
        with self.state_lock:
            self.state["tset_water"] = value
            self.state["heat_targets"]["temp_1"] = value
        self._save_heat_config()
        if self.state.get("heat_auto", True):
            self._evaluate_heat_needs()
        else:
            self._update_heater_outputs()
        self._publish_user_action("heat_target_water_update", {"value": value})

    def set_reserve(self, value: float) -> None:
        with self.state_lock:
            self.state["tset_res"] = value
            self.state["heat_targets"]["temp_2"] = value
        self._save_heat_config()
        if self.state.get("heat_auto", True):
            self._evaluate_heat_needs()
        else:
            self._update_heater_outputs()
        self._publish_user_action("heat_target_reserve_update", {"value": value})

    def set_autocool(self, thresh: float) -> None:
        with self.state_lock:
            self.state["auto_thresh"] = thresh
            self.state["auto_fan"] = True
        self._evaluate_fan()
        self._publish_user_action("fan_auto_threshold_update", {"threshold": thresh})

    def set_fan_manual(self, value: int) -> None:
        with self.state_lock:
            prev = bool(self.state.get("fan_on", False))
            self.state["auto_fan"] = False
            self.state["fan_on"] = bool(value)
            self.state["fan"] = 255 if value else 0
            new = self.state["fan_on"]
        self._drive_fan_gpio(bool(value))
        self._publish_user_action(
            "fan_manual_toggle", {"from": prev, "to": new, "value": int(bool(value))}
        )

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
        self._publish_user_action("fan_auto_mode_update", {"enable": enable})

    def update_temp_names(self, names: Dict[str, str]) -> None:
        if not isinstance(names, dict):
            return
        allowed = {"temp_1", "temp_2", "temp_3", "temp_4"}
        with self.state_lock:
            current = self.state.setdefault("temp_names", {})
            for key, val in names.items():
                if key in allowed and isinstance(val, str) and val.strip():
                    current[key] = val.strip()
        self._save_temp_names()
        self._publish_user_action(
            "temperature_names_update", {"names": {k: v for k, v in names.items() if k in allowed}}
        )

    def set_heat_mode(self, auto: bool) -> None:
        with self.state_lock:
            self.state["heat_auto"] = auto
        self._save_heat_config()
        if auto:
            self._evaluate_heat_needs()
        self._publish_user_action("heat_mode_update", {"auto": auto})

    def set_heat_power(self, enable: bool) -> None:
        with self.state_lock:
            prev = bool(self.state.get("heat_enabled", False))
            if self.state.get("heat_auto", True) and not enable:
                raise RuntimeError("Désactiver impossible en mode automatique")
            self.state["heat_enabled"] = enable
            self.state["heat_state"]["water"] = enable
            self.state["heat_state"]["reserve"] = enable
            new = bool(self.state["heat_enabled"])
        self._save_heat_config()
        self._update_heater_outputs()
        self._publish_user_action("heat_manual_toggle", {"from": prev, "to": new})

    def toggle_protect(self, enable: bool) -> None:
        with self.state_lock:
            self.state["protect"] = enable
        self._publish_user_action("protect_mode_update", {"enable": enable})

    def set_servo(self, angle: int) -> None:
        with self.state_lock:
            self.state["servo_angle"] = angle
        self._send_command(f"SERVO {angle}")
        self._publish_user_action("servo_angle_set", {"angle": angle})

    def dispense_macro(self) -> None:
        self._send_command("SERVOFEED")
        self._publish_user_action("servo_macro_dispense", None)

    def set_mtr_auto_off(self, enable: bool) -> None:
        with self.state_lock:
            self.state["mtr_auto_off"] = enable
        self._publish_user_action("motor_auto_off_update", {"enable": enable})

    def set_steps_speed(self, steps: int, speed: int) -> None:
        with self.state_lock:
            self.steps_per_job = steps
            self.state["steps"] = steps
            self.state["speed"] = speed
        self._publish_user_action(
            "pump_steps_speed_update", {"steps": steps, "speed": speed}
        )

    def set_global_speed(self, speed: int) -> None:
        with self.state_lock:
            self.global_speed = speed
            self.state["speed"] = speed
        self._publish_user_action("global_speed_update", {"speed": speed})

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
        command_speed = max(speed, 50)
        self._send_command(f"PUMP {axis} {signed_steps} {command_speed}")
        if auto_off:
            threading.Thread(
                target=self._auto_motor_off_delay,
                args=(abs(steps), command_speed),
                daemon=True,
            ).start()
        dose_details = self._record_peristaltic_dose(
            axis,
            source="user",
            backwards=backwards,
            metadata={
                "backwards": backwards,
                "steps": steps,
                "speed": command_speed,
                "auto_off": auto_off,
            },
        )
        self._publish_user_action("pump_manual_run", dose_details)

    def _auto_motor_off_delay(self, steps: int, speed: int) -> None:
        duration = (steps * speed * 2) / 1_000_000.0
        time.sleep(duration + 0.5)
        try:
            self._send_command("MTR OFF", timeout=1.0)
        except Exception:
            pass

    def emergency_stop(self) -> None:
        self._send_command("MTR OFF")
        self._publish_user_action("emergency_stop", None)

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
        self._publish_user_action(
            "pump_config_update",
            {
                "axis": axis,
                "name": name,
                "volume_ml": volume_ml,
                "direction": direction,
            },
        )

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
        self._publish_user_action(
            "light_schedule_update",
            {"day": key, "on": on_time, "off": off_time},
        )

    def toggle_light(
        self, state: Optional[bool] = None, event_type: Optional[str] = None
    ) -> None:
        if isinstance(state, str):
            state = state.strip().lower() in ("1", "true", "on")
        with self.state_lock:
            prev = bool(self.state.get("light_state", False))
            if state is None:
                self.state["light_state"] = not prev
            else:
                self.state["light_state"] = bool(state)
            new = bool(self.state["light_state"])
        self._drive_light_gpio(self.state["light_state"])
        if event_type:
            self._publish_user_action(event_type, {"from": prev, "to": new})

    def set_light_auto(self, enable: bool) -> None:
        with self.state_lock:
            self.state["light_auto"] = enable
        self._publish_user_action("light_auto_mode_update", {"enable": enable})

    def set_feeder_auto(self, enable: bool) -> None:
        with self.state_lock:
            self.state["feeder_auto"] = bool(enable)
        self._save_feeder_config()
        self._publish_user_action("feeder_auto_mode_update", {"enable": enable})

    def update_feeder_schedule(self, entries: list[Dict[str, Any]]) -> None:
        valid = []
        if isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                time_str = str(item.get("time", "")).strip()
                url_str = str(item.get("url", "")).strip()
                method = str(item.get("method", "GET")).upper()
                if not time_str or not url_str:
                    continue
                # simple validation HH:MM
                try:
                    hh, mm = time_str.split(":", 1)
                    hh_i = int(hh)
                    mm_i = int(mm)
                    if not (0 <= hh_i < 24 and 0 <= mm_i < 60):
                        continue
                except Exception:
                    continue
                if method not in ("GET", "POST"):
                    method = "GET"
                valid.append(
                    {"time": f"{hh_i:02d}:{mm_i:02d}", "url": url_str, "method": method}
                )
        with self.state_lock:
            self.state["feeder_schedule"] = valid
        self._save_feeder_config()
        self._publish_user_action(
            "feeder_schedule_update", {"count": len(valid), "entries": valid}
        )

    def trigger_feeder_url(self, url: str, method: str = "GET") -> None:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("URL manquante")
        method_norm = method.upper() if isinstance(method, str) else "GET"
        if method_norm not in ("GET", "POST"):
            method_norm = "GET"
        clean_url = url.strip()
        self._trigger_feeder_url(clean_url, f"manual|{method_norm}|{clean_url}", method_norm)

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
