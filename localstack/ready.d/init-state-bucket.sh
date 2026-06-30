#!/usr/bin/env bash

# ========================================
# LocalStack init hook: create the Terraform state bucket
# ========================================
# Runs INSIDE the LocalStack container once it reports "ready"
# (mounted at /etc/localstack/init/ready.d). The S3 backend defined in
# root.hcl expects the bucket to already exist, so we create it here.
#
# Bucket name matches root.hcl: terraform-state-<AWS_ACCOUNT_ID>

set -euo pipefail

ACCOUNT_ID="${TF_STATE_ACCOUNT_ID:-000000000000}"
REGION="${DEFAULT_REGION:-us-east-2}"
BUCKET="terraform-state-${ACCOUNT_ID}"

echo "[init] Ensuring Terraform state bucket: ${BUCKET} (${REGION})"

# us-east-1 (and every non-us-east-1 region) needs a LocationConstraint
awslocal s3api create-bucket \
  --bucket "${BUCKET}" \
  --region "${REGION}" \
  --create-bucket-configuration "LocationConstraint=${REGION}" 2>/dev/null \
  || awslocal s3api create-bucket --bucket "${BUCKET}" --region "${REGION}" 2>/dev/null \
  || echo "[init] Bucket ${BUCKET} already exists"

awslocal s3api put-bucket-versioning \
  --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled

echo "[init] State bucket ready: ${BUCKET}"
