from __future__ import annotations

from importlib import import_module

from fastapi.testclient import TestClient

from pungmail.api.app import create_app
from pungmail.repositories.mail_store import enqueue_messages
from pungmail.repositories.tracking import TrackedNode, create_workflow_run, finish_workflow_run


def test_management_pages_render_observable_run(isolated_settings, monkeypatch) -> None:
    run_id = create_workflow_run("mail_processing", trigger_type="TEST")
    enqueue_messages([{"id": "message-ui", "threadId": "thread-ui"}], run_id)
    with TrackedNode(run_id, "discover_gmail") as node:
        node.set_output({"discovered": 1})
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
            ("메일 처리", "발주", "오더시트·재고표 확인", "오더", "RW·SI DB 처리", "보류"),
        ),
        ("/runs", ("실행 이력",)),
        (f"/runs/{run_id}", ("discover_gmail",)),
        ("/mails", ("(수집 대기)",)),
        ("/mails/message-ui", ("message-ui",)),
    ):
        response = client.get(path)
        assert response.status_code == 200
        for expected in expected_values:
            assert expected in response.text
