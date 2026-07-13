#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12 ou superior e obrigatorio.")
PY

"$PYTHON_BIN" -m venv .venv

# shellcheck disable=SC1091
. ".venv/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
python -m telegram_mt5_copier --check

if [ ! -f ".env" ]; then
  printf "\nArquivo .env nao encontrado.\n"
  printf "Crie uma copia de .env.example como .env e preencha os dados diretamente nesta maquina.\n"
fi
