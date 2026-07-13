#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
PROJECT_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PYTHON_CANDIDATES=(
  "python3.12"
  "/opt/homebrew/bin/python3.12"
  "/usr/local/bin/python3.12"
  "python3"
  "python"
)

resolve_python() {
  local candidate="$1"

  if [[ "$candidate" == */* ]]; then
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
    fi
    return
  fi

  command -v "$candidate" 2>/dev/null || true
}

compatible_python_version() {
  "$1" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit(1)

print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
PY
}

clear_macos_hidden_flags() {
  if command -v chflags >/dev/null 2>&1; then
    chflags -R nohidden "$1" 2>/dev/null || true
  fi
}

PYTHON_BIN=""
PYTHON_VERSION=""

for candidate in "${PYTHON_CANDIDATES[@]}"; do
  resolved_python="$(resolve_python "$candidate")"
  if [ -z "$resolved_python" ]; then
    continue
  fi

  if version="$(compatible_python_version "$resolved_python" 2>/dev/null)"; then
    PYTHON_BIN="$resolved_python"
    PYTHON_VERSION="$version"
    break
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  printf "Python 3.12 ou superior não encontrado. Instale com: brew install python@3.12\n" >&2
  exit 1
fi

VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
VENV_STATUS="criada"

if [ -d "$VENV_DIR" ]; then
  if [ -x "$VENV_PYTHON" ] && venv_version="$(compatible_python_version "$VENV_PYTHON" 2>/dev/null)"; then
    VENV_STATUS="reutilizada"
  else
    printf "Ambiente virtual existente usa Python inferior a 3.12 ou esta invalido. Recriando .venv...\n"
    rm -rf "$VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    venv_version="$(compatible_python_version "$VENV_PYTHON")"
  fi
else
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  venv_version="$(compatible_python_version "$VENV_PYTHON")"
fi

clear_macos_hidden_flags "$VENV_DIR"

# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pytest
python -m pip install -e .
clear_macos_hidden_flags "$VENV_DIR"
python - <<'PY'
import telegram_mt5_copier
import telegram_mt5_copier.telegram_login

print(f"Import OK: {telegram_mt5_copier.__file__}")
PY

mkdir -p data logs sessions

if [ ! -f ".env" ]; then
  printf "\nArquivo .env nao encontrado.\n"
  printf "Copie .env.example para .env e preencha os dados diretamente nesta maquina.\n"
fi

set +e
python -m pytest -v
TEST_EXIT_CODE=$?
set -e

if [ "$TEST_EXIT_CODE" -eq 0 ]; then
  TEST_RESULT="passou"
else
  TEST_RESULT="falhou"
fi

printf "\nResumo do setup macOS\n"
printf "Python selecionado: %s\n" "$PYTHON_BIN"
printf "Versao selecionada: %s\n" "$PYTHON_VERSION"
printf "Ambiente virtual: %s (%s, Python %s)\n" "$VENV_DIR" "$VENV_STATUS" "$venv_version"
printf "Resultado dos testes: %s\n" "$TEST_RESULT"

if [ "$TEST_EXIT_CODE" -ne 0 ]; then
  exit "$TEST_EXIT_CODE"
fi
