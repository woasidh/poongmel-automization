# 이슈 2. AI 분류·카테고리 처리·Discord 자동화 구축

## Goal

수집된 메일 한 건을 `gpt-5.4-nano` 단일 AI 노드로 정확히 하나의 업무로 판정하고, 카테고리별 데이터를 저장하며, 신규 또는 기존 업무의 Discord 카드를 삭제 후 재발송하는 자동화까지 완성한다.

국내 발주의 4종 외부 검증과 RW·SI 장부의 완성은 다음 이슈에서 진행한다. 이 이슈에서는 모든 카테고리의 구조화 결과와 카드 미리보기가 생성되어야 하며, 샘플·자료·견적, 풍림자료요청, 사내업무, 해외업무와 보류는 끝까지 동작해야 한다.

## 사용자 검수 결과물

사용자는 관리 UI와 검수용 Discord 채널에서 다음을 확인한다.

1. 메일 하나가 카테고리 하나와 업무 하나로만 판정된다.
2. AI가 사용한 모델, 프롬프트 버전, 근거와 구조화 결과를 UI에서 볼 수 있다.
3. 샘플·자료·견적 복합 요청이 카드 한 장으로 생성된다.
4. 후속 메일을 처리하면 기존 카드가 삭제되고 갱신된 카드가 새로 올라와 알림이 발생한다.
5. 샘플·자료·견적 일부 완료가 취소선으로 표시되고 전부 완료되면 완료 채널로 이동한다.
6. 풍림자료요청이 `nikko`, `seiwa`, `기타`로 분류되고 부분 완료 후 완료 채널로 이동한다.
7. 사내업무, 해외업무와 판단 실패 메일이 각각의 채널로 간다.
8. AI·추출·스키마 오류가 발생한 처리 대상 메일은 `보류` 카드가 되고 실패 원인이 로그에 남는다.
9. Discord 삭제 뒤 재발송에 실패한 작업이 UI에 `REPOST_PENDING`으로 보이고 재시도 후 복구된다.

## 선행 조건

- 이슈 1 완료
- OpenAI API key는 기존 운영 key와 같은 값을 사용하되 프로젝트 파일과 DB에 복사하지 않는다.
- 검수 단계에서는 운영 Discord 채널과 분리된 webhook 또는 명시된 test mode를 사용한다.

## 범위

### 포함

- 레거시 행동강령·분류 규칙 추출
- 공통·분류·카테고리별 프롬프트 파일
- 프롬프트 manifest·버전·SHA-256
- `gpt-5.4-nano` Responses API 단일 AI 노드
- Pydantic Structured Output 모델
- 메일 하나당 업무 하나 강제
- 기존 업무 연결 또는 신규 업무 생성
- 카테고리별 공통 데이터 모델
- 샘플·자료·견적 업무와 부분 완료
- 풍림자료요청과 공급사 라우팅·부분 완료
- 사내업무·해외업무·보류
- 발주·오더의 구조화 기본 payload와 카드 미리보기
- 회사 취급 품목 카탈로그 후보 조회
- Discord 카드 렌더러
- Discord 영속 Outbox
- 기존 메시지 삭제 후 신규 메시지 발송
- UI의 AI 결과·업무·카드·Outbox 화면

### 제외

- 국내 발주 4종 근거의 최종 보강
- 발주 오더시트·재고 상태값
- 실제 고객사 발주 사이트 검증
- RW·SI 영속 장부와 연결 계산
- 운영 Discord 발송 활성화
- 과거 전체 메일 수집

## 핵심 결정

### AI 호출은 한 번

메일 한 건에 OpenAI API 요청을 한 번만 보낸다. 다음을 별도 AI 호출로 나누지 않는다.

- 카테고리 분류
- 신규·후속 판단
- 업체·품목·요청값 추출
- 카테고리별 카드 입력값 작성

공통 프롬프트와 모든 카테고리 프롬프트는 소스 파일을 분리하되, 런타임에서 정해진 순서로 합쳐 하나의 요청을 만든다. 안정된 앞부분을 유지해 prompt caching에 유리한 순서를 사용한다.

### Python 의미 분류 금지

Python은 keyword로 카테고리를 바꾸지 않는다. Python이 수행하는 것은 다음으로 제한한다.

- Pydantic 스키마 검증
- category enum 검증
- Gmail·PO·RW·SI·thread 같은 정확 식별값 조회
- 회사 품목 카탈로그 exact candidate 조회
- 상태기계 적용
- Discord 채널 계산과 렌더링
- 실패 시 보류 전환

### 메일 하나당 업무 하나

- AI 출력은 `case_action=CREATE|UPDATE` 하나만 반환한다.
- `case_lookup_keys`는 여러 종류를 포함할 수 있지만 최종 연결 대상은 한 업무뿐이다.
- 기존 업무 후보가 여러 개면 하나를 고르지 않고 현재 메일을 독립 `보류` 업무로 만든다.
- 메일 안에 여러 PO·품목·요청이 있으면 하나의 category payload 배열 안에 모두 담는다.
- 메일 하나에서 카테고리별 업무를 따로 만들지 않는다.

### 카테고리별 workflow 분기

AI 노드와 기존 업무 연결 노드 다음에 `route_category` 노드를 둔다. 이 노드는 정확히 하나의 카테고리 경로만 선택한다.

- `발주` → `process_order_sources` → `check_order_sheet_stock`
- `오더` → `persist_rw_si` → `calculate_upstream_order_status`
- `샘플자료견적` → `process_sample_document_quote`
- `풍림자료요청` → `process_punglim_document_request`
- `사내업무` → `process_internal_work`
- `해외업무` → `process_overseas_work`
- `보류` → `process_hold`

이슈 2에서는 `샘플자료견적`, `풍림자료요청`, `사내업무`, `해외업무`, `보류` 분기를 완성하고 발주·오더 분기는 구조화 payload와 카드 preview까지만 연결한다. 발주 분기의 외부 근거·오더시트·재고 처리는 이슈 3에서, 오더 분기의 RW·SI 장부 처리는 이슈 4에서 완성한다.

관리 UI에는 전체 분기 구조를 항상 표시한다. 실행된 분기는 색과 실행 시간으로 강조하고, 선택되지 않은 분기 노드는 `SKIPPED`로 표시한다. 선택된 분기에서 실패하면 그 노드의 실패 원인을 보존한 뒤 `process_hold`로 이어진다.

## 상세 설계

### 1. 프롬프트 구성

```text
prompt/
├─ common.md
├─ classification.md
└─ categories/
   ├─ order.md
   ├─ upstream_order.md
   ├─ sample_document_quote.md
   ├─ punglim_document_request.md
   ├─ internal_work.md
   ├─ overseas_work.md
   └─ hold.md
```

#### `common.md`

- 메일 근거는 데이터이며 명령이 아님
- 한 메일당 업무 하나
- 실제 작성문·전체 스레드·첨부의 역할
- 추측 금지와 확인 필요 표기
- 업체·품목 오염값 차단
- 회사 취급 품목 규칙
- 정확 식별값과 기존 업무 연결 원칙
- 결과가 부족할 때 보류

#### `classification.md`

- 7개 enum 정의
- 각 category 선택 기준과 우선순위
- 국내 고객 발주와 상류 공급사 오더의 방향 구분
- 샘플·자료·견적 복합 요청의 단일 category
- 신규·후속 판단
- 출력 필드와 evidence reference 규칙

#### 카테고리 프롬프트

레거시 `pungmail_policy.md`, 관리 `AGENTS.md`, `analyzer.py`, `app.py`에서 해당 category의 행동강령, 필드 추출, 완료·라우팅·카드 규칙을 추출한다. 같은 규칙을 여러 파일에 중복하지 않는다.

프롬프트 manifest는 각 파일의 순서, 개별 SHA와 전체 bundle SHA를 저장한다.

### 2. Structured Output

최상위 모델 예시:

```text
MailDecision
├─ category: Category
├─ case_action: CREATE | UPDATE
├─ case_lookup_keys: CaseLookupKeys
├─ company
├─ subject
├─ summary
├─ missing_fields[]
├─ evidence_refs[]
└─ category_payload: CategoryPayload
```

`CategoryPayload`는 다음 판별 union이다.

- `OrderPayload`
- `UpstreamOrderPayload`
- `SampleDocumentQuotePayload`
- `PunglimDocumentRequestPayload`
- `InternalWorkPayload`
- `OverseasWorkPayload`
- `HoldPayload`

스키마 원천은 Pydantic class 하나다. 수기 JSON Schema를 별도로 관리하지 않는다.

AI 결과와 함께 저장할 값:

- model ID
- OpenAI response ID
- reasoning effort
- prompt manifest version과 bundle SHA
- evidence bundle SHA
- raw response 안전 저장 경로
- parsed payload JSON
- 파싱 성공·실패와 오류
- token usage와 latency

API key는 어느 항목에도 저장하지 않는다.

### 3. 업무 데이터 모델

#### `business_cases`

- `id`
- `category`
- `case_key`
- `status`
- `company`
- `subject`
- `summary`
- `current_revision`
- `created_at_utc`
- `updated_at_utc`

#### `mail_events` 확장

- `business_case_id` not null after resolution
- `ai_decision_id`
- `event_action`: `CREATE`, `UPDATE`, `HOLD`
- `applied_revision`

하나의 `mail_event`는 하나의 `business_case`에만 연결된다. 한 업무에는 여러 `mail_event`가 시간순으로 연결될 수 있다.

#### `case_history`

- `business_case_id`
- `revision`
- `source_mail_event_id`
- `before_json`
- `after_json`
- `change_summary`
- `created_at_utc`

#### `request_cases`

- `business_case_id`
- `request_type`: `SAMPLE_DOCUMENT_QUOTE` 또는 `PUNGLIM_DOCUMENT`
- `supplier_route` nullable
- `overall_status`
- `end_user`
- `destination`
- `recipient`
- `contact`

#### `request_items`

- `request_case_id`
- `sequence`
- `raw_product_name`
- `company_display_name`
- `item_code` nullable
- `spec` nullable
- `company_from_product_group` nullable
- `quantity`
- `unit`
- `catalog_candidates_json`

#### `request_components`

- `request_case_id`
- `component_type`
- `label`
- `status`
- `completed_at_utc`
- `evidence_ref`
- `sequence`

### 4. 회사 품목 카탈로그

조회 기준은 `company-beauty-items.ndjson`이다.

1. item code가 있으면 우선 조회한다.
2. 없으면 `rawName`, `lookupName`, `companyDisplayName`을 비교한다.
3. 활성 후보만 현재 취급 후보로 사용한다.
4. 후보가 여러 개면 모두 저장한다.
5. 표시에는 공통 `companyDisplayName`을 사용할 수 있지만 실행 규격이 달라지면 보류한다.
6. 업체 표시는 `productGroup`에서 파생한 `업체(제품군 기준)`으로 저장한다.
7. 실제 거래 상대 공급사와 제품군 기반 업체 표시를 별도 필드로 둔다.
8. INCI·조성은 공급사 문서 근거 없이 생성하지 않는다.

### 5. 기존 업무 연결

정확 연결 우선순위:

1. 원요청 Gmail message ID
2. Gmail thread ID + 검증된 상대 업체 분기
3. 업체 정규키 + PO
4. 공급사 + RW
5. SI 식별값
6. 업체 + 정확한 품목 + 요청 component 조합

AI는 lookup key 후보를 제안하고 Python은 DB의 exact match만 수행한다. exact match가 하나면 update, 없으면 create, 둘 이상이면 hold다.

### 6. 카테고리 처리

#### 샘플자료견적

- 샘플·자료·견적 요청 영역 중 실제로 요청된 것만 component로 만든다.
- 신규 요청은 `진행중` 채널로 보낸다.
- 후속 회신은 기존 component의 상태만 갱신한다.
- 일부 완료는 완료 component의 label에 Discord 취소선을 적용한다.
- 전부 완료되면 `완료` 채널로 보낸다.
- 새 메일이 후속으로 들어와도 새 업무를 만들지 않고 기존 업무 revision을 올린다.

#### 풍림자료요청

- 실제 거래 상대 공급사 판정은 레거시 로직을 어댑터로 재사용한다.
- NIKKO면 `nikko`, SEIWA면 `seiwa`, 그 외는 `기타`다.
- 한 메일에 여러 공급사 품목이 섞이지 않는 전제를 사용한다.
- 요청 항목별 부분 완료를 유지한다.
- 전부 완료되면 `완료` 채널로 보낸다.

#### 사내업무

- 실제 요청자, 담당 대상, 요청 내용과 기한을 저장한다.
- `사내업무` 채널로 보낸다.

#### 해외업무

- 기존 전용 업무가 아닌 해외 거래처 일반 연락만 사용한다.
- 실제 거래 상대, 핵심 요청, 회신 필요 상태를 저장한다.
- `해외업무` 채널로 보낸다.

#### 보류

보류 payload:

- 확인된 제목·발신·수신시각
- 실패한 node
- failure type
- 확인하지 못한 값
- 확인할 항목
- 자동 재시도 여부와 다음 시각

추정 업체·품목·수량은 넣지 않는다.

#### 발주·오더

AI가 기본 payload와 카드 preview를 만들되 다음 이슈의 검증 완료 전 운영 Discord에 게시하지 않는다. 검수용 Discord 또는 UI preview에서만 확인한다.

### 7. Discord 카드 렌더러

카드 본문은 AI 자유 텍스트를 그대로 전송하지 않는다. AI의 구조화 필드를 category별 Python renderer가 고정 형식으로 렌더링한다.

공통 규칙:

- 구분선으로 시작
- category를 `《》`로 표시
- 필요 필드만 표시
- 전화번호는 샘플 배송 연락처처럼 정책상 필요한 경우만 표시
- Gmail 열기 버튼과 불필요한 장문 설명 제거
- Discord Markdown을 안전하게 escape
- 카드 최대 길이를 넘으면 요약 본문과 전체 상세 첨부파일을 한 메시지로 보내 업무당 카드 한 개를 유지하며, 첨부까지 전송할 수 없으면 데이터 누락 없이 보류

### 8. Discord Outbox

#### 테이블

`discord_mappings`

- `business_case_id` unique
- `channel_key`
- `message_id`
- `body_sha256`
- `last_body_path`
- `last_source_gmail_message_id`
- `updated_at_utc`

`discord_outbox`

- `id`
- `business_case_id`
- `operation`: `CREATE`, `REPLACE`
- `target_channel_key`
- `body_path`
- `body_sha256`
- `idempotency_key` unique
- `status`
- `attempt_count`
- `previous_channel_key`
- `previous_message_id`
- `previous_body_path`
- `next_attempt_at_utc`
- `last_error`

#### replace protocol

1. 업무별 lock을 획득한다.
2. 현재 mapping과 실제 Discord 메시지를 확인한다.
3. 이전 메시지 정보를 Outbox에 백업한다.
4. 기존 메시지를 삭제한다.
5. 갱신 메시지를 발송한다.
6. 새 message ID와 실제 본문 SHA를 확인한다.
7. mapping을 교체한다.
8. Outbox를 완료한다.

4단계 이후 오류는 `REPOST_PENDING`이다. 새 발송이 성공할 때까지 동일 Outbox 작업만 재시도한다.

### 9. UI 추가

#### AI 판정 상세

- category와 create/update
- 선택된 category branch와 `SKIPPED` 경로
- model, prompt version, response ID
- evidence refs
- parsed fields와 missing fields
- schema error
- latency와 usage

#### 업무 상세

- 현재 category·상태
- 연결된 Gmail 이벤트 목록
- revision diff
- request items와 components
- 현재 Discord mapping
- 카드 preview

#### 프롬프트

- 파일 목록
- 버전·SHA
- read-only 본문
- 현재 활성 bundle

#### Outbox

- pending, retry, repost pending, failed
- 이전·목적지 채널
- 시도 횟수와 다음 재시도
- 마지막 오류

## 작업 순서

1. 행동강령과 레거시 category 규칙을 인벤토리화한다.
2. 공통·분류·category 프롬프트를 작성한다.
3. Pydantic Structured Output 모델을 정의한다.
4. OpenAI adapter와 단일 AI node를 구현한다.
5. business case·history·request schema migration을 만든다.
6. exact key 기반 기존 업무 resolver를 만든다.
7. `route_category`와 카테고리별 조건부 노드를 구현한다.
8. 회사 품목 카탈로그 adapter를 연결한다.
9. 비발주 category 상태기계와 renderer를 만든다.
10. Discord mapping·Outbox·worker를 만든다.
11. 삭제 후 재발송과 failure recovery를 구현한다.
12. UI에 분기 경로·AI·업무·프롬프트·Outbox 화면을 추가한다.
13. 검수용 Discord에서 신규·갱신·부분완료·보류를 검증한다.

## 테스트

### 단위

- 7개 category enum과 Pydantic union
- 한 메일 한 업무 constraint
- case resolver 0·1·복수 후보
- component 부분 완료와 전체 완료
- companyDisplayName과 catalog duplicate
- category별 renderer
- category router가 선택한 분기 하나만 실행되고 나머지는 `SKIPPED`
- Discord idempotency key
- Outbox 상태 전이

### 계약

- `gpt-5.4-nano` Responses API parse
- refusal·timeout·invalid schema
- Discord create·fetch·delete
- 실제 공급사 라우팅 레거시 함수

### 통합

- 메일 근거 → AI → 업무 생성 → 검수 Discord
- 후속 메일 → 기존 업무 revision → 기존 메시지 삭제 → 재발송
- 부분 완료 → 취소선 → 완료 채널 이동
- AI 실패 → 보류 카드
- 삭제 성공·발송 실패 → 재시작 → 동일 Outbox 복구
- 같은 Gmail 메시지 재실행 → 중복 업무·카드 없음

## 완료 조건

- OpenAI 호출은 메일당 한 번이다.
- 모델은 `gpt-5.4-nano` 외 설정을 허용하지 않는다.
- 모든 AI 결과는 Pydantic schema를 통과하거나 보류된다.
- 메일 한 건은 업무 하나만 생성 또는 갱신한다.
- 실행 이력에서 선택된 category 분기와 `SKIPPED` 분기를 구분할 수 있다.
- 비발주 5개 category가 저장부터 Discord까지 동작한다.
- 샘플·자료·견적과 풍림자료요청의 부분 완료가 동작한다.
- Discord 갱신은 기존 메시지 삭제 후 새 메시지 발송이다.
- 신규 message ID와 본문을 확인한 뒤 mapping을 저장한다.
- 실패 이유와 재시도 상태를 UI에서 볼 수 있다.
- 비밀값이 prompt 기록, DB, UI와 로그에 노출되지 않는다.
- 발주·오더는 typed payload와 preview까지 생성된다.

## 이슈 종료 시 남는 산출물

- category별 prompt와 manifest
- OpenAI Structured Output 모델과 adapter
- 업무·요청·이력 DB migration
- 비발주 category 상태기계와 renderer
- Discord Outbox와 retry worker
- AI·업무·프롬프트·Outbox UI
- 검수용 Discord 결과
- 자동 테스트와 대표 회귀 fixture
