from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from fastapi import Request

from config import SECRET_KEY

serializer = URLSafeTimedSerializer(SECRET_KEY, salt="session")
SESSION_COOKIE = "session"
MAX_AGE = 60 * 60 * 24 * 7  # 7 дней


def create_session(data: dict) -> str:
    return serializer.dumps(data)


def read_session(token: str):
    try:
        return serializer.loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return read_session(token)