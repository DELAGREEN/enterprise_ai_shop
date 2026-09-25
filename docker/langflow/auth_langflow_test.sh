#!/bin/bash

# --- Конфигурация ---
TOKEN_URL="http://localhost:8080/realms/langflow/protocol/openid-connect/token"
CLIENT_ID="langflow-app"
CLIENT_SECRET="XmDAsoUB8XX6ymOScb444WM1ev3yU37X"
USERNAME="testuser"
PASSWORD="password"
WHOAMI_URL="http://localhost:7860/api/v1/users/whoami"

# --- Получение токена ---
echo "Получение токена от Keycloak..."
RESPONSE=$(curl -s -X POST "$TOKEN_URL" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "client_id=$CLIENT_ID" \
  --data-urlencode "client_secret=$CLIENT_SECRET" \
  --data-urlencode "grant_type=password" \
  --data-urlencode "username=$USERNAME" \
  --data-urlencode "password=$PASSWORD")

# Извлечение access_token (с поддержкой jq или fallback)
if command -v jq >/dev/null 2>&1; then
    ACCESS_TOKEN=$(echo "$RESPONSE" | jq -r '.access_token')
else
    ACCESS_TOKEN=$(echo "$RESPONSE" | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
fi

# Проверка наличия токена
if [ -z "$ACCESS_TOKEN" ] || [ "$ACCESS_TOKEN" = "null" ]; then
    echo "❌ Ошибка: Не удалось получить токен. Ответ сервера:"
    echo "$RESPONSE"
    exit 1
fi

# --- Проверка доступа к Langflow ---
echo "Проверка доступа к Langflow..."
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X GET "$WHOAMI_URL" -H "Authorization: Bearer $ACCESS_TOKEN")

if [ "$HTTP_STATUS" -eq 200 ]; then
    echo "✅ Успех: Аутентификация прошла успешно."
    exit 0
else
    echo "❌ Ошибка: Запрос к whoami завершился с HTTP статусом $HTTP_STATUS."
    exit 1
fi