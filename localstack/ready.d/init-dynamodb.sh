#!/usr/bin/env bash

# ========================================
# LocalStack init hook: create the DynamoDB metadata table
# ========================================
# Runs INSIDE the LocalStack container once it reports "ready"

set -euo pipefail

TABLE_NAME="VpcProvisionerApiMetadata"

echo "[init] Ensuring DynamoDB table: ${TABLE_NAME}"

awslocal dynamodb create-table \
  --table-name "${TABLE_NAME}" \
  --attribute-definitions AttributeName=VpcId,AttributeType=S \
  --key-schema AttributeName=VpcId,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST 2>/dev/null \
  || echo "[init] DynamoDB Table ${TABLE_NAME} already exists or failed to create"

echo "[init] DynamoDB setup complete for ${TABLE_NAME}"
