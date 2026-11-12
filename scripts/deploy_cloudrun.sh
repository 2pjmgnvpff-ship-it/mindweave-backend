#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICE_NAME="${SERVICE_NAME:-mindweave-backend}"
REGION="${REGION:-asia-east1}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"

if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  echo "🔍 正在自动检测你账号下的 Google Cloud / AI Studio 项目..."
  PROJECT_ID="$(gcloud projects list --filter="lifecycleState:ACTIVE" --format="value(projectId)" --limit=1 2>/dev/null || true)"
  if [[ -z "$PROJECT_ID" ]]; then
    echo "❌ 未检测到可用的 Google Cloud 项目。请先在 https://console.cloud.google.com 创建一个项目，或运行："
    echo "   PROJECT_ID=你的项目ID bash scripts/deploy_cloudrun.sh"
    exit 1
  fi
  gcloud config set project "$PROJECT_ID" >/dev/null
fi

echo "🚀 目标 Google Cloud 项目: ${PROJECT_ID} (区域: ${REGION})"
echo "⏳ 正在开启 Cloud Run / Cloud Build 服务接口（首次约需 20 秒）..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$PROJECT_ID" \
  --quiet

ENV_VARS="MINDWEAVE_ENVIRONMENT=dev"
if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  echo "✨ 已检测到 GEMINI_API_KEY，将自动启用真实 Gemini 2.5 Flash 模型！"
  ENV_VARS="${ENV_VARS},MINDWEAVE_GEMINI_API_KEY=${GEMINI_API_KEY}"
fi

echo "📦 正在构建容器并部署至 Google Cloud Run（约需 1.5 ~ 2.5 分钟）..."
gcloud run deploy "$SERVICE_NAME" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --platform managed \
  --execution-environment gen2 \
  --cpu-boost \
  --allow-unauthenticated \
  --memory 1Gi \
  --cpu 1 \
  --max-instances 1 \
  --port 8000 \
  --set-env-vars "$ENV_VARS" \
  --quiet

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" --format="value(status.url)")"

echo ""
echo "=================================================================="
echo "✅ MindWeave (心络) 部署成功！已生成 Google Cloud Run 公共链接："
echo "🌐 心络网页端成品 : ${SERVICE_URL}"
echo "📖 Swagger API文档: ${SERVICE_URL}/docs"
echo "🩺 数据库就绪探针 : ${SERVICE_URL}/readyz"
echo "=================================================================="
