#!/usr/bin/env bash
# One-time, on the VPS: add the field-encryption keys to the stack's .env.
#   bash /opt/converge-agent/data-keys.sh /opt/converge-agent/.env
#
# Two random 32-byte keys, in base64, appended as DATA_KEYS (version 1) and
# DATA_INDEX_KEY. Nothing is printed: copy them into the password manager by
# reading the .env in your own terminal. Refuses to touch a file that already
# has either key — replacing a key makes the data encrypted with it unreadable.
set -euo pipefail

env_file="${1:?usage: data-keys.sh <env-file>}"
if grep -Eq '^(DATA_KEYS|DATA_INDEX_KEY)=' "$env_file"; then
  echo "data-keys: $env_file já tem as chaves; nada foi feito." >&2
  exit 1
fi
key() { head -c 32 /dev/urandom | base64 -w0; }
# A last line without a newline would swallow the first key.
if [ -s "$env_file" ] && [ -n "$(tail -c 1 "$env_file")" ]; then
  echo >> "$env_file"
fi
{
  printf 'DATA_KEYS=1:%s\n' "$(key)"
  printf 'DATA_INDEX_KEY=%s\n' "$(key)"
} >> "$env_file"
echo "chaves gravadas em $env_file"
