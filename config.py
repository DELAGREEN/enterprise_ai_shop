import os
from dotenv import load_dotenv

load_dotenv()

# --- Langflow ---
LANGFLOW_URL = os.getenv("LANGFLOW_URL", "http://localhost:7860/api/v1")
LANGFLOW_API_KEY = os.getenv("LANGFLOW_API_KEY", "")

# --- PostgreSQL ---
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/langflow_admin",
)

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
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")