#!/bin/bash
# ─────────────────────────────────────────────
# HealX — Test Webhook Script
#
# Simulates a GitHub Actions workflow_run failure webhook
# with proper HMAC-SHA256 signature.
#
# Usage:
#   chmod +x test_webhook.sh
#   ./test_webhook.sh
# ─────────────────────────────────────────────

# Load webhook secret from .env (or set manually)
WEBHOOK_SECRET=$(grep GITHUB_WEBHOOK_SECRET .env | cut -d '=' -f2)
API_URL="http://localhost:8000/webhook/github"

# ─── Simulated GitHub Payload ───
PAYLOAD=$(cat <<'EOF'
{
  "action": "completed",
  "workflow_run": {
    "id": 12345678,
    "name": "CI",
    "head_branch": "feature/auth-fix",
    "head_sha": "abc123def456789012345678901234567890abcd",
    "conclusion": "failure",
    "html_url": "https://github.com/test-org/test-repo/actions/runs/12345678",
    "logs_url": "https://api.github.com/repos/test-org/test-repo/actions/runs/12345678/logs"
  },
  "repository": {
    "full_name": "test-org/test-repo",
    "html_url": "https://github.com/test-org/test-repo"
  }
}
EOF
)

# ─── Compute HMAC-SHA256 Signature ───
SIGNATURE="sha256=$(echo -n "$PAYLOAD" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $2}')"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔬 Testing HealX Webhook"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "URL:       $API_URL"
echo "Secret:    ${WEBHOOK_SECRET:0:8}..."
echo "Signature: ${SIGNATURE:0:30}..."
echo ""

# ─── Send Request ───
echo "📡 Sending webhook..."
echo ""

RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST "$API_URL" \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: workflow_run" \
  -H "X-Hub-Signature-256: $SIGNATURE" \
  -d "$PAYLOAD")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | sed '$d')

echo "Response ($HTTP_CODE):"
echo "$BODY" | python3 -m json.tool 2>/dev/null || echo "$BODY"
echo ""

# ─── Quick Validation ───
if [ "$HTTP_CODE" = "202" ]; then
  echo "✅ Webhook accepted! Job enqueued."
  
  # Extract job_id and check status
  JOB_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])" 2>/dev/null)
  if [ -n "$JOB_ID" ]; then
    echo ""
    echo "📋 Checking job status..."
    sleep 1
    curl -s "http://localhost:8000/jobs/$JOB_ID" | python3 -m json.tool 2>/dev/null
  fi
elif [ "$HTTP_CODE" = "200" ]; then
  echo "⏭️  Webhook ignored (duplicate or filtered)."
elif [ "$HTTP_CODE" = "401" ]; then
  echo "❌ Signature validation failed. Check GITHUB_WEBHOOK_SECRET in .env"
else
  echo "❌ Unexpected response code: $HTTP_CODE"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Test 2: Health Check ───
echo ""
echo "🏥 Health Check:"
curl -s http://localhost:8000/health | python3 -m json.tool 2>/dev/null

# ─── Test 3: List Jobs ───
echo ""
echo "📋 All Jobs:"
curl -s http://localhost:8000/jobs | python3 -m json.tool 2>/dev/null
