# 이슈 1. 관찰 가능한 메일 수집 플랫폼 구축

## Goal

Windows 로컬 PC에서 차세대 풍멜이를 한 번에 실행하고, Gmail 메일이 수집·추출되는 전체 과정을 관리 UI에서 노드별로 확인할 수 있는 기반을 완성한다.

이 이슈가 끝나면 AI 분류와 Discord 발송은 아직 없어도 된다. 대신 실제 또는 지정한 Gmail 메시지가 대기열에 들어오고, 본문·스레드·첨부·OCR 근거가 저장되며, 두 워크플로의 구조와 실행 이력이 UI에 보여야 한다.

## 사용자 검수 결과물

사용자는 다음 결과를 직접 확인한다.

1. 로컬 시작 스크립트 한 번으로 Prefect, worker, 관리 UI가 실행된다.
2. 관리 UI에 `메일 처리`, `오더시트·재고 갱신` 두 워크플로만 표시된다.
3. Gmail 대상 메시지가 1분 안에 수집 목록에 나타난다.
4. 메일 상세에서 제목, 발신·수신, 전체 스레드, 본문과 첨부 목록을 볼 수 있다.
5. PDF·Excel·DOCX·ZIP·이미지의 추출 결과와 OCR 상태를 볼 수 있다.
6. 실행 상세에서 각 노드의 성공·실패·소요시간·재시도와 안전하게 가린 입출력을 볼 수 있다.
7. 서버 로그 화면에서 Gmail message ID 또는 workflow run ID로 검색할 수 있다.

## 선행 조건

- 없음
- 기존 Gmail OAuth credential과 token은 새 프로젝트에 복사하지 않고 설정된 기존 경로를 읽기 전용으로 사용할 수 있다.
- 기존 운영 프로젝트와 발주 사이트 프로젝트는 변경하지 않는다.

## 범위

### 포함

- Python 프로젝트 골격과 의존성 고정
- 로컬 설정·비밀값 로딩
- Prefect self-hosted server와 worker
- 정확히 두 개의 Prefect flow 정의
- 업무 SQLite와 Prefect SQLite 분리
- Alembic 초기 마이그레이션
- Gmail `historyId` 증분 수집
- 영속 메시지 대기열과 중복 차단
- 메일 전체 스레드와 첨부 수집
- 레거시 첨부 추출·OCR 로직의 어댑터 이관
- 근거 파일 저장과 SHA-256
- 공통 구조 로그
- 관리 UI의 대시보드·워크플로·실행·메일 근거·로그 화면
- health check와 로컬 시작·중지 스크립트

### 제외

- OpenAI API 호출
- 카테고리 분류
- 업무 카드 렌더링
- Discord 생성·삭제
- 발주 사이트·오더시트·재고 판정
- RW·SI 장부
- 과거 전체 메일 수집

## 구현 원칙

### 두 워크플로만 정의

Prefect flow 정의는 다음 두 개만 만든다.

- `mail_processing`
- `order_status_refresh`

이 이슈에서 `order_status_refresh`는 실행 가능한 빈 골격과 health 기록까지만 구현한다. 이후 이슈에서 실제 발주 갱신 노드를 채운다.

메일마다 별도의 세 번째 flow나 category flow를 만들지 않는다. 메일 한 건의 세부 상태는 앱 DB의 `mail_event`와 `node_run`으로 추적한다.

`mail_processing`의 정적 노드 그래프에는 AI 판정 뒤 `카테고리 분기`와 7개 카테고리 전용 경로를 미리 정의한다. 이후 이슈에서 실제 로직을 채우며, 선택되지 않은 분기는 실행 상세에서 `SKIPPED`로 표시한다. 카테고리별 경로는 별도 flow가 아니라 같은 flow 안의 조건부 노드다.

### 데이터베이스 분리

- `data/pungmail.db`: 업무·메일·근거·UI 데이터
- `runtime/prefect/prefect.db`: Prefect 실행 상태

Prefect 내부 테이블을 업무 코드가 직접 조회하지 않는다. Prefect API를 사용하고 앱 DB에는 화면에 필요한 안전한 실행 요약과 상관 ID만 저장한다.

### 로컬 전용

- 모든 서버는 `127.0.0.1`에만 바인딩한다.
- 로그인 기능은 만들지 않는다.
- 외부 CDN에 의존하지 않도록 UI 정적 자산을 프로젝트 안에 둔다.
- Docker는 사용하지 않는다.

## 상세 설계

### 1. 프로젝트 초기화

권장 초기 구조:

```text
src/pungmail/
├─ api/
├─ workflows/
├─ nodes/
├─ domain/
├─ repositories/
├─ adapters/
│  ├─ gmail/
│  └─ legacy/
└─ observability/
```

설정은 Pydantic Settings 또는 동등한 typed settings로 정의한다. `.env.example`에는 값이 아니라 필요한 key와 설명만 넣는다.

주요 설정:

- Gmail credential·token 경로
- 업무 DB 경로
- Prefect home과 API URL
- evidence 저장 경로
- 로그 레벨과 로그 경로
- 메일 확인 간격
- 회차당 최대 메시지 수와 실행 시간
- 첨부 개수·크기·ZIP 해제 한도
- OCR 실행파일 경로

### 2. 초기 데이터 모델

#### `workflow_runs`

- `id`
- `workflow_type`
- `trigger_type`: `SCHEDULED`, `MANUAL`, `RECOVERY`
- `prefect_flow_run_id`
- `status`
- `started_at_utc`
- `finished_at_utc`
- `summary_json`

#### `node_runs`

- `id`
- `workflow_run_id`
- `mail_event_id` nullable
- `node_key`
- `branch_key` nullable
- `attempt`
- `prefect_task_run_id`
- `status`: `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `RETRY_WAIT`, `SKIPPED`
- `input_summary_json`
- `output_summary_json`
- `error_type`
- `error_message`
- `started_at_utc`
- `finished_at_utc`

노드 입출력 요약에는 API key, token, credential, 첨부 바이너리와 서명 URL query를 저장하지 않는다.

#### `gmail_messages`

- `message_id` primary key
- `thread_id`
- `history_id`
- `internal_date_utc`
- `sender`
- `recipients_json`
- `cc_json`
- `subject`
- `labels_json`
- `body_text_path`
- `body_html_path`
- `raw_message_path`
- `content_sha256`
- `collected_at_utc`

#### `gmail_pending_messages`

- `message_id` unique
- `thread_id`
- `priority`
- `status`
- `attempt_count`
- `last_error`
- `next_attempt_at_utc`
- `queued_at_utc`

#### `mail_events`

- `id`
- `gmail_message_id` unique
- `workflow_run_id`
- `status`
- `current_node`
- `created_at_utc`
- `updated_at_utc`

메일 하나에 `mail_events` 행이 둘 이상 생기지 않게 DB unique constraint로 보장한다.

#### `mail_attachments`

- `id`
- `gmail_message_id`
- `gmail_attachment_id`
- `file_name`
- `mime_type`
- `size_bytes`
- `sha256`
- `storage_path`
- `extraction_status`
- `extracted_text_path`
- `ocr_text_path`
- `warning_json`

#### `evidence_snapshots`

- `id`
- `mail_event_id`
- `evidence_type`
- `source_key`
- `storage_path`
- `payload_json`
- `sha256`
- `created_at_utc`

### 3. Gmail 증분 수집

레거시 `gmail_client.py`의 다음 능력을 어댑터 인터페이스 뒤에서 재사용한다.

- OAuth 인증
- Gmail `historyId` 증분 조회
- message·thread 조회
- MIME 본문과 첨부 다운로드
- Richwood 대용량 첨부 다운로드

수집 순서:

1. 마지막 성공 `historyId`를 읽는다.
2. 신규 메시지 ID를 조회한다.
3. Gmail 원문을 읽기 전에 메시지 ID를 대기열에 저장한다.
4. 신규 메시지를 오래된 복구 메시지보다 우선한다.
5. 한 메시지 실패가 뒤 메시지를 막지 않게 메시지별로 처리한다.
6. 회차 처리 한도에 도달하면 남은 행은 다음 회차에 유지한다.
7. 완료된 메시지만 processed 상태로 바꾼다.

history cursor가 만료되면 레거시 복구 규칙에 따라 제한된 기간과 페이지 크기로 복구한다. 복구한 메시지도 같은 unique constraint를 통과해야 한다.

### 4. 대상 범위 표시

이 이슈에서는 최종 업무 유효성 분류를 하지 않는다. 다만 다음 hard exclusion은 수집 단계에서 기록한다.

- 스팸
- 휴지통
- 프로모션
- 소셜
- Richwood 관련 주소가 전혀 없는 메시지

제외 이유를 저장하고 UI에서 확인할 수 있게 한다. 최종적으로 `보류`로 보내야 하는 업무 유효성 판단은 이슈 2에서 AI 노드와 함께 구현한다.

### 5. 메일·첨부 근거 추출

지원 형식:

- plain text, HTML
- PDF
- XLSX, XLS
- DOCX
- 이미지
- ZIP 안의 지원 파일

추출 규칙:

- 전체 Gmail 스레드를 시간순으로 저장한다.
- 메시지별 실제 작성문과 인용문 분리 결과를 함께 저장한다.
- PDF 텍스트가 불완전하면 OCR을 수행한다.
- 이미지에는 OCR을 수행한다.
- ZIP은 개수·개별크기·전체해제크기·중첩 제한을 적용한다.
- 원본, 추출문, OCR 결과는 서로 다른 파일로 보존한다.
- 모든 파일과 정규화된 근거 묶음에 SHA-256을 기록한다.
- 실패를 `텍스트 없음` 하나로 축약하지 않고 단계와 원인을 기록한다.

### 6. 실행 추적

각 노드는 공통 wrapper를 사용한다.

공통 wrapper가 기록할 내용:

- node key와 표시명
- 시작·종료 시각
- 시도 번호
- 입력 요약
- 출력 요약
- 오류 유형과 안전한 오류 메시지
- workflow, Gmail, mail event 상관 ID

Prefect 로그와 앱 구조 로그에 같은 상관 ID를 넣는다.

### 7. 관리 UI

#### 대시보드

- 네 프로세스 health
- 다음 메일·발주 갱신 예약 시각
- 최근 24시간 실행 수
- 성공·실패·재시도 수
- 대기 중인 Gmail 메시지 수
- 최근 오류 10건

#### 워크플로 목록·상세

- 두 워크플로 이름과 스케줄
- 카테고리 라우터와 7개 분기가 포함된 정적 노드 그래프
- 현재 정의 버전과 코드 SHA
- 최근 실행 링크

#### 실행 목록·상세

- trigger, 상태, 시작·종료, 소요시간
- 노드별 상태와 재시도
- 선택된 카테고리 경로 강조와 미선택 분기 `SKIPPED` 표시
- 메일 메시지별 필터
- node input/output 요약
- 관련 로그

#### 메일 근거

- 메일 메타데이터
- 시간순 스레드
- 실제 작성문과 인용문
- 첨부 목록
- 추출문·OCR 결과
- 경고와 SHA-256

#### 로그

- 시간, 레벨, 서비스, workflow, node, Gmail ID 필터
- 비밀값 없는 상세 메시지

## 작업 순서

1. Python 패키지와 디렉터리 구조를 만든다.
2. 설정과 비밀값 로더를 만든다.
3. 업무 SQLite와 Alembic 초기 스키마를 만든다.
4. 구조 로그와 상관 ID 규칙을 만든다.
5. Prefect server·worker와 두 flow 골격을 만든다.
6. Gmail 증분 수집과 영속 대기열을 이관한다.
7. thread·attachment 수집 어댑터를 이관한다.
8. 파일 추출·OCR·evidence 저장을 이관한다.
9. workflow·node·mail 실행 기록을 연결한다.
10. 관리 UI의 대시보드·워크플로·실행·메일·로그 화면을 만든다.
11. 시작·중지·상태 확인 스크립트를 만든다.
12. 실제 또는 통제된 Gmail 메시지로 검수한다.

## 테스트

### 단위

- Gmail message ID 중복 차단
- retry 시간 계산
- 파일명과 저장경로 안전화
- ZIP bomb 한도
- 본문·첨부 해시
- 로그 비밀값 마스킹

### 계약

- 레거시 Gmail thread 반환 형식
- PDF·Excel·DOCX·이미지 추출 결과
- OAuth token 재사용
- Prefect flow/task run ID 연결

### 통합

- 신규 메일 1건 수집부터 evidence 저장까지
- 메시지 1건 실패 후 다음 메시지 계속 처리
- 재시작 후 pending 메시지 복구
- history cursor 만료 복구
- UI에서 실행과 근거 조회

## 완료 조건

- 로컬 시작 스크립트로 모든 프로세스가 실행되고 health가 정상이다.
- Prefect와 관리 UI에 정확히 두 워크플로만 보인다.
- Gmail 증분 수집이 1분 주기로 실행된다.
- 같은 Gmail 메시지의 `mail_event`가 하나만 생성된다.
- 전체 스레드와 지원 첨부가 저장된다.
- 이미지와 필요한 PDF의 OCR 결과가 저장된다.
- 모든 근거에 경로와 SHA-256이 있다.
- 실행 이력과 노드 상태가 UI에 보인다.
- 카테고리별 전체 분기 구조와 실행별 선택·건너뜀 경로가 UI에서 구분된다.
- 서버 로그를 UI에서 검색할 수 있다.
- 재시작 후 history cursor, pending queue와 실행 이력이 유지된다.
- 기존 운영 코드·DB·token 파일이 수정되지 않는다.

## 이슈 종료 시 남는 산출물

- 실행 가능한 로컬 프로젝트 골격
- 초기 Alembic 마이그레이션
- 두 Prefect flow 골격
- Gmail·추출 레거시 어댑터
- 관리 UI 1차 버전
- 시작·중지·health 스크립트
- 이 이슈 범위의 자동 테스트
- 사용자 검수용 실제 실행 기록
