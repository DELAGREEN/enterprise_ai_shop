import os

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


class Templates:
    """Обёртка над Jinja2 с отключённым кэшем (для удобной разработки)."""

    def __init__(self, directory: str):
        self.env = Environment(
            loader=FileSystemLoader(directory),
            autoescape=select_autoescape(["html", "xml"]),
            cache_size=0,
            auto_reload=True,
        )

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