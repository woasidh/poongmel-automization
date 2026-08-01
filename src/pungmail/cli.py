from __future__ import annotations

import argparse
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from pungmail.config import get_settings
from pungmail.observability.logging import configure_logging


def db_upgrade() -> None:
    settings = get_settings()
    configuration = Config(str(settings.project_root / "alembic.ini"))
    command.upgrade(configuration, "head")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pungmail")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("db-upgrade", help="업무 DB를 최신 스키마로 올립니다.")
    commands.add_parser("gmail-check", help="Gmail 조회 인증과 history ID만 확인합니다.")
    commands.add_parser("run-mail-once", help="메일 처리 워크플로를 한 번 실행합니다.")
    commands.add_parser("run-order-refresh", help="시간별 갱신 골격을 한 번 실행합니다.")
    commands.add_parser("seed-demo", help="UI 검수용 메일 한 건을 만듭니다.")
    commands.add_parser("seed-issue2-demo", help="2단계 UI 검수용 생성·갱신 업무를 만듭니다.")
    serve = commands.add_parser("serve-ui", help="로컬 관리 UI를 실행합니다.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8000, type=int)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    configure_logging(settings)
    if args.command == "db-upgrade":
        db_upgrade()
    elif args.command == "gmail-check":
        from pungmail.adapters.gmail import GmailReadOnlyClient

        history_id = GmailReadOnlyClient(settings).current_history_id()
        print(f"Gmail read-only connection OK; history id length={len(history_id)}")
    elif args.command == "run-mail-once":
        from pungmail.workflows.mail_processing import mail_processing

        print(mail_processing(trigger_type="MANUAL"))
    elif args.command == "run-order-refresh":
        from pungmail.workflows.order_status_refresh import order_status_refresh

        print(order_status_refresh(trigger_type="MANUAL"))
    elif args.command == "seed-demo":
        from pungmail.demo import seed_demo

        print(f"Demo mail ready: {seed_demo()}")
    elif args.command == "seed-issue2-demo":
        from pungmail.demo import seed_issue2_demo

        print(f"Issue 2 demo case ready: {seed_issue2_demo()}")
    elif args.command == "serve-ui":
        import uvicorn

        if args.host not in {"127.0.0.1", "localhost"}:
            raise SystemExit("관리 UI는 localhost에만 바인딩할 수 있습니다.")
        uvicorn.run("pungmail.api.app:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
