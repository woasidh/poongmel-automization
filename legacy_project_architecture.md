# 풍멜이 프로젝트 전체 흐름

- 작성 기준일: 2026-07-28
- 관리 지침: `C:\Users\ASUS\Documents\풍멜이 관리`
- 실제 운영 코드: `C:\Users\ASUS\Documents\New project`
- 발주사이트 자동화: `C:\Users\ASUS\Documents\Pungmail Order Sites`

## 핵심 구조

이 프로젝트는 다음 순서로 작동한다.

`Gmail 증분 수집 → 첨부 추출 → Python 규칙 분류 → 3회 검증 → 사이트·오더시트·재고 보강 → 카드 초안 → LLM 최종검토 → Python 재검증 → Discord 반영 → SQLite 저장`

LLM이 처음부터 메일을 전부 분류하는 구조는 아니다. Python 규칙 엔진이 먼저 분석하고, 외부 근거까지 보강한 결과를 OpenAI API LLM이 Discord 전송 직전에 최종검토한다.

## 전체 처리 흐름

1. 프로젝트 실행
   1. `python -m gmail_monitor`로 실행한다.
   2. `gmail_monitor/__main__.py`에서 `gmail_monitor/app.py`의 `main()`을 호출한다.
   3. 기본적으로 1분마다 전체 처리 사이클을 반복한다.
   4. 현재 설정은 `CHECK_INTERVAL_MINUTES=1`이다.

2. 신규 메일 확인
   1. 담당 파일은 `gmail_monitor/gmail_client.py`다.
   2. Gmail API OAuth를 사용한다.
   3. 매번 전체 메일함을 검색하지 않고 Gmail `historyId`로 새 메시지를 증분 수집한다.
   4. 보낸사람·받는사람·참조에 Richwood가 포함된 업무 메일을 선별한다.
   5. 스팸·휴지통·프로모션·소셜 메일은 제외한다.

3. 메일 처리 대기열 저장
   1. 담당 파일은 `gmail_monitor/storage.py`다.
   2. 신규 Gmail 메시지를 SQLite 대기열에 먼저 저장한다.
   3. 실패한 메일은 실패 횟수와 다음 재시도 시간을 기록한다.
   4. 한 메일이 실패해도 뒤의 다른 메일은 계속 처리한다.
   5. 운영 데이터베이스는 `C:\Users\ASUS\Documents\New project\gmail_monitor.db`다.

4. 메일 내용과 첨부파일 추출
   1. 담당 파일은 `gmail_monitor/gmail_client.py`다.
   2. 제목·발신자·수신자·참조·본문·전체 Gmail 스레드를 수집한다.
   3. PDF, Excel, DOCX, ZIP, 이미지 첨부를 판독한다.
   4. PDF 텍스트가 불완전하면 OCR을 실행한다.
   5. Richwood 대용량 첨부 링크도 별도로 내려받아 검증한다.

5. 메일 1차 분류 및 업무값 추출
   1. 담당 파일은 `gmail_monitor/analyzer.py`다.
   2. 이 단계는 LLM이 아니라 Python 정규식·키워드·업체별 하드코딩 규칙을 사용한다.
   3. 발주, 납기변경, 자료, 샘플, 견적, SI, RW, 사내업무 등을 판별한다.
   4. 업체·품목·수량·납기·PO·SI·RW 등을 `Analysis` 객체로 만든다.
   5. `analyzer.py`는 약 532KB인 대형 규칙 엔진이다.

6. 메일 3회 분석 및 교차검증
   1. 주 담당은 `gmail_monitor/app.py`의 `_analyze_thread_three_pass()`다.
   2. 1차로 인용문을 포함한 전체 스레드를 분석한다.
   3. 2차로 각 메시지의 실제 새 작성문과 첨부 근거를 분석한다.
   4. 3차로 발주서·원요청 등 업무 기준 원문을 다시 분석한다.
   5. 세 결과가 충돌하거나 업체·품목에 오염값이 남으면 게시를 보류한다.
   6. 한 메일에 여러 PO가 있으면 PO별 독립 업무로 분리한다.

7. ECOUNT 발주서 확인
   1. 담당 파일은 `gmail_monitor/ecount_reader.py`다.
   2. ECOUNT 링크에서 실제 발주서 내용을 읽는다.
   3. 업체·품목·수량·납기·납품처를 구조화한다.
   4. ECOUNT 화면이나 문서의 메뉴명·버튼명·표 머리글은 품목에서 제외한다.

8. 고객사 발주사이트 확인
   1. 본체 연결 파일은 `gmail_monitor/site_order_client.py`다.
   2. 실제 브라우저 자동화 코드는 `C:\Users\ASUS\Documents\Pungmail Order Sites`에 별도 프로젝트로 존재한다.
   3. 콜마, 코스맥스, LG, 아모레, 코리아나, 제뉴원, GC녹십자, 코스모코스 등의 발주사이트를 조회한다.
   4. PO·사업장·품목·수량·납기를 읽기 전용으로 검증한다.
   5. 사이트 확인이 필요한 발주는 검증 전 Discord에 게시하지 않는다.

9. 오더시트 확인
   1. 담당 파일은 `gmail_monitor/order_sheet_reader.py`다.
   2. 발주 납기일에 해당하는 월별 시트를 조회한다.
   3. 업체·품목·수량·납기·납품처·PO·ERP 체크를 대조한다.
   4. 입력됨·미입력·일부입력·변경 미반영·납기 불일치 등을 판정한다.
   5. OAuth가 실패하면 검증된 공개 XLSX 내보내기 또는 제한된 스냅샷을 사용한다.

10. 재고와 수입 진행 확인
    1. 담당 파일은 `gmail_monitor/stock_reader.py`다.
    2. 최신 재고표를 읽는다.
    3. 최신 재고에서 미래 미출고 예약량을 빼 가용재고를 계산한다.
    4. 부족한 품목은 RW·SI·ETA와 연결해 `발주-수입중` 상태로 추적한다.
    5. SI 도착 후 재고표 증가량으로 실제 국내 창고 입고 여부를 확인한다.

11. 회사 취급 품목명 확인
    1. 회사 품목 카탈로그를 이용해 품목명을 정규화한다.
    2. 선두의 독립된 `NIKKOL`·`PROMOIS` 접두어를 회사 표시에서 제거한다.
    3. 같은 이름이 여러 품목코드·규격으로 존재하면 임의로 하나를 선택하지 않는다.
    4. 상품명과 INCI·일반 원료명을 같은 것으로 단정하지 않는다.

12. Discord 카드 초안 생성
    1. 대부분의 보강·라우팅·렌더링 로직은 `gmail_monitor/app.py`에 있다.
    2. 분석 결과에 따라 카드 제목·본문·목적지 채널을 계산한다.
    3. Gmail thread, 업체+PO, SI, RW 등의 키로 기존 카드를 검색한다.
    4. 신규 카드 생성인지 기존 카드 수정인지 결정한다.
    5. `app.py`는 약 706KB이며 오케스트레이션·보강·렌더링·라우팅 로직이 한 파일에 집중되어 있다.

13. OpenAI API LLM 최종검토
    1. `gmail_monitor/final_review.py`가 검토 작업과 근거 파일을 대기열에 저장한다.
    2. `gmail_monitor/openai_review_worker.py`가 OpenAI API를 호출한다.
    3. 현재 `FINAL_REVIEW_ENABLED=true`다.
    4. 현재 허용 모델은 `gpt-5.4-nano`다.
    5. LLM은 `approve`, `hold`, `ignore` 중 하나를 반환한다.
    6. 분류·업체·품목·수량·납기·완료상태를 전체 근거와 행동강령에 따라 독립 재검토한다.
    7. LLM은 Discord나 SQLite를 직접 수정하지 않는다.

14. Python 최종 안전검증
    1. LLM 결과를 그대로 전송하지 않는다.
    2. Python이 정정된 분석값으로 채널과 카드 본문을 다시 계산한다.
    3. 행동강령·payload·근거 파일의 SHA-256을 확인한다.
    4. 기존 Discord 본문과 message ID를 전송 직전에 다시 확인한다.
    5. 중복 작업·오래된 작업·분류와 채널 불일치를 차단한다.
    6. 승인 결과와 Python 재계산 결과가 한 글자라도 다르면 전송하지 않는다.

15. Discord 생성·수정·삭제
    1. 담당 파일은 `gmail_monitor/discord_client.py`다.
    2. 신규 업무면 카드를 생성한다.
    3. 동일 업무면 새 카드를 만들지 않고 기존 카드를 수정한다.
    4. 채널 이동이 필요하면 목적지 카드 생성·반영을 확인한 뒤 기존 카드를 삭제한다.
    5. Discord 반영 후 SQLite에 channel key와 message ID를 저장한다.
    6. 신규 Discord 생성 후 DB 저장이 실패하면 생성한 메시지를 즉시 삭제해 롤백한다.

16. SQLite 상태 및 중복 관리
    1. 담당 파일은 `gmail_monitor/storage.py`다.
    2. 처리 완료 Gmail 메시지와 대기 메시지를 관리한다.
    3. Discord 카드의 message ID와 channel key를 저장한다.
    4. 발주·오더시트 스냅샷·사이트 검증·첨부 양식 프로필을 저장한다.
    5. RW·SI·수입 진행·재고 입고 상태를 장부 형태로 저장한다.
    6. 같은 업무의 중복 카드 생성을 막는다.

17. 주기적인 기존 카드 갱신
    1. 매 사이클마다 활성 발주 카드와 오더시트를 다시 비교한다.
    2. ERP 체크·납기변경·취소·분할납품 상태를 갱신한다.
    3. 활성 SI와 최신 재고표를 비교해 국내 창고 입고 여부를 확인한다.
    4. 상태만 바뀐 경우 새 카드를 만들지 않고 같은 Discord 메시지를 수정한다.

18. 보조 운영 도구
    1. `gmail_monitor/rebuild.py`: Gmail·Discord 전체 재구축용 스테이징 도구
    2. `gmail_monitor/policy_publisher.py`: 행동강령 Discord 게시 도구
    3. `gmail_monitor/order_sheet_publisher.py`: 오더시트 기반 카드 게시 도구
    4. `gmail_monitor/final_review_worker.py`: 파일 대기열 기반 최종검토 관련 도구
    5. `scripts/restart_gmail_monitor.ps1`: 모니터 재시작 스크립트

19. 정책과 테스트
    1. 최신 행동강령은 `C:\Users\ASUS\Documents\New project\docs\pungmail_policy.md`다.
    2. 변경기록은 `C:\Users\ASUS\Documents\New project\docs\changelog.md`다.
    3. OpenAI 최종검토 절차는 `C:\Users\ASUS\Documents\New project\docs\codex_final_review_automation.md`다.
    4. 회귀 테스트는 `C:\Users\ASUS\Documents\New project\tests`에 있다.
    5. 현재 `test_*.py` 형태의 테스트 파일은 39개다.

## 디렉터리 역할

1. `C:\Users\ASUS\Documents\풍멜이 관리`
   1. 프로젝트 운영 지침을 두는 관리 루트다.
   2. 실제 운영 코드는 이 폴더에 없다.

2. `C:\Users\ASUS\Documents\New project`
   1. Gmail 모니터 본체가 있는 실제 운영 코드 루트다.
   2. SQLite DB, 환경설정, 로그, 런타임 큐, 문서와 테스트가 같이 있다.

3. `C:\Users\ASUS\Documents\Pungmail Order Sites`
   1. 고객사 발주사이트 조회를 담당하는 별도 Python 프로젝트다.
   2. Playwright 브라우저 자동화와 사이트별 파서가 있다.

## 한 줄 평가

현재 구조는 **Python 규칙 기반 거대 모놀리스 + 외부 운영자료 검증 + OpenAI LLM 승인 게이트 + 별도 사이트 자동화 프로젝트**다.
