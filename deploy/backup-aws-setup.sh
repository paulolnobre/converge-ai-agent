#!/usr/bin/env bash
# One-time: the S3 bucket the VPS's restic repository lives in, and the key the
# VPS uses. Run on your own machine as an AWS admin profile, never on the VPS:
#   AWS_PROFILE=admin bash deploy/backup-aws-setup.sh <bucket-name>
#
# What it makes, in São Paulo (sa-east-1) so the data never leaves Brazil:
#   - a private bucket: public access blocked, TLS only, SSE-S3 at rest (under
#     restic's own encryption)
#   - versioning with Object Lock, GOVERNANCE, 30 days: an uploaded object
#     cannot be destroyed for 30 days, not even with an admin key that lacks
#     the bypass permission
#   - a lifecycle rule that removes an object's old versions 14 days after
#     restic deletes it. Never one that expires current objects: restic keeps
#     using old packs for as long as a snapshot needs them, so a rule like that
#     would corrupt the repository. Retention is restic's job (forget --keep-within).
#   - IAM user converge-backup-writer: read, write, list and delete in this
#     bucket, which is what restic needs — but not s3:DeleteObjectVersion, so
#     its deletes only hide objects, and a stolen key cannot destroy backups.
#
# Prints the key once, in the shape /etc/converge-backup.env expects. Safe to
# run again except for create-access-key, which makes a new key each time
# (IAM allows two per user): delete the old one when you rotate.
set -euo pipefail

bucket="${1:?usage: backup-aws-setup.sh <bucket-name>}"
region=sa-east-1
user=converge-backup-writer
lock_days=30
noncurrent_days=14

if ! command -v aws >/dev/null; then
  aws() { docker run --rm -i -v "$HOME/.aws:/root/.aws:ro" -e AWS_PROFILE amazon/aws-cli "$@"; }
fi

if ! aws s3api head-bucket --bucket "$bucket" 2>/dev/null; then
  aws s3api create-bucket --bucket "$bucket" --region "$region" \
    --create-bucket-configuration "LocationConstraint=$region" \
    --object-lock-enabled-for-bucket
fi

aws s3api put-public-access-block --bucket "$bucket" --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

aws s3api put-bucket-encryption --bucket "$bucket" --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

aws s3api put-object-lock-configuration --bucket "$bucket" --object-lock-configuration \
  "{\"ObjectLockEnabled\":\"Enabled\",\"Rule\":{\"DefaultRetention\":{\"Mode\":\"GOVERNANCE\",\"Days\":$lock_days}}}"

# Object Lock turns versioning on: restic's deletes leave a noncurrent version,
# removed 14 days later (and never before its 30-day lock runs out).
aws s3api put-bucket-lifecycle-configuration --bucket "$bucket" --lifecycle-configuration "{
  \"Rules\": [{
    \"ID\": \"remove-deleted-versions\",
    \"Status\": \"Enabled\",
    \"Filter\": {},
    \"NoncurrentVersionExpiration\": {\"NoncurrentDays\": $noncurrent_days},
    \"AbortIncompleteMultipartUpload\": {\"DaysAfterInitiation\": 1},
    \"Expiration\": {\"ExpiredObjectDeleteMarker\": true}
  }]
}"

aws s3api put-bucket-policy --bucket "$bucket" --policy "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Sid\": \"TlsOnly\",
    \"Effect\": \"Deny\",
    \"Principal\": \"*\",
    \"Action\": \"s3:*\",
    \"Resource\": [\"arn:aws:s3:::$bucket\", \"arn:aws:s3:::$bucket/*\"],
    \"Condition\": {\"Bool\": {\"aws:SecureTransport\": \"false\"}}
  }]
}"

aws iam get-user --user-name "$user" >/dev/null 2>&1 || aws iam create-user --user-name "$user" >/dev/null
aws iam put-user-policy --user-name "$user" --policy-name restic-backups --policy-document "{
  \"Version\": \"2012-10-17\",
  \"Statement\": [{
    \"Effect\": \"Allow\",
    \"Action\": [\"s3:ListBucket\", \"s3:GetBucketLocation\"],
    \"Resource\": \"arn:aws:s3:::$bucket\"
  }, {
    \"Effect\": \"Allow\",
    \"Action\": [\"s3:GetObject\", \"s3:PutObject\", \"s3:DeleteObject\"],
    \"Resource\": \"arn:aws:s3:::$bucket/*\"
  }]
}"

aws iam create-access-key --user-name "$user" --output text \
  --query 'AccessKey.[AccessKeyId,SecretAccessKey]' | {
  read -r id secret
  cat <<EOF

Bucket $bucket ready in $region. For /etc/converge-backup.env on the VPS:

RESTIC_REPOSITORY=s3:s3.$region.amazonaws.com/$bucket/vps
AWS_DEFAULT_REGION=$region
AWS_ACCESS_KEY_ID=$id
AWS_SECRET_ACCESS_KEY=$secret

This key is shown once. It cannot destroy a backup; restore checks use your own profile.
EOF
}
