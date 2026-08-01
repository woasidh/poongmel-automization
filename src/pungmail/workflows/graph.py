from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GraphNode:
    key: str
    label: str
    stage: str
    branch: str | None = None


MAIL_GRAPH: tuple[GraphNode, ...] = (
    GraphNode("discover_gmail", "Gmail 증분 조회", "common"),
    GraphNode("enqueue_message", "후보 메일 대기열 등록", "common"),
    GraphNode("validate_scope", "대상 범위 확인", "common"),
    GraphNode("collect_thread", "전체 스레드·첨부 수집", "common"),
    GraphNode("extract_evidence", "본문·첨부·OCR 근거 추출", "common"),
    GraphNode("lookup_catalog", "회사 품목 후보 조회", "common"),
    GraphNode("classify_and_extract", "AI 분류·업무값 추출", "common"),
    GraphNode("resolve_case", "기존 업무 찾기 또는 신규 업무 생성", "common"),
    GraphNode("route_category", "카테고리별 처리 분기", "router"),
    GraphNode("process_order_sources", "발주 기본정보 구조화", "branch", "발주"),
    GraphNode("check_order_sheet_stock", "오더시트·재고표 확인", "branch", "발주"),
    GraphNode("persist_rw_si", "RW·SI 기본정보 구조화", "branch", "오더"),
    GraphNode("calculate_upstream_order_status", "오더 진행·완료 계산", "branch", "오더"),
    GraphNode("process_sample_document_quote", "샘플·자료·견적 처리", "branch", "샘플자료견적"),
    GraphNode("process_punglim_document_request", "풍림 자료 요청 처리", "branch", "풍림자료요청"),
    GraphNode("process_internal_work", "사내 업무 처리", "branch", "사내업무"),
    GraphNode("process_overseas_work", "해외 업무 처리", "branch", "해외업무"),
    GraphNode("process_hold", "보류 처리", "branch", "보류"),
    GraphNode("render_discord", "Discord 카드 만들기", "common"),
    GraphNode("dispatch_outbox", "Discord 알림 반영", "common"),
    GraphNode("finalize_case", "결과·상태·이력 확정", "common"),
)


ORDER_REFRESH_GRAPH: tuple[GraphNode, ...] = (
    GraphNode("load_active_orders", "진행 중 발주 조회", "common"),
    GraphNode("refresh_source_snapshots", "오더시트·재고 원본 갱신", "common"),
    GraphNode("match_order_lines", "품목·수량·납기 대조", "common"),
    GraphNode("calculate_available_stock", "가용 재고 계산", "common"),
    GraphNode("recalculate_order_state", "발주 상태 다시 계산", "common"),
    GraphNode("publish_changed_orders", "변경 카드 다시 알림", "common"),
)


WORKFLOW_GRAPHS = {
    "mail_processing": MAIL_GRAPH,
    "order_status_refresh": ORDER_REFRESH_GRAPH,
}


FUTURE_MAIL_NODES = tuple(
    node
    for node in MAIL_GRAPH
    if node.key
    not in {
        "discover_gmail",
        "enqueue_message",
        "validate_scope",
        "collect_thread",
        "extract_evidence",
    }
)
