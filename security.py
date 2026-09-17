from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyCookie

from session import SESSION_COOKIE, get_current_user as _get_session

# Схема cookie для OpenAPI и Swagger UI
cookie_scheme = APIKeyCookie(
    name=SESSION_COOKIE,
    scheme_name="Session cookie",
    description="Подписанная cookie сессии (itsdangerous, `session`).",
    auto_error=False,
)


def current_user(
    request: Request,
    _: str | None = Depends(cookie_scheme),
) -> dict:
    """
    Зависимость: возвращает сессию пользователя или 401.
    Для SSR-запросов middleware уже сделал редирект, сюда мы попадаем
    только если сессия есть. Для API-эндпоинтов выбросит 401.
    """
    user = _get_session(request)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def require_admin(user: dict = Depends(current_user)) -> dict:
    """Зависимость: пользователь + is_admin. Для /admin/* и админ-API."""
    if not user.get("is_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Требуются права администратора",
        )
    return user