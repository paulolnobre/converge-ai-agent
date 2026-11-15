# Backups

The code is in GitHub. The data is not: contacts, conversations, appointments,
panel users and the automations queue live in one Postgres on the VPS, and
`.env` and `content/staff.json` exist only there. This is how they survive the
VPS.

## What runs

| | |
|---|---|
| **Tool** | [restic](https://restic.net) 0.17+ (Ubuntu's package), one repository for every stack on the server |
| **What** | per stack, two snapshots per run, tagged with the stack: `db` — `pg_dump -Fc -Z0` streamed straight into restic (never written to the VPS disk; a failed dump fails the snapshot) — and `files` — `.env`, `content/staff.json`, `google_credentials.json` |
| **Not backed up** | the Inbox's kept media (`/data/media`: photos, audio, files). It is short-lived on purpose — audio 48 h (168 h at most), the rest 90 days — and a restore must not bring back files the cleanup already removed. Their sealed file names are in the database dumps, like the rest of the conversation |
| **Encryption** | restic's, AES-256 with the repository password, before anything leaves the server. The bucket holds ciphertext only |
| **When** | daily at 03:15 BRT (systemd timer `converge-backup@<stack>.timer`), and before every release's migrations (`deploy.sh`) — a release that cannot back up stops before it changes anything |
| **Where** | S3 `sa-east-1` (São Paulo) |
| **Kept** | snapshots 30 days (`forget --keep-within 30d`, daily); unreferenced data pruned on Sundays, when 5% of the data is also read back and checked |
| **Who can destroy a backup** | nobody with the VPS's key. It can read, write and delete — restic needs that — but the bucket is versioned, so a delete only hides an object; its versions stay 14 days, and Object Lock keeps every upload 30 days. The key has no `s3:DeleteObjectVersion` |
| **Watched by** | healthchecks.io (`BACKUP_PING_URL`), one check per stack (`<stack>-backup`): a failed run *and* a run that never happened both alert |
| **Stacks** | `converge-agent` and `ai-agents-construction` — `backup.sh` ships with this repository and backs up any stack in `/opt` |
| **Cost** | well under US$ 0.10/month at today's size (≈10 MB of data, compressed ~6× and deduplicated day to day) |

Files: [deploy/backup.sh](../deploy/backup.sh) (the run),
[deploy/restore-check.sh](../deploy/restore-check.sh) (the proof),
[deploy/backup-aws-setup.sh](../deploy/backup-aws-setup.sh) (the bucket),
[deploy/backup-install.sh](../deploy/backup-install.sh) and
[deploy/systemd/](../deploy/systemd/) (the timer).

### The trade-off of restic

The VPS holds the repository password, because restic needs it to write. So
someone in control of the VPS can read the backups, as well as the live
database they already have. What they cannot do is destroy them (see above). A
stolen bucket, or a leaked AWS key on its own, yields ciphertext.

## Setting it up (once)

1. **The bucket.** On your machine: `AWS_PROFILE=<admin> bash deploy/backup-aws-setup.sh converge-backups-<something-unique>`.
   It prints `RESTIC_REPOSITORY` and the VPS's key.
2. **The server**, as root, once `backup.sh` and `backup-install.sh` are in
   `/opt/converge-agent` (every deploy ships them):
   ```bash
   bash /opt/converge-agent/backup-install.sh converge-agent ai-agents-construction
   # fill in /etc/converge-backup.env: the bucket's lines, and
   # RESTIC_PASSWORD=$(openssl rand -base64 32) — copy it to the password manager
   bash /opt/converge-agent/backup-install.sh converge-agent ai-agents-construction
   systemctl start converge-backup@converge-agent.service
   journalctl -u converge-backup@converge-agent.service -n 20
   ```
   The second run creates the repository (`restic init`) and turns the timers on.
3. **Your own key**, on your machine (`restic` installed, `RESTIC_REPOSITORY`
   exported, the server's password at hand once):
   ```bash
   AWS_PROFILE=<admin> restic key add      # asks for the server's password, then yours
   ```
   Keep yours in the password manager. With two keys, the server's can be
   rotated or revoked (`restic key remove`) without losing a single backup.
   Lose every password and every backup is noise.
4. **Prove it restores** — below. Until this has passed once, there is no backup.

## Proving a backup restores

On a machine with Docker, restic and an AWS profile that can read the bucket:

```bash
export RESTIC_REPOSITORY=s3:s3.sa-east-1.amazonaws.com/<bucket>/vps
export AWS_PROFILE=<admin>
bash deploy/restore-check.sh converge-agent            # or: ... converge-agent <snapshot-id>
```

It lists the stack's snapshots, streams the latest database snapshot into a
throwaway Postgres (no port, removed at the end) and prints every table with
its row count. Compare with production. Nothing decrypted touches the disk. Do
it once a quarter, and after any change to the schema or to these scripts.

## Restoring for real

The VPS is gone, or the data is wrong. On a fresh server bootstrapped with
`deploy/bootstrap.sh`, with the stack's compose file in place and restic
configured as above:

**On the same server** (the data is wrong, the VPS is fine), first stop the
bot and remove every kept media file and name, **before** the restored
database comes up: an older database must never meet files of someone deleted
after the backup. The restored `stored` records become `missing` on the first
cleanup pass.

```bash
cd /opt/converge-agent && docker compose stop reception-bot
docker compose run --rm --no-deps reception-bot python -m cli.media purge   # ends showing an empty status
```

On a fresh server there is nothing to purge: the media folder starts empty
(create it as in `docs/CRIPTOGRAFIA.md`, "Os arquivos do Inbox").

```bash
restic snapshots --tag converge-agent                   # pick the snapshot (a pre-deploy-* one, after a bad release)
restic restore --tag converge-agent,files latest --target /root/restore
cp /root/restore/opt/converge-agent/.env /opt/converge-agent/.env
cp /root/restore/opt/converge-agent/content/staff.json /opt/converge-agent/content/staff.json
shred -u /root/restore/opt/converge-agent/.env
cd /opt/converge-agent && docker compose up -d --wait postgres
restic dump --tag converge-agent,db latest /converge-agent-db.dump \
  | docker compose exec -T postgres sh -c \
    'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner --exit-on-error'
```

Then deploy the image tag from `/opt/converge-agent/.env` (`IMAGE_TAG`) or
newer — migrations are additive. Re-apply any deletion requests (LGPD art. 18)
handled after the snapshot's date: a restore must not bring back someone who
asked to be removed.

### If the repository was deleted

Someone with the VPS's key deleted the objects: restic finds no repository.
Within 14 days, an admin profile undoes the deletes made since the incident
began (`T`, in UTC), then clears the locks the deleted runs left behind:

```bash
T=2026-10-02T00:00:00Z
aws s3api list-object-versions --bucket <bucket> --prefix vps/ \
  --query "DeleteMarkers[?IsLatest && LastModified>='$T'].[Key,VersionId]" --output text |
while read -r key version; do
  aws s3api delete-object --bucket <bucket> --key "$key" --version-id "$version"
done
restic unlock && restic check --read-data
```

Rotate the VPS's AWS key and restic key afterwards. (Rehearsed against an
S3-compatible store with Object Lock: everything deleted, undeleted, checked
clean, restored.)

## LGPD

*Engineering notes, not legal advice — confirm with whoever answers for data
protection at Converge.*

- **No international transfer.** The bucket is in São Paulo; the data does not
  leave Brazil, so art. 33 does not come into play. AWS acts as operator under
  its data processing addendum; list it among the operators in the record of
  processing (art. 37).
- **Security (art. 46).** Encrypted before it leaves the server; TLS-only
  bucket policy; SSE-S3 at rest beneath restic's encryption; public access
  blocked; a key that cannot destroy a backup. Data that leaks from the bucket
  is unintelligible without the repository password (art. 48 §3).
- **Retention (arts. 15–16).** Fixed and automatic. A snapshot is forgotten
  after 30 days, its data pruned by the following Sunday, and the hidden
  versions removed 14 days later: personal data deleted in production is gone
  from the backups within about 51 days. State it in the privacy notice with
  margin ("backups are kept for up to 60 days").
- **Deletion requests (art. 18).** Done in production at once; backups age out
  as above and are not edited in between. Keep a log of deletions so a restore
  can re-apply them.
- **Access.** Whoever holds a repository password can read every backup: the
  VPS and Paulo today. Record who holds one; when that changes, add a new key
  and `restic key remove` the old one.
