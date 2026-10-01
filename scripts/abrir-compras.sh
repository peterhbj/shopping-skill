#!/usr/bin/env bash
# Sobe o site (se ainda não estiver rodando) e abre no navegador.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${SHOP_PORT:-8765}"
URL="http://127.0.0.1:$PORT"
up() { curl -fsS -o /dev/null "$URL/api/status" 2>/dev/null; }

if ! up; then
  mkdir -p "$HOME/.cache"
  nohup .venv/bin/python -m webapp.server --no-open >"$HOME/.cache/compras-andorinha.log" 2>&1 &
  for _ in $(seq 1 40); do up && break; sleep 0.25; done
fi
up || { echo "O site não subiu. Veja ~/.cache/compras-andorinha.log"; exit 1; }
xdg-open "$URL" >/dev/null 2>&1 || .venv/bin/python -m webbrowser "$URL"
