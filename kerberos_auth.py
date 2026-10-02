"""
Чтение имени пользователя из заголовка X-Remote-User,
который проставляет Nginx после успешного Kerberos-хендшейка (auth_gss).

Безопасность: заголовок принимается ТОЛЬКО если запрос пришёл
с доверенного IP (нашего Nginx). Иначе легко подделать.
"""
import ipaddress
import logging

from fastapi import Request

import config

logger = logging.getLogger(__name__)


# Имя заголовка, куда Nginx пишет $remote_user
HEADER_NAME = getattr(config, "HEADER_KERBEROS_USER", "X-Remote-User")


def _parse_trusted_networks() -> list:
    """Разбирает TRUSTED_PROXY_IPS в список ip_network."""
    raw = getattr(config, "TRUSTED_PROXY_IPS", ["127.0.0.1", "::1"])
    nets = []
    for item in raw:
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("Некорректный CIDR в TRUSTED_PROXY_IPS: %r", item)
    return nets


_TRUSTED = _parse_trusted_networks()


def _is_trusted(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED)


def get_kerberos_user(request: Request) -> str | None:
    """
    Возвращает username из X-Remote-User, если:
      1. Запрос пришёл с доверенного IP (Nginx).
      2. Заголовок не пустой.
    Иначе — None.
    """
    client_ip = request.client.host if request.client else None
    if not client_ip or not _is_trusted(client_ip):
        return None

    user = (request.headers.get(HEADER_NAME) or "").strip()
    return user or None