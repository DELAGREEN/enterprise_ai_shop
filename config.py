import os
from dotenv import load_dotenv

load_dotenv()

# ---------- Брендинг ----------
# Значения по умолчанию. Админка (/admin/settings) имеет приоритет над env.
APP_NAME = os.getenv("APP_NAME", "Langflow Agents")
APP_ICON = os.getenv("APP_ICON", "")          # устарело: emoji-иконка
APP_LOGO_URL = os.getenv("APP_LOGO_URL", "").strip()
APP_ICON_URL = (
    os.getenv("APP_ICON_URL", "").strip()
    or APP_LOGO_URL
    or "/static/vendor/image/logo.svg"
)
APP_HEADER_COLOR = os.getenv("APP_HEADER_COLOR", "#111827")
APP_HEADER_TEXT_COLOR = os.getenv("APP_HEADER_TEXT_COLOR", "#ffffff")

# ---------- Тема интерфейса ----------
THEME_COOKIE = "ui_theme"
THEMES = ("light", "dark")
DEFAULT_THEME = "light"

# --- Langflow ---
LANGFLOW_URL = os.getenv("LANGFLOW_URL", "")
LANGFLOW_API_KEY = os.getenv("LANGFLOW_API_KEY", "")
ENABLE_DEMO_MODE = os.getenv("ENABLE_DEMO_MODE", "false").lower() in ("1", "true", "yes", "on")
DEMO_FLOW_ID = os.getenv("DEMO_FLOW_ID", "demo-flow")
DEMO_FLOW_NAME = os.getenv("DEMO_FLOW_NAME", "Demo assistant")
DEMO_FLOW_DESCRIPTION = os.getenv("DEMO_FLOW_DESCRIPTION", "Демо-агент для тестирования интерфейса без Langflow.")

# --- PostgreSQL ---
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/langflow_admin",
)
# ---------- PostgreSQL connection pool ----------
def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name}: ожидалось целое число, получено {raw!r}")
    if not (minimum <= value <= maximum):
        raise RuntimeError(
            f"{name}={value} вне диапазона [{minimum}, {maximum}]. "
            f"Проверьте значение в .env."
        )
    return value

DB_POOL_SIZE = _env_int("DB_POOL_SIZE", default=10, minimum=1, maximum=100)
DB_MAX_OVERFLOW = _env_int("DB_MAX_OVERFLOW", default=20, minimum=0, maximum=200)

# --- LDAP ---
LDAP_SERVER = os.getenv("LDAP_SERVER", "localhost:389")
LDAP_USE_SSL = os.getenv("LDAP_USE_SSL", "false").lower() in ("1", "true", "yes")
LDAP_BASE_DN = os.getenv("LDAP_BASE_DN", "ou=users,dc=example,dc=com")
LDAP_USER_ATTR = os.getenv("LDAP_USER_ATTR", "uid")

# Сервисный аккаунт (используется для поиска/проверки групп)
LDAP_USER_DN = os.getenv("LDAP_USER_DN", "cn=admin,dc=example,dc=com")
LDAP_PASSWORD = os.getenv("LDAP_PASSWORD", "password")

# Группа администраторов (DN). Пусто = все админы
LDAP_ADMIN_GROUP = os.getenv("LDAP_ADMIN_GROUP", "")

# --- Session ---
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-super-secret")

# --- Прогресс ингеста ---
# Токен, которым компонент Langflow подтверждает, что это он присылает
# прогресс. Отдельная переменная, чтобы не светить SECRET_KEY в flow.
PROGRESS_TOKEN = os.getenv("PROGRESS_TOKEN", "") or SECRET_KEY

# Базовый адрес приложения, доступный ИЗ КОНТЕЙНЕРА Langflow. localhost там
# указывает на сам Langflow, поэтому по умолчанию — host.docker.internal.
#
# ТРЕБОВАНИЕ: у сервиса langflow в docker-compose должен быть
#   extra_hosts:
#     - "host.docker.internal:host-gateway"
# Без этой строки имя не резолвится и прогресс молча не доходит.
# Проверка: docker exec langflow getent hosts host.docker.internal
#
# Если приложение окажется в ОДНОЙ docker-сети с langflow, адрес можно
# заменить на http://<имя-сервиса>:<внутренний-порт>, например
# http://enterprise-ai-shop:5001. Важно: порт должен быть тем, который
# слушает uvicorn ВНУТРИ сети. Если снаружи виден редирект (302), значит
# запрос попал не туда — проверьте порт и маршрут через nginx.
PROGRESS_BASE_URL = (
    os.getenv("PROGRESS_BASE_URL", "").rstrip("/")
    or "http://host.docker.internal:5001"
)

# --- Fallback ---
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SYSTEM_ADMIN_ENABLED = bool(ADMIN_PASSWORD)

# Сдвиг локальной таймзоны от UTC в часах. МСК = 3.
LOCAL_TZ_OFFSET_HOURS = int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "3"))

TRUSTED_PROXY_IPS = [
    ip.strip() 
    for ip in os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1,172.16.0.0/12").split(",") if ip.strip()
]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:5001").rstrip("/")
EMBED_TOKEN_TTL = int(os.getenv("EMBED_TOKEN_TTL", "300"))

HEADER_KERBEROS_USER = os.getenv("HEADER_KERBEROS_USER", "X-Remote-User")