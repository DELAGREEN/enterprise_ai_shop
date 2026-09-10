import logging

import ldap

import config

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)


def _build_server_url() -> str:
    scheme = "ldaps" if config.LDAP_USE_SSL else "ldap"
    return f"{scheme}://{config.LDAP_SERVER}"


def ldap_authenticate(username: str, password: str):
    """
    Пытается забиндиться под uid=<username>,<LDAP_BASE_DN>.
    Возвращает (True, user_dn) при успехе, (False, None) иначе.
    """
    logger.debug("Попытка аутентификации пользователя: %s", username)

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

        logger.debug("User DN: %s", user_dn)
        logger.info("Пробуем bind с DN: %s", user_dn)
        conn.simple_bind_s(user_dn, password)
        logger.info("Bind успешен")
        try:
            conn.unbind_s()
        except Exception:
            pass
        return True, user_dn

    except ldap.INVALID_CREDENTIALS as e:
        logger.error("Неверные учётные данные для %s: %s", username, e, exc_info=True)
        return False, None
    except ldap.NO_SUCH_OBJECT as e:
        logger.error("DN не найден: %s — %s", user_dn, e, exc_info=True)
        return False, None
    except ldap.SERVER_DOWN as e:
        logger.error("LDAP-сервер недоступен: %s", e, exc_info=True)
        return False, None
    except Exception as e:
        logger.error("Ошибка LDAP: %s", e, exc_info=True)
        return False, None


def ldap_is_admin(user_dn: str) -> bool:
    """
    Проверяет, входит ли пользователь в LDAP_ADMIN_GROUP.
    Если группа не задана — считается, что все админы.
    """
    if not config.LDAP_ADMIN_GROUP:
        logger.info("LDAP_ADMIN_GROUP не задан — права администратора у всех")
        return True

    try:
        server_url = _build_server_url()
        logger.info("Проверка группы %s через %s", config.LDAP_ADMIN_GROUP, server_url)

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
        # member может быть bytes
        members = [m.decode() if isinstance(m, bytes) else m for m in members]
        is_admin = user_dn in members
        logger.info("Пользователь %s %s в группе", user_dn, "входит" if is_admin else "НЕ входит")
        return is_admin

    except Exception as e:
        logger.error("Не удалось проверить группу: %s", e, exc_info=True)
        return False


def authenticate(username: str, password: str) -> bool:
    """
    Высокоуровневая обёртка:
      1) bind в LDAP
      2) если группа админов задана — проверка членства
      3) fallback на ADMIN_USERNAME / ADMIN_PASSWORD из .env
    """
    ok, user_dn = ldap_authenticate(username, password)

    if ok:
        if config.LDAP_ADMIN_GROUP and not ldap_is_admin(user_dn):
            logger.warning("Пользователь %s не админ — доступ запрещён", username)
            return False
        return True

    # Fallback на .env
    if username == config.ADMIN_USERNAME and password == config.ADMIN_PASSWORD:
        logger.info("Fallback-аутентификация для %s", username)
        return True

    return False