#!/usr/bin/env bash
set -euo pipefail

PROJECT="gdrive-mcp-492818"
REGION="us-central1"
SERVICE="gdrive-mcp"
IMAGE="us-central1-docker.pkg.dev/$PROJECT/cloud-run-source-deploy/$SERVICE:latest"

cd "$(dirname "$0")/.."

# Check gcloud auth, re-auth if expired
if ! gcloud auth print-access-token --project="$PROJECT" &>/dev/null; then
  echo "gcloud auth expired — logging in..."
  gcloud auth login --project="$PROJECT"
fi

echo "==> Building image via Cloud Build..."
gcloud builds submit \
  --tag "$IMAGE" \
  --project="$PROJECT" --quiet

echo "==> Deploying to Cloud Run..."
gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --project="$PROJECT" \
  --quiet

echo "==> Deployed. Running smoke test..."
URL="https://gdrive-mcp-1055579418514.us-central1.run.app/mcp"
KEY=$(gcloud secrets versions access latest --secret=gdrive-mcp-api-key --project="$PROJECT")

TOOL_COUNT=$(curl -s -X POST "$URL" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer $KEY" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | sed -n 's/^data: //p' \
  | python3 -c "import sys, json; r=json.loads(sys.stdin.read()); print(len(r['result']['tools']))")

echo "$TOOL_COUNT tools exposed"
if [ "$TOOL_COUNT" = "24" ]; then
  echo "Smoke test passed."
else
  echo "WARNING: Expected 24 tools, got $TOOL_COUNT"
  exit 1
fi
