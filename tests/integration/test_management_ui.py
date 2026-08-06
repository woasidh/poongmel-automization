from __future__ import annotations

from importlib import import_module

from fastapi.testclient import TestClient

from pungmail.api.app import create_app, templates
from pungmail.repositories.mail_store import enqueue_messages
from pungmail.repositories.tracking import TrackedNode, create_workflow_run, finish_workflow_run


def test_json_views_render_korean_without_unicode_escapes() -> None:
    rendered = templates.env.from_string("{{ value | tojson(indent=2) }}").render(
        value={"category": "해외업무", "summary": "일정 확인 요청"}
    )

    assert '"category": "해외업무"' in rendered
    assert '"summary": "일정 확인 요청"' in rendered
    assert "\\ud574\\uc678\\uc5c5\\ubb34" not in rendered


def test_management_pages_render_observable_run(isolated_settings, monkeypatch) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    enqueue_messages([{"id": "message-ui", "threadId": "thread-ui"}], run_id)
    with TrackedNode(run_id, "discover_gmail") as node:
        node.set_output({"discovered": 1})
    with TrackedNode(run_id, "process_order_sources", branch_key="발주") as node:
        node.set_output({"checked": True})
    finish_workflow_run(run_id, "SUCCEEDED", {"discovered": 1})

    app_module = import_module("pungmail.api.app")
    monkeypatch.setattr(
        app_module, "_prefect_health", lambda _settings: {"status": "중지", "ok": False}
    )
    client = TestClient(create_app(isolated_settings))

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["database"] is True

    for path, expected_values in (
        ("/", ("운영 대시보드",)),
        (
            "/workflows/mail_processing",
            (
                "메일 처리",
                "지메일 새 메일 조회",
                "인공지능 분류·업무값 추출",
                "공통 처리",
                "발주 분기",
                "오더시트·재고표 확인",
                "오더",
                "RW·SI 기본정보 구조화",
                "보류",
            ),
        ),
        ("/runs", ("실행 이력",)),
        (
            f"/runs/{run_id}",
            (
                "처리한 메일",
                "선택 메일의 노드 흐름",
                "노드 결과",
                "지메일 새 메일 조회",
                "발주 기본정보 구조화",
                "정상 완료",
            ),
        ),
        ("/mails", ("(수집 대기)",)),
        ("/mails/message-ui", ("message-ui",)),
    ):
        response = client.get(path)
        assert response.status_code == 200
        for expected in expected_values:
            assert expected in response.text

    for path, heading in (
        ("/cases", "업무"),
        ("/decisions", "AI 판정"),
        ("/prompts", "프롬프트"),
        ("/outbox", "알림 Outbox"),
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert heading in response.text

    run_response = client.get(f"/runs/{run_id}")
    assert "discover_gmail" not in run_response.text
    assert "enqueue_message" not in run_response.text
    assert 'branch-column active' in run_response.text
    assert 'data-node-id="step-1"' in run_response.text
    assert 'href="/mails"' not in run_response.text
    assert 'href="/decisions"' not in run_response.text
    assert 'href="/cases">진행 업무</a>' in run_response.text

    workflow_response = client.get("/workflows/mail_processing")
    for internal_key in (
        "discover_gmail",
        "enqueue_message",
        "classify_and_extract",
        "process_order_sources",
        "render_discord",
    ):
        assert internal_key not in workflow_response.text
