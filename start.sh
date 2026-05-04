#!/bin/bash
cd "$(dirname "$0")"

# Načti env proměnné z .env (pokud existuje)
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

python3 server.py
