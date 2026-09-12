#!/usr/bin/env bash
# Вход бота в Google (Application Default Credentials).
# Откроется браузер: выберите свой Google-аккаунт и разрешите доступ
# к Google Таблицам и Google Диску — обе галочки обязательны.
export CLOUDSDK_PYTHON="${CLOUDSDK_PYTHON:-/opt/homebrew/bin/python3.12}"
GCLOUD="${GCLOUD:-$HOME/google-cloud-sdk/google-cloud-sdk/bin/gcloud}"

"${GCLOUD}" auth application-default login \
  --scopes=https://www.googleapis.com/auth/spreadsheets,https://www.googleapis.com/auth/drive,openid,https://www.googleapis.com/auth/userinfo.email

echo
if [ -f "$HOME/.config/gcloud/application_default_credentials.json" ]; then
  echo "✅ Вход выполнен. Можно запускать выгрузку: /gsheets в боте."
else
  echo "⚠️  Файл учётных данных не появился — вход не завершён. Попробуйте ещё раз."
fi
