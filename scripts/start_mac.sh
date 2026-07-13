#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if [ ! -f ".env" ]; then
  printf "Arquivo .env nao encontrado. Crie o arquivo a partir de .env.example.\n" >&2
  exit 1
fi

if [ ! -f ".venv/bin/activate" ]; then
  printf "Ambiente virtual nao encontrado. Execute scripts/setup_mac.sh primeiro.\n" >&2
  exit 1
fi

# shellcheck disable=SC1091
. ".venv/bin/activate"

LOG_DIR="$(python - <<'PY'
from telegram_mt5_copier.config import AppConfig

config = AppConfig.load(create_dirs=True)
print(config.log_dir)
PY
)"

mkdir -p "$LOG_DIR"
TIMESTAMP="$(python - <<'PY'
from datetime import datetime

print(datetime.now().strftime("%Y%m%d-%H%M%S"))
PY
)"
LOG_FILE="$LOG_DIR/start-$TIMESTAMP.log"

printf "Iniciando aplicacao. Logs: %s\n" "$LOG_FILE"

set +e
python -m telegram_mt5_copier >> "$LOG_FILE" 2>&1
EXIT_CODE=$?
set -e

if [ "$EXIT_CODE" -ne 0 ]; then
  printf "Aplicacao finalizada com erro %s. Veja o log: %s\n" "$EXIT_CODE" "$LOG_FILE" >&2
fi

exit "$EXIT_CODE"
