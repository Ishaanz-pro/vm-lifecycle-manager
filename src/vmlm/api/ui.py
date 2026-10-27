"""Browser dashboard: static files under /ui plus one small config endpoint.

The page is plain HTML/CSS/JS with no build step and no third-party requests, and it talks to
the same public REST API as any other client. Responses carry a strict Content-Security-Policy.

In demo mode `/ui/config` returns the *user-tier* API key so visitors can try the API; the
admin key is never exposed (Settings refuses demo mode unless both keys are set and distinct).
"""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from vmlm.api.deps import get_settings
from vmlm.config import Settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}

ui = APIRouter(include_in_schema=False)


@ui.get("/")
def root() -> RedirectResponse:
    return RedirectResponse("/ui/")


@ui.get("/ui/config")
def ui_config(settings: Annotated[Settings, Depends(get_settings)]) -> JSONResponse:
    payload = {
        "demo": settings.demo_mode,
        "auth_required": settings.api_key is not None,
        "api_key": (
            settings.api_key.get_secret_value()
            if settings.demo_mode and settings.api_key is not None
            else None
        ),
    }
    return JSONResponse(payload, headers={**SECURITY_HEADERS, "Cache-Control": "no-store"})


class _SecureStatic(StaticFiles):
    async def get_response(self, path: str, scope: Scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        response.headers.update(SECURITY_HEADERS)
        return response


def install_ui(app: FastAPI) -> None:
    # The router (with /ui/config) is registered before the mount so it wins the match.
    app.include_router(ui)
    app.mount("/ui", _SecureStatic(directory=WEB_DIR, html=True), name="ui")
