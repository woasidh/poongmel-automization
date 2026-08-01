# 이슈 3. 국내 발주 전체 자동화 구축

## Goal

`발주`로 판정된 메일 한 건에서 본문, 첨부 발주서, ECOUNT, 고객사 발주 사이트의 해당 근거를 수집하고 발주 데이터를 저장한 뒤, 오더시트와 가용재고를 즉시 비교하여 올바른 Discord 채널과 카드까지 결정하는 국내 발주 전체 흐름을 완성한다.

이 이슈가 끝나면 사용자는 실제 발주 한 건을 선택해 원문 근거, 파싱 결과, 품목별 재고 계산, 오더시트 매칭, 최종 상태값과 카드가 어떻게 만들어졌는지 UI에서 확인할 수 있어야 한다.

## 사용자 검수 결과물

1. 본문 발주, 첨부 발주서, ECOUNT, 고객사 사이트 발주 사례를 각각 실행한다.
2. 발주 상세 화면에서 네 근거의 적용 여부와 원문·파싱값을 비교한다.
3. 업체, 모든 PO, 품목·정확한 grade, 수량, 납기, 납품처가 저장된다.
4. 품목별 현재재고·예약량·가용재고·부족량을 확인한다.
5. 품목별 오더시트 일치 행, 탭, ERP 체크와 불일치 이유를 확인한다.
6. 모든 품목의 오더시트와 가용재고가 충족되면 상태값이 `true`가 된다.
7. 상태값 `true`는 `오더시트`, `false`는 `발주` 채널 카드가 된다.
8. 한 메일에 여러 PO가 있어도 발주 업무와 Discord 카드는 하나만 생기고 모든 PO와 품목이 카드에 표시된다.
9. 근거를 확정하지 못한 발주는 추정 카드가 아니라 보류 카드가 된다.
10. 실행 상세에서 `발주 DB·4종 근거 처리`와 `오더시트·재고표 확인 및 상태 계산` 노드가 실행된 것을 확인한다.

## 선행 조건

- 이슈 1 완료
- 이슈 2 완료
- 기존 `Pungmail Order Sites` 프로젝트가 현재 PC에서 읽기 전용 조회 가능
- 오더시트와 재고표의 기존 OAuth·공개 export·검증 스냅샷 중 최소 하나가 사용 가능

## 범위

### 포함

- 레거시 국내 발주 행동강령 이관
- 메일 본문 발주 파서
- PDF·Excel·이미지 첨부 발주서 파서
- ECOUNT 수신문서 reader
- 고객사 발주 사이트 reader 연결
- 발주 문서 profile 재사용
- 회사 품목 카탈로그 정규화
- 발주·PO·품목 line 영속 모델
- 오더시트 reader와 exact match
- 재고표 reader와 가용재고 계산
- 신규 발주 처리 중 즉시 상태 계산
- `order_sheet_ready` 상태값
- 발주 카드 renderer
- `발주`·`오더시트` Discord 라우팅
- 발주 상세·근거·재고·시트 UI
- 레거시 발주 회귀 테스트 이관

### 제외

- 1시간 주기 전체 활성 발주 재검사
- RW·SI 장부와 수입 진행 연결
- 실제 ERP 출고·batch 완료 처리
- 오더시트·재고표 쓰기
- 고객사 사이트의 승인·확인·변경 버튼 조작
- 운영 Discord 활성화

## 핵심 원칙

### 메일 한 건은 발주 업무 한 건

- 한 Gmail 메시지의 발주 결과는 `orders` 한 행이다.
- 여러 PO는 `order_references` 여러 행으로 보존한다.
- 여러 품목·분할 납기는 `order_lines` 여러 행으로 보존한다.
- 한 메일에서 PO별 업무나 PO별 Discord 카드를 만들지 않는다.
- 후속 메일이 기존 발주 하나와 exact match되면 기존 발주만 갱신한다.

### 외부 시스템은 읽기 전용

- ECOUNT 원문을 읽을 수 있지만 수정하지 않는다.
- 고객사 사이트는 조회만 한다.
- 오더시트와 재고표는 조회만 한다.
- 자동화 과정에서 확인·승인·변경·저장 버튼을 누르지 않는다.

### 의미 분류는 AI, 수치 판정은 Python

- 발주 category와 원문 업무값 후보는 이슈 2의 AI node가 만든다.
- Python은 발주서·사이트·시트·재고의 구조화 데이터를 읽고 정확 값과 수치를 계산한다.
- 외부 근거가 AI 후보와 충돌하면 Python이 임의 교정하지 않고 보류한다.
- 사이트나 발주서의 검증값이 명확하고 AI payload의 단순 표기 차이만 있으면 카탈로그 표시 규칙을 적용한다.

### 발주 카테고리 분기 노드

AI가 `발주`를 선택하면 `mail_processing` 안에서 다음 전용 노드만 실행한다.

1. `process_order_sources`: 발주 DB와 본문·첨부·ECOUNT·고객사 사이트 근거를 처리한다.
2. `check_order_sheet_stock`: 오더시트 exact match, 현재재고·예약량·가용재고와 `order_sheet_ready`를 계산한다.

두 노드는 별도의 `node_run`과 입출력 요약을 남긴다. 관리 UI에서는 이 경로를 강조하고 오더·샘플자료견적 등 선택되지 않은 분기를 `SKIPPED`로 표시한다. 어느 노드에서든 근거를 확정할 수 없으면 실패 위치를 남기고 `process_hold`로 이동한다.

## 상세 설계

### 1. 발주 데이터 모델

#### `orders`

- `business_case_id` primary/foreign key
- `customer_company`
- `customer_company_key`
- `order_status`
- `order_sheet_ready`
- `direct_delivery`
- `note`
- `latest_source_mail_event_id`
- `created_at_utc`
- `updated_at_utc`

#### `order_references`

- `id`
- `order_id`
- `reference_type`: `PO`, `ECOUNT_SERIAL`, `SITE_NOTICE`
- `reference_value`
- `customer_company_key`
- `sequence`

동일 PO 문자열은 고객사가 다르면 다른 식별값이다. unique 범위는 `customer_company_key + reference_type + reference_value + order_id`다.

#### `order_lines`

- `id`
- `order_id`
- `sequence`
- `order_reference_id` nullable
- `raw_product_name`
- `company_display_name`
- `item_code` nullable
- `spec` nullable
- `catalog_candidates_json`
- `quantity_decimal`
- `unit`
- `due_date`
- `delivery_destination`
- `customer_item_code` nullable
- `note`

#### `order_source_checks`

- `id`
- `order_id`
- `source_type`: `MAIL_BODY`, `ATTACHMENT`, `ECOUNT`, `CUSTOMER_SITE`
- `source_key`
- `required`
- `status`
- `parser_name`
- `payload_json`
- `evidence_snapshot_id`
- `checked_at_utc`
- `error`

#### `order_document_profiles`

레거시 구조를 정리해 다음을 보존한다.

- customer company key
- file kind
- header signature
- field mapping
- parser name
- template fingerprint
- source message·PO
- validation·reuse count

검증 완료 전에는 profile을 생성하거나 갱신하지 않는다.

#### `order_sheet_matches`

- `order_line_id`
- `sheet_month`
- `sheet_name`
- `row_number`
- `status`: `MATCHED`, `MISSING`, `PARTIAL`, `DUE_MISMATCH`, `AMBIGUOUS`, `READ_FAILED`
- `matched_company`
- `matched_product`
- `matched_quantity`
- `matched_due_date`
- `erp_registered`
- `source_sha256`
- `checked_at_utc`

#### `stock_checks`

- `order_line_id`
- `stock_as_of_date`
- `on_hand_quantity`
- `reserved_quantity`
- `available_quantity`
- `required_quantity`
- `shortage_quantity`
- `status`
- `source_sha256`
- `checked_at_utc`

#### `order_status_history`

- `order_id`
- `revision`
- `previous_ready`
- `current_ready`
- `previous_channel`
- `current_channel`
- `reason_json`
- `source_mail_event_id` nullable
- `workflow_run_id`
- `created_at_utc`

### 2. 네 가지 근거 선택

각 메일은 해당되는 source reader만 실행한다.

#### 메일 본문

필수 대조값:

- 실제 작성문
- 품목·정확 grade
- 수량·단위
- 납기
- 납품처
- PO 또는 기타 발주 식별값

서명, 주소, 연락처, 표 머리글, 문서번호와 인용된 과거 발주는 현재 발주값으로 사용하지 않는다.

#### 첨부 발주서

지원:

- PDF text
- PDF OCR
- XLSX·XLS
- 이미지 OCR

표 머리글과 완전한 행 관계를 보존한다. 품목·수량·납기 행 수가 맞지 않거나 OCR과 원본 표가 충돌하면 보류한다.

#### ECOUNT

- 허용된 ECOUNT 수신문서 링크만 연다.
- 원본 발주서에서 업체·품목·수량·납기·입고지를 읽는다.
- 화면 메뉴·버튼·표 머리글을 품목으로 사용하지 않는다.
- 제로폭 문자 등 시스템 노이즈를 제거한다.
- 실제 품목행을 찾지 못하면 숫자를 추정하지 않는다.

#### 고객사 발주 사이트

기존 별도 프로젝트의 검증된 CLI 또는 Python interface를 subprocess adapter로 호출한다.

대상 예:

- 콜마
- 코스맥스
- LG
- 아모레
- 코리아나
- 제뉴원
- GC녹십자
- 코스모코스

사이트 발주 업체는 site result가 성공하기 전 Discord 발주 카드를 확정하지 않는다. 조회 결과에는 site key, PO, 사업장, 품목, 수량, 납기, 납품처, 상태와 원본 snapshot hash를 포함한다.

### 3. 근거 병합

근거 우선순위 하나로 무조건 덮어쓰지 않는다. 필드별로 모든 적용 source를 비교한다.

1. 각 source의 구조화 값을 별도로 저장한다.
2. exact identifier로 같은 발주·품목인지 확인한다.
3. 값이 일치하면 verified field로 확정한다.
4. 표기 차이는 회사 카탈로그와 검증된 업체별 normalization만 적용한다.
5. 핵심값이 충돌하면 `source conflict`로 보류한다.
6. 어떤 source가 원래 존재하지 않는 경우는 실패로 보지 않는다.
7. 존재해야 하는 site/ECOUNT/attachment를 읽지 못한 경우는 실패다.

### 4. 회사 품목명

- 카드와 업무 표시에는 `companyDisplayName`을 사용한다.
- exact item code가 있으면 해당 catalog row를 우선한다.
- 이름만 있고 복수 규격이면 후보를 모두 evidence에 저장한다.
- 발주 실행이 규격에 의존하면 구분할 수 있을 때까지 보류한다.
- catalog `spec`과 발주 총수량을 같은 의미로 비교하지 않는다.
- `NIKKOL`, `PROMOIS` 선두 접두어만 승인 규칙대로 제거한다.
- 상품명과 INCI를 합치지 않는다.

### 5. 오더시트 조회

납기일이 속한 월 탭을 각 `order_line`별로 조회한다. 현재 월·다음 달로 제한하지 않는다.

exact match 조건:

- 업체 정규키
- 회사 표시 규칙을 적용한 정확 품목
- 수량
- 납기
- PO가 시트에 있으면 현재 발주 PO

여러 행이 후보면 임의 선택하지 않는다. 품목별 납기가 다르면 각 월 탭을 따로 조회한다.

### 6. 가용재고 계산

레거시 계산을 동일하게 이관한다.

```text
available = latest_on_hand - future_unshipped_reservations
shortage = max(required - available, 0)
```

주요 규칙:

- 현재 발주와 exact match된 오더시트 행은 예약량에서 한 번 제외한다.
- 다른 업체의 같은 품목·수량 행은 현재 발주 예약에서 제외하지 않는다.
- 최신 재고표 기준일 다음 영업일 납기까지는 이미 재고표에 반영된 출고로 처리하는 레거시 규칙을 유지한다.
- 재고표를 정상 판독했고 정확 품목 행이 없으면 재고·가용은 0이다.
- 재고표 자체를 판독하지 못했으면 0으로 추정하지 않고 확인 실패다.
- 수량 단위가 변환 가능한 경우에만 표준 단위로 변환한다.

### 7. `order_sheet_ready`

각 line의 조건:

```text
line_ready = order_sheet_match == MATCHED
             and available_quantity >= required_quantity
```

전체 발주:

```text
order_sheet_ready = all(line_ready for every order_line)
```

따라서 다음은 모두 `false`다.

- 일부 품목만 입력
- 수량 불일치
- 납기 불일치
- 후보 행 복수
- 재고 부족
- 재고 판독 실패
- 필수 site·ECOUNT·attachment 검증 실패

### 8. 카드와 라우팅

카드 필드:

- category와 업체
- 모든 PO·발주번호
- 모든 품목·수량·납기
- 납품처
- 품목별 재고·가용·부족
- 오더시트 상태
- ERP 체크 표시
- 직출·분할·변경 등 레거시 비고
- 검증 필요한 값

채널:

- `order_sheet_ready=false` → `발주`
- `order_sheet_ready=true` → `오더시트`

한 메일의 복수 PO·품목을 임의 개수로 자르지 않는다. Discord 본문 한도를 넘으면 요약 본문과 전체 상세 첨부파일을 한 메시지로 보내 업무와 카드·mapping을 각각 하나로 유지한다. 첨부까지 전송할 수 없으면 일부 품목을 누락하지 않고 보류한다.

### 9. UI

#### 발주 목록

- 업체, PO 목록, 품목 수
- 현재 ready 상태
- 현재 Discord 채널
- 마지막 근거 갱신 시각
- 보류·불일치 표시

#### 발주 상세

- 메일 이벤트와 업무 revision
- 네 source check 카드
- source별 구조화 값 비교
- order line 표
- catalog candidates
- order sheet matched row와 원본 hash
- stock calculation breakdown
- 최종 `order_sheet_ready` 이유
- 카드 preview와 Discord mapping
- 상태 history

UI에서 site, sheet, stock 원본을 수정하는 기능은 만들지 않는다.

## 작업 순서

1. 국내 발주 레거시 규칙과 테스트를 목록화한다.
2. order DB migration을 만든다.
3. 본문·첨부 발주서 reader adapter를 이관한다.
4. order document profile을 이관한다.
5. ECOUNT reader를 이관한다.
6. 고객사 site adapter를 연결한다.
7. source별 구조화 결과와 병합 validator를 만든다.
8. catalog normalization을 발주값에 적용한다.
9. order sheet reader와 exact matcher를 이관한다.
10. stock reader와 가용재고 계산을 이관한다.
11. `process_order_sources`와 `check_order_sheet_stock`을 독립 노드로 연결한다.
12. 즉시 `order_sheet_ready` 계산과 history를 만든다.
13. 발주 renderer와 test Discord 라우팅을 연결한다.
14. 발주 UI를 만든다.
15. 레거시 발주 회귀 테스트와 실제 표본을 검증한다.

## 테스트

### 단위

- 복수 PO를 한 order에 보존
- 품목별 납기와 월 탭 선택
- company+PO 식별키
- catalog duplicate와 spec 구분
- stock reservation 계산
- ready all 조건
- renderer 전체 품목 보존

### 계약

- 본문·PDF·Excel·OCR parser
- ECOUNT result schema
- 각 고객사 site CLI result schema
- order sheet row schema
- stock snapshot schema

### 통합

- 네 source 유형별 발주 한 건
- site 필수 업체의 조회 실패 보류
- 복수 품목 중 한 품목 재고 부족
- 오더시트 일부입력·납기불일치
- 모든 품목 충족 후 `오더시트` 채널
- 후속 메일로 기존 발주 하나 갱신
- 한 메일 복수 PO가 한 카드에 표시
- Discord 삭제 후 새 channel 재발송

### 회귀

기존 테스트 중 Gmail attachment, ECOUNT, 고객사별 orders, multi PO, order sheet, stock, product display와 order change 관련 사례를 신규 interface로 이관한다.

## 완료 조건

- 네 발주 source가 adapter로 분리되어 동작한다.
- 적용되는 모든 source의 결과와 hash가 저장된다.
- 업체·PO·품목·수량·납기·납품처가 구조화된다.
- 메일 하나에 발주 업무와 Discord mapping이 하나만 생긴다.
- 모든 line에 order sheet와 stock check가 있다.
- `order_sheet_ready`가 레거시 로직과 같은 결과를 낸다.
- false는 `발주`, true는 `오더시트`로 렌더링된다.
- 필수 source 실패와 핵심값 충돌은 보류된다.
- UI에서 계산과 최종 판단 근거를 확인할 수 있다.
- 외부 site·sheet·stock에는 쓰기 동작이 없다.
- 검수용 Discord에서 신규·갱신·채널 변경이 확인된다.

## 이슈 종료 시 남는 산출물

- 국내 발주 domain과 DB migration
- 네 source reader adapter
- 발주 근거 병합 validator
- order sheet·stock reader와 계산기
- 발주 renderer·라우터
- 발주 관리 UI
- 이관된 발주 회귀 테스트
- 사용자 검수용 대표 발주 실행 기록
