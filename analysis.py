import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

import requests
from influxdb_client import InfluxDBClient
from influxdb_client.client.flux_table import FluxRecord


BASE_DIR = Path(__file__).resolve().parent
ANALYSIS_QUERIES_PATH = BASE_DIR / "analysis_queries.json"
DEFAULT_BUCKET = os.environ.get("INFLUXDB_BUCKET", "reef-data")
OPENAI_KEY_FILE_PATH = BASE_DIR / ".openai_api_key"
OPENAI_KEY_MISSING_ERROR = "OPENAI_API_KEY_MISSING"
BUCKET_BY_PERIOD = {
    "last_3_days": "6h",
    "last_week": "1d",
    "last_month": "1d",
    "last_year": "1mo",
}
PERIOD_OFFSETS = {
    "last_3_days": (-3, 0),
    "last_week": (-7, -3),
    "last_month": (-30, -7),
    "last_year": (-365, -30),
}


DEFAULT_QUERIES: Dict[str, str] = {
    "last_3_days": f"""
from(bucket: "{DEFAULT_BUCKET}")
  |> range(start: -3d)
  |> filter(fn: (r) =>
      r["_measurement"] == "sensor_readings" or
      r["_measurement"] == "device_events" or
      r["_measurement"] == "settings" or
      r["_measurement"] == "water_quality_manual")
""",
    "last_week": f"""
from(bucket: "{DEFAULT_BUCKET}")
  |> range(start: -7d)
  |> filter(fn: (r) =>
      r["_measurement"] == "sensor_readings" or
      r["_measurement"] == "device_events" or
      r["_measurement"] == "settings" or
      r["_measurement"] == "water_quality_manual")
""",
    "last_month": f"""
from(bucket: "{DEFAULT_BUCKET}")
  |> range(start: -30d)
  |> filter(fn: (r) =>
      r["_measurement"] == "sensor_readings" or
      r["_measurement"] == "device_events" or
      r["_measurement"] == "settings" or
      r["_measurement"] == "water_quality_manual")
""",
    "last_year": f"""
from(bucket: "{DEFAULT_BUCKET}")
  |> range(start: -365d)
  |> filter(fn: (r) =>
      r["_measurement"] == "sensor_readings" or
      r["_measurement"] == "device_events" or
      r["_measurement"] == "settings" or
      r["_measurement"] == "water_quality_manual")
""",
}

_influx_client: Optional[InfluxDBClient] = None


def _ensure_queries_file() -> None:
    if ANALYSIS_QUERIES_PATH.exists():
        return
    ANALYSIS_QUERIES_PATH.write_text(json.dumps(DEFAULT_QUERIES, indent=2), encoding="utf-8")


def get_influx_client() -> InfluxDBClient:
    global _influx_client
    if _influx_client:
        return _influx_client
    url = os.environ.get("INFLUXDB_URL")
    token = os.environ.get("INFLUXDB_TOKEN")
    org = os.environ.get("INFLUXDB_ORG")
    if not all([url, token, org]):
        raise RuntimeError("Variables InfluxDB manquantes (INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG)")
    _influx_client = InfluxDBClient(url=url, token=token, org=org)
    return _influx_client


def load_analysis_queries() -> Dict[str, str]:
    _ensure_queries_file()
    try:
        return json.loads(ANALYSIS_QUERIES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        ANALYSIS_QUERIES_PATH.write_text(json.dumps(DEFAULT_QUERIES, indent=2), encoding="utf-8")
        return DEFAULT_QUERIES.copy()


def save_analysis_queries(payload: Dict[str, str]) -> Dict[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Format de requêtes invalide.")
    for key, value in payload.items():
        if key not in DEFAULT_QUERIES:
            raise ValueError(f"Période inconnue: {key}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Requête vide pour {key}")
    ANALYSIS_QUERIES_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def run_flux_query(query: str) -> List[Dict[str, Any]]:
    client = get_influx_client()
    query_api = client.query_api()
    try:
        tables = query_api.query(query=query)
    except Exception as exc:  # pragma: no cover - defensive branch
        raise RuntimeError(f"Flux query failed: {exc}") from exc
    rows: List[Dict[str, Any]] = []
    for table in tables:
        for record in table.records:
            rows.append(_record_to_dict(record))
    rows.sort(key=lambda item: item.get("time") or "")
    return rows


def _record_to_dict(record: FluxRecord) -> Dict[str, Any]:
    tags = {
        key: value
        for key, value in record.values.items()
        if key not in {"_time", "_value", "_measurement", "_field", "result", "table"}
    }
    return {
        "time": record.get_time().isoformat() if record.get_time() else None,
        "measurement": record.get_measurement(),
        "field": record.get_field(),
        "value": record.get_value(),
        "tags": tags,
    }


def fetch_history(period: str) -> Dict[str, Any]:
    queries = load_analysis_queries()
    if period not in queries:
        raise ValueError(f"Période inconnue: {period}")
    rows = run_flux_query(queries[period])
    earliest_time = None
    for row in rows:
        ts = row.get("time")
        if not ts:
            continue
        if earliest_time is None or ts < earliest_time:
            earliest_time = ts
    grouped: Dict[str, List[Dict[str, Any]]] = {
        "sensor_readings": [],
        "device_events": [],
        "settings": [],
        "water_quality_manual": [],
    }
    for row in rows:
        measurement = row.get("measurement")
        if measurement in grouped:
            grouped[measurement].append(row)
    return {
        "series": grouped,
        "earliest_time": earliest_time,
    }


def build_summary(periods: List[str]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {
        "generated_at": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
        "periods": {},
    }
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    for period in periods:
        history = fetch_history(period)
        series = history["series"]
        earliest_time = history.get("earliest_time")
        bucket_size = BUCKET_BY_PERIOD.get(period, "6h")
        start_offset, end_offset = PERIOD_OFFSETS.get(period, (-3, 0))
        period_start = now + timedelta(days=start_offset)
        period_end = now + timedelta(days=end_offset)
        period_summary = {
            "temperatures": _summarize_temperatures(series["sensor_readings"]),
            "ph": _summarize_ph(series["sensor_readings"]),
            "lux": _summarize_lux(series["sensor_readings"]),
            "water_levels": _summarize_levels(series["sensor_readings"]),
            "relay_states": _summarize_relays(series["device_events"]),
            "peristaltic": _summarize_peristaltic(series["device_events"]),
            "heater": _summarize_heater(series["device_events"]),
            "manual_water_quality": _summarize_manual_water(series["water_quality_manual"]),
            "settings": _summarize_settings(series["settings"]),
            "device_events": _list_relevant_events(series["device_events"]),
            "earliest_time": earliest_time,
            "range": {"start": period_start.isoformat(), "end": period_end.isoformat()},
            "timelines": {
                "sensor_buckets": _aggregate_sensor_buckets(series["sensor_readings"], bucket_size),
                "manual_water_buckets": _aggregate_manual_water_buckets(
                    series["water_quality_manual"], bucket_size
                ),
                "device_event_buckets": _aggregate_device_event_buckets(
                    series["device_events"], bucket_size
                ),
            },
        }
        summary["periods"][period] = period_summary
    return summary


def _summarize_temperatures(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    sensors: Dict[str, List[float]] = {}
    for row in rows:
        if row["field"] != "celsius":
            continue
        sensor = row["tags"].get("sensor_id") or row["tags"].get("sensor_name", "unknown")
        value = row.get("value")
        if isinstance(value, (int, float)):
            sensors.setdefault(sensor, []).append(value)
    return {sensor: _basic_stats(values) for sensor, values in sensors.items() if values}


def _summarize_ph(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ph_values = [row["value"] for row in rows if row["field"] == "ph" and isinstance(row["value"], (int, float))]
    voltage_values = [row["value"] for row in rows if row["field"] == "voltage" and isinstance(row["value"], (int, float))]
    return {
        "ph": _basic_stats(ph_values),
        "voltage": _basic_stats(voltage_values),
    }


def _summarize_lux(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    lux_values = [row["value"] for row in rows if row["field"] == "lux" and isinstance(row["value"], (int, float))]
    return _basic_stats(lux_values)


def _summarize_levels(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row["field"] not in {"state", "state_text"}:
            continue
        sensor = row["tags"].get("sensor_id") or row["tags"].get("sensor_name")
        if not sensor:
            continue
        latest.setdefault(sensor, {})
        latest[sensor][row["field"]] = row["value"]
    return latest


def _summarize_relays(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    relays: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if row["measurement"] != "device_events":
            continue
        if row["tags"].get("device_type") != "relay":
            continue
        device_id = row["tags"].get("device_id", "relay")
        state = row["value"] if row["field"] in {"state", "state_int"} else row["tags"].get("state")
        relays[device_id] = {"field": row["field"], "value": state, "time": row["time"]}
    return relays


def _summarize_peristaltic(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    volume_per_axis: Dict[str, float] = {}
    activations: Dict[str, int] = {}
    for row in rows:
        dtype = row["tags"].get("device_type")
        if dtype not in {"pump", "peristaltic_power"}:
            continue
        axis = row["tags"].get("axis") or row["tags"].get("device_id", "unknown")
        if "volume_ml" in row["tags"]:
            try:
                volume = float(row["tags"]["volume_ml"])
                volume_per_axis[axis] = volume_per_axis.get(axis, 0.0) + volume
            except (TypeError, ValueError):
                pass
        if row["field"] in {"state", "state_int"}:
            activations[axis] = activations.get(axis, 0) + 1
    return {"volumes_ml": volume_per_axis, "activations": activations}


def _summarize_heater(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    heater_events = [
        row
        for row in rows
        if row["tags"].get("device_type") in {"heater", "heater_zone"}
    ]
    latest_state = None
    hysteresis = None
    for row in heater_events:
        if row["field"] in {"state", "state_int"}:
            latest_state = {"value": row["value"], "time": row["time"], "zone": row["tags"].get("device_id")}
        if row["field"] == "hysteresis":
            hysteresis = row["value"]
    return {"latest_state": latest_state, "hysteresis": hysteresis}


def _summarize_manual_water(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    metrics = {}
    for row in rows:
        field = row["field"]
        value = row["value"]
        if field in {"no3", "no2", "gh", "kh", "cl2", "po4"} and isinstance(value, (int, float)):
            metrics.setdefault(field, []).append({"value": value, "time": row["time"]})
    latest = {name: series[-1] for name, series in metrics.items() if series}
    return {"latest": latest, "history": metrics}


def _summarize_settings(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for row in rows:
        group = row["tags"].get("setting_group")
        name = row["tags"].get("setting_name")
        if not group or not name:
            continue
        group_entry = summary.setdefault(group, {})
        group_entry[name] = {
            "field": row["field"],
            "value": row["value"],
            "time": row["time"],
        }
    focus = {
        "heat_targets": {
            "water": _resolve_setting_value(summary, "heat", "target_water"),
            "reserve": _resolve_setting_value(summary, "heat", "target_reserve"),
        },
        "fan_auto_threshold": _resolve_setting_value(summary, "fan", "auto_threshold"),
        "light_schedule": summary.get("light_schedule", {}),
    }
    return {"raw": summary, "focus": focus}


def _resolve_setting_value(summary: Dict[str, Any], group: str, name: str) -> Optional[Any]:
    group_entry = summary.get(group, {})
    entry = group_entry.get(name)
    if not entry:
        return None
    return entry.get("value")


def _list_relevant_events(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    interesting = []
    topics = {"pump", "relay", "heater", "peristaltic_power", "feeder_webhook"}
    for row in rows:
        if row["tags"].get("device_type") in topics:
            interesting.append(
                {
                    "time": row["time"],
                    "device_type": row["tags"].get("device_type"),
                    "device_id": row["tags"].get("device_id"),
                    "field": row["field"],
                    "value": row["value"],
                    "source": row["tags"].get("source"),
                    "extra": row["tags"],
                }
            )
    return interesting


def _basic_stats(values: Iterable[float]) -> Dict[str, Any]:
    series = [float(val) for val in values if isinstance(val, (int, float))]
    if not series:
        return {}
    return {
        "min": min(series),
        "max": max(series),
        "avg": mean(series),
        "trend": series[-1] - series[0] if len(series) > 1 else 0.0,
        "latest": series[-1],
    }


def _aggregate_sensor_buckets(rows: List[Dict[str, Any]], granularity: str) -> List[Dict[str, Any]]:
    bucket_map: Dict[str, Dict[str, Dict[str, List[float]]]] = {}
    for row in rows:
        field = row.get("field")
        if field not in {"celsius", "ph", "voltage", "lux"}:
            continue
        value = row.get("value")
        if not isinstance(value, (int, float)):
            continue
        bucket_key = _bucket_key(row.get("time"), granularity)
        if not bucket_key:
            continue
        sensor = row["tags"].get("sensor_id") or row["tags"].get("sensor_name") or field
        bucket_entry = bucket_map.setdefault(bucket_key, {})
        sensor_entry = bucket_entry.setdefault(sensor, {})
        sensor_entry.setdefault(field, []).append(float(value))
    results = []
    for bucket_key in sorted(bucket_map.keys()):
        sensors = {
            sensor: {
                field: _basic_stats(values)
                for field, values in fields.items()
                if values
            }
            for sensor, fields in bucket_map[bucket_key].items()
        }
        results.append({"bucket_start": bucket_key, "sensors": sensors})
    return results


def _aggregate_manual_water_buckets(rows: List[Dict[str, Any]], granularity: str) -> List[Dict[str, Any]]:
    allowed_fields = {"no3", "no2", "gh", "kh", "cl2", "po4"}
    bucket_map: Dict[str, Dict[str, List[float]]] = {}
    for row in rows:
        field = row.get("field")
        if field not in allowed_fields:
            continue
        value = row.get("value")
        if not isinstance(value, (int, float)):
            continue
        bucket_key = _bucket_key(row.get("time"), granularity)
        if not bucket_key:
            continue
        bucket_map.setdefault(bucket_key, {}).setdefault(field, []).append(float(value))
    results = []
    for bucket_key in sorted(bucket_map.keys()):
        results.append(
            {
                "bucket_start": bucket_key,
                "values": {
                    field: _basic_stats(values)
                    for field, values in bucket_map[bucket_key].items()
                    if values
                },
            }
        )
    return results


def _aggregate_device_event_buckets(rows: List[Dict[str, Any]], granularity: str) -> List[Dict[str, Any]]:
    bucket_map: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        bucket_key = _bucket_key(row.get("time"), granularity)
        if not bucket_key:
            continue
        bucket_entry = bucket_map.setdefault(
            bucket_key, {"total_events": 0, "per_type": {}}
        )
        bucket_entry["total_events"] += 1
        dtype = row["tags"].get("device_type", "unknown")
        bucket_entry["per_type"][dtype] = bucket_entry["per_type"].get(dtype, 0) + 1
    results = []
    for bucket_key in sorted(bucket_map.keys()):
        results.append(
            {
                "bucket_start": bucket_key,
                "total_events": bucket_map[bucket_key]["total_events"],
                "per_type": bucket_map[bucket_key]["per_type"],
            }
        )
    return results


def _bucket_key(time_str: Optional[str], granularity: str) -> Optional[str]:
    if not time_str:
        return None
    dt = _parse_time(time_str)
    if dt is None:
        return None
    if granularity == "6h":
        hour = (dt.hour // 6) * 6
        dt = dt.replace(hour=hour, minute=0, second=0, microsecond=0)
    elif granularity == "1d":
        dt = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    elif granularity == "1mo":
        dt = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        dt = dt.replace(minute=0, second=0, microsecond=0)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_time(time_str: str) -> Optional[datetime]:
    try:
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(time_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _load_ai_api_key() -> Optional[str]:
    env_key = os.environ.get("AQUARIUM_AI_KEY") or os.environ.get("OPENAI_API_KEY")
    if env_key:
        return env_key.strip()
    if OPENAI_KEY_FILE_PATH.exists():
        try:
            key = OPENAI_KEY_FILE_PATH.read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            return None
    return None


def ask_aquarium_ai(
    summary_json: Dict[str, Any],
    user_context: str = "",
    client_timestamp: Optional[str] = None,
) -> Dict[str, str]:
    api_key = _load_ai_api_key()
    api_url = os.environ.get("AQUARIUM_AI_URL", "https://api.openai.com/v1/chat/completions")
    model = os.environ.get("AQUARIUM_AI_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError(OPENAI_KEY_MISSING_ERROR)
    request_time = client_timestamp or datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
    extra_context = user_context.strip()
    context_section = f"\nContexte utilisateur: {extra_context}" if extra_context else ""
    prompt_text = (
        f"Analyse l'état de l'aquarium (requête du {request_time}).\n"
        "JSON:\n"
        f"{json.dumps(summary_json, ensure_ascii=False, indent=2)}\n\n"
        "Instructions:\n"
        "1. Analyse générale de l'état du bac.\n"
        "2. Points de vigilance ou tendances remarquées.\n"
        "3. Actions recommandées à court terme."
        f"{context_section}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Tu es une IA experte en aquariophilie."},
            {
                "role": "user",
                "content": prompt_text,
            },
        ],
        "temperature": 0.4,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response = requests.post(api_url, headers=headers, json=payload, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"Erreur appel IA: HTTP {response.status_code} - {response.text}")
    data = response.json()
    try:
        analysis_text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise RuntimeError("Réponse IA invalide.")
    if not analysis_text:
        analysis_text = "L'IA n'a pas fourni de contenu exploitable."
    return {
        "analysis": analysis_text,
        "prompt": prompt_text,
    }
