import os
from datetime import datetime, timedelta, timezone

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import LOCAL_TZ_OFFSET_HOURS, DEFAULT_THEME, THEMES, THEME_COOKIE

import branding

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def localtime(dt: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Naive-UTC из БД → локальное время → строка."""
    if not dt:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=LOCAL_TZ_OFFSET_HOURS)))
    return local.strftime(fmt)


def human_size(n: int | None) -> str:
    if not n:
        return "0 B"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


class Templates:
    def __init__(self, directory: str):
        self.env = Environment(
            
            loader=FileSystemLoader(directory),
            autoescape=select_autoescape(["html", "xml"]),
            cache_size=0,
            auto_reload=True,
        )
        # вот эта строка обязательна
        self.env.filters["localtime"] = localtime
        self.env.filters["human_size"] = human_size

        # Глобалы брендинга — доступны во всех шаблонах без передачи в контекст
        self.env.globals["APP_ICON"] = ""
        self.env.globals["APP_NAME"] = branding.env_defaults()["app_name"]
        self.env.globals["APP_LOGO_URL"] = branding.env_defaults()["app_icon_url"]
        self.env.globals["APP_ICON_URL"] = branding.env_defaults()["app_icon_url"]
        self.env.globals["APP_HEADER_COLOR"] = branding.env_defaults()["app_header_color"]
        self.env.globals["APP_HEADER_TEXT_COLOR"] = branding.env_defaults()["app_header_text_color"]

    def TemplateResponse(
        self,
        name: str,
        context: dict,
        status_code: int = 200,
        headers: dict | None = None,
        media_type: str = "text/html",
    ) -> HTMLResponse:
        ctx = dict(context)

        # Брендинг: значения из админки важнее .env
        values = branding.get()
        ctx.setdefault("APP_NAME", values["app_name"])
        ctx.setdefault("APP_ICON_URL", values["app_icon_url"])
        ctx.setdefault("APP_HEADER_COLOR", values["app_header_color"])
        ctx.setdefault("APP_HEADER_TEXT_COLOR", values["app_header_text_color"])

        # Тема: cookie помнит выбор между устройствами и до отрисовки
        if "theme" not in ctx:
            theme = DEFAULT_THEME
            request = ctx.get("request")
            if request is not None:
                cookie = request.cookies.get(THEME_COOKIE)
                if cookie in THEMES:
                    theme = cookie
            ctx["theme"] = theme

        template = self.env.get_template(name)
        html = template.render(**ctx)
        return HTMLResponse(
            content=html, status_code=status_code, headers=headers, media_type=media_type
        )


templates = Templates(TEMPLATES_DIR)