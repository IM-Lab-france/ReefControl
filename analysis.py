import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

import requests
from influxdb_client import InfluxDBClient
from influxdb_client.client.flux_table import FluxRecord

from ai_config import load_ai_config


BASE_DIR = Path(__file__).resolve().parent
ANALYSIS_QUERIES_PATH = BASE_DIR / "analysis_queries.json"
DEFAULT_BUCKET = os.environ.get("INFLUXDB_BUCKET", "reef-data")
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
logger = logging.getLogger("reef.analysis")
AI_CALL_TIMEOUT = 60


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


def _prepare_provider_configs(config: Dict[str, Any]) -> Dict[str, Optional[Dict[str, Any]]]:
    local_base = (config.get("local_ai_base_url") or "").strip()
    local_model = (config.get("local_ai_model") or "").strip()
    local_key = (config.get("local_ai_api_key") or "").strip()
    cloud_base = (config.get("cloud_ai_base_url") or "").strip()
    cloud_model = (config.get("cloud_ai_model") or "").strip()
    cloud_key = (config.get("cloud_ai_api_key") or "").strip()
    providers: Dict[str, Optional[Dict[str, Any]]] = {"local": None, "cloud": None}
    if local_base and local_model:
        providers["local"] = {
            "mode": "local",
            "base_url": local_base.rstrip("/"),
            "model": local_model,
            "api_key": local_key,
        }
    if cloud_base and cloud_model and cloud_key:
        providers["cloud"] = {
            "mode": "cloud",
            "base_url": cloud_base.rstrip("/"),
            "model": cloud_model,
            "api_key": cloud_key,
        }
    return providers


def _extract_message_content(message: Dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for chunk in content:
            if not isinstance(chunk, dict):
                continue
            text = chunk.get("text")
            if text:
                parts.append(str(text))
        return "\n".join(parts).strip()
    return ""


def _call_provider(
    provider: Dict[str, Any],
    messages: List[Dict[str, Any]],
    temperature: float,
    max_tokens: Optional[int],
    timeout: int,
) -> Dict[str, Any]:
    endpoint = f"{provider['base_url']}/chat/completions"
    payload: Dict[str, Any] = {"model": provider["model"], "messages": messages, "temperature": temperature}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    headers = {"Content-Type": "application/json", "User-Agent": "ReefControl/1.0"}
    api_key = provider.get("api_key")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"Connexion IA impossible: {exc}") from exc
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code}: {response.text}")
    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("Réponse vide du modèle.")
    message = choices[0].get("message") or {}
    content = _extract_message_content(message)
    if not content:
        raise RuntimeError("Contenu IA indisponible.")
    return {"content": content, "raw": data}


def call_llm(
    messages: List[Dict[str, Any]],
    *,
    temperature: float = 0.4,
    max_tokens: Optional[int] = None,
    allow_fallback: bool = True,
    force_mode: Optional[str] = None,
    request_timeout: int = AI_CALL_TIMEOUT,
) -> Dict[str, Any]:
    if not isinstance(messages, list) or not messages:
        raise ValueError("Messages IA invalides.")
    config = load_ai_config(include_secrets=True)
    providers = _prepare_provider_configs(config)
    order: List[str] = []

    def _mode_available(mode: str) -> bool:
        return providers.get(mode) is not None

    if force_mode:
        mode_norm = force_mode.lower()
        if mode_norm not in {"local", "cloud"}:
            raise ValueError("Mode IA inconnu.")
        if not _mode_available(mode_norm):
            if mode_norm == "cloud":
                raise RuntimeError(OPENAI_KEY_MISSING_ERROR)
            raise RuntimeError("Configuration IA locale incomplète.")
        order = [mode_norm]
    else:
        preferred = config.get("ai_mode", "cloud")
        if _mode_available(preferred):
            order.append(preferred)
        fallback = "cloud" if preferred == "local" else "local"
        if allow_fallback and _mode_available(fallback) and fallback not in order:
            order.append(fallback)

    if not order:
        if config.get("ai_mode") == "cloud":
            raise RuntimeError(OPENAI_KEY_MISSING_ERROR)
        raise RuntimeError("Aucun moteur IA disponible.")

    errors: List[str] = []
    for mode in order:
        provider = providers.get(mode)
        if not provider:
            continue
        try:
            logger.info("Appel IA via mode %s", mode)
            result = _call_provider(
                provider,
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=request_timeout,
            )
            result["mode_used"] = mode
            return result
        except RuntimeError as exc:
            logger.warning("Appel IA (%s) en échec: %s", mode, exc)
            if mode == "cloud" and str(exc) == OPENAI_KEY_MISSING_ERROR:
                raise
            errors.append(f"{mode}: {exc}")
        except Exception as exc:
            logger.warning("Appel IA (%s) erreur inattendue: %s", mode, exc)
            errors.append(f"{mode}: {exc}")

    raise RuntimeError("Echec appel IA - " + " | ".join(errors))


def ask_aquarium_ai(
    summary_json: Dict[str, Any],
    user_context: str = "",
    client_timestamp: Optional[str] = None,
) -> Dict[str, str]:
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
    messages = [
        {"role": "system", "content": "Tu es une IA experte en aquariophilie."},
        {"role": "user", "content": prompt_text},
    ]
    result = call_llm(messages, temperature=0.4, allow_fallback=True)
    analysis_text = result.get("content") or "L'IA n'a pas fourni de contenu exploitable."
    return {
        "analysis": analysis_text,
        "prompt": prompt_text,
        "mode_used": result.get("mode_used"),
    }


def _format_stat_value(values: Dict[str, Any]) -> str:
    latest = values.get("latest")
    if isinstance(latest, (int, float)):
        return f"{latest:.2f}"
    avg = values.get("avg")
    if isinstance(avg, (int, float)):
        return f"{avg:.2f}"
    return "--"


def _build_telemetry_summary_text(period_summary: Dict[str, Any]) -> str:
    parts: List[str] = []
    temps = period_summary.get("temperatures") or {}
    if temps:
        sensor_lines = []
        for sensor, stats in temps.items():
            sensor_lines.append(f"{sensor}: {_format_stat_value(stats)}°C")
        parts.append("Températures " + ", ".join(sensor_lines))
    ph_stats = (period_summary.get("ph") or {}).get("ph")
    if ph_stats:
        parts.append(f"pH moyen {ph_stats.get('avg', '--')}")
    lux_stats = period_summary.get("lux") or {}
    if isinstance(lux_stats.get("avg"), (int, float)):
        parts.append(f"Luminosité moyenne {lux_stats['avg']:.0f} lux")
    heater = period_summary.get("heater") or {}
    if heater.get("latest_state"):
        state = heater["latest_state"].get("value")
        if state is not None:
            parts.append(f"Chauffage {'actif' if state else 'arrêté'}")
    peristaltic = period_summary.get("peristaltic") or {}
    volumes = peristaltic.get("volumes_ml") or {}
    if volumes:
        total = sum(float(value) for value in volumes.values())
        parts.append(f"Doses péristaltiques totales {total:.1f} ml")
    manual_latest = (period_summary.get("manual_water") or {}).get("latest") or {}
    if manual_latest:
        metrics = ", ".join(f"{name.upper()} {entry['value']}" for name, entry in manual_latest.items())
        parts.append(f"Mesures manuelles: {metrics}")
    if not parts:
        return "Aucune donnée récente n'est disponible."
    return " | ".join(parts)


def build_ai_summary_payload(period: str = "last_3_days") -> Dict[str, Any]:
    summary = build_summary([period])
    period_data = summary["periods"].get(period)
    if not period_data:
        raise ValueError(f"Aucune donnée pour la période {period}.")
    telemetry_summary = _build_telemetry_summary_text(period_data)
    events = list(period_data.get("device_events") or [])
    events = events[-20:]
    return {
        "period": period,
        "generated_at": summary["generated_at"],
        "telemetry_summary": telemetry_summary,
        "range": period_data.get("range"),
        "stats": {
            "temperatures": period_data.get("temperatures"),
            "ph": period_data.get("ph"),
            "lux": period_data.get("lux"),
            "water_levels": period_data.get("water_levels"),
            "peristaltic": period_data.get("peristaltic"),
            "heater": period_data.get("heater"),
            "manual_water": period_data.get("manual_water"),
            "relay_states": period_data.get("relay_states"),
        },
        "events": events,
    }
