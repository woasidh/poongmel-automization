# 이슈 4. 오더·RW·SI 및 시간별 상태 갱신 구축

## Goal

상류 공급사 오더 메일을 RW와 SI 장부로 저장하고 품목별 배정 관계를 추적하며, 관리 UI에서 RW와 SI를 양방향으로 조회할 수 있게 한다. 동시에 두 번째 Prefect 워크플로를 완성하여 모든 활성 국내 발주의 오더시트·재고 상태를 1시간마다 다시 계산하고 변경된 Discord 카드를 삭제 후 재발송한다.

## 사용자 검수 결과물

1. 실제 또는 fixture 오더 메일에서 RW와 품목별 발주수량이 장부에 저장된다.
2. SI 메일과 선적서류에서 SI, 운송방식, ETD·ETA, 품목별 선적수량이 저장된다.
3. RW 하나가 여러 SI로 분할되는 사례를 UI에서 확인한다.
4. SI 하나가 여러 RW 품목을 포함하는 사례를 UI에서 확인한다.
5. RW 상세에서 연결 SI로, SI 상세에서 연결 RW로 바로 이동한다.
6. 오더 진행 상태에 따라 `rw`, `si`, `완료` 카드가 생성 또는 갱신된다.
7. 관리 UI에서 1시간 갱신 워크플로가 실제 노드와 이력으로 보인다.
8. 오더시트 입력 또는 가용재고가 바뀌면 발주 상태와 Discord 채널·본문이 자동 반영된다.
9. 아무 값도 바뀌지 않은 발주는 Discord 알림을 다시 만들지 않는다.
10. 한 발주가 실패해도 같은 회차의 다른 발주는 계속 갱신된다.
11. 오더 메일 실행 상세에서 `RW·SI DB 처리`와 `진행·완료 상태 계산` 노드가 실행된 것을 확인한다.

## 선행 조건

- 이슈 1 완료
- 이슈 2 완료
- 이슈 3 완료
- 레거시 오더·RW·SI 파서와 기존 DB 구조를 읽을 수 있음

## 범위

### 포함

- 상류 오더 category 행동강령 이관
- RW·SI 데이터 모델
- 공급사-qualified RW 식별키
- Invoice·Packing List·AWB·BL 등 선적서류 파서 이관
- RW 품목과 SI 품목의 다대다 allocation
- 오더 진행·완료 상태기계
- `rw`, `si`, `완료` Discord 라우팅과 renderer
- RW·SI 목록·상세·양방향 링크 UI
- `order_status_refresh` Prefect flow 전체 구현
- 활성 국내 발주 1시간 재검사
- 변경 감지와 Discord 삭제·재발송
- 메일 갱신과 시간 갱신의 동시성·오래된 쓰기 차단
- 상태 갱신 실행 UI와 이력

### 제외

- 해외 공급사 시스템 쓰기
- 오더시트·재고표 자동 수정
- ERP 출고·batch 완료 기능
- 사용자가 UI에서 RW·SI 값을 직접 편집하는 기능
- 운영 전환과 기존 시스템 종료

## 핵심 원칙

### 국내 발주와 오더를 구분

- 외부 국내 고객이 풍림에 보내는 구매 요청은 `발주`다.
- 풍림이 상류 공급사에 보내는 구매 PO와 그 회신은 `오더`다.
- 제목의 `발주`, `PO`, `Order` 단어만으로 방향을 뒤집지 않는다.
- AI가 분류하지만 구조화된 송수신 방향과 발주서의 buyer·supplier 근거를 저장한다.
- 방향 근거가 충돌하면 보류한다.

### RW와 SI는 별도 장부

- RW는 공급사에 대한 구매 오더다.
- SI는 실제 선적 단위다.
- 동일 번호라도 공급사가 다르면 다른 RW다.
- RW와 SI는 품목별 allocation으로 연결한다.
- 품목명만 같다는 이유로 임의 연결하지 않는다.

### 시간 갱신은 최신 메일을 덮지 않음

Gmail mail event가 같은 업무를 처리 중이면, 시간 갱신은 과거 snapshot으로 그 업무를 덮어쓰지 않는다. 업무 revision과 source revision을 비교해 오래된 갱신을 차단한다.

### 오더 카테고리 분기 노드

AI가 `오더`를 선택하면 `mail_processing` 안에서 다음 전용 노드만 실행한다.

1. `persist_rw_si`: 메일과 선적서류를 판독해 RW·SI 장부와 품목별 allocation을 생성 또는 갱신한다.
2. `calculate_upstream_order_status`: RW·SI 진행·완료 상태와 `rw`, `si`, `완료` Discord 목적지를 계산한다.

두 노드는 별도의 `node_run`과 입출력 요약을 남긴다. 관리 UI에서는 오더 분기가 선택되었고 RW·SI DB 처리가 수행됐음을 직접 보여주며, 발주를 포함한 나머지 분기는 `SKIPPED`로 표시한다.

오더 메일 분기에서는 국내 발주의 오더시트·재고표를 확인하지 않는다. 국내 발주 확인은 `발주` 분기의 `check_order_sheet_stock`과 별도 `order_status_refresh` workflow만 담당한다.

## 상세 설계

### 1. RW 데이터 모델

#### `rw_orders`

- `id`
- `business_case_id`
- `supplier_key`
- `supplier_name`
- `rw_reference`
- `status`
- `source_thread_id`
- `source_mail_event_id`
- `order_date`
- `requested_timing`
- `attachment_name`
- `created_at_utc`
- `updated_at_utc`

unique key는 `supplier_key + rw_reference`다.

#### `rw_items`

- `id`
- `rw_order_id`
- `sequence`
- `raw_product_name`
- `company_display_name`
- `product_key`
- `ordered_quantity`
- `unit`
- `allocated_quantity`
- `received_quantity`
- `remaining_quantity`

수량은 원문 단위와 계산용 표준값을 함께 보존한다.

### 2. SI 데이터 모델

#### `si_shipments`

- `id`
- `business_case_id`
- `si_key`
- `si_reference`
- `supplier_key`
- `supplier_name`
- `transport_mode`: `AIR`, `SEA`, `UNKNOWN`
- `departure`
- `eta`
- `customs_scheduled_date`
- `customs_actual_date`
- `warehouse_scheduled_date`
- `actual_inbound_date`
- `route`
- `booking_reference`
- `vessel_or_flight`
- `status`
- `source_thread_id`
- `source_mail_event_id`

정식 SI 번호가 없지만 실제 Invoice·Packing List·운송장으로 선적이 확인되면 임의 SI 번호를 만들지 않고 검증된 provisional `si_key`와 `si_reference=확인 필요`를 사용한다.

#### `si_items`

- `id`
- `si_shipment_id`
- `sequence`
- `raw_product_name`
- `company_display_name`
- `product_key`
- `shipped_quantity`
- `unit`
- `lot` nullable

#### `rw_si_allocations`

- `id`
- `rw_item_id`
- `si_item_id`
- `allocated_quantity`
- `unit`
- `allocation_source_type`
- `evidence_snapshot_id`
- `status`
- `created_at_utc`
- `updated_at_utc`

한 RW 품목의 allocation 합계는 원 발주수량을 넘을 수 없다. 초과하면 자동 보정하지 않고 보류한다.

### 3. 오더 source 추출

메일과 첨부에서 다음을 읽는다.

- supplier
- RW·PO reference
- 품목과 전체 grade
- ordered quantity
- 요청 납기 또는 `ASAP`
- supplier 접수·확정 여부
- Invoice·Packing List 품목행
- SI·IV reference
- AIR·SEA
- ETD·ETA
- booking·vessel·flight·route
- 통관과 창고 입고 예정·실제 일자

Invoice의 상품 표는 첫 제품행부터 `TOTAL` 직전까지 모두 보존한다. `iVC`처럼 상품명에 포함된 token을 임의 브랜드 접두어로 제거하지 않는다.

### 4. 업무 연결

RW 연결:

1. supplier key + RW reference
2. supplier key + source thread + 정확 품목·수량

SI 연결:

1. SI reference
2. Invoice·AWB·BL 등 검증된 선적 식별값
3. supplier + source thread + 전체 품목·수량 조합

RW-SI allocation:

- 원문에서 RW reference가 명시되면 직접 연결
- SI 품목·수량과 남은 RW 품목·수량이 exact match인 유일 후보면 연결
- 후보가 없거나 둘 이상이면 `연결 확인 필요`로 유지
- 이미 완료된 과거 RW나 도착 SI를 품목명만으로 재사용하지 않음

### 5. 상태기계

#### RW

- `CREATED`
- `SUPPLIER_PENDING`
- `PARTIALLY_ALLOCATED`
- `FULLY_ALLOCATED`
- `PARTIALLY_RECEIVED`
- `COMPLETED`
- `HOLD`

#### SI

- `IDENTIFIED`
- `DEPARTURE_PENDING`
- `IN_TRANSIT`
- `ARRIVED`
- `CUSTOMS`
- `WAREHOUSE_PENDING`
- `RECEIVED`
- `COMPLETED`
- `HOLD`

실제 창고 입고 완료는 검증된 재고표 증가 또는 운영자 근거가 있을 때만 확정한다. ETA만으로 실제 입고를 만들지 않는다.

### 6. 오더 카드와 채널

#### `rw` 채널

- 신규·진행 중 RW
- 공급사 회신 대기
- SI 미배정 또는 부분 배정

#### `si` 채널

- 실제 선적이 확인된 SI
- 운송·ETA·통관·창고 입고 진행

#### `완료` 채널

- 해당 오더 업무의 완료 조건이 모두 충족

한 메일은 하나의 오더 업무와 카드만 만든다. 한 메일에 RW와 SI 내용이 같이 있으면 AI가 최신 실제 업무 목적을 하나로 선택하고 나머지는 그 업무의 연결 데이터로 저장한다.

카드에는 모든 관련 품목을 표시하며 임의 상한으로 누락하지 않는다.

### 7. RW·SI UI

#### RW 목록

- supplier, RW, status
- 품목 수, ordered·allocated·received·remaining
- 연결 SI 수
- 마지막 갱신

#### RW 상세

- 원 메일·첨부 근거
- 품목별 수량 progress
- allocation 목록
- 연결 SI 링크
- 업무 revision과 Discord mapping

#### SI 목록

- SI, supplier, mode, ETA, status
- 품목 수
- 연결 RW 수
- 실제 입고 여부

#### SI 상세

- 선적 일정과 경로
- 품목·수량·LOT
- allocation 목록
- 연결 RW 링크
- 원문 근거와 상태 history

양방향 링크는 URL query가 아니라 실제 FK를 사용한다.

## 오더시트·재고 갱신 워크플로

### 1. 실행 단위

- schedule: 1시간마다
- concurrency: 1
- 입력: 활성 국내 발주 전체
- 출력: 발주별 상태 점검 이력과 필요한 Discord mutation

동일 회차에서 오더시트와 재고표 원본은 가능한 한 한 번씩 읽고 snapshot hash를 고정한다. 발주마다 원본을 다시 내려받아 서로 다른 시점 데이터를 섞지 않는다.

### 2. 노드

1. `load_active_orders`
2. `refresh_order_sheet_snapshot`
3. `refresh_stock_snapshot`
4. `recheck_orders`
5. `calculate_status_changes`
6. `enqueue_discord_reposts`
7. `finalize_refresh_run`

`recheck_orders` 내부에서 발주별 독립 node run을 기록한다. 한 발주 실패는 그 발주 결과만 실패·보류로 남기고 다음 발주를 계속 처리한다.

### 3. 활성 발주

포함:

- 현재 `발주` 또는 `오더시트` 추적 대상
- 완료·취소가 확정되지 않은 발주
- 오류 후 재검사가 필요한 발주

제외:

- 완료·취소·종료된 발주
- 사용자가 명시적으로 추적 종료한 발주
- 데이터 무결성 오류로 별도 수동 복구 상태인 발주

### 4. 변경 감지

다음 값의 canonical snapshot hash를 비교한다.

- order line values
- order sheet match
- ERP check
- stock on hand
- reservation
- available·shortage
- `order_sheet_ready`
- destination channel
- rendered Discord body

본문 SHA와 채널이 모두 같으면 Discord 작업을 만들지 않는다. 상태 점검 이력만 저장한다.

### 5. 동시성 제어

- business case별 DB lock 또는 optimistic revision check
- 갱신 시작 시 `source_case_revision` 저장
- Discord enqueue 전 현재 revision 재조회
- 현재 revision이 달라졌으면 periodic 작업을 `SUPERSEDED` 처리
- 최신 Gmail mail event가 pending·running이면 periodic 작업이 current snapshot을 덮지 못함

### 6. Discord 반영

변경이 있으면 이슈 2 Outbox의 `REPLACE` protocol을 그대로 사용한다.

- false → true: 기존 `발주` 메시지 삭제 후 `오더시트` 채널 재발송
- true → false: 기존 `오더시트` 메시지 삭제 후 `발주` 채널 재발송
- 동일 채널 본문 변경: 기존 메시지 삭제 후 같은 채널 재발송
- 변경 없음: 작업 없음

### 7. 갱신 UI

- schedule과 다음 실행
- 회차별 active·success·changed·unchanged·failed 수
- 사용한 오더시트·재고 snapshot 시각과 hash
- 발주별 이전·현재 ready와 변경 이유
- superseded와 lock 충돌
- 생성된 Outbox 링크

## 작업 순서

1. 레거시 RW·SI 규칙과 DB를 목록화한다.
2. RW·SI·allocation migration을 만든다.
3. 오더·선적서류 parser를 이관한다.
4. supplier-qualified identifier와 resolver를 만든다.
5. allocation validator와 상태기계를 만든다.
6. 오더 renderer와 Discord route를 연결한다.
7. RW·SI 양방향 UI를 만든다.
8. 두 번째 Prefect flow의 실제 노드를 구현한다.
9. snapshot 고정·active order 재검사·변경 감지를 구현한다.
10. Gmail 처리와 periodic 갱신의 revision guard를 구현한다.
11. 변경된 발주의 Discord replace를 연결한다.
12. 실제/fixture RW·SI와 시간 갱신 사례를 검수한다.

## 테스트

### 단위

- supplier-qualified RW key
- RW remaining quantity
- 다대다 allocation 합계
- RW·SI 상태 전이
- ETA와 실제 입고 구분
- active order filter
- canonical change hash
- source revision guard

### 계약

- Invoice·Packing List·AWB·BL parser
- 레거시 upstream DB 반환값
- Prefect 1시간 schedule
- order sheet·stock snapshot reuse

### 통합

- RW 생성 → SI 부분 배정 → 복수 SI → 완료
- SI 하나에 여러 RW 품목 배정
- 후보 RW 복수 시 hold
- 시간 갱신 false → true와 channel 이동
- 시간 갱신 true → false 복귀
- 내용 변경 없음에서 Discord mutation 0
- 한 발주 실패 후 다음 발주 성공
- Gmail update와 periodic update 경쟁에서 오래된 갱신 차단
- worker 재시작 후 Outbox 계속 처리

## 완료 조건

- RW와 SI가 별도 정규화 장부로 저장된다.
- RW·SI allocation이 품목·수량 단위로 연결된다.
- 복수 후보를 임의 연결하지 않는다.
- 오더 카드가 `rw`, `si`, `완료`로 라우팅된다.
- RW·SI UI 양방향 링크가 동작한다.
- `order_status_refresh`가 정확히 1시간마다 실행된다.
- 모든 활성 발주가 같은 회차 snapshot으로 재검사된다.
- 변경된 카드만 삭제 후 재발송된다.
- 메일 처리 중인 최신 업무를 periodic snapshot이 덮지 않는다.
- 한 발주 실패가 다른 발주를 막지 않는다.
- 실행·변경·실패 근거가 UI와 로그에 남는다.

## 이슈 종료 시 남는 산출물

- RW·SI·allocation DB migration
- 오더·선적서류 adapter
- RW·SI 상태기계와 renderer
- 양방향 장부 UI
- 완성된 `order_status_refresh` flow
- revision guard와 변경 감지
- 시간 갱신 회귀·통합 테스트
- 사용자 검수용 RW·SI·발주 갱신 실행 기록
