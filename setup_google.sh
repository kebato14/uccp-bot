#!/usr/bin/env bash
# Настройка Google Sheets для бота УЦЦП.
# Запускать ПОСЛЕ `gcloud auth login`.
set -euo pipefail

# gcloud распакован в ~/google-cloud-sdk и требует Python 3.10+
GCLOUD="${GCLOUD:-$HOME/google-cloud-sdk/google-cloud-sdk/bin/gcloud}"
export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-/opt/homebrew/bin/python3.12}"
command -v gcloud >/dev/null 2>&1 && GCLOUD="$(command -v gcloud)"

PROJECT="${1:?укажите ID проекта Google Cloud: ./setup_google.sh my-project}"
SA="sheets-bot-sa"
EMAIL="${SA}@${PROJECT}.iam.gserviceaccount.com"
KEY="$(dirname "$0")/google_credentials.json"

echo "→ Проект: ${PROJECT}"
"${GCLOUD}" config set project "${PROJECT}"

echo "→ Включаю Google Sheets API и Google Drive API…"
"${GCLOUD}" services enable sheets.googleapis.com drive.googleapis.com

echo "→ Сервисный аккаунт ${SA}…"
"${GCLOUD}" iam service-accounts create "${SA}" \
  --display-name="Sheets Bot Service Account" 2>/dev/null \
  || echo "   уже существует, пропускаю"

echo "→ Создаю ключ ${KEY}…"
if "${GCLOUD}" iam service-accounts keys create "${KEY}" --iam-account="${EMAIL}" 2>/tmp/gcloud_key_err; then
  chmod 600 "${KEY}"
  echo
  echo "✅ Готово. Ключ: ${KEY}"
  echo "   Адрес сервисного аккаунта: ${EMAIL}"
  echo "   Откройте доступ к таблице для этого адреса (роль «Редактор»),"
  echo "   либо оставьте GOOGLE_SHARE_EMAIL в .env — бот создаст таблицу сам."
else
  rm -f "${KEY}"
  echo
  echo "⚠️  Политика организации запрещает создавать ключи сервисных аккаунтов:"
  sed -n "1,3p" /tmp/gcloud_key_err
  echo
  echo "Это не проблема — используйте вход от своего аккаунта (ADC):"
  echo
  echo "  CLOUDSDK_PYTHON=/opt/homebrew/bin/python3.12 \\"
  echo "  ${GCLOUD} auth application-default login \\"
  echo "    --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive,openid,https://www.googleapis.com/auth/userinfo.email"
  echo
  echo "Таблица тогда создаётся на вашем Google-диске и доступ открывать не нужно."
fi
