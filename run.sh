#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then
    uv venv -q -p 3.11 .venv
  else
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
      || { echo "Se necesita Python 3.11 o superior." >&2; exit 1; }
    python3 -m venv .venv
  fi
fi

if [ ! -f .venv/.instalado ] || [ requirements.txt -nt .venv/.instalado ]; then
  if command -v uv >/dev/null; then
    uv pip install -q -p .venv -r requirements.txt
  else
    .venv/bin/python -m pip install -q -r requirements.txt
  fi
  touch .venv/.instalado
fi

exec .venv/bin/python -m app
