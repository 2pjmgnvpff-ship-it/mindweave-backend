#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

SERVICE_NAME="${SERVICE_NAME:-mindweave-backend}"
REGION="${REGION:-asia-east1}"
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"

ENVIRONMENT="${MINDWEAVE_ENVIRONMENT:-staging}"
AI_PRIMARY_PROVIDER="${MINDWEAVE_AI_PRIMARY_PROVIDER:-${AI_PRIMARY_PROVIDER:-gemini}}"
AI_FALLBACK_PROVIDER="${MINDWEAVE_AI_FALLBACK_PROVIDER:-none}"
GEMINI_MODEL_FAST="${MINDWEAVE_GEMINI_MODEL_FAST:-gemini-3.8-flash}"
GEMINI_MODEL_CHAT="${MINDWEAVE_GEMINI_MODEL_CHAT:-gemini-3.8-flash}"
GEMINI_MODEL_LITE="${MINDWEAVE_GEMINI_MODEL_LITE:-gemini-3.5-flash-lite}"
EMAIL_PROVIDER="${MINDWEAVE_EMAIL_PROVIDER:-${EMAIL_PROVIDER:-resend}}"
EMAIL_FROM_NAME="${MINDWEAVE_EMAIL_FROM_NAME:-心络 MindWeave}"
EMAIL_FROM_ADDRESS="${MINDWEAVE_EMAIL_FROM_ADDRESS:-${EMAIL_FROM_ADDRESS:-}}"
RESEND_API_KEY="${MINDWEAVE_RESEND_API_KEY:-${RESEND_API_KEY:-}}"
SMTP_HOST="${MINDWEAVE_SMTP_HOST:-${SMTP_HOST:-}}"
SMTP_PORT="${MINDWEAVE_SMTP_PORT:-${SMTP_PORT:-465}}"
SMTP_USERNAME="${MINDWEAVE_SMTP_USERNAME:-${SMTP_USERNAME:-}}"
SMTP_PASSWORD="${MINDWEAVE_SMTP_PASSWORD:-${SMTP_PASSWORD:-}}"
SMTP_USE_SSL="${MINDWEAVE_SMTP_USE_SSL:-true}"
SMTP_USE_STARTTLS="${MINDWEAVE_SMTP_USE_STARTTLS:-false}"
CORS_ORIGINS="${MINDWEAVE_CORS_ORIGINS:-[\"mindweave://app\"]}"
TRUSTED_PROXY_HOPS="${MINDWEAVE_TRUSTED_PROXY_HOPS:-1}"

# -----------------------------------------------------------------------------
# Pre-Flight Fail-Closed Validation (Exits 1 BEFORE triggering Cloud Build!)
# -----------------------------------------------------------------------------
if [[ "$ENVIRONMENT" == "staging" || "$ENVIRONMENT" == "prod_cn" ]]; then
  if [[ "$EMAIL_PROVIDER" == "resend" ]]; then
    if [[ -z "$RESEND_API_KEY" || -z "$EMAIL_FROM_ADDRESS" ]]; then
      echo "❌ EMAIL_PROVIDER=resend 需要设置 MINDWEAVE_RESEND_API_KEY 和 MINDWEAVE_EMAIL_FROM_ADDRESS。" >&2
      echo "   提示：若暂无已验证发信域名，可改用 QQ / Gmail 零域名 fallback：" >&2
      echo "   EMAIL_PROVIDER=smtp SMTP_HOST=smtp.qq.com SMTP_PORT=465 SMTP_USERNAME=你的QQ@qq.com SMTP_PASSWORD=16位授权码" >&2
      exit 1
    fi
    if [[ "${EMAIL_FROM_ADDRESS,,}" == *"@resend.dev" ]]; then
      echo "❌ Resend 测试发件人 onboarding@resend.dev 在 staging/prod_cn 中禁止使用（仅能发给自己账号邮箱）。" >&2
      echo "   请在 resend.com/domains 验证子域名（2026 年 8 月后为 CNAME SPF/DKIM，务必关闭 Cloudflare 橙云代理；" >&2
      echo "   DMARC 请用 v=DMARC1; p=quarantine; 并遵循 RFC 9989 省略旧版 pct=），或改用 EMAIL_PROVIDER=smtp（QQ/Gmail SSL 465）。" >&2
      exit 1
    fi
  elif [[ "$EMAIL_PROVIDER" == "smtp" ]]; then
    if [[ -z "$SMTP_HOST" || -z "$SMTP_USERNAME" || -z "$SMTP_PASSWORD" ]]; then
      echo "❌ EMAIL_PROVIDER=smtp 需要设置 SMTP_HOST (如 smtp.qq.com / smtp.gmail.com)、SMTP_USERNAME 与 SMTP_PASSWORD（16 位授权码/App Password）。" >&2
      exit 1
    fi
    if [[ -z "$EMAIL_FROM_ADDRESS" ]]; then
      EMAIL_FROM_ADDRESS="$SMTP_USERNAME"
    fi
  else
    echo "❌ staging / prod_cn 禁止使用 EMAIL_PROVIDER=${EMAIL_PROVIDER}，请配置 resend 或 smtp。" >&2
    exit 1
  fi

  case "$AI_PRIMARY_PROVIDER" in
    gemini)
      if [[ -z "${MINDWEAVE_GEMINI_API_KEY:-${GEMINI_API_KEY:-}}" ]]; then
        echo "❌ AI_PRIMARY_PROVIDER=gemini 需要设置 GEMINI_API_KEY（默认模型 ${GEMINI_MODEL_FAST}）。" >&2
        exit 1
      fi
      ;;
    deepseek)
      if [[ -z "${MINDWEAVE_DEEPSEEK_API_KEY:-${DEEPSEEK_API_KEY:-}}" ]]; then
        echo "❌ AI_PRIMARY_PROVIDER=deepseek 需要设置 DEEPSEEK_API_KEY。" >&2
        exit 1
      fi
      ;;
    dashscope)
      if [[ -z "${MINDWEAVE_DASHSCOPE_API_KEY:-${DASHSCOPE_API_KEY:-}}" ]]; then
        echo "❌ AI_PRIMARY_PROVIDER=dashscope 需要设置 DASHSCOPE_API_KEY。" >&2
        exit 1
      fi
      ;;
    ark)
      if [[ -z "${MINDWEAVE_ARK_API_KEY:-${ARK_API_KEY:-}}" ]]; then
        echo "❌ AI_PRIMARY_PROVIDER=ark 需要设置 ARK_API_KEY。" >&2
        exit 1
      fi
      ;;
    *)
      echo "❌ staging / prod_cn 禁止使用 AI_PRIMARY_PROVIDER=${AI_PRIMARY_PROVIDER}。" >&2
      exit 1
      ;;
  esac
fi

if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  echo "🔍 正在自动检测你账号下的 Google Cloud / AI Studio 项目..."
  PROJECT_ID="$(gcloud projects list --filter="lifecycleState:ACTIVE" --format="value(projectId)" --limit=1 2>/dev/null || true)"
  if [[ -z "$PROJECT_ID" ]]; then
    echo "❌ 未检测到可用的 Google Cloud 项目。请先在 https://console.cloud.google.com 创建一个项目，或运行：" >&2
    echo "   PROJECT_ID=你的项目ID bash scripts/deploy_cloudrun.sh" >&2
    exit 1
  fi
  gcloud config set project "$PROJECT_ID" >/dev/null
fi

# -----------------------------------------------------------------------------
# Mode A (External Managed PG15) vs Mode B (Warm Single-Instance Embedded PG15)
# -----------------------------------------------------------------------------
APP_DATABASE_URL="${MINDWEAVE_APP_DATABASE_URL:-${APP_DATABASE_URL:-}}"
SYSTEM_DATABASE_URL="${MINDWEAVE_SYSTEM_DATABASE_URL:-${SYSTEM_DATABASE_URL:-}}"
MIGRATION_DATABASE_URL="${MINDWEAVE_MIGRATION_DATABASE_URL:-${MIGRATION_DATABASE_URL:-}}"

DEPLOY_FLAGS=(
  --source .
  --project "$PROJECT_ID"
  --region "$REGION"
  --platform managed
  --execution-environment gen2
  --min-instances 1
  --cpu 1
  --cpu-boost
  --concurrency 40
  --timeout 60
  --port 8000
  --allow-unauthenticated
)

if [[ -n "$APP_DATABASE_URL" ]]; then
  echo "🗄️  模式 A：外部托管 PostgreSQL 15（推荐持久化生产模式）"
  DEPLOY_FLAGS+=(--max-instances 4 --memory 1Gi)
  if [[ -n "${CLOUDSQL_INSTANCE:-}" ]]; then
    DEPLOY_FLAGS+=(--add-cloudsql-instances "$CLOUDSQL_INSTANCE")
  fi
  EMBED_WORKER="${MINDWEAVE_EMBED_WORKER_IN_API:-false}"
else
  echo "⚠️  模式 B：单实例预热内嵌 PostgreSQL 15（/tmp 数据随重新部署重置；长期个人手账强烈建议配置 Neon / Cloud SQL！）"
  DEPLOY_FLAGS+=(--max-instances 1 --memory 2Gi --no-cpu-throttling)
  EMBED_WORKER="${MINDWEAVE_EMBED_WORKER_IN_API:-true}"
fi

# -----------------------------------------------------------------------------
# Safe Temp YAML Env File (chmod 600 + trap cleanup; never comma-delimited --set-env-vars!)
# -----------------------------------------------------------------------------
ENV_YAML="$(mktemp /tmp/mw-cloudrun-env.XXXXXX.yaml)"
chmod 600 "$ENV_YAML"
trap 'rm -f "$ENV_YAML"' EXIT

yaml_quote() {
  local val="$1"
  val="${val//\\/\\\\}"
  val="${val//\"/\\\"}"
  printf '"%s"' "$val"
}

{
  printf 'MINDWEAVE_ENVIRONMENT: %s\n' "$(yaml_quote "$ENVIRONMENT")"
  printf 'MINDWEAVE_DEBUG: "false"\n'
  printf 'MINDWEAVE_ALLOW_DEV_MOCK_APPLE_AUTH: "false"\n'
  printf 'MINDWEAVE_EXPOSE_DEV_OTP_IN_RESPONSE: "false"\n'
  printf 'MINDWEAVE_PUSH_APNS_ADAPTER: "disabled"\n'
  printf 'MINDWEAVE_PUSH_WEB_PUSH_ADAPTER: "disabled"\n'
  printf 'MINDWEAVE_MIGRATE_ON_BOOT: "true"\n'
  printf 'MINDWEAVE_EMBED_WORKER_IN_API: %s\n' "$(yaml_quote "$EMBED_WORKER")"
  printf 'MINDWEAVE_CORS_ORIGINS: %s\n' "$(yaml_quote "$CORS_ORIGINS")"
  printf 'MINDWEAVE_TRUSTED_PROXY_HOPS: %s\n' "$(yaml_quote "$TRUSTED_PROXY_HOPS")"
  printf 'MINDWEAVE_AI_PRIMARY_PROVIDER: %s\n' "$(yaml_quote "$AI_PRIMARY_PROVIDER")"
  printf 'MINDWEAVE_AI_FALLBACK_PROVIDER: %s\n' "$(yaml_quote "$AI_FALLBACK_PROVIDER")"
  printf 'MINDWEAVE_GEMINI_MODEL_FAST: %s\n' "$(yaml_quote "$GEMINI_MODEL_FAST")"
  printf 'MINDWEAVE_GEMINI_MODEL_CHAT: %s\n' "$(yaml_quote "$GEMINI_MODEL_CHAT")"
  printf 'MINDWEAVE_GEMINI_MODEL_LITE: %s\n' "$(yaml_quote "$GEMINI_MODEL_LITE")"
  printf 'MINDWEAVE_GEMINI_PAID_TIER_ACKNOWLEDGED: "true"\n'
  printf 'MINDWEAVE_EMAIL_PROVIDER: %s\n' "$(yaml_quote "$EMAIL_PROVIDER")"
  printf 'MINDWEAVE_EMAIL_FROM_NAME: %s\n' "$(yaml_quote "$EMAIL_FROM_NAME")"
  printf 'MINDWEAVE_EMAIL_FROM_ADDRESS: %s\n' "$(yaml_quote "$EMAIL_FROM_ADDRESS")"
  printf 'MINDWEAVE_OTP_RESEND_COOLDOWN_SECONDS: "%s"\n' "${MINDWEAVE_OTP_RESEND_COOLDOWN_SECONDS:-60}"

  if [[ -n "${MINDWEAVE_GEMINI_API_KEY:-${GEMINI_API_KEY:-}}" ]]; then
    printf 'MINDWEAVE_GEMINI_API_KEY: %s\n' "$(yaml_quote "${MINDWEAVE_GEMINI_API_KEY:-${GEMINI_API_KEY:-}}")"
  fi
  if [[ -n "${MINDWEAVE_DEEPSEEK_API_KEY:-${DEEPSEEK_API_KEY:-}}" ]]; then
    printf 'MINDWEAVE_DEEPSEEK_API_KEY: %s\n' "$(yaml_quote "${MINDWEAVE_DEEPSEEK_API_KEY:-${DEEPSEEK_API_KEY:-}}")"
  fi
  if [[ -n "${MINDWEAVE_DASHSCOPE_API_KEY:-${DASHSCOPE_API_KEY:-}}" ]]; then
    printf 'MINDWEAVE_DASHSCOPE_API_KEY: %s\n' "$(yaml_quote "${MINDWEAVE_DASHSCOPE_API_KEY:-${DASHSCOPE_API_KEY:-}}")"
  fi
  if [[ -n "${MINDWEAVE_AI_ASR_PROVIDER:-}" ]]; then
    printf 'MINDWEAVE_AI_ASR_PROVIDER: %s\n' "$(yaml_quote "${MINDWEAVE_AI_ASR_PROVIDER}")"
  fi
  if [[ -n "${MINDWEAVE_DASHSCOPE_ASR_MODEL:-}" ]]; then
    printf 'MINDWEAVE_DASHSCOPE_ASR_MODEL: %s\n' "$(yaml_quote "${MINDWEAVE_DASHSCOPE_ASR_MODEL}")"
  fi
  if [[ -n "${MINDWEAVE_ARK_API_KEY:-${ARK_API_KEY:-}}" ]]; then
    printf 'MINDWEAVE_ARK_API_KEY: %s\n' "$(yaml_quote "${MINDWEAVE_ARK_API_KEY:-${ARK_API_KEY:-}}")"
  fi
  if [[ "$EMAIL_PROVIDER" == "resend" && -n "$RESEND_API_KEY" ]]; then
    printf 'MINDWEAVE_RESEND_API_KEY: %s\n' "$(yaml_quote "$RESEND_API_KEY")"
  fi
  if [[ "$EMAIL_PROVIDER" == "smtp" ]]; then
    printf 'MINDWEAVE_SMTP_HOST: %s\n' "$(yaml_quote "$SMTP_HOST")"
    printf 'MINDWEAVE_SMTP_PORT: %s\n' "$(yaml_quote "$SMTP_PORT")"
    printf 'MINDWEAVE_SMTP_USERNAME: %s\n' "$(yaml_quote "$SMTP_USERNAME")"
    printf 'MINDWEAVE_SMTP_PASSWORD: %s\n' "$(yaml_quote "$SMTP_PASSWORD")"
    printf 'MINDWEAVE_SMTP_USE_SSL: %s\n' "$(yaml_quote "$SMTP_USE_SSL")"
    printf 'MINDWEAVE_SMTP_USE_STARTTLS: %s\n' "$(yaml_quote "$SMTP_USE_STARTTLS")"
  fi
  if [[ -n "$APP_DATABASE_URL" ]]; then
    printf 'MINDWEAVE_APP_DATABASE_URL: %s\n' "$(yaml_quote "$APP_DATABASE_URL")"
  fi
  if [[ -n "$SYSTEM_DATABASE_URL" ]]; then
    printf 'MINDWEAVE_SYSTEM_DATABASE_URL: %s\n' "$(yaml_quote "$SYSTEM_DATABASE_URL")"
  fi
  if [[ -n "$MIGRATION_DATABASE_URL" ]]; then
    printf 'MINDWEAVE_MIGRATION_DATABASE_URL: %s\n' "$(yaml_quote "$MIGRATION_DATABASE_URL")"
  fi
  if [[ -n "${MINDWEAVE_KEK_HEX:-}" ]]; then
    printf 'MINDWEAVE_KEK_HEX: %s\n' "$(yaml_quote "$MINDWEAVE_KEK_HEX")"
  fi
  if [[ -n "${MINDWEAVE_JWT_SECRET:-}" ]]; then
    printf 'MINDWEAVE_JWT_SECRET: %s\n' "$(yaml_quote "$MINDWEAVE_JWT_SECRET")"
  fi
} > "$ENV_YAML"

echo "🚀 目标 Google Cloud 项目: ${PROJECT_ID} (区域: ${REGION}, 环境: ${ENVIRONMENT})"
echo "✨ AI 主引擎: ${AI_PRIMARY_PROVIDER} (${GEMINI_MODEL_FAST} / ${GEMINI_MODEL_LITE}) | 发信通道: ${EMAIL_PROVIDER}"
echo "⏳ 正在开启 Cloud Run / Cloud Build 服务接口（首次约需 20 秒）..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$PROJECT_ID" \
  --quiet

echo "📦 正在构建容器并部署至 Google Cloud Run（--min-instances 1 零冷启动）..."
gcloud run deploy "$SERVICE_NAME" \
  "${DEPLOY_FLAGS[@]}" \
  --env-vars-file "$ENV_YAML" \
  --quiet

SERVICE_URL="$(gcloud run services describe "$SERVICE_NAME" --project "$PROJECT_ID" --region "$REGION" --format="value(status.url)")"

echo ""
echo "=================================================================="
echo "✅ MindWeave (心络) 部署成功！已生成 Google Cloud Run 公共链接："
echo "🌐 应用访问入口   : ${SERVICE_URL}"
echo "🩺 就绪状态探针   : ${SERVICE_URL}/readyz"
echo "🔎 运行态自检     : ${SERVICE_URL}/v1/meta/runtime-status"
echo "=================================================================="
