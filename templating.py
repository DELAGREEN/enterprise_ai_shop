import os
from datetime import datetime, timedelta, timezone

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")

# Сдвиг локальной таймзоны от UTC в часах. МСК = 3.
LOCAL_TZ_OFFSET_HOURS = int(os.getenv("LOCAL_TZ_OFFSET_HOURS", "3"))


def localtime(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Naive-UTC из БД → локальное время → строка."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS)))
    return local.strftime(fmt)


class Templates:
    def __init__(self, directory: str):
        self.env = Environment(
            loader=FileSystemLoader(directory),
            autoescape=select_autoescape(["html", "xml"]),
            cache_size=0,
            auto_reload=True,
        )
        # 👇 вот эта строка обязательна
        self.env.filters["localtime"] = localtime

    def TemplateResponse(
        self,
        name: str,
        context: dict,
        status_code: int = 200,
        headers: dict | None = None,
        media_type: str = "text/html",
    ) -> HTMLResponse:
        template = self.env.get_template(name)
        html = template.render(**context)
        return HTMLResponse(
            content=html, status_code=status_code, headers=headers, media_type=media_type
        )


templates = Templates(TEMPLATES_DIR)