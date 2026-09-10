# Langflow Agent Manager

Веб-приложение на FastAPI для управления AI-агентами (flow) из Langflow.

## Возможности
- Публичный дашборд с опубликованными агентами
- Админка для управления публикацией (LDAP-аутентификация)
- Чат с выбранным агентом (Markdown-рендеринг, блок «мышления модели»)
- Хранение статуса публикации в PostgreSQL
- Интеграция с Langflow API

## Быстрый старт

### 1. Подготовка окружения

```bash
python -m venv venv
source venv/bin/activate          # Linux/macOS
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 2. Настройки

```bash
cp .env.example .env
# отредактируйте .env под своё окружение
```

### 3. Запуск зависимостей

Убедитесь, что запущены:
- **PostgreSQL** (база `langflow_admin` создана)
- **LDAP** на `localhost:389` с base DN `ou=users,dc=example,dc=com`
- **Langflow** на `http://localhost:7860`

Создать БД можно так:

```bash
createdb langflow_admin
# или psql -c "CREATE DATABASE langflow_admin;"
```

### 4. Запуск приложения

```bash
uvicorn app:app --host 0.0.0.0 --port 5001 --reload
```

Откройте:
- Дашборд: http://localhost:5001/
- Админка: http://localhost:5001/admin
- Логин:   http://localhost:5001/auth/login

### 5. Тестовый LDAP (пример через osixia/openldap)

```bash
docker run -d --name ldap -p 389:389 \
  -e LDAP_ORGANISATION="Example" \
  -e LDAP_DOMAIN="example.com" \
  -e LDAP_ADMIN_PASSWORD="password" \
  osixia/openldap:latest
```

## Структура БД

Таблица `flow_publications` создаётся автоматически при старте:

| Поле          | Тип       | Описание                     |
| ------------- | --------- | ---------------------------- |
| flow_id       | string PK | ID flow из Langflow          |
| is_published  | bool      | Опубликован ли               |
| published_at  | timestamp | Дата публикации              |
| updated_at    | timestamp | Дата обновления              |


## Установка и запуск:
``` 
# 1. Системные зависимости (Linux)
sudo apt-get install -y build-essential libldap2-dev libsasl2-dev libssl-dev

# 2. Python-пакеты
pip install -r requirements.txt

# 3. Настройки
cp .env.example .env
# отредактируйте .env под своё окружение

# 4. Запуск
uvicorn app:app --host 0.0.0.0 --port 5001
```