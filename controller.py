import json
import math
import logging
import logging.handlers
import os
import queue
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import WriteApi, WriteOptions
import serial
import serial.tools.list_ports
import requests
import openai

try:
    import RPi.GPIO as GPIO  # type: ignore
except Exception:
    GPIO = None

try:
    import board  # type: ignore
    import busio  # type: ignore
    import adafruit_tsl2591  # type: ignore

    HAS_TSL2591 = True
except Exception:
    board = None
    busio = None
    adafruit_tsl2591 = None
    HAS_TSL2591 = False

BAUDRATE = 115200
HANDSHAKE_TIMEOUT = 4.0
BASE_DIR = Path(__file__).resolve().parent
PUMP_CONFIG_PATH = BASE_DIR / "pump_config.json"
LIGHT_SCHEDULE_PATH = BASE_DIR / "light_schedule.json"
HEAT_CONFIG_PATH = BASE_DIR / "heat_config.json"
FEEDER_CONFIG_PATH = BASE_DIR / "feeder_config.json"
PERISTALTIC_SCHEDULE_PATH = BASE_DIR / "peristaltic_schedule.json"
PERISTALTIC_LAST_RUNS_PATH = BASE_DIR / "peristaltic_last_runs.json"
CONTROL_FILE_PATH = BASE_DIR / "control.txt"
VALUES_POST_PERIOD = 10.0
REQUEST_TIMEOUT = 3.0
VALUES_LOG_PATH = BASE_DIR / "telemetry_values.log"
EVENTS_LOG_PATH = BASE_DIR / "telemetry_events.log"
INFLUX_LOG_PATH = BASE_DIR / "telemetry_influx.log"
PUMP_GPIO_PIN = 22
FAN_GPIO_PIN = 23
HEAT_GPIO_PIN = 24  # relais chauffe eau
TEMP_NAMES_PATH = Path("temp_names.json")
LIGHT_GPIO_PIN = 27
LIGHT_QUERY_PERIOD = 6.0
LEVEL_HIGH_GPIO_PIN = 25
LIGHT_DAY_KEYS = [
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
]
OPENAI_KEY_FILE_PATH = BASE_DIR / ".openai_api_key"
PERISTALTIC_STEPS_PER_ML = 5000
DEFAULT_FEEDER_STOP_PUMP = False
DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN = 5

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
            write_options = WriteOptions(
                batch_size=500, flush_interval=10_000, jitter_interval=2_000
            )
            self._write_api = self._client.write_api(write_options=write_options)
        except Exception as exc:
            logger.error("Impossible d'initialiser le client InfluxDB: %s", exc)
            self._client = None
            self._write_api = None

    def close(self) -> None:
        if self._client:
            self._client.close()

    def emit(
        self,
        measurement: str,
        tags: Dict[str, Any],
        fields: Dict[str, Any],
    ) -> None:
        if not all([measurement, tags, fields, self._write_api, self.bucket, self.org]):
            return

        point = Point(measurement)
        for key, value in tags.items():
            if value is not None:
                point.tag(str(key), str(value))

        valid_fields = False
        for key, value in fields.items():
            coerced_value = self._coerce_field_value(value)
            if coerced_value is not None:
                point.field(str(key), coerced_value)
                valid_fields = True

        if not valid_fields:
            return

        try:
            self._write_api.write(bucket=self.bucket, org=self.org, record=point)
            telemetry_influx_logger.info(
                "INFLUX measurement=%s tags=%s fields=%s",
                measurement,
                tags,
                fields,
            )
        except Exception as exc:
            telemetry_influx_logger.error(
                "INFLUX measurement=%s tags=%s fields=%s error=%s",
                measurement,
                tags,
                fields,
                exc,
            )

    @staticmethod
    def _coerce_field_value(
        value: Any,
    ) -> Optional[Union[float, int, bool, str]]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return value
        try:
            return float(value)
        except (TypeError, ValueError):
            try:
                return str(value)
            except Exception:
                return None


telemetry_publisher = TelemetryPublisher()


class LightSensorTSL2591:
    def __init__(self) -> None:
        if (
            not HAS_TSL2591
            or board is None
            or busio is None
            or adafruit_tsl2591 is None
        ):
            raise RuntimeError("TSL2591 library not available")
        self._i2c = busio.I2C(board.SCL, board.SDA)
        self._sensor = adafruit_tsl2591.TSL2591(self._i2c)

    def read_lux(self) -> Optional[float]:
        try:
            lux = self._sensor.lux
            if lux is None:
                return None
            return float(lux)
        except Exception:
            return None


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
    OPENAI_KEY_MISSING_ERROR = "OPENAI_API_KEY_MISSING"

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
            "peristaltic_auto": True,
            "peristaltic_schedule": {
                "X": {"time": None},
                "Y": {"time": None},
                "Z": {"time": None},
                "E": {"time": None},
            },
            "light_lux": None,
        }
        self._openai_api_key: Optional[str] = None
        self.global_speed = 300
        self.steps_per_job = 1000
        self._light_sensor: Optional[LightSensorTSL2591] = None
        self._last_light_query = 0.0
        self._load_configs()
        self._ensure_pump_defaults()
        self._ensure_light_schedule_defaults()
        self._load_heat_config()
        self._load_temp_names()
        self._load_feeder_config()
        self._load_peristaltic_schedule()
        self._ensure_peristaltic_schedule_defaults()
        self._peristaltic_runs_lock = threading.Lock()
        self._peristaltic_last_runs: Dict[str, Optional[str]] = {
            axis: None for axis in ("X", "Y", "Z", "E")
        }
        self._load_peristaltic_last_runs()
        self.light_gpio_ready = False
        self.pump_gpio_ready = False
        self.fan_gpio_ready = False
        self.heat_gpio_ready = False
        self.level_gpio_ready = False
        self._init_light_gpio()
        self._init_pump_gpio()
        self._init_fan_gpio()
        self._init_heat_gpio()
        self._init_level_gpio()
        self._drive_pump_gpio(self.state.get("pump_state", False))
        self._drive_fan_gpio(self.state.get("fan", 0) > 0)
        self._drive_heat_gpio(self.state.get("heat_enabled", False))
        self._update_high_level_state()
        self._last_temp_query = 0.0
        self._last_level_query = 0.0
        self._last_status_query = 0.0
        self._last_values_push = 0.0
        self._last_auto_connect_attempt = 0.0
        self._last_feeder_runs: Dict[str, float] = {}
        self._last_peristaltic_runs: Dict[str, float] = {}
        if HAS_TSL2591:
            try:
                self._light_sensor = LightSensorTSL2591()
                logger.info("Capteur TSL2591 initialisé")
            except Exception as exc:
                self._light_sensor = None
                logger.warning("Impossible d'initialiser le capteur TSL2591: %s", exc)
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
        self.peristaltic_scheduler = threading.Thread(
            target=self._peristaltic_scheduler_loop, daemon=True
        )
        self.peristaltic_scheduler.start()
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
                            stop_pump = bool(
                                entry.get("stop_pump", DEFAULT_FEEDER_STOP_PUMP)
                            )
                            duration = self._sanitize_pump_stop_duration(
                                entry.get(
                                    "pump_stop_duration_min",
                                    (
                                        DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN
                                        if stop_pump
                                        else 0
                                    ),
                                )
                            )
                            if stop_pump and duration == 0:
                                duration = DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN
                            schedule.append(
                                {
                                    "time": entry.get("time", ""),
                                    "url": entry.get("url", ""),
                                    "method": method,
                                    "stop_pump": stop_pump,
                                    "pump_stop_duration_min": duration,
                                }
                            )
                        self.state["feeder_schedule"] = schedule
                    if "auto" in data:
                        self.state["feeder_auto"] = bool(data.get("auto", True))
            except Exception as exc:
                logger.error("Unable to load feeder config: %s", exc)

    def _save_feeder_config(self) -> None:
        with self.state_lock:
            auto = bool(self.state.get("feeder_auto", True))
            existing_schedule = list(self.state.get("feeder_schedule", []))
        schedule: list[Dict[str, Any]] = []
        for entry in existing_schedule:
            if not isinstance(entry, dict):
                continue
            method = str(entry.get("method", "GET")).upper()
            if method not in ("GET", "POST"):
                method = "GET"
            stop_pump = bool(entry.get("stop_pump", DEFAULT_FEEDER_STOP_PUMP))
            duration = self._sanitize_pump_stop_duration(
                entry.get(
                    "pump_stop_duration_min",
                    DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN if stop_pump else 0,
                )
            )
            if stop_pump and duration == 0:
                duration = DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN
            schedule.append(
                {
                    "time": entry.get("time", ""),
                    "url": entry.get("url", ""),
                    "method": method,
                    "stop_pump": stop_pump,
                    "pump_stop_duration_min": duration,
                }
            )
        try:
            FEEDER_CONFIG_PATH.write_text(
                json.dumps(
                    {
                        "auto": auto,
                        "schedule": schedule,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error("Unable to save feeder config: %s", exc)

    def _normalize_time_string(
        self, value: Optional[Union[str, int, float]]
    ) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text or ":" not in text:
            return None
        try:
            hh_text, mm_text = text.split(":", 1)
            hh = int(hh_text)
            mm = int(mm_text)
        except Exception:
            return None
        if not (0 <= hh < 24 and 0 <= mm < 60):
            return None
        return f"{hh:02d}:{mm:02d}"

    def _sanitize_pump_stop_duration(self, value: Any) -> int:
        try:
            duration = int(value)
        except (TypeError, ValueError):
            return 0
        if duration < 0:
            return 0
        return duration

    def _load_peristaltic_schedule(self) -> None:
        if not PERISTALTIC_SCHEDULE_PATH.exists():
            return
        try:
            data = json.loads(PERISTALTIC_SCHEDULE_PATH.read_text("utf-8"))
        except Exception as exc:
            logger.error("Unable to load peristaltic schedule: %s", exc)
            return
        if not isinstance(data, dict):
            return
        if "auto" in data:
            self.state["peristaltic_auto"] = bool(data.get("auto", True))
        raw_schedule = data.get("schedule", {})
        if not isinstance(raw_schedule, dict):
            return
        schedule: Dict[str, Dict[str, Optional[str]]] = {}
        for axis, entry in raw_schedule.items():
            axis_key = str(axis).upper()
            if isinstance(entry, dict):
                normalized = self._normalize_time_string(entry.get("time"))
            else:
                normalized = self._normalize_time_string(entry)
            schedule[axis_key] = {"time": normalized}
        self.state["peristaltic_schedule"] = schedule

    def _save_peristaltic_schedule(self) -> None:
        with self.state_lock:
            payload = {
                "auto": self.state.get("peristaltic_auto", True),
                "schedule": self.state.get("peristaltic_schedule", {}),
            }
        try:
            PERISTALTIC_SCHEDULE_PATH.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error("Unable to save peristaltic schedule: %s", exc)

    def _ensure_peristaltic_schedule_defaults(self) -> None:
        with self.state_lock:
            schedule = self.state.setdefault("peristaltic_schedule", {})
            for axis in ("X", "Y", "Z", "E"):
                entry = schedule.get(axis)
                if not isinstance(entry, dict):
                    schedule[axis] = {"time": None}
                else:
                    entry.setdefault("time", None)

    def _load_peristaltic_last_runs(self) -> None:
        if not PERISTALTIC_LAST_RUNS_PATH.exists():
            return
        try:
            data = json.loads(PERISTALTIC_LAST_RUNS_PATH.read_text("utf-8"))
        except Exception as exc:
            logger.error("Unable to load peristaltic last runs: %s", exc)
            return
        if not isinstance(data, dict):
            return
        with self._peristaltic_runs_lock:
            for axis in ("X", "Y", "Z", "E"):
                value = data.get(axis)
                normalized = self._normalize_time_string(value)
                self._peristaltic_last_runs[axis] = normalized

    def _save_peristaltic_last_runs(self) -> None:
        with self._peristaltic_runs_lock:
            payload = {
                axis: self._peristaltic_last_runs.get(axis)
                for axis in ("X", "Y", "Z", "E")
            }
        try:
            PERISTALTIC_LAST_RUNS_PATH.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            logger.error("Unable to save peristaltic last runs: %s", exc)

    def _current_minute_label(self) -> str:
        return time.strftime("%H:%M", time.localtime())

    def _ensure_peristaltic_not_recent(self, axis: str, minute_label: str) -> None:
        normalized = self._normalize_time_string(minute_label)
        if not normalized:
            return
        with self._peristaltic_runs_lock:
            last = self._peristaltic_last_runs.get(axis.upper())
            if last == normalized:
                raise RuntimeError(
                    f"Pompe {axis.upper()} déjà déclenchée à {normalized}, attendre la minute suivante."
                )

    def _record_peristaltic_run_label(self, axis: str, minute_label: str) -> None:
        normalized = self._normalize_time_string(minute_label)
        if not normalized:
            return
        axis_key = axis.upper()
        changed = False
        with self._peristaltic_runs_lock:
            if self._peristaltic_last_runs.get(axis_key) != normalized:
                self._peristaltic_last_runs[axis_key] = normalized
                changed = True
        if changed:
            self._save_peristaltic_last_runs()

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

    def _pause_requested(self) -> bool:
        try:
            if not CONTROL_FILE_PATH.exists():
                return False
            content = CONTROL_FILE_PATH.read_text(encoding="utf-8").strip().lower()
            return "stop" in content
        except Exception:
            return False

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

    def _init_level_gpio(self) -> None:
        if GPIO is None:
            logger.debug("RPi.GPIO not available; level sensor disabled")
            self.level_gpio_ready = False
            return
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(LEVEL_HIGH_GPIO_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
            self.level_gpio_ready = True
            logger.info("High level sensor configured on GPIO %s", LEVEL_HIGH_GPIO_PIN)
        except Exception as exc:
            self.level_gpio_ready = False
            logger.warning(
                "Unable to configure high level GPIO %s: %s", LEVEL_HIGH_GPIO_PIN, exc
            )

    def _read_high_level_gpio(
        self, samples: int = 5, delay_s: float = 0.02
    ) -> Optional[bool]:
        if not self.level_gpio_ready or GPIO is None:
            return None
        try:
            first_read = GPIO.input(LEVEL_HIGH_GPIO_PIN)
            for _ in range(samples - 1):
                time.sleep(delay_s)
                value = GPIO.input(LEVEL_HIGH_GPIO_PIN)
                if value != first_read:
                    logger.debug("High level sensor value is unstable (debouncing).")
                    return None
            return not bool(first_read)
        except Exception as exc:
            logger.error("High level GPIO read failed: %s", exc)
            self.level_gpio_ready = False
            return None

    def _update_high_level_state(self) -> None:
        level = self._read_high_level_gpio()
        if level is None:
            return
        new_value = "1" if level else "0"
        with self.state_lock:
            prev = self.state.get("lvl_high")
            self.state["lvl_high"] = new_value
        if prev != new_value:
            self._publish_device_event(
                device_type="level",
                device_id="high",
                source="gpio",
                fields={"state": level},
            )

    def _publish_sensor_reading(
        self, sensor_id: str, sensor_name: str, fields: Dict[str, Any]
    ) -> None:
        """Publie une lecture de capteur vers InfluxDB."""
        telemetry_values_logger.info(
            "SENSOR sensor_id=%s sensor_name=%s fields=%s",
            sensor_id,
            sensor_name,
            fields,
        )
        if self.telemetry:
            self.telemetry.emit(
                measurement="sensor_readings",
                tags={"sensor_id": sensor_id, "sensor_name": sensor_name},
                fields=fields,
            )

    def _publish_device_event(
        self, device_type: str, device_id: str, source: str, fields: Dict[str, Any]
    ) -> None:
        """Publie un événement d'appareil vers InfluxDB."""

        # On duplique les booléens sur des champs *_int pour éviter les conflits de type
        payload: Dict[str, Any] = {}
        for key, val in fields.items():
            if isinstance(val, bool):
                payload[f"{key}_int"] = 1 if val else 0
            else:
                payload[key] = val

        telemetry_events_logger.info(
            "DEVICE device_type=%s device_id=%s source=%s fields=%s",
            device_type,
            device_id,
            source,
            payload,
        )
        if self.telemetry:
            self.telemetry.emit(
                measurement="device_events",
                tags={
                    "device_type": device_type,
                    "device_id": device_id,
                    "source": source,
                },
                fields=payload,
            )

    def _publish_setting_change(
        self, setting_group: str, setting_name: str, value: Any
    ) -> None:
        """Publie un changement de paramètre vers InfluxDB."""
        fields: Dict[str, Any] = {}

        if isinstance(value, bool):
            fields["value_bool"] = value
        elif isinstance(value, (int, float)):
            try:
                fields["value_float"] = float(value)
            except Exception:
                try:
                    fields["value_string"] = str(value)
                except Exception:
                    return
        else:
            try:
                fields["value_string"] = str(value)
            except Exception:
                return
        telemetry_events_logger.info(
            "SETTING group=%s name=%s value=%s", setting_group, setting_name, value
        )
        if self.telemetry and fields:
            self.telemetry.emit(
                measurement="settings",
                tags={"setting_group": setting_group, "setting_name": setting_name},
                fields=fields,
            )

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
            self._publish_device_event(
                device_type="fan",
                device_id="main_fan",
                source="automation",
                fields={
                    "state": desired,
                    "trigger_temp": temp_val,
                    "threshold": thresh,
                },
            )

    def toggle_pump(self, state: Optional[bool] = None, source: str = "user") -> None:
        with self.state_lock:
            prev_state = bool(self.state.get("pump_state", False))
            if state is None:
                new_state = not prev_state
            else:
                new_state = bool(state)
            self.state["pump_state"] = new_state
        self._drive_pump_gpio(new_state)
        self._publish_device_event(
            device_type="pump",
            device_id="main",
            source=source,
            fields={"state": new_state, "previous_state": prev_state},
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
                if self._pause_requested():
                    time.sleep(1.0)
                    continue
                now = time.time()
                if self.connected:
                    if now - self._last_temp_query > 2.0:
                        self._last_temp_query = now
                        try:
                            self.read_temps_once()
                        except Exception as exc:
                            logger.debug("TEMP? query failed: %s", exc)
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
                if now - self._last_level_query > 2.0:
                    self._last_level_query = now
                    self._update_high_level_state()
                if now - self._last_values_push >= VALUES_POST_PERIOD:
                    self._last_values_push = now
                    self._post_values()
                if (
                    self._light_sensor
                    and now - self._last_light_query > LIGHT_QUERY_PERIOD
                ):
                    self._last_light_query = now
                    try:
                        lux = self._light_sensor.read_lux()
                        with self.state_lock:
                            self.state["light_lux"] = lux
                    except Exception as exc:
                        logger.debug("Lecture TSL2591 échouée: %s", exc)
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
                            stop_pump = bool(
                                entry.get("stop_pump", DEFAULT_FEEDER_STOP_PUMP)
                            )
                            duration = self._sanitize_pump_stop_duration(
                                entry.get(
                                    "pump_stop_duration_min",
                                    (
                                        DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN
                                        if stop_pump
                                        else 0
                                    ),
                                )
                            )
                            if stop_pump and duration == 0:
                                duration = DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN
                            telemetry_events_logger.info(
                                "Feeder scheduled trigger %s %s key=%s stop_pump=%s duration=%s",
                                method,
                                url_norm,
                                key,
                                stop_pump,
                                duration,
                            )
                            threading.Thread(
                                target=self._execute_feeding_task,
                                args=(
                                    {
                                        "time": f"{hh_i:02d}:{mm_i:02d}",
                                        "url": url_norm,
                                        "method": method,
                                        "stop_pump": stop_pump,
                                        "pump_stop_duration_min": duration,
                                    },
                                    key,
                                ),
                                daemon=True,
                            ).start()
                time.sleep(10)
            except Exception as exc:
                logger.error("Feeder scheduler error: %s", exc)
                time.sleep(5)

    def _peristaltic_scheduler_loop(self) -> None:
        while True:
            try:
                with self.state_lock:
                    auto = bool(self.state.get("peristaltic_auto", True))
                    schedule = dict(self.state.get("peristaltic_schedule", {}))
                if auto:
                    now = time.localtime()
                    for axis, entry in schedule.items():
                        candidate = (
                            entry.get("time") if isinstance(entry, dict) else entry
                        )
                        normalized = self._normalize_time_string(candidate)
                        if not normalized:
                            continue
                        try:
                            hh_text, mm_text = normalized.split(":", 1)
                            hh = int(hh_text)
                            mm = int(mm_text)
                        except Exception:
                            continue
                        if now.tm_hour != hh or now.tm_min != mm:
                            continue
                        key = f"{axis}|{normalized}"
                        last_run = self._last_peristaltic_runs.get(key, 0.0)
                        if time.time() - last_run < 70:
                            continue
                        self._last_peristaltic_runs[key] = time.time()
                        threading.Thread(
                            target=self._run_scheduled_peristaltic_cycle,
                            args=(axis, normalized, key),
                            daemon=True,
                        ).start()
                time.sleep(10)
            except Exception as exc:
                logger.error("Peristaltic scheduler error: %s", exc)
                time.sleep(5)

    def _run_scheduled_peristaltic_cycle(
        self, axis: str, schedule_time: str, key: str
    ) -> None:
        try:
            self.run_peristaltic_cycle(
                axis,
                source="automation",
                reason="schedule",
                extra_fields={"schedule_time": schedule_time, "schedule_key": key},
            )
        except Exception as exc:
            logger.error(
                "Scheduled peristaltic cycle %s at %s failed: %s", axis, key, exc
            )

    def _execute_feeding_task(self, entry: Dict[str, Any], key: str) -> None:
        url = str(entry.get("url", "") or "").strip()
        if not url:
            logger.warning("Feeding task %s skipped due to missing URL", key)
            return
        url = self._normalize_url(url)
        method = str(entry.get("method", "GET")).upper()
        if method not in ("GET", "POST"):
            method = "GET"
        stop_pump = bool(entry.get("stop_pump", DEFAULT_FEEDER_STOP_PUMP))
        duration = self._sanitize_pump_stop_duration(
            entry.get(
                "pump_stop_duration_min",
                DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN if stop_pump else 0,
            )
        )
        if stop_pump and duration == 0:
            duration = DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN
        pump_relay_state = False
        pump_was_running = False
        stop_executed = False
        restart_delay_min = duration if stop_pump and duration > 0 else 0
        if stop_pump and duration > 0:
            with self.state_lock:
                pump_relay_state = bool(self.state.get("pump_state", False))
            pump_was_running = not pump_relay_state
            if pump_was_running:
                telemetry_events_logger.info(
                    "Stopping pump for feeding key=%s duration=%s", key, duration
                )
                try:
                    self.toggle_pump(True, source="automation")
                    stop_executed = True
                except Exception as exc:
                    logger.error("Unable to stop pump before feeding %s: %s", key, exc)
            else:
                telemetry_events_logger.info(
                    "Pump already off before feeding key=%s", key
                )
            self._publish_device_event(
                device_type="pump",
                device_id="main",
                source="automation",
                fields={
                    "event": "feeding_pump_stop",
                    "state": True,
                    "key": key,
                    "duration_min": duration,
                    "initial_state": pump_relay_state,
                    "pump_running_before": pump_was_running,
                    "pump_stop_executed": stop_executed,
                },
            )
        try:
            self._trigger_feeder_url(url, key, method)
        finally:
            if stop_pump and duration > 0:

                def _delayed_restart() -> None:
                    try:
                        time.sleep(restart_delay_min * 60)
                        if stop_executed and pump_was_running:
                            telemetry_events_logger.info(
                                "Restarting pump automatically after feeding key=%s",
                                key,
                            )
                            try:
                                self.toggle_pump(False, source="automation")
                                self._publish_device_event(
                                    device_type="pump",
                                    device_id="main",
                                    source="automation",
                                    fields={
                                        "event": "feeding_pump_restart",
                                        "state": False,
                                        "key": key,
                                        "duration_min": restart_delay_min,
                                        "pump_running_after": True,
                                    },
                                )
                            except Exception as exc:
                                logger.error(
                                    "Unable to restart pump after feeding %s: %s",
                                    key,
                                    exc,
                                )
                        else:
                            telemetry_events_logger.info(
                                "Skipping automatic pump restart after feeding key=%s",
                                key,
                            )
                    except Exception as exc:
                        logger.error(
                            "Pump restart timer failed for feeding %s: %s", key, exc
                        )

                threading.Thread(target=_delayed_restart, daemon=True).start()

    def _trigger_feeder_url(self, url: str, key: str, method: str = "GET") -> None:
        method_norm = method.upper() if isinstance(method, str) else "GET"
        if method_norm not in ("GET", "POST"):
            method_norm = "GET"
        source = (
            "user"
            if isinstance(key, str) and key.startswith("manual|")
            else "automation"
        )
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
            self._publish_device_event(
                device_type="feeder_webhook",
                device_id=str(key),
                source=source,
                fields={
                    "status": resp.status_code,
                    "method": method_norm,
                    "url": url,
                },
            )
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
            self._publish_device_event(
                device_type="feeder_webhook",
                device_id=str(key),
                source=source,
                fields=details,
            )

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
            light_lux = self.state.get("light_lux")
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
            "light": {"lux": light_lux},
        }

    def _post_values(self) -> None:
        payload = self._build_values_payload()
        try:
            # Lectures de capteurs
            for entry in payload.get("temperatures", []):
                if isinstance(entry, dict) and entry.get("value") is not None:
                    sensor_name = str(entry.get("name", "")) or str(
                        entry.get("key", "")
                    )
                    self._publish_sensor_reading(
                        sensor_id=sensor_name,
                        sensor_name=sensor_name,
                        fields={"celsius": entry.get("value")},
                    )

            ph_data = payload.get("ph", {})
            if isinstance(ph_data, dict) and ph_data.get("value") is not None:
                self._publish_sensor_reading(
                    sensor_id="ph_probe",
                    sensor_name="Sonde pH",
                    fields={
                        "ph": ph_data.get("value"),
                        "voltage": ph_data.get("voltage"),
                        "raw": ph_data.get("raw"),
                    },
                )

            light_data = payload.get("light", {})
            if isinstance(light_data, dict):
                lux_value = light_data.get("lux")
                if lux_value is not None:
                    self._publish_sensor_reading(
                        sensor_id="tsl2591",
                        sensor_name="Capteur lumière TSL2591",
                        fields={"lux": lux_value},
                    )

            # États des appareils
            levels = payload.get("levels", {})
            if isinstance(levels, dict):
                for name, value in levels.items():
                    fields: Dict[str, Any] = {}
                    numeric_state: Optional[float]
                    try:
                        numeric_state = float(value)
                    except (TypeError, ValueError):
                        numeric_state = None
                    if numeric_state is not None and not math.isnan(numeric_state):
                        fields["state"] = numeric_state
                    else:
                        fields["state_text"] = str(value)
                    self._publish_sensor_reading(
                        sensor_id=f"level_{name}",
                        sensor_name=f"Niveau {name}",
                        fields=fields,
                    )

            pumps = payload.get("pumps", {})
            if isinstance(pumps, dict):
                if "main" in pumps:
                    self._publish_device_event(
                        device_type="pump",
                        device_id="main",
                        source="state_poll",
                        fields={"state": pumps["main"]},
                    )
                if "motors_powered" in pumps:
                    self._publish_device_event(
                        device_type="peristaltic_power",
                        device_id="main_stepper_power",
                        source="state_poll",
                        fields={"state": pumps["motors_powered"]},
                    )

            relays = payload.get("relays", {})
            if isinstance(relays, dict):
                for name, state in relays.items():
                    self._publish_device_event(
                        device_type="relay",
                        device_id=name,
                        source="state_poll",
                        fields={"state": state},
                    )

            # Consignes de température (publier régulièrement pour Grafana)
            with self.state_lock:
                heat_targets = self.state.get("heat_targets", {})
                water_target = heat_targets.get("temp_1", self.state.get("tset_water"))
                reserve_target = heat_targets.get("temp_2", self.state.get("tset_res"))
            if water_target is not None:
                self._publish_setting_change(
                    setting_group="heat",
                    setting_name="target_water",
                    value=water_target,
                )
            if reserve_target is not None:
                self._publish_setting_change(
                    setting_group="heat",
                    setting_name="target_reserve",
                    value=reserve_target,
                )
        except Exception as exc:
            logger.error("Erreur lors de la publication des mesures InfluxDB: %s", exc)

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
            self._publish_device_event(
                device_type="relay",
                device_id="light",
                source="automation",
                fields={"state": should_on, "day_of_week": day_key},
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
                        self._publish_device_event(
                            device_type="peristaltic_power",
                            device_id="main_stepper_power",
                            source="status_line",
                            fields={"state": new_state, "previous_state": prev},
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
                        prev = bool(
                            self.state.get("peristaltic_state", {}).get(axis_key, False)
                        )
                        new_state = value in ("1", "ON", "TRUE", "true", "on")
                        self.state.setdefault("peristaltic_state", {})[
                            axis_key
                        ] = new_state
                        if new_state != prev:
                            name, volume = self._get_peristaltic_profile(axis_key)
                            device_id = name or axis_key
                            self._publish_device_event(
                                device_type="peristaltic_pump",
                                device_id=device_id,
                                source="status_line",
                                fields={
                                    "state": new_state,
                                    "previous_state": prev,
                                    "axis": axis_key,
                                },
                            )
                            if new_state:
                                self._publish_device_event(
                                    device_type="peristaltic_pump",
                                    device_id=device_id,
                                    source="automation",
                                    fields={
                                        "product_name": name,
                                        "volume_ml": volume,
                                        "reason": "status_line",
                                        "axis": axis_key,
                                    },
                                )

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
            for zone, new_state in states.items():
                prev = prev_states.get(zone)
                if new_state != prev:
                    self._publish_device_event(
                        device_type="heater_zone",
                        device_id=str(zone),
                        source="automation",
                        fields={
                            "state": new_state,
                            "previous_state": prev,
                            "target": targets.get(zone),
                            "temperature": self._parse_temperature_value(
                                temps.get(zone)
                            ),
                            "hysteresis": hysteresis,
                        },
                    )
            self._publish_device_event(
                device_type="heater",
                device_id="main",
                source="automation",
                fields={
                    "state": any(states.values()),
                    "hysteresis": hysteresis,
                },
            )

    def set_heat_hyst(self, value: float) -> None:
        with self.state_lock:
            self.state["heat_hyst"] = value
        self._save_heat_config()
        self._evaluate_heat_needs()
        self._publish_setting_change(
            setting_group="heat", setting_name="hysteresis", value=value
        )

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
        self._publish_device_event(
            device_type="serial",
            device_id=str(port),
            source="system",
            fields={"connected": True},
        )

    def disconnect(self) -> None:
        port = self.serial.port
        self.serial.close()
        self.connected = False
        self._last_temp_query = 0.0
        self._last_level_query = 0.0
        self.status_text = "Déconnecté"
        self._drive_heat_gpio(False)
        self._drive_fan_gpio(False)
        self._publish_device_event(
            device_type="serial",
            device_id=str(port),
            source="system",
            fields={"connected": False},
        )

    # ---------- Actions exposed to API ----------
    def read_temps_once(self) -> None:
        self._send_query("TEMP?")

    def read_levels_once(self) -> None:
        self._update_high_level_state()

    def set_water(self, value: float) -> None:
        with self.state_lock:
            self.state["tset_water"] = value
            self.state["heat_targets"]["temp_1"] = value
        self._save_heat_config()
        if self.state.get("heat_auto", True):
            self._evaluate_heat_needs()
        else:
            self._update_heater_outputs()
        self._publish_setting_change(
            setting_group="heat", setting_name="target_water", value=value
        )

    def set_reserve(self, value: float) -> None:
        with self.state_lock:
            self.state["tset_res"] = value
            self.state["heat_targets"]["temp_2"] = value
        self._save_heat_config()
        if self.state.get("heat_auto", True):
            self._evaluate_heat_needs()
        else:
            self._update_heater_outputs()
        self._publish_setting_change(
            setting_group="heat", setting_name="target_reserve", value=value
        )

    def set_autocool(self, thresh: float) -> None:
        with self.state_lock:
            self.state["auto_thresh"] = thresh
            self.state["auto_fan"] = True
        self._evaluate_fan()
        self._publish_setting_change(
            setting_group="fan", setting_name="auto_threshold", value=thresh
        )

    def set_fan_manual(self, value: int) -> None:
        with self.state_lock:
            prev = bool(self.state.get("fan_on", False))
            self.state["auto_fan"] = False
            self.state["fan_on"] = bool(value)
            self.state["fan"] = 255 if value else 0
            new = self.state["fan_on"]
        self._drive_fan_gpio(bool(value))
        self._publish_device_event(
            device_type="fan",
            device_id="main_fan",
            source="user",
            fields={
                "state": new,
                "previous_state": prev,
                "manual_value": int(bool(value)),
            },
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
        self._publish_setting_change(
            setting_group="fan", setting_name="auto_mode", value=enable
        )

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
        self._publish_setting_change(
            setting_group="temperature_names",
            setting_name="labels",
            value={k: v for k, v in names.items() if k in allowed},
        )

    def set_heat_mode(self, auto: bool) -> None:
        with self.state_lock:
            self.state["heat_auto"] = auto
        self._save_heat_config()
        if auto:
            self._evaluate_heat_needs()
        self._publish_setting_change(
            setting_group="heat", setting_name="auto_mode", value=auto
        )

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
        self._publish_device_event(
            device_type="heater",
            device_id="manual_override",
            source="user",
            fields={"state": new, "previous_state": prev},
        )

    def toggle_protect(self, enable: bool) -> None:
        with self.state_lock:
            self.state["protect"] = enable
        self._publish_setting_change(
            setting_group="safety", setting_name="protect_mode", value=enable
        )

    def set_servo(self, angle: int) -> None:
        with self.state_lock:
            self.state["servo_angle"] = angle
        self._send_command(f"SERVO {angle}")
        self._publish_device_event(
            device_type="servo",
            device_id="feeder_servo",
            source="user",
            fields={"angle": angle},
        )

    def dispense_macro(self) -> None:
        self._send_command("SERVOFEED")
        self._publish_device_event(
            device_type="servo",
            device_id="feeder_servo",
            source="user",
            fields={"action": "macro_dispense"},
        )

    def set_mtr_auto_off(self, enable: bool) -> None:
        with self.state_lock:
            self.state["mtr_auto_off"] = enable
        self._publish_setting_change(
            setting_group="pump", setting_name="auto_motor_off", value=enable
        )

    def set_steps_speed(self, steps: int, speed: int) -> None:
        with self.state_lock:
            self.steps_per_job = steps
            self.state["steps"] = steps
            self.state["speed"] = speed
        self._publish_setting_change(
            setting_group="pump",
            setting_name="steps_speed",
            value={"steps": steps, "speed": speed},
        )

    def set_global_speed(self, speed: int) -> None:
        with self.state_lock:
            self.global_speed = speed
            self.state["speed"] = speed
        self._publish_setting_change(
            setting_group="pump", setting_name="global_speed", value=speed
        )

    def _compute_steps_for_volume(self, volume_ml: float) -> int:
        steps = int(round(abs(volume_ml) * PERISTALTIC_STEPS_PER_ML))
        return max(1, steps)

    def _execute_peristaltic_job(
        self,
        axis: str,
        steps: int,
        speed: int,
        backwards: bool,
        source: str,
        reason: Optional[str] = None,
        volume_override: Optional[float] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
        minute_label: Optional[str] = None,
    ) -> None:
        axis_key = axis.upper()
        steps_abs = abs(int(steps))
        if steps_abs <= 0:
            raise RuntimeError("Nombre de pas invalide pour la pompe")
        with self.state_lock:
            auto_off = bool(self.state.get("mtr_auto_off", True))
            protect = bool(self.state.get("protect", False))
            low = self.state.get("lvl_low")
        low_text = str(low).strip().lower()
        if protect and low_text in ("1", "low", "true", "on"):
            raise RuntimeError("Niveau bas - pompe bloquée")
        command_speed = max(int(speed or 0), 50)
        signed_steps = -steps_abs if backwards else steps_abs
        self._send_command(f"PUMP {axis_key} {signed_steps} {command_speed}")
        if auto_off:
            threading.Thread(
                target=self._auto_motor_off_delay,
                args=(steps_abs, command_speed),
                daemon=True,
            ).start()
        name, default_volume = self._get_peristaltic_profile(axis_key)
        volume = default_volume
        if volume_override is not None:
            try:
                volume = float(volume_override)
            except (TypeError, ValueError):
                pass
        signed_volume = -abs(volume) if backwards else abs(volume)
        fields: Dict[str, Any] = {
            "product_name": name,
            "volume_ml": signed_volume,
            "steps": signed_steps,
            "speed": command_speed,
            "direction": -1 if backwards else 1,
            "axis": axis_key,
        }
        if reason:
            fields["reason"] = reason
        if extra_fields and isinstance(extra_fields, dict):
            fields.update(extra_fields)
        self._publish_device_event(
            device_type="peristaltic_pump",
            device_id=name or axis_key,
            source=source,
            fields=fields,
        )
        label = minute_label or self._current_minute_label()
        self._record_peristaltic_run_label(axis_key, label)

    def run_peristaltic_cycle(
        self,
        axis: str,
        source: str = "user",
        reason: Optional[str] = None,
        extra_fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        axis_key = axis.upper()
        with self.state_lock:
            pump_cfg = (self.state.get("pump_config", {}).get(axis_key) or {}).copy()
            speed = int(self.state.get("speed") or self.global_speed or 300)
        volume_raw = pump_cfg.get("volume_ml", 0.0)
        try:
            volume = float(volume_raw or 0.0)
        except (TypeError, ValueError):
            volume = 0.0
        if volume <= 0:
            raise RuntimeError(f"Volume invalide pour la pompe {axis_key}")
        direction = pump_cfg.get("direction", 1)
        try:
            direction_val = int(direction)
        except (TypeError, ValueError):
            direction_val = 1
        backwards = direction_val < 0
        steps = self._compute_steps_for_volume(volume)
        minute_label = self._current_minute_label()
        self._ensure_peristaltic_not_recent(axis_key, minute_label)
        self._execute_peristaltic_job(
            axis_key,
            steps=steps,
            speed=speed,
            backwards=backwards,
            source=source,
            reason=reason,
            volume_override=volume,
            extra_fields=extra_fields,
            minute_label=minute_label,
        )

    def set_peristaltic_auto(self, enable: bool) -> None:
        with self.state_lock:
            self.state["peristaltic_auto"] = bool(enable)
        self._save_peristaltic_schedule()
        self._publish_setting_change(
            setting_group="peristaltic", setting_name="auto_mode", value=enable
        )

    def update_peristaltic_schedule(self, axis: str, time_text: Optional[str]) -> None:
        axis_key = axis.upper()
        normalized = self._normalize_time_string(time_text)
        with self.state_lock:
            schedule = self.state.setdefault("peristaltic_schedule", {})
            entry = schedule.setdefault(axis_key, {"time": None})
            entry["time"] = normalized
        self._save_peristaltic_schedule()
        self._publish_setting_change(
            setting_group="peristaltic",
            setting_name=f"schedule_{axis_key}",
            value={"time": normalized},
        )

    def pump(self, axis: str, backwards: bool = False) -> None:
        axis = axis.upper()
        with self.state_lock:
            steps = self.steps_per_job
            speed = self.state["speed"] or self.global_speed
        minute_label = self._current_minute_label()
        self._ensure_peristaltic_not_recent(axis, minute_label)
        self._execute_peristaltic_job(
            axis,
            steps=steps,
            speed=speed,
            backwards=backwards,
            source="user",
            minute_label=minute_label,
        )

    def _auto_motor_off_delay(self, steps: int, speed: int) -> None:
        duration = (steps * speed * 2) / 1_000_000.0
        time.sleep(duration + 0.5)
        try:
            self._send_command("MTR OFF", timeout=1.0)
        except Exception:
            pass

    def emergency_stop(self) -> None:
        self._send_command("MTR OFF")
        self._publish_device_event(
            device_type="pump",
            device_id="all",
            source="user",
            fields={"action": "emergency_stop"},
        )

    def restart_service(self) -> None:
        command = ["sudo", "systemctl", "restart", "reef"]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            logger.info("Reef service restart requested from UI.")
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            logger.error(
                "Reef service restart failed (code=%s): %s",
                exc.returncode,
                stderr,
            )
            raise RuntimeError("Impossible de redémarrer le service reef.") from exc
        except FileNotFoundError as exc:
            logger.error("systemctl introuvable pour redémarrer reef: %s", exc)
            raise RuntimeError("Commande systemctl introuvable sur cet hôte.") from exc
        except Exception as exc:
            logger.error("Unexpected error restarting reef service: %s", exc)
            raise RuntimeError("Erreur lors du redémarrage du service reef.") from exc

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
        self._publish_setting_change(
            setting_group="pump",
            setting_name=f"config_{axis}",
            value={
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
        self._publish_setting_change(
            setting_group="light_schedule",
            setting_name=key,
            value={"on": on_time, "off": off_time},
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
            self._publish_device_event(
                device_type="relay",
                device_id="light",
                source="user",
                fields={"state": new, "previous_state": prev, "event": event_type},
            )

    def set_light_auto(self, enable: bool) -> None:
        with self.state_lock:
            self.state["light_auto"] = enable
        self._publish_setting_change(
            setting_group="light", setting_name="auto_mode", value=enable
        )

    def set_feeder_auto(self, enable: bool) -> None:
        with self.state_lock:
            self.state["feeder_auto"] = bool(enable)
        self._save_feeder_config()
        self._publish_setting_change(
            setting_group="feeder", setting_name="auto_mode", value=enable
        )

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
                stop_pump = bool(item.get("stop_pump", DEFAULT_FEEDER_STOP_PUMP))
                duration = self._sanitize_pump_stop_duration(
                    item.get(
                        "pump_stop_duration_min",
                        DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN if stop_pump else 0,
                    )
                )
                if stop_pump and duration == 0:
                    duration = DEFAULT_FEEDER_PUMP_STOP_DURATION_MIN
                valid.append(
                    {
                        "time": f"{hh_i:02d}:{mm_i:02d}",
                        "url": url_str,
                        "method": method,
                        "stop_pump": stop_pump,
                        "pump_stop_duration_min": duration,
                    }
                )
        with self.state_lock:
            self.state["feeder_schedule"] = valid
        self._save_feeder_config()
        self._publish_setting_change(
            setting_group="feeder",
            setting_name="schedule",
            value={"count": len(valid), "entries": valid},
        )

    def trigger_feeder_url(
        self,
        url: str,
        method: str = "GET",
        stop_pump: Optional[bool] = None,
        pump_stop_duration_min: Optional[int] = None,
    ) -> None:
        if not isinstance(url, str) or not url.strip():
            raise ValueError("URL manquante")
        method_norm = method.upper() if isinstance(method, str) else "GET"
        if method_norm not in ("GET", "POST"):
            method_norm = "GET"
        clean_url = url.strip()
        url_norm = self._normalize_url(clean_url)
        stop_flag = bool(stop_pump)
        duration = self._sanitize_pump_stop_duration(pump_stop_duration_min)
        if stop_flag and duration == 0:
            duration = DEFAULT_FEEDER_PUMP_STOP_DURATION
        key = f"manual|{method_norm}|{url_norm}"
        if stop_flag and duration > 0:
            entry = {
                "time": "",
                "url": url_norm,
                "method": method_norm,
                "stop_pump": True,
                "pump_stop_duration_min": duration,
            }
            self._execute_feeding_task(entry, key)
        else:
            self._trigger_feeder_url(url_norm, key, method_norm)

    def _load_openai_api_key(self) -> Optional[str]:
        env_key = os.environ.get("OPENAI_API_KEY")
        if env_key:
            return env_key.strip()
        if self._openai_api_key:
            return self._openai_api_key
        if OPENAI_KEY_FILE_PATH.exists():
            try:
                stored_key = OPENAI_KEY_FILE_PATH.read_text(encoding="utf-8").strip()
                if stored_key:
                    self._openai_api_key = stored_key
                    return stored_key
            except OSError as exc:
                logger.error("Impossible de lire la clé API OpenAI: %s", exc)
        return None

    def _protect_openai_key_file(self) -> None:
        if not OPENAI_KEY_FILE_PATH.exists():
            return
        if os.name == "nt":
            try:
                import ctypes

                FILE_ATTRIBUTE_HIDDEN = 0x02
                attrs = ctypes.windll.kernel32.GetFileAttributesW(
                    str(OPENAI_KEY_FILE_PATH)
                )
                if attrs != -1 and not attrs & FILE_ATTRIBUTE_HIDDEN:
                    ctypes.windll.kernel32.SetFileAttributesW(
                        str(OPENAI_KEY_FILE_PATH), attrs | FILE_ATTRIBUTE_HIDDEN
                    )
            except Exception as exc:
                logger.debug(
                    "Impossible de masquer le fichier de clé API OpenAI: %s", exc
                )
        else:
            try:
                os.chmod(OPENAI_KEY_FILE_PATH, 0o600)
            except OSError as exc:
                logger.debug(
                    "Impossible de restreindre les permissions de la clé OpenAI: %s",
                    exc,
                )

    def set_openai_api_key(self, api_key: str) -> None:
        clean_key = str(api_key or "").strip()
        if not clean_key:
            raise ValueError("Clé API OpenAI invalide.")
        try:
            OPENAI_KEY_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            OPENAI_KEY_FILE_PATH.write_text(clean_key, encoding="utf-8")
            self._protect_openai_key_file()
        except OSError as exc:
            logger.error("Impossible d'enregistrer la clé API OpenAI: %s", exc)
            raise
        self._openai_api_key = clean_key

    def get_ai_analysis(self) -> Dict[str, str]:
        """
        Collecte les données locales et demande une analyse à l'API d'OpenAI.
        """
        api_key = self._load_openai_api_key()
        if not api_key:
            raise RuntimeError(self.OPENAI_KEY_MISSING_ERROR)
        client = openai.OpenAI(api_key=api_key)
        current_data = self._build_values_payload()
        prompt_template = """
        Rôle: Tu es un expert en aquariophilie récifale, spécialisé dans l'analyse des paramètres de l'eau et la maintenance des écosystèmes marins.

        Contexte: Voici les données de mon aquarium récifal. Analyse-les et fournis des recommandations claires et actionnables.

        Données:
        ```json
        {data_json}
        ```

        Tâche:
        1.  Analyse générale: Sur la base de toutes les données, y a-t-il des paramètres qui sortent des plages idéales pour un aquarium récifal ? Lesquels et pourquoi ?
        2.  Identification des risques: Détectes-tu des problèmes potentiels ou des tendances inquiétantes (par exemple, une instabilité, une augmentation des nitrates) ?
        3.  Santé globale: Fournis un résumé de l'état de santé général de l'aquarium (Excellent, Bon, Passable, Problématique).
        4.  Plan d'action: Propose une liste de recommandations concrètes et priorisées. Pour chaque point, explique la raison en te basant sur les données.

        Format de la réponse: Structure ta réponse avec les sections suivantes :
        -   Résumé de l'état de santé
        -   Points de vigilance
        -   Recommandations
        """
        data_as_json_string = json.dumps(current_data, indent=2)
        final_prompt = prompt_template.format(data_json=data_as_json_string)
        try:
            completion = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Tu es un expert en aquariophilie récifale.",
                    },
                    {"role": "user", "content": final_prompt},
                ],
                temperature=0.5,
            )
            response_content = completion.choices[0].message.content
            if not response_content:
                response_content = "L'IA n'a pas retourné de réponse."
            return {
                "analysis": response_content,
                "prompt": final_prompt.strip(),
            }
        except Exception as exc:
            logger.error("Erreur lors de l'appel à l'API OpenAI: %s", exc)
            raise RuntimeError(f"Erreur de communication avec l'API OpenAI: {exc}")

    def submit_water_quality(self, params: Dict[str, Any]) -> None:
        if not isinstance(params, dict):
            raise ValueError("Paramètres invalides")
        allowed_keys = ("no3", "no2", "gh", "kh", "cl2", "po4")
        fields: Dict[str, Any] = {}
        for key in allowed_keys:
            value = params.get(key)
            if value is None:
                continue
            try:
                numeric_value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isnan(numeric_value) or math.isinf(numeric_value):
                continue
            fields[key] = numeric_value
        if not fields:
            raise ValueError("Aucune valeur valide fournie")
        if self.telemetry:
            self.telemetry.emit(
                measurement="water_quality_manual",
                tags={"source": "manual"},
                fields=fields,
            )

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
