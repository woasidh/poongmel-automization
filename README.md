# 차세대 풍멜이

Gmail 업무 메일을 수집하고 본문·첨부·OCR 근거, AI 판정, 카테고리별 처리, 업무 revision, Discord 알림 작업을 로컬 관리 UI에서 추적하는 프로젝트입니다.

현재 `docs/issues/02-ai-category-discord.md`까지 구현되어 있습니다. Discord는 기본 `PREVIEW` 모드이므로 운영 채널에 전송하지 않습니다. 실제 네트워크 검수는 운영 채널과 분리된 테스트 webhook을 설정하고 `LIVE_TEST` 모드를 명시해야 합니다.

## 최초 실행

1. PowerShell에서 `scripts/setup.ps1`을 실행합니다.
2. 필요하면 `.env.example`을 참고해 `.env`를 작성합니다.
3. `scripts/start.ps1`을 실행합니다.
4. 브라우저에서 `http://127.0.0.1:8000`을 엽니다.

프로세스 상태는 `scripts/status.ps1`, 종료는 `scripts/stop.ps1`로 확인·실행합니다.

## 검수 데이터

- 1단계 메일 근거: `scripts/seed-demo.ps1`
- 2단계 신규→후속 갱신 업무: `scripts/seed-issue2-demo.ps1`

2단계 데이터에서는 AI 판정, 샘플자료견적 활성 분기, 업무 revision 1·2, 완료 카드, Outbox `CREATE`·`REPLACE`를 함께 확인할 수 있습니다.

기존 `C:\Users\ASUS\Documents\New project`의 코드·DB·토큰은 수정하지 않습니다. OpenAI API key는 기존 비밀 파일에서 메모리로만 읽으며 새 프로젝트 파일이나 DB에 복사하지 않습니다.
