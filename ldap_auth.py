import logging

import ldap

import config

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def _build_server_url() -> str:
    scheme = "ldaps" if config.LDAP_USE_SSL else "ldap"
    return f"{scheme}://{config.LDAP_SERVER}"


def ldap_authenticate(username: str, password: str):
    """Bind под uid=<username>,<LDAP_BASE_DN>. Возвращает (ok, user_dn)."""
    if not username or not password:
        return False, None

    user_dn = f"{config.LDAP_USER_ATTR}={username},{config.LDAP_BASE_DN}"
    server_url = _build_server_url()

    try:
        logger.info("Подключаемся к %s", server_url)
        conn = ldap.initialize(server_url)
        conn.set_option(ldap.OPT_REFERRALS, 0)
        conn.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
        if config.LDAP_USE_SSL:
            conn.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)

        logger.info("Bind с DN: %s", user_dn)
        conn.simple_bind_s(user_dn, password)
        try:
            conn.unbind_s()
        except Exception:
            pass
        return True, user_dn

    except ldap.INVALID_CREDENTIALS as e:
        logger.warning("Неверные учётные данные для %s: %s", username, e)
        return False, None
    except ldap.NO_SUCH_OBJECT as e:
        logger.warning("DN не найден: %s — %s", user_dn, e)
        return False, None
    except ldap.SERVER_DOWN as e:
        logger.error("LDAP-сервер недоступен: %s", e, exc_info=True)
        return False, None
    except Exception as e:
        logger.error("Ошибка LDAP: %s", e, exc_info=True)
        return False, None


def ldap_is_admin(user_dn: str) -> bool:
    """
    Проверяет членство user_dn в LDAP_ADMIN_GROUP.
    Если LDAP_ADMIN_GROUP не задан — LDAP-пользователи НЕ получают админку.
    """
    if not config.LDAP_ADMIN_GROUP:
        logger.warning(
            "LDAP_ADMIN_GROUP не задан — LDAP-пользователи не получают прав админа"
        )
        return False

    try:
        server_url = _build_server_url()
        conn = ldap.initialize(server_url)
        conn.set_option(ldap.OPT_REFERRALS, 0)
        conn.set_option(ldap.OPT_PROTOCOL_VERSION, 3)
        if config.LDAP_USE_SSL:
            conn.set_option(ldap.OPT_X_TLS_REQUIRE_CERT, ldap.OPT_X_TLS_NEVER)

        conn.simple_bind_s(config.LDAP_USER_DN, config.LDAP_PASSWORD)
        result = conn.search_s(
            config.LDAP_ADMIN_GROUP,
            ldap.SCOPE_BASE,
            "(objectClass=*)",
            ["member"],
        )
        conn.unbind_s()

        if not result:
            logger.warning("Группа %s не найдена", config.LDAP_ADMIN_GROUP)
            return False

        members = result[0][1].get("member", [])
        members = [m.decode() if isinstance(m, bytes) else m for m in members]
        return user_dn in members

    except Exception as e:
        logger.error("Не удалось проверить группу: %s", e, exc_info=True)
        return False


def authenticate_full(username: str, password: str) -> dict | None:
    """
    Полная аутентификация.
    Возвращает словарь для сессии:
        {"username": ..., "user_dn": ..., "is_admin": bool}
    или None, если вход неудачен.
    """
    ok, user_dn = ldap_authenticate(username, password)

    if ok:
        is_admin = ldap_is_admin(user_dn) if config.LDAP_ADMIN_GROUP else False
        logger.info(
            "Вход %s (%s), is_admin=%s", username, user_dn, is_admin
        )
        return {"username": username, "user_dn": user_dn, "is_admin": is_admin}

    # Fallback из .env — всегда админ
    if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
        logger.info("Fallback-вход администратора: %s", username)
        return {
            "username": username,
            "user_dn": f"cn={username},dc=fallback",
            "is_admin": True,
        }

    return None