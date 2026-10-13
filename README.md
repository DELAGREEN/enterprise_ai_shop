# Langflow Agent Manager

FastAPI-приложение для управления AI-агентами и публикацией flow из Langflow. В проекте есть публичный дашборд, админка, LDAP-аутентификация, чат, интеграции и API.

## Что умеет проект

- публичный дашборд с опубликованными flow;
- админка для управления публикациями, пользователями, группами и интеграциями;
- LDAP-авторизация и fallback-админ для локальной разработки;
- чат с выбранным flow;
- API для получения статуса flow и работы с чатами;
- хранение данных в PostgreSQL;
- автоинициализация схемы и базовых системных групп при старте.

---

## Требования

- Python 3.11+
- PostgreSQL 14/16
- Docker (опционально, если поднимаете БД и LDAP через compose)
- Langflow API (если используется работа с flow)

---

## 1. Установка зависимостей

```bash
git clone <repo-url>
cd enterprise_ai_shop
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

---

## 2. Настройка окружения

Создайте файл `.env` и заполните переменные под своё окружение:

```bash
cp .env.example .env
```

Пример содержимого `.env`:

```env
# Langflow
LANGFLOW_URL=http://localhost:7860/api/v1
LANGFLOW_API_KEY=your_key_here

# PostgreSQL
DATABASE_URL=postgresql+asyncpg://ai_shop:ai_shop_password@localhost/ai_shop

# Session
SECRET_KEY=change-me-super-secret

# LDAP
LDAP_SERVER=localhost:389
LDAP_USE_SSL=false
LDAP_BASE_DN=ou=users,dc=example,dc=com
LDAP_USER_ATTR=uid

# Fallback admin
ADMIN_USERNAME=admin
ADMIN_PASSWORD=admin
```

> Важно: локальный запуск приложения ожидает доступную PostgreSQL на `127.0.0.1:5432` и базу `ai_shop`.

---

## 3. Поднятие базы данных и зависимостей

### Вариант A: через Docker

```bash
docker compose -f docker/docker-compose.yml up -d ia_shop_db ldap
```

### Вариант B: PostgreSQL вручную

```bash
docker run -d --name ai_shop_db   -e POSTGRES_USER=ai_shop   -e POSTGRES_PASSWORD=ai_shop_password   -e POSTGRES_DB=ai_shop   -p 5432:5432   postgres:16
```

Проверка доступности:

```bash
pg_isready -h 127.0.0.1 -p 5432 -U ai_shop
```

---

## 4. Запуск приложения

### 4.1. Продакшен / рабочее окружение

Для полноценного запуска с реальным Langflow и PostgreSQL подготовьте `.env`:

```env
LANGFLOW_URL=http://localhost:7860/api/v1
LANGFLOW_API_KEY=your_key_here
DATABASE_URL=postgresql+asyncpg://ai_shop:ai_shop_password@127.0.0.1:5432/ai_shop
ENABLE_DEMO_MODE=false
```

Запуск сервиса:

```bash
cd enterprise_ai_shop
PYTHONPATH=. uvicorn app:app --host 0.0.0.0 --port 5001
```

Для локальной разработки с автоперезагрузкой:

```bash
cd enterprise_ai_shop
PYTHONPATH=. uvicorn app:app --host 0.0.0.0 --port 5001 --reload
```

> Важно: `--reload` и `--workers` нельзя использовать одновременно. Для production-режима используйте один worker/один процесс без `--reload`.

Открыть в браузере:

- dashboard: http://localhost:5001/
- admin: http://localhost:5001/admin
- login: http://localhost:5001/auth/login

### 4.2. Демонстрационный режим (без Langflow и vLLM)

Для ручного UI-тестирования и показа интерфейса без внешних сервисов включите демо-режим:

```bash
cd enterprise_ai_shop
export ENABLE_DEMO_MODE=true
PYTHONPATH=. uvicorn app:app --host 0.0.0.0 --port 8000
```

Дополнительно можно задать параметры демо-flow:

```env
ENABLE_DEMO_MODE=true
DEMO_FLOW_ID=demo-flow
DEMO_FLOW_NAME="Demo assistant"
DEMO_FLOW_DESCRIPTION="Демо-агент для тестирования интерфейса без Langflow."
```

В демо-режиме:

- приложение не пытается ходить в Langflow;
- список flow и ответ модели генерируются локально;
- можно открыть чат, посмотреть UX и показать сценарии без внешней зависимости.

Открыть в браузере:

- http://localhost:8000/
- http://localhost:8000/admin/settings

---

## 5. Запуск тестов

Проект поддерживает запуск через `pytest` и `unittest`.

### Pytest

```bash
cd enterprise_ai_shop
PYTHONPATH=. pytest -q
```

### Проверка конкретного набора

```bash
cd enterprise_ai_shop
PYTHONPATH=. pytest -q tests/test_demo_mode.py
PYTHONPATH=. pytest -q tests/test_route_smoke.py tests/test_auth_redirects.py tests/test_app_startup.py
```

### Legacy unittest

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

---

## 6. Проверка корректности запуска

После старта в логах должен появиться вывод вида:

```text
Application startup complete.
```

Если приложение не запускается:

1. проверьте, что PostgreSQL запущен и доступен;
2. проверьте значения в `.env`;
3. убедитесь, что в проекте нет старых uvicorn-процессов на порту 5001;
4. проверьте, что база `ai_shop` существует и доступна пользователю `ai_shop`.

---

## 7. Docker и инфраструктура

В проекте есть конфиги в директории `docker/`:

```bash
ls docker
```

Для запуска базовой инфраструктуры:

```bash
docker compose -f docker/docker-compose.yml up -d
```

---

## 8. Частые проблемы и решения

### Ошибка подключения к PostgreSQL

```text
connection refused
```

Решение:

```bash
docker compose -f docker/docker-compose.yml up -d ia_shop_db
```

### Uvicorn не стартует из-за `--workers`

```text
workers are ignored when reloading is enabled
```

Решение: уберите `--workers` при запуске с `--reload`.

### Маршруты не найдены / 404

Проверьте:
- что роутеры подключены в [app.py](app.py);
- что URL совпадает с объявленным в маршрутах;
- что нет старых процессов uvicorn.

---

## 9. Полезные команды

```bash
# запуск приложения
uvicorn app:app --host 0.0.0.0 --port 5001 --reload

# запуск PostgreSQL через Docker
docker compose -f docker/docker-compose.yml up -d ia_shop_db ldap

# запуск всех тестов
python -m unittest discover -s tests -p "test_*.py" -v

# проверка процесса на порту
ss -lntp | grep 5001
```

---

## 10. Основные файлы проекта

- [app.py](app.py) — точка входа, middleware и lifespan;
- [database.py](database.py) — инициализация async SQLAlchemy;
- [config.py](config.py) — переменные окружения;
- [models.py](models.py) — модели базы данных;
- [routers/](routers/) — FastAPI-маршруты;
- [templates/](templates/) — HTML-шаблоны;
- [tests/](tests/) — регрессионные тесты.

---

## 11. Безопасность

- редирект после логина проверяется и должен принимать только локальные относительные пути;
- публичные endpoint'ы должны проверять наличие сессии;
- не используйте `--workers` одновременно с `--reload`.
