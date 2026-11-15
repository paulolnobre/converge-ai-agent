#!/usr/bin/env bash
# One release, run on the VPS by CI:  deploy.sh <image-tag>  < registry-token
#
# Pull the image CI tested, apply pending migrations, start it, and wait for its
# readiness check. If startup or release setup fails, put the previous image
# back so a failed deploy does not leave a partially configured bot running.
#
# The registry token arrives on stdin and is dropped when this ends: the server
# holds no GitHub credential between deploys.
#
# Migrations are not rolled back with the image, so they must stay additive:
# add a column, never drop or rename one the running version still reads.
#
# Data format: an image declares, in the label org.converge.data-format, the
# newest format of stored data it reads (no label = 0). Postgres (data_format)
# and Redis (key data_format) record the newest format already written. No
# image below that may start — not as a release and not as a rollback: it
# would run on data it cannot read. Lowering it is a manual procedure
# (docs/CRIPTOGRAFIA.md). After a recorded release, cli.seal_data converts and
# locks the data; if it fails the release stays (it reads half-converted data)
# and the job fails.
set -euo pipefail
cd "$(dirname "$0")"

new="${1:?usage: deploy.sh <image-tag>}"
prev="$(cat .deployed-tag 2>/dev/null || true)"
container=converge-agent-bot

# Prepare both metadata files on the same filesystem before changing the
# running image. The private staging directory protects the copied .env.
staging="$(mktemp -d .release.XXXXXX)"
trap 'docker logout ghcr.io >/dev/null 2>&1 || true; rm -rf -- "$staging"' EXIT
cp -p .env "$staging/env.before"
sed '/^IMAGE_TAG=/d' .env > "$staging/env.next"
# A hand edit can leave the last line without a newline: end it first, or the
# tag is glued to that setting and both break (2026-10-29).
if [ -s "$staging/env.next" ] && [ -n "$(tail -c1 "$staging/env.next")" ]; then
  printf '\n' >> "$staging/env.next"
fi
printf 'IMAGE_TAG=%s\n' "$new" >> "$staging/env.next"
chmod --reference=.env "$staging/env.next"
# Kept until the end: a refused rollback writes it back even after env.next was
# moved into .env and then undone (the tag write failed).
cp -p "$staging/env.next" "$staging/env.new"
printf '%s\n' "$new" > "$staging/tag.next"

# The format an image reads, from its label; 0 when it has none.
image_format() {
  local image
  # `config --images <service>` lists every service's image on some compose
  # versions; the JSON names the service for sure.
  image="$(IMAGE_TAG="$1" docker compose config --format json 2>/dev/null | python3 -c \
    'import json, sys; print(json.load(sys.stdin)["services"]["reception-bot"]["image"])' \
    2>/dev/null || true)"
  local label=""
  if [ -n "$image" ]; then
    label="$(docker image inspect --format '{{ index .Config.Labels "org.converge.data-format" }}' \
      "$image" 2>/dev/null || true)"
  fi
  case "$label" in ''|*[!0-9]*) echo 0 ;; *) echo "$label" ;; esac
}

# The host folder behind /data/media when it is a bind mount (the encrypted
# overlay); empty for the named volume of a machine without it.
media_host_dir() {
  docker compose config --format json 2>/dev/null | python3 -c '
import json, sys
for volume in json.load(sys.stdin)["services"]["reception-bot"].get("volumes", []):
    if volume.get("type") == "bind" and volume.get("target") == "/data/media":
        print(volume["source"])' 2>/dev/null || true
}

# The newest format stored, as "<number> <where>": the higher of Postgres and
# Redis. "unknown" when either cannot be read — a failed psql, or a Redis still
# loading its AOF ("LOADING ..."). Fail closed: an unreadable marker is never 0.
required_format() {
  local pg=0 redis_format has_table
  if ! has_table="$(docker compose exec -T postgres sh -c \
      'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM pg_tables WHERE tablename = '"'"'data_format'"'"'"' \
      2>/dev/null)"; then
    echo unknown; return
  fi
  has_table="$(printf '%s' "$has_table" | tr -d '[:space:]')"
  case "$has_table" in ''|*[!0-9]*) echo unknown; return ;; esac
  if [ "$has_table" != 0 ]; then
    if ! pg="$(docker compose exec -T postgres sh -c \
        'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT COALESCE(max(version), 0) FROM data_format"' \
        2>/dev/null)"; then
      echo unknown; return
    fi
    pg="$(printf '%s' "$pg" | tr -d '[:space:]')"
    case "$pg" in ''|*[!0-9]*) echo unknown; return ;; esac
  fi
  if ! redis_format="$(docker compose exec -T redis redis-cli GET data_format 2>/dev/null)"; then
    echo unknown; return
  fi
  redis_format="$(printf '%s' "$redis_format" | tr -d '[:space:]')"
  case "$redis_format" in '') redis_format=0 ;; *[!0-9]*) echo unknown; return ;; esac
  if [ "$redis_format" -gt "$pg" ]; then echo "$redis_format Redis"; else echo "$pg Postgres"; fi
}

rollback() {
  if [ -n "$prev" ]; then
    local needed where
    read -r needed where <<< "$(required_format)"
    if [ "$needed" = unknown ] || [ "$(image_format "$prev")" -lt "$needed" ]; then
      if [ "$needed" = unknown ]; then
        echo "::error::Não consegui ler o formato dos dados; parando o bot em vez de subir $prev."
      else
        echo "::error::A imagem $prev não lê o formato $needed que o $where já tem; parando o bot em vez de subir ela."
      fi
      docker compose stop reception-bot || true
      # The next boot runs `compose up -d` with .env's IMAGE_TAG (the volume
      # unlock): it must name the image that reads this data, not $prev.
      cp -p "$staging/env.new" .env || true
      return
    fi
    echo "Rolling back to $prev"
    IMAGE_TAG="$prev" docker compose up -d reception-bot
  else
    echo "No previous release; stopping the failed bot"
    docker compose stop reception-bot || true
  fi
}

docker login ghcr.io -u "${GHCR_USER:-paulolnobre}" --password-stdin

export IMAGE_TAG="$new"
# Customers' files go only to the encrypted volume: a folder that is missing
# (volume locked, or never created) stops the release before anything changes.
media_dir="$(media_host_dir)"
if [ -n "$media_dir" ] && [ ! -d "$media_dir" ]; then
  echo "::error::A pasta $media_dir (montada em /data/media) não existe; crie-a no volume cifrado como diz docs/CRIPTOGRAFIA.md. Nada foi feito."
  exit 1
fi
docker compose pull reception-bot
docker compose up -d --wait postgres redis
read -r needed where <<< "$(required_format)"
if [ "$needed" = unknown ]; then
  echo "::error::Não consegui ler o formato dos dados no Postgres ou no Redis; nada foi feito."
  exit 1
fi
new_format="$(image_format "$new")"
if [ "$new_format" -lt "$needed" ]; then
  # Refused before anything changed: the bot running now reads this data.
  echo "::error::A imagem $new é do formato $new_format, e o $where já tem dados do formato $needed; nada foi feito."
  exit 1
fi
# A restore point before migrations touch the schema. They are not rolled back
# with the image, so this archive is the way back from a bad one — and a
# release that cannot take it stops here, before anything has changed: no
# rollback needed, the previous reception-bot is still the one running.
if ! ./backup.sh "$PWD" "pre-deploy-$new"; then
  echo "::error::Pre-deploy backup failed; migration not started for $new"
  exit 1
fi
docker compose run --rm --no-deps reception-bot python -m cli.migrate
if ! docker compose up -d reception-bot; then
  echo "::error::Could not start $new"
  rollback
  exit 1
fi

status=starting
for _ in $(seq 1 40); do
  status="$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo missing)"
  [ "$status" = healthy ] && break
  sleep 3
done

if [ "$status" != healthy ]; then
  echo "::error::$new did not become healthy (last status: $status)"
  docker logs --tail 60 "$container" || true
  rollback
  exit 1
fi

if ! docker compose exec -T reception-bot python -m cli.load_content; then
  echo "::error::Could not load content for $new"
  rollback
  exit 1
fi
if grep -Eq '^PUBLIC_BASE_URL=.+' .env; then
  if ! docker compose exec -T reception-bot python -m cli.set_webhook; then
    echo "::error::Could not configure the webhook for $new"
    rollback
    exit 1
  fi
else
  echo "PUBLIC_BASE_URL is not set in .env — webhook left as it is."
fi

# Compose reads IMAGE_TAG from .env during manual operations. Commit both
# markers only after a healthy release. On a failed write, restore the old
# metadata and image so the next deploy still has a correct rollback target.
if ! mv "$staging/env.next" .env; then
  echo "::error::Could not record IMAGE_TAG for $new"
  rollback
  exit 1
fi
if ! mv "$staging/tag.next" .deployed-tag; then
  echo "::error::Could not record deployed tag for $new"
  if ! mv "$staging/env.before" .env; then
    echo "::error::Could not restore .env after metadata failure"
  fi
  rollback
  exit 1
fi
# Convert what is still in clear and lock the old columns. Recorded release:
# no rollback from here — the image reads half-converted data, and the image
# before it may not read the new format.
if ! docker compose exec -T reception-bot python -m cli.seal_data; then
  echo "::error::seal_data failed for $new; the release stays. Fix and run it again."
  exit 1
fi
# An enabled availability monitor uses the release image just pulled. It is
# independent of the bot, but is advanced after migrations and readiness.
if [ -f ./admin-monitor.sh ]; then
  bash ./admin-monitor.sh
fi
# Prometheus and Grafana, when that overlay is on: never a reason to fail a
# release that is already recorded and healthy.
if [ -f ./observability.sh ]; then
  bash ./observability.sh || echo "::warning::Observability services not updated"
fi
# Unused images older than a week go, so releases do not fill the disk. The
# running image is always kept — it is the next deploy's rollback target.
docker image prune -af --filter "until=168h" >/dev/null || true
echo "Deployed $new"
