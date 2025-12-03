"""
Worker d'analyse IA locale.

Lancez ce script sur le PC qui heberge LM Studio pour produire periodiquement des
insights automatiques. Personnalisez le fichier worker_config.json pour ajuster
l'URL du backend Flask, la periode analyse et l'intervalle entre deux executions.

Le parametre ``failure_backoff_seconds`` du fichier de configuration determine
l'attente avant une nouvelle tentative en cas d'echec (ex: backend arrete).

Exemple d'utilisation sous Windows (tache planifiee toutes les heures) :
  python llm\\ai_worker_local.py --once
  # puis ajoutez une tache planifiee qui relance le script sans --once.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict

import requests

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent
CONFIG_PATH = BASE_DIR / "worker_config.json"
DEFAULT_CONFIG = {
    "backend_url": "http://192.168.1.16:5000",
    "analysis_period": "last_3_days",
    "poll_interval_seconds": 1800,
    "failure_backoff_seconds": 60,
    "insight_source": "local_worker",
    "risk_level": "info",
    "prompt_template": (
        "Tu es une IA qui surveille l'aquarium. Resume les informations ci-dessous et "
        "signale toute anomalie. JSON resume:\n{summary}"
    ),
}

sys.path.insert(0, str(PROJECT_ROOT))
from analysis import call_llm  # noqa: E402

logger = logging.getLogger("reef.ai.worker")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)


def load_worker_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("worker_config.json invalide, retour aux valeurs par defaut.")
        return dict(DEFAULT_CONFIG)
    merged = dict(DEFAULT_CONFIG)
    merged.update({k: v for k, v in data.items() if v is not None})
    return merged


def fetch_summary(config: Dict[str, Any]) -> Dict[str, Any]:
    backend = config["backend_url"].rstrip("/")
    url = f"{backend}/api/ai/summary"
    params = {"period": config.get("analysis_period", "last_3_days")}
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:  # pragma: no cover - réseau
        raise RuntimeError(f"Backend IA injoignable ({url}): {exc}") from exc
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or "Resume IA indisponible.")
    return payload["summary"]


def post_insight(
    config: Dict[str, Any], text: str, mode_used: str, summary: Dict[str, Any]
) -> None:
    backend = config["backend_url"].rstrip("/")
    url = f"{backend}/api/ai/insight"
    payload = {
        "text": text,
        "source": config.get("insight_source", "local_worker"),
        "risk_level": config.get("risk_level", "info"),
        "mode": mode_used,
        "metadata": {
            "period": summary.get("period"),
            "generated_at": summary.get("generated_at"),
        },
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except requests.exceptions.RequestException as exc:  # pragma: no cover - réseau
        raise RuntimeError(
            f"Impossible d'enregistrer l'insight ({url}): {exc}"
        ) from exc


def run_once(config: Dict[str, Any]) -> None:
    summary = fetch_summary(config)
    prompt = config["prompt_template"].format(
        summary=json.dumps(summary, ensure_ascii=False, indent=2)
    )
    messages = [
        {
            "role": "system",
            "content": "Tu es une IA qui surveille en continu un aquarium recifal.",
        },
        {"role": "user", "content": prompt},
    ]
    logger.info("Appel IA local pour analyse periodique...")
    llm_response = call_llm(
        messages, temperature=0.3, allow_fallback=False, force_mode="local"
    )
    analysis_text = llm_response.get("content") or "Pas d'analyse generée."
    post_insight(config, analysis_text, llm_response.get("mode_used", "local"), summary)
    logger.info("Insight IA publie via %s", llm_response.get("mode_used"))


def main() -> None:
    run_once_flag = "--once" in sys.argv
    config = load_worker_config()
    interval = int(config.get("poll_interval_seconds", 1800))
    failure_backoff = max(int(config.get("failure_backoff_seconds", 60)), 10)
    if run_once_flag:
        run_once(config)
        return
    logger.info("Demarrage du worker IA (intervalle %s s)", interval)
    while True:
        try:
            run_once(config)
            logger.info("Pause jusqu'au prochain cycle (%s s)", interval)
            time.sleep(max(interval, 60))
        except Exception as exc:  # pragma: no cover - robuste
            logger.error("Execution worker echouee: %s", exc)
            message = str(exc).lower()
            if "injoignable" in message or "connectionpool" in message or "winerror 10061" in message:
                logger.info(
                    "Conseil: verifiez que le backend Flask tourne sur %s ou modifiez backend_url dans llm/worker_config.json.",
                    config.get("backend_url", "http://127.0.0.1:5000"),
                )
            logger.info("Nouvelle tentative dans %s s (backoff)", failure_backoff)
            time.sleep(failure_backoff)


if __name__ == "__main__":
    main()
