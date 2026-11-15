#!/usr/bin/env bash
# One-time, on the VPS: add the Web Push (VAPID) keys to the stack's .env.
#   bash /opt/converge-agent/push-keys.sh /opt/converge-agent/.env
#
# A P-256 pair made here by openssl, in base64url: the public key goes to the
# browsers, the private one signs every notification. Nothing is printed: copy
# the private key into the password manager by reading the .env in your own
# terminal. Refuses a file that already has them — a new pair would silence
# every subscribed device until it subscribes again. Restart the bot after.
set -euo pipefail

env_file="${1:?usage: push-keys.sh <env-file>}"
if grep -Eq '^PUSH_VAPID_(PUBLIC|PRIVATE)_KEY=.' "$env_file"; then
  echo "push-keys: $env_file já tem as chaves; nada foi feito." >&2
  exit 1
fi
b64url() { base64 -w0 | tr '+/' '-_' | tr -d '='; }
# The key lives in files in a private folder in RAM, removed on exit. Bytes are
# cut from the files, never from a pipe a reader closes early (with pipefail,
# `tail | head -c` died of SIGPIPE about 1 run in 10 on the server).
work="$(mktemp -d -p "$( [ -d /dev/shm ] && echo /dev/shm || echo /tmp )")"
trap 'rm -rf "$work"' EXIT
openssl ecparam -name prime256v1 -genkey -noout -out "$work/key.pem" 2>/dev/null
openssl ec -in "$work/key.pem" -outform DER -out "$work/key.der" 2>/dev/null
openssl ec -in "$work/key.pem" -pubout -outform DER -out "$work/pub.der" 2>/dev/null
# SEC1 DER: the 32-byte private scalar starts at byte 8.
private="$(dd if="$work/key.der" bs=1 skip=7 count=32 status=none | b64url)"
# SubjectPublicKeyInfo DER ends with the 65-byte uncompressed point.
public="$(dd if="$work/pub.der" bs=1 skip=$(( $(stat -c %s "$work/pub.der") - 65 )) status=none | b64url)"
if [ "${#private}" -ne 43 ] || [ "${#public}" -ne 87 ]; then
  echo "push-keys: não consegui gerar as chaves; nada foi feito." >&2
  exit 1
fi
# The empty lines copied from .env.example give way to the new ones.
sed -i -E '/^PUSH_VAPID_(PUBLIC_KEY|PRIVATE_KEY|SUBJECT)=/d' "$env_file"
# A last line without a newline would swallow the first key.
if [ -s "$env_file" ] && [ -n "$(tail -c 1 "$env_file")" ]; then
  echo >> "$env_file"
fi
{
  printf 'PUSH_VAPID_PUBLIC_KEY=%s\n' "$public"
  printf 'PUSH_VAPID_PRIVATE_KEY=%s\n' "$private"
  printf 'PUSH_VAPID_SUBJECT=mailto:team@getconverge.me\n'
} >> "$env_file"
echo "chaves de notificação gravadas em $env_file; reinicie o bot"
