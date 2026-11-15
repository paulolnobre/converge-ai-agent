#!/usr/bin/env bash
# One backup of one compose stack into the restic repository, run on the VPS:
#   backup.sh <stack-dir> [label]        label: daily | pre-deploy-<tag> | manual
#
# Daily by the systemd timer (deploy/systemd), and by deploy.sh before every
# migration. One run makes two snapshots, both tagged <stack> and <label>:
#   db      pg_dump -Fc -Z0 of the stack's Postgres, streamed into restic —
#           never written to this disk. Uncompressed so restic can deduplicate
#           one day against the next; restic compresses it instead.
#   files   what exists only on this server: .env, content/staff.json,
#           google_credentials.json — whichever the stack has
# restic encrypts everything before it leaves (AES-256, the repository
# password), so the bucket only ever holds ciphertext.
#
# The daily run also forgets snapshots older than BACKUP_KEEP_DAYS and, on
# Sundays, prunes what nothing references and checks a sample of the data.
#
# The repository is S3 São Paulo (deploy/backup-aws-setup.sh). This server's key
# can write and delete, but a delete in that bucket only hides an object: every
# version stays for 14 days, and Object Lock keeps each one 30 days from its
# upload. Nobody with this server's key can destroy a backup.
#
# Settings live in /etc/converge-backup.env (root:deploy, 0640) — never in the
# stack's .env, which the bot container reads and which must not hold a storage
# key. BACKUP_PING_URL, if set, hears every run (healthchecks.io): a backup that
# stops running altogether is noticed, not only one that fails.
set -euo pipefail
umask 077

stack_dir="$(cd "${1:?usage: backup.sh <stack-dir> [label]}" && pwd)"
label="${2:-manual}"
[[ "$label" =~ ^[A-Za-z0-9._-]+$ ]] || { echo "Invalid label: $label" >&2; exit 2; }
stack="$(basename "$stack_dir")"

config="${BACKUP_CONFIG:-/etc/converge-backup.env}"
[ -r "$config" ] || { echo "::error::Backup settings not readable: $config" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "$config"
set +a
for var in RESTIC_REPOSITORY AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY; do
  [ -n "${!var:-}" ] || { echo "::error::$var is not set in $config" >&2; exit 1; }
done
[ -n "${RESTIC_PASSWORD:-}${RESTIC_PASSWORD_FILE:-}" ] \
  || { echo "::error::RESTIC_PASSWORD is not set in $config" >&2; exit 1; }
keep_days="${BACKUP_KEEP_DAYS:-30}"

ping() {
  [ -n "${BACKUP_PING_URL:-}" ] || return 0
  curl -fsS -m 10 --retry 3 -o /dev/null "$BACKUP_PING_URL/$stack-backup$1?create=1" || true
}

# Every stack shares one repository, and prune needs it to itself: one run at a
# time on this server, whether from the timer or from a deploy.
exec 9>"${BACKUP_LOCK:-/run/lock/converge-backup.lock}"
flock -w 1800 9

done_ok=false
trap '$done_ok || ping /fail' EXIT
ping /start
cd "$stack_dir"
tags=(--tag "$stack" --tag "$label")

# restic fails the snapshot if pg_dump fails: a broken dump is never "latest".
# shellcheck disable=SC2016  # expanded by the container's shell, not this one
restic backup -q "${tags[@]}" --tag db --stdin-filename "$stack-db.dump" \
  --stdin-from-command -- \
  docker compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -Z0'

files=()
for f in .env content/staff.json google_credentials.json; do
  if [ -s "$f" ]; then files+=("$stack_dir/$f"); fi
done
if [ "${#files[@]}" -gt 0 ]; then
  restic backup -q "${tags[@]}" --tag files "${files[@]}"
fi

if [ "$label" = daily ]; then
  restic forget -q --tag "$stack" --group-by host,paths --keep-within "${keep_days}d"
  if [ "$(date +%u)" = 7 ]; then
    restic prune -q
    restic check -q --read-data-subset 5%
  fi
fi

done_ok=true
ping ""
echo "Backed up $stack ($label) to $RESTIC_REPOSITORY"
