#!/usr/bin/env bash
set -euo pipefail

# Compile and upload Pump/Mega/PhSerialMonitor/PhSerialMonitor.ino
# Usage: ./flash_ph_monitor.sh /dev/ttyACM0
# Optional env vars:
#   PORT=/dev/ttyACM1
#   FQBN=arduino:avr:mega
#   BUILD_DIR=build/ph-monitor

if ! command -v arduino-cli >/dev/null 2>&1; then
  echo "arduino-cli introuvable. Installez-le puis exécutez 'arduino-cli core install arduino:avr'." >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH_DIR="${PROJECT_ROOT}/Pump/Mega/PhSerialMonitor"
if [[ ! -d "${SKETCH_DIR}" ]]; then
  echo "Sketch introuvable dans ${SKETCH_DIR}" >&2
  exit 1
fi

FQBN="${FQBN:-arduino:avr:mega}"
BUILD_DIR="${BUILD_DIR:-${PROJECT_ROOT}/build/ph-monitor}"
PORT="${PORT:-${1:-}}"

if [[ -z "${PORT}" ]]; then
  mapfile -t CANDIDATES < <(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true)
  if [[ ${#CANDIDATES[@]} -eq 1 ]]; then
    PORT="${CANDIDATES[0]}"
  else
    echo "Port série non spécifié. Fournissez-le en argument ou via PORT=..." >&2
    exit 1
  fi
fi

if [[ ! -e "${PORT}" ]]; then
  echo "Port ${PORT} introuvable." >&2
  exit 1
fi

echo "[1/2] Compilation vers ${BUILD_DIR}..."
arduino-cli compile --fqbn "${FQBN}" --output-dir "${BUILD_DIR}" "${SKETCH_DIR}"

echo "[2/2] Téléversement vers ${PORT}..."
arduino-cli upload --fqbn "${FQBN}" --input-dir "${BUILD_DIR}" -p "${PORT}" "${SKETCH_DIR}"

echo "Flash pH monitor terminé."
