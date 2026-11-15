#!/usr/bin/env bash
# One-time on the VPS, as root: create, format and mount the agent's encrypted
# volume, with the key from AWS SSM (the same parameter the unlock script
# reads). Starts nothing and moves no data — that is the migration.
#   sudo bash /opt/converge-agent/luks/create-volume.sh
set -euo pipefail

LUKS_ENV="${LUKS_ENV:-/etc/converge-luks-aws.env}"
LUKS_IMAGE="${LUKS_IMAGE:-/var/lib/converge-agent.img}"
LUKS_NAME="${LUKS_NAME:-converge-agent}"
LUKS_MAPPER_DIR="${LUKS_MAPPER_DIR:-/dev/mapper}"
LUKS_MOUNT="${LUKS_MOUNT:-/srv/converge-agent}"
LUKS_SIZE="${LUKS_SIZE:-10G}"

# Never over an existing volume: formatting it would destroy the data inside.
if [ -e "$LUKS_IMAGE" ]; then
  echo "create-volume: $LUKS_IMAGE já existe; nada foi feito." >&2
  exit 1
fi
# Nor over a mountpoint in use: mounting over folders already there would hide
# them, and a later boot would show them to postgres again, unencrypted.
mounted="$(findmnt -n -o SOURCE --mountpoint "$LUKS_MOUNT" || true)"
if [ -n "$mounted" ]; then
  echo "create-volume: $mounted está montado em $LUKS_MOUNT; nada foi feito." >&2
  exit 1
fi
if [ -d "$LUKS_MOUNT" ] && [ -n "$(ls -A "$LUKS_MOUNT")" ]; then
  echo "create-volume: há arquivos em $LUKS_MOUNT; nada foi feito." >&2
  exit 1
fi
[ "$(id -u)" = 0 ] || { echo "Rode como root." >&2; exit 1; }

set -a
# shellcheck disable=SC1090
. "$LUKS_ENV"
set +a

# The same request as converge-agent-unlock: credentials on curl's stdin, the
# key only in a shell variable.
fetch_key() {
  printf 'user = "%s:%s"\n' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" \
    | curl -f -sS --max-time 20 -K - \
        --aws-sigv4 "aws:amz:${AWS_DEFAULT_REGION}:ssm" \
        -H "X-Amz-Target: AmazonSSM.GetParameter" \
        -H "Content-Type: application/x-amz-json-1.1" \
        -d "{\"Name\":\"${LUKS_SSM_PARAMETER}\",\"WithDecryption\":true}" \
        "https://ssm.${AWS_DEFAULT_REGION}.amazonaws.com/" \
    | python3 -c 'import json, sys; sys.stdout.write(json.load(sys.stdin)["Parameter"]["Value"])'
}

key="$(fetch_key)"
[ -n "$key" ] || { echo "create-volume: a chave veio vazia da AWS." >&2; exit 1; }

fallocate -l "$LUKS_SIZE" "$LUKS_IMAGE"
chmod 600 "$LUKS_IMAGE"
printf '%s' "$key" | cryptsetup luksFormat --batch-mode --type luks2 \
  --cipher aes-xts-plain64 --key-size 512 --pbkdf argon2id --key-file=- "$LUKS_IMAGE"
printf '%s' "$key" | cryptsetup open --type luks2 --key-file=- "$LUKS_IMAGE" "$LUKS_NAME"
unset key AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

mkfs.ext4 -q -L converge-agent "$LUKS_MAPPER_DIR/$LUKS_NAME"
mkdir -p "$LUKS_MOUNT"
# Immutable while empty: nothing can create files in it on the root disk.
chattr +i "$LUKS_MOUNT"
mount -o noatime "$LUKS_MAPPER_DIR/$LUKS_NAME" "$LUKS_MOUNT"
mkdir -m 700 "$LUKS_MOUNT/postgres" "$LUKS_MOUNT/redis"
echo "Volume criado e montado em $LUKS_MOUNT (postgres/ e redis/ vazios)."
