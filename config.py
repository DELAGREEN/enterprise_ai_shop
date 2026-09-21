import os
from dotenv import load_dotenv

load_dotenv()

# ---------- Брендинг ----------
APP_ICON = os.getenv("APP_ICON", "")
APP_NAME = os.getenv("APP_NAME", "Langflow Agents")
APP_LOGO_URL = os.getenv("APP_LOGO_URL", "").strip()

# --- Langflow ---
LANGFLOW_URL = os.getenv("LANGFLOW_URL", "http://localhost:7860/api/v1")
LANGFLOW_API_KEY = os.getenv("LANGFLOW_API_KEY", "")

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

# --- Fallback ---
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SYSTEM_ADMIN_ENABLED = bool(ADMIN_PASSWORD)

# Сдвиг локальной таймзоны от UTC в часах. МСК = 3.
LOCAL_TZ_OFFSET_HOURS = int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "3"))