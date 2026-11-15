#!/usr/bin/env bash
# Called after a healthy release. The optional overlay decides whether it runs.
set -euo pipefail
services="$(docker compose config --services)"
case $'\n'"$services"$'\n' in
  *$'\nmonitor\n'*) docker compose up -d --no-deps monitor ;;
esac
