"""管理后台静态资源托管。

生产：`web/dist` 由 FastAPI 挂到 `/admin`，SPA 路由 404 时回落到 index.html。
开发：Vite dev server 用 base `/admin/`，代理 `/admin/api` 到 127.0.0.1:8300。
"""
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await super().get_response("index.html", scope)


def resolve_admin_dist() -> Path:
    configured = os.environ.get("PISMOR_ADMIN_DIST")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[3] / "web" / "dist"


def mount_admin(app: FastAPI) -> None:
    dist = resolve_admin_dist()
    if dist.is_dir():
        app.mount("/admin", SPAStaticFiles(directory=dist, html=True), name="admin")
        return

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    def _admin_not_built():
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<title>pismor admin</title>"
            "<h1>管理后台尚未构建</h1>"
            "<p>执行 <code>scripts/build_web.sh</code> 后重启服务。</p>"
            f"<p>期望目录：<code>{dist}</code></p>",
            status_code=200,
        )
