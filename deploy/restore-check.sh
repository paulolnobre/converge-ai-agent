#!/usr/bin/env bash
# Prove a stack's backup restores, from a machine that is not the VPS:
#   restore-check.sh <stack> [snapshot-id]       e.g. restore-check.sh converge-agent
#
# Needs restic, Docker, the repository (RESTIC_REPOSITORY, and RESTIC_PASSWORD,
# RESTIC_PASSWORD_FILE or RESTIC_PASSWORD_COMMAND — your own restic key, see
# docs/BACKUP.md) and AWS credentials that can read the bucket (AWS_PROFILE).
#
# Streams the database snapshot (the latest one, or the one named) into a
# throwaway Postgres container — no published port, removed at the end — and
# prints every table with its row count. Nothing decrypted touches the disk.
set -euo pipefail

stack="${1:?usage: restore-check.sh <stack> [snapshot-id]}"
snapshot="${2:-latest}"
pg_image="${RESTORE_PG_IMAGE:-postgres:16-alpine}"
container="restore-check-$$"
trap 'docker rm -f "$container" >/dev/null 2>&1 || true' EXIT

echo "== Snapshots of $stack"
restic snapshots --tag "$stack"

docker run -d --name "$container" -e POSTGRES_PASSWORD=restore-check "$pg_image" >/dev/null
for _ in $(seq 1 60); do
  docker exec "$container" pg_isready -U postgres -q 2>/dev/null && break
  sleep 1
done
docker exec "$container" createdb -U postgres restored

restic dump --tag "$stack,db" "$snapshot" "/$stack-db.dump" \
  | docker exec -i "$container" pg_restore -U postgres -d restored --no-owner --no-acl --exit-on-error

echo "== Server files in the latest files snapshot (names only)"
restic ls --long --tag "$stack,files" latest | awk '$1 ~ /^-/ {print $NF}'

echo "== Restored tables (rows)"
docker exec "$container" psql -U postgres -d restored -AtF ' ' -c "
  SELECT table_name,
         (xpath('/row/c/text()', query_to_xml(
            format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name),
            false, true, '')))[1]::text
  FROM information_schema.tables
  WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
  ORDER BY table_name"
echo "== Restore check passed"
