# 차세대 풍멜이

이 저장소는 Gmail 업무 메일의 수집·본문/첨부 추출·OCR·노드 실행 이력을 로컬 UI에서 추적하는 차세대 풍멜이 프로젝트다.

현재 구현 범위는 `docs/issues/01-observable-mail-platform.md`다. AI 분류와 Discord 변경은 아직 수행하지 않는다.

## 최초 실행

1. PowerShell에서 `scripts/setup.ps1`을 실행한다.
2. 필요한 경우 `.env.example`을 참고해 `.env`를 작성한다.
3. `scripts/start.ps1`을 실행한다.
4. 브라우저에서 `http://127.0.0.1:8000`을 연다.

프로세스 상태는 `scripts/status.ps1`, 종료는 `scripts/stop.ps1`로 확인·수행한다.

화면 검수용 메일·첨부 근거가 필요하면 실행 중 `scripts/seed-demo.ps1`을 한 번 실행한다. 메일 근거 화면에서 `demo-mail-001`을 확인할 수 있다.

기존 `C:\Users\ASUS\Documents\New project`의 코드·DB·토큰은 수정하지 않는다. Gmail token은 읽기 전용으로 불러오며 refresh가 필요하면 메모리에서만 갱신한다.
