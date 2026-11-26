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

# Брендинг (значения по умолчанию; админка важнее)
APP_NAME=Enterprise AI Shop
APP_ICON_URL=/static/vendor/image/logo.svg
APP_HEADER_COLOR=#111827
APP_HEADER_TEXT_COLOR=#ffffff
```

> Важно: локальный запуск приложения ожидает доступную PostgreSQL на `127.0.0.1:5432` и базу `ai_shop`.

### Брендинг и тема

Название, иконку сайта и цвета шапки можно задать двумя способами:

1. **Переменные окружения** — значения по умолчанию:
   `APP_NAME`, `APP_ICON_URL` (или устаревшая `APP_LOGO_URL`),
   `APP_HEADER_COLOR`, `APP_HEADER_TEXT_COLOR`.
2. **Админка** → «Настройки» → карточка «Оформление».
   Значения из админки хранятся в таблице `app_settings` и **перекрывают**
   переменные окружения. Кнопка «Сбросить к .env» возвращает значения из env.

Иконки интерфейса лежат локально в `static/vendor/image/*.svg`
(шаблоны, файл, корзина, звезда и т. д.). Иконка сайта по умолчанию —
`static/vendor/image/logo.svg`; вместо неё можно указать любой SVG/PNG
из этой папки или внешний URL.

Тема (светлая/тёмная) переключается кнопкой в шапке. Выбор запоминается
в `localStorage` (мгновенно, без мигания при загрузке), в cookie `ui_theme`
(чтобы сервер отрисовал нужную тему сразу) и в `users.theme`
(чтобы пережить смену браузера — подставляется при входе).

Эмблему можно не только указать ссылкой, но и **загрузить файлом**
(«Загрузить картинку» в админке). Файл хранится в таблице `app_assets`
и имеет приоритет над URL; отдаётся по адресу `/branding/icon`.
Поддерживаются SVG, PNG, JPEG, WEBP и ICO до 2 МБ.

### Какие файлы можно прикреплять в чате
Агент извлекает текст из документа, поэтому принимаются только текстовые
форматы и PDF с текстовым слоем:

```
.txt .md .csv .rtf .odt .docx .pdf
```

Сканы и фотографии документов (в том числе PDF без текстового слоя) не
подойдут — в них нет текста, который можно прочитать. Список задаётся
в `uploads.py`, проверяется и в браузере (атрибут `accept` + подсказка),
и на сервере (`400` с понятным сообщением).

### Прогресс обработки документов

Пока Langflow разбирает файл, приложение показывает стадию и процент
(«разбор PDF: 240 / 673», «осталось ~4 мин»). Langflow не отдаёт прогресс
через job API, поэтому компоненты flow сами шлют его по HTTP:

```
Langflow (парсер/ингест) ──POST──► /api/jobs/{id}/progress ──► админка
                X-Progress-Token
```

Значения `job_id`, `progress_url`, `progress_token` **не нужно вписывать
в компоненты вручную** — приложение подставляет их через tweaks при каждом
запуске из админки. В Langflow Playground эти поля останутся пустыми, и
прогресса просто не будет (это нормально).

#### Адрес приложения: `PROGRESS_BASE_URL`

```env
PROGRESS_TOKEN=<общий секрет; пусто — берётся SECRET_KEY>
PROGRESS_BASE_URL=http://host.docker.internal:5001
```

`localhost` внутри контейнера Langflow указывает на сам Langflow, поэтому
адрес приложения задаётся именем хоста.

**`host.docker.internal` работает только при наличии строки в compose**
у сервиса `langflow`:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

Проверка:

```bash
docker exec langflow getent hosts host.docker.internal   # должен вернуть IP
docker exec langflow curl -s -o /dev/null -w '%{http_code}\n' \
  -X POST http://host.docker.internal:5001/api/jobs/probe/progress \
  -H "X-Progress-Token: $PROGRESS_TOKEN" -d '{}'
# 404 = эндпоинт есть (job не найден); 401 = не тот адрес или старый образ
```

**Если приложение окажется в одной docker-сети с Langflow**, адрес можно
заменить на имя сервиса:

```env
PROGRESS_BASE_URL=http://enterprise-ai-shop:5001
```

Но порт должен быть тем, который слушает uvicorn **внутри** сети, и образ
должен быть пересобран с актуальным кодом. Проверка на этой машине:

```
host.docker.internal:5001  -> 404  (эндпоинт прогресса есть)
enterprise-ai-shop:5001    -> 401  (контейнер со старым образом)
```

То есть имя резолвится, но в контейнере старая версия без
эндпоинта — переключение на него без пересборки сломает прогресс.

### Очередь ингеста в админке

Страница `/admin/ingestion` показывает задачи и позволяет ими управлять.
Главная проблема, которую она решает: **зависшая задача занимает воркер
Langflow**. Если таких накопится столько же, сколько воркеров
(`LANGFLOW_WORKERS`, по умолчанию 3), новые загрузки встанут навсегда —
при этом в интерфейсе ничего не сломается, просто «ничего не происходит».

**Зависшая** — задача в статусе `queued`/`running`, у которой нет прогресса
дольше `INGEST_STALE_MINUTES` (по умолчанию 30 минут). Для ещё не
подхваченной задачи точкой отсчёта берётся время постановки в очередь.
В таблице такие помечаются бейджем «зависла».

| Действие | Что происходит | История чата |
|---|---|---|
| **Отменить** | остановка в Langflow, статус `failed` | появится «⚠️ Отменено администратором» |
| **Удалить** | остановка в Langflow, строка удаляется | ничего не пишется |
| **Очистить зависшие** | предпросмотр → подтверждение → массовое удаление | — |
| **Удалить выбранные** | по чекбоксам | — |

Порядок важен: **сначала остановка в Langflow, потом удаление записи**.
Если удалить только строку в БД, воркер продолжит считать документ, а
прогресс будет стучаться в 404. Остановка идёт через
`POST /api/v2/workflows/stop`; ответ `404 / JOB_NOT_FOUND` считается нормой
(задача уже завершилась), а вот сетевую ошибку интерфейс покажет — в этом
случае воркер мог остаться, и это стоит проверить вручную.

Очистка двухшаговая: сначала `dry_run` показывает, что попадёт под нож,
и только по подтверждению выполняется. Так нельзя случайно снести живую
задачу одним кликом.

```bash
# посмотреть, что будет очищено, ничего не меняя
curl -X POST localhost/api/jobs/purge-stale \
  -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"older_than_minutes": 30, "dry_run": true}'

# удалить (action: delete) или перевести в failed (action: cancel)
curl -X POST localhost/api/jobs/purge-stale \
  -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"older_than_minutes": 30, "action": "delete", "dry_run": false}'
```

Ограничение: Langflow не отдаёт список всех своих задач (только по
известному `job_id` или `pending` по конкретному flow), поэтому «сироту»,
у которой в БД уже нет записи, через эту страницу не видно. Если после
очистки новые загрузки всё равно стоят, помогает перезапуск langflow —
он сбрасывает очередь.

### Иконки агентов

Иконка агента — необязательная. По умолчанию её нет, карточка показывается
без иконки.

**Базовая библиотека** (18 иконок) идёт в комплекте и лежит в
`static/vendor/image/agents/*.svg` — доступна сразу, без загрузки:
робот, аналитик, отчёты, документы, поддержка, продажи, юрист, персонал,
разработка, инфраструктура, безопасность, поиск, консультант, идеи,
настройки, база знаний, данные, рассылки.

**Свои иконки** загружаются в админке: «Настройки» → «Библиотека иконок» →
«Загрузить иконку» (SVG, PNG, JPEG, WEBP, GIF до 1 МБ). Кнопка
«Посмотреть библиотеку» открывает отдельное окно: сначала базовые иконки,
затем загруженные — от первой загруженной к последней. Файлы лежат в
таблице `icon_library`.

**Назначить агенту** — «Публикации» → колонка «Иконка». В диалоге есть
«Без иконки», базовые и загруженные иконки. Иконку можно назначить и до
публикации агента. Базовые иконки хранятся в `icon_id` как `base:<slug>`,
загруженные — как uuid из `icon_library`.

Иконки отдаются по `/icons/{id}` (доступно без авторизации, как и логотип).
При удалении иконки из библиотеки она автоматически снимается со всех
агентов.

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

### Фоновые поллеры и число воркеров

Приложение запускается двумя воркерами uvicorn (`--workers 2`). Каждый
воркер — отдельный процесс, который выполняет `lifespan` и поднимает свои
фоновые задачи. Поллеров два вида:

| Поллер | Что делает | Период |
|---|---|---|
| `_poll_jobs` | спрашивает у Langflow статус активных задач, дописывает историю чата | 10 с |
| `_poll_chat_purges` | дочищает память удалённых чатов в Langflow | 60 с |

Поллер нужен потому, что Langflow не сообщает о завершении сам: `mode=background`
возвращает только `job_id`. И потому, что пользователь может закрыть вкладку —
тогда результат в историю чата допишет только поллер.

**Работает из них ровно один процесс.** Право опрашивать выдаёт
advisory-лок Postgres (`pg_try_advisory_lock`). Лок привязан к соединению,
поэтому падение процесса или обрыв связи снимают его автоматически, и
ведущим становится второй воркер (в течение ≤ 20 с).

Без этого каждый воркер опрашивал Langflow независимо: при 4 воркерах и
20 активных задачах — около 480 запросов в минуту впустую, плюс четыре
конкурентных `UPDATE` на каждый переход статуса.

Проверка, что ведущий один:

```bash
docker logs enterprise-ai-shop --since 5m 2>&1 | grep "ведущий"
# ожидаем по одной строке на каждый вид поллера, а не по строке на воркер
```

Если воркеров станет больше, менять ничего не нужно: лок достанется одному,
остальные будут спать. Лимит соединений Postgres при этом тоже под контролем —
`DB_POOL_SIZE + DB_MAX_OVERFLOW` умножается на число воркеров, а
`max_connections` по умолчанию 100 (при 2 воркерах это максимум 60).

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
