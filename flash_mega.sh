#!/usr/bin/env bash
set -euo pipefail

# Compile Pump/Mega/Mega.ino with arduino-cli and upload it to an Arduino Mega.
# Usage:
#   ./flash_mega.sh /dev/ttyACM0
# or set PORT env var: PORT=/dev/ttyACM0 ./flash_mega.sh
# Optional env vars:
#   FQBN=arduino:avr:mega
#   BUILD_DIR=build/mega

if ! command -v arduino-cli >/dev/null 2>&1; then
  echo "arduino-cli introuvable. Installez-le puis exécutez 'arduino-cli core install arduino:avr'." >&2
  exit 1
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKETCH_DIR="${PROJECT_ROOT}/Pump/Mega"
if [[ ! -d "${SKETCH_DIR}" ]]; then
  echo "Impossible de trouver le sketch dans ${SKETCH_DIR}" >&2
  exit 1
fi

FQBN="${FQBN:-arduino:avr:mega}"
BUILD_DIR="${BUILD_DIR:-${PROJECT_ROOT}/build/mega}"
PORT="${PORT:-${1:-}}"
SERVICE_NAME="${SERVICE_NAME:-reef}"

if [[ -z "${PORT}" ]]; then
  # essayer de deviner un port ACM/USB unique
  mapfile -t CANDIDATES < <(ls /dev/ttyACM* /dev/ttyUSB* 2>/dev/null || true)
  if [[ ${#CANDIDATES[@]} -eq 1 ]]; then
    PORT="${CANDIDATES[0]}"
  else
    echo "Port série non spécifié. Passez-le en argument (ex: ./flash_mega.sh /dev/ttyACM0) ou via PORT=..." >&2
    exit 1
  fi
fi

if [[ ! -e "${PORT}" ]]; then
  echo "Port ${PORT} introuvable." >&2
  exit 1
fi

service_active=0
if systemctl is-active --quiet "${SERVICE_NAME}"; then
  service_active=1
  echo "[0/3] Arrêt du service ${SERVICE_NAME}..."
  sudo systemctl stop "${SERVICE_NAME}"
fi

cleanup() {
  local status=$?
  if [[ ${service_active} -eq 1 ]]; then
    echo "[3/3] Redémarrage du service ${SERVICE_NAME}..."
    sudo systemctl start "${SERVICE_NAME}"
  fi
  return ${status}
}
trap cleanup EXIT

echo "[1/3] Compilation vers ${BUILD_DIR}..."
arduino-cli compile --fqbn "${FQBN}" --output-dir "${BUILD_DIR}" "${SKETCH_DIR}"

echo "[2/3] Téléversement vers ${PORT}..."
arduino-cli upload --fqbn "${FQBN}" --input-dir "${BUILD_DIR}" -p "${PORT}" "${SKETCH_DIR}"

echo "Flash terminé."
