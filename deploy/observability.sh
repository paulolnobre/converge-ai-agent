#!/usr/bin/env bash
# Called after a healthy release. Only when the observability overlay is on
# (COMPOSE_FILE): bring Prometheus, node-exporter and Grafana to what the
# compose files say, and make Prometheus read the configuration just shipped.
set -euo pipefail
services="$(docker compose config --services)"
case $'\n'"$services"$'\n' in
  *$'\nprometheus\n'*)
    docker compose up -d --no-deps prometheus node-exporter grafana
    docker compose kill -s HUP prometheus
    ;;
esac
