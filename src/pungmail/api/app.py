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
from pungmail.prompts import build_prompt_bundle
from pungmail.repositories.database import session_scope
from pungmail.repositories.queries import (
    dashboard_data,
    get_mail_detail,
    get_ai_decision,
    get_business_case,
    get_run,
    list_mails,
    list_ai_decisions,
    list_business_cases,
    list_outbox,
    list_run_mails,
    list_runs,
    parse_json,
)
from pungmail.workflows.graph import WORKFLOW_GRAPHS


PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
templates.env.policies["json.dumps_kwargs"]["ensure_ascii"] = False

NODE_STATUS_LABELS = {
    "PENDING": "실행 안 함",
    "RUNNING": "실행 중",
    "SUCCEEDED": "정상 완료",
    "FAILED": "실패",
    "RETRY_WAIT": "재시도 대기",
    "SKIPPED": "건너뜀",
}
NODE_STATUS_PRIORITY = ("FAILED", "RETRY_WAIT", "RUNNING", "SUCCEEDED", "SKIPPED")
WORKFLOW_LABELS = {
    "mail_processing": "메일 처리",
    "order_status_refresh": "오더시트·재고 갱신",
}
RUN_STATUS_LABELS = {
    "PENDING": "대기",
    "RUNNING": "실행 중",
    "SUCCEEDED": "정상 완료",
    "PARTIAL": "일부 실패",
    "FAILED": "실패",
}
TRIGGER_LABELS = {
    "SCHEDULED": "정기 실행",
    "MANUAL": "수동 실행",
    "VALIDATION": "실제 메일 검증",
    "RECOVERY": "복구 실행",
    "TEST": "테스트",
}

NODE_LABELS = {
    node.key: node.label
    for graph in WORKFLOW_GRAPHS.values()
    for node in graph
}


def _node_label(node_key: str) -> str:
    return NODE_LABELS.get(node_key, "기타 처리 단계")


def _build_run_graph(workflow_type: str, node_runs: list[Any]) -> dict[str, Any]:
    definitions = WORKFLOW_GRAPHS.get(workflow_type, ())
    records_by_key: dict[str, list[Any]] = {}
    for record in node_runs:
        records_by_key.setdefault(record.node_key, []).append(record)

    nodes: list[dict[str, Any]] = []
    for definition in definitions:
        records = records_by_key.get(definition.key, [])
        statuses = {record.status for record in records}
        status = next(
            (candidate for candidate in NODE_STATUS_PRIORITY if candidate in statuses),
            "PENDING",
        )
        duration = sum(
            max((record.finished_at_utc - record.started_at_utc).total_seconds(), 0)
            for record in records
            if record.started_at_utc and record.finished_at_utc
        )
        record_views = [
            {
                "status": record.status,
                "status_label": NODE_STATUS_LABELS.get(record.status, record.status),
                "time": (
                    f"{record.started_at_utc.strftime('%H:%M:%S')} → "
                    f"{record.finished_at_utc.strftime('%H:%M:%S') if record.finished_at_utc else '진행 중'}"
                    if record.started_at_utc
                    else "시각 기록 없음"
                ),
                "input": record.input_summary_json,
                "output": record.output_summary_json,
                "error": (
                    f"{record.error_type} · {record.error_message}"
                    if record.error_message
                    else ""
                ),
            }
            for record in records
        ]
        nodes.append(
            {
                "key": definition.key,
                "dom_id": f"step-{len(nodes) + 1}",
                "label": definition.label,
                "stage": definition.stage,
                "branch": definition.branch,
                "status": status,
                "status_label": NODE_STATUS_LABELS[status],
                "record_count": len(records),
                "duration": f"{duration:.1f}초" if records else "기록 없음",
                "records": record_views,
                "error": next((view["error"] for view in record_views if view["error"]), ""),
            }
        )

    tail_keys = {"render_discord", "dispatch_outbox", "finalize_case"}
    common_nodes = [
        node
        for node in nodes
        if node["stage"] == "common" and node["key"] not in tail_keys
    ]
    tail_nodes = [node for node in nodes if node["key"] in tail_keys]
    router = next((node for node in nodes if node["stage"] == "router"), None)
    branches: list[dict[str, Any]] = []
    for node in nodes:
        if not node["branch"]:
            continue
        branch = next((item for item in branches if item["name"] == node["branch"]), None)
        if branch is None:
            branch = {"name": node["branch"], "nodes": [], "active": False}
            branches.append(branch)
        branch["nodes"].append(node)
        if node["status"] not in {"PENDING", "SKIPPED"}:
            branch["active"] = True

    return {
        "common_nodes": common_nodes,
        "router": router,
        "branches": branches,
        "tail_nodes": tail_nodes,
        "all_nodes": nodes,
    }


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
            "node_label": _node_label,
            "node_status_label": lambda status: NODE_STATUS_LABELS.get(status, "상태 확인 필요"),
            "run_status_label": lambda status: RUN_STATUS_LABELS.get(status, "상태 확인 필요"),
            "trigger_label": lambda trigger: TRIGGER_LABELS.get(trigger, "실행 방식 확인 필요"),
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
                "status": (
                    "자동 조회 켜짐"
                    if active.mail_schedule_enabled
                    else "자동 조회 꺼짐"
                ),
                "ok": (
                    not active.mail_schedule_enabled
                    or (active.gmail_enabled and active.gmail_token_path.exists())
                ),
            },
        }
        next_mail = (
            datetime.now(UTC) + timedelta(seconds=active.mail_check_interval_seconds)
            if active.mail_schedule_enabled and active.gmail_enabled
            else None
        )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            context(request, data=data, services=services, next_mail=next_mail),
        )

    @application.get("/workflows", response_class=HTMLResponse)
    def workflows(request: Request) -> HTMLResponse:
        if active.mail_schedule_enabled:
            seconds = active.mail_check_interval_seconds
            mail_schedule = (
                f"{seconds // 60}분마다" if seconds % 60 == 0 else f"{seconds}초마다"
            )
        else:
            mail_schedule = "자동 실행 꺼짐"
        rows = [
            {"key": "mail_processing", "name": "메일 처리", "schedule": mail_schedule, "description": "지메일 수집부터 카테고리 분기까지", "nodes": len(WORKFLOW_GRAPHS["mail_processing"])},
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
    def run_detail(
        request: Request,
        run_id: str,
        mail: str = "",
        node: str = "",
    ) -> HTMLResponse:
        run, nodes = get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")

        run_mails = list_run_mails(run_id)
        selected_mail = next(
            ((event, message) for event, message in run_mails if event.id == mail),
            run_mails[0] if run_mails else None,
        )
        selected_event = selected_mail[0] if selected_mail else None
        selected_message = selected_mail[1] if selected_mail else None
        scoped_nodes = [
            item
            for item in nodes
            if item.mail_event_id is None
            or (selected_event is not None and item.mail_event_id == selected_event.id)
        ]
        graph = _build_run_graph(run.workflow_type, scoped_nodes)
        available_keys = {
            item["key"] for item in graph["all_nodes"] if item["record_count"]
        }
        selected_node = next(
            (
                item["key"]
                for item in graph["all_nodes"]
                if item["record_count"] and node in {item["key"], item["dom_id"]}
            ),
            "",
        )
        if not selected_node:
            failed = next(
                (
                    item["key"]
                    for item in graph["all_nodes"]
                    if item["record_count"] and item["status"] == "FAILED"
                ),
                "",
            )
            mail_node = next(
                (
                    item.node_key
                    for item in scoped_nodes
                    if selected_event is not None
                    and item.mail_event_id == selected_event.id
                    and item.status != "SKIPPED"
                ),
                "",
            )
            selected_node = failed or mail_node or next(iter(available_keys), "")

        detail_context: dict[str, Any] = {
            "mail_detail": None,
            "thread_views": [],
            "attachment_views": [],
            "decision_detail": None,
            "case_detail": None,
            "card_body": "",
        }
        if selected_message is not None:
            mail_detail_data = get_mail_detail(selected_message.message_id)
            if mail_detail_data is not None:
                detail_context.update(
                    {
                        "mail_detail": mail_detail_data,
                        "thread_views": [
                            {
                                "row": message,
                                "actual_body": _read_text(active, message.actual_body_path),
                                "quoted_body": _read_text(active, message.quoted_body_path),
                            }
                            for message in mail_detail_data["thread_messages"]
                        ],
                        "attachment_views": [
                            {
                                "row": attachment,
                                "extracted_text": _read_text(
                                    active, attachment.extracted_text_path
                                ),
                                "ocr_text": _read_text(active, attachment.ocr_text_path),
                            }
                            for attachment in mail_detail_data["attachments"]
                        ],
                    }
                )
            if selected_event and selected_event.ai_decision_id:
                detail_context["decision_detail"] = get_ai_decision(
                    selected_event.ai_decision_id
                )
            if selected_event and selected_event.business_case_id:
                case_detail_data = get_business_case(selected_event.business_case_id)
                detail_context["case_detail"] = case_detail_data
                if case_detail_data is not None:
                    detail_context["card_body"] = _read_text(
                        active, case_detail_data["case"].card_body_path
                    )
        return templates.TemplateResponse(
            request,
            "run_detail.html",
            context(
                request,
                run=run,
                workflow_name=WORKFLOW_LABELS.get(run.workflow_type, run.workflow_type),
                selected_run_status_label=RUN_STATUS_LABELS.get(run.status, "상태 확인 필요"),
                selected_trigger_label=TRIGGER_LABELS.get(run.trigger_type, "실행 방식 확인 필요"),
                graph=graph,
                node_record_count=len(scoped_nodes),
                run_mails=run_mails,
                selected_event=selected_event,
                selected_message=selected_message,
                selected_node=selected_node,
                **detail_context,
            ),
        )

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

    @application.get("/decisions", response_class=HTMLResponse)
    def decisions_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "decisions.html",
            context(request, decisions=list_ai_decisions()),
        )

    @application.get("/decisions/{decision_id}", response_class=HTMLResponse)
    def decision_detail(request: Request, decision_id: str) -> HTMLResponse:
        detail = get_ai_decision(decision_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="AI decision not found")
        return templates.TemplateResponse(
            request,
            "decision_detail.html",
            context(request, **detail),
        )

    @application.get("/cases", response_class=HTMLResponse)
    def cases_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "cases.html",
            context(request, cases=list_business_cases()),
        )

    @application.get("/cases/{case_id}", response_class=HTMLResponse)
    def case_detail(request: Request, case_id: str) -> HTMLResponse:
        detail = get_business_case(case_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="business case not found")
        card_body = _read_text(active, detail["case"].card_body_path)
        return templates.TemplateResponse(
            request,
            "case_detail.html",
            context(request, **detail, card_body=card_body),
        )

    @application.get("/prompts", response_class=HTMLResponse)
    def prompts_page(request: Request, file: str = "") -> HTMLResponse:
        bundle = build_prompt_bundle()
        selected = next((item for item in bundle.files if item.path == file), None)
        if selected is None and bundle.files:
            selected = bundle.files[0]
        return templates.TemplateResponse(
            request,
            "prompts.html",
            context(request, bundle=bundle, selected=selected),
        )

    @application.get("/outbox", response_class=HTMLResponse)
    def outbox_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "outbox.html",
            context(
                request,
                rows=list_outbox(),
                discord_mode=active.discord_mode,
            ),
        )

    return application


app = create_app()
