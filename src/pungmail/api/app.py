from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import requests
from sqlalchemy import text

from pungmail import __version__
from pungmail.config import Settings, get_settings
from pungmail.observability.logging import configure_logging, read_log_lines
from pungmail.repositories.database import session_scope
from pungmail.repositories.queries import (
    dashboard_data,
    get_mail_detail,
    get_run,
    list_mails,
    list_runs,
    parse_json,
)
from pungmail.workflows.graph import WORKFLOW_GRAPHS


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def _read_text(settings: Settings, stored_path: str | None, limit: int = 250_000) -> str:
    if not stored_path:
        return ""
    path = (settings.project_root / stored_path).resolve()
    if settings.project_root.resolve() not in path.parents or not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def _prefect_health(settings: Settings) -> dict[str, Any]:
    try:
        response = requests.get(f"{settings.prefect_api_url}/health", timeout=0.8)
        return {"status": "정상" if response.ok else "오류", "ok": response.ok}
    except requests.RequestException:
        return {"status": "중지", "ok": False}


def create_app(settings: Settings | None = None) -> FastAPI:
    active = settings or get_settings()
    configure_logging(active)
    application = FastAPI(title="차세대 풍멜이 관리", version=__version__, docs_url=None, redoc_url=None)
    application.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    def context(request: Request, **values: Any) -> dict[str, Any]:
        return {
            "request": request,
            "version": __version__,
            "now": datetime.now(UTC),
            "parse_json": parse_json,
            **values,
        }

    @application.get("/healthz")
    def health() -> dict[str, Any]:
        database_ok = False
        try:
            with session_scope() as session:
                session.execute(text("SELECT 1"))
            database_ok = True
        except Exception:
            database_ok = False
        prefect = _prefect_health(active)
        return {
            "status": "ok" if database_ok else "degraded",
            "database": database_ok,
            "prefect": prefect["ok"],
            "version": __version__,
        }

    @application.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        data = dashboard_data()
        services = {
            "관리 화면": {"status": "정상", "ok": True},
            "업무 DB": {"status": "정상", "ok": True},
            "Prefect": _prefect_health(active),
            "Gmail 조회": {
                "status": "설정됨" if active.gmail_enabled else "꺼짐",
                "ok": active.gmail_enabled and active.gmail_token_path.exists(),
            },
        }
        next_mail = datetime.now(UTC) + timedelta(seconds=active.mail_check_interval_seconds)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(request, data=data, services=services, next_mail=next_mail),
        )

    @application.get("/workflows", response_class=HTMLResponse)
    def workflows(request: Request) -> HTMLResponse:
        rows = [
            {"key": "mail_processing", "name": "메일 처리", "schedule": "1분마다", "description": "Gmail 수집부터 카테고리 분기까지", "nodes": len(WORKFLOW_GRAPHS["mail_processing"])},
            {"key": "order_status_refresh", "name": "오더시트·재고 갱신", "schedule": "1시간마다", "description": "이슈 1에서는 실행 가능한 골격", "nodes": len(WORKFLOW_GRAPHS["order_status_refresh"])},
        ]
        return templates.TemplateResponse(request, "workflows.html", context(request, workflows=rows))

    @application.get("/workflows/{workflow_type}", response_class=HTMLResponse)
    def workflow_detail(request: Request, workflow_type: str) -> HTMLResponse:
        graph = WORKFLOW_GRAPHS.get(workflow_type)
        if graph is None:
            raise HTTPException(status_code=404, detail="workflow not found")
        grouped: dict[str, list[Any]] = {}
        for node in graph:
            grouped.setdefault(node.branch or node.stage, []).append(node)
        return templates.TemplateResponse(
            request,
            "workflow_detail.html",
            context(request, workflow_type=workflow_type, graph=graph, grouped=grouped, runs=list_runs(workflow_type=workflow_type, limit=12)),
        )

    @application.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, workflow_type: str = "", status: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "runs.html",
            context(request, runs=list_runs(workflow_type=workflow_type, status=status), workflow_type=workflow_type, status=status),
        )

    @application.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str) -> HTMLResponse:
        run, nodes = get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        return templates.TemplateResponse(request, "run_detail.html", context(request, run=run, nodes=nodes))

    @application.get("/mails", response_class=HTMLResponse)
    def mails_page(request: Request, status: str = "", q: str = Query(default="", max_length=120)) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "mails.html",
            context(request, mails=list_mails(status=status, query=q), status=status, q=q),
        )

    @application.get("/mails/{message_id}", response_class=HTMLResponse)
    def mail_detail(request: Request, message_id: str) -> HTMLResponse:
        detail = get_mail_detail(message_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="mail not found")
        thread_views = [
            {"row": message, "body_text": _read_text(active, message.body_text_path), "actual_body": _read_text(active, message.actual_body_path), "quoted_body": _read_text(active, message.quoted_body_path)}
            for message in detail["thread_messages"]
        ]
        attachment_views = [
            {"row": attachment, "extracted_text": _read_text(active, attachment.extracted_text_path), "ocr_text": _read_text(active, attachment.ocr_text_path)}
            for attachment in detail["attachments"]
        ]
        return templates.TemplateResponse(
            request,
            "mail_detail.html",
            context(request, **detail, thread_views=thread_views, attachment_views=attachment_views),
        )

    @application.get("/logs", response_class=HTMLResponse)
    def logs_page(request: Request, q: str = Query(default="", max_length=160), level: str = "") -> HTMLResponse:
        rows = read_log_lines(active.log_path, query=q, level=level)
        return templates.TemplateResponse(request, "logs.html", context(request, logs=rows, q=q, level=level))

    return application


app = create_app()
