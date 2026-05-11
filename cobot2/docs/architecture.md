# cobot2 web — 아키텍처 요약

음성 명령 → 로봇 시퀀스 변환 + Rive UI 브리지.
**`backend/main.py` (FastAPI + ROS 브리지)** 가 부모 프로세스, **`backend/wakeup_worker.py`** 가 spawn 자식 프로세스.

```
[마이크] → wakeup_worker (자식 프로세스)
              │
              ├─ openwakeword(onnx)  ──▶ 호출어 감지
              ├─ PyAudio 녹음(≥5s, 무음 1s 종료, ≤10s)
              ├─ Whisper STT (ko 고정)
              └─ GPT-4o command parser  ──▶ sequence + reply JSON
              │
              ▼  ROS 토픽 publish
              │
[main.py FastAPI/ROS Node]
              │  ROS 토픽 subscribe + WS broadcast
              ▼
        [frontend/index.html]  ─ Rive 상태 머신 + TTS 재생
```

## 프로세스 구조

| 프로세스 | 역할 | 진입점 |
|---|---|---|
| **main (부모)** | FastAPI 서버 (`:8001`), `/ws/client` WebSocket, `/tts` 엔드포인트, ROS 구독 노드 `ui_bridge` | `backend/main.py` |
| **wakeup_worker (자식)** | 마이크 캡처 · 호출어 감지 · 녹음 · STT · LLM 정제 · ROS 발행 | `backend/wakeup_worker.py` |

- 자식은 `multiprocessing.get_context('spawn').Process` 로 생성. `fork` 대신 `spawn` 사용 이유: `rclpy.init()` 이중 호출/마이크 핸들 상속 회피.
- 부모 종료 시 `finally` 블록이 자식 `terminate()` 호출.

## ROS 토픽

| 방향 | 토픽 | 페이로드 | 생성 | 소비 |
|---|---|---|---|---|
| pub | `/wakeup_status` | `{detected, confidence, wake_word}` JSON | worker | main (→ WS) |
| pub | `/stt_result` | `{stt, sequence, reply}` JSON | worker | main (→ WS) |
| pub | `/wakeup_debug` | 디버그 raw 문자열 | worker | main (→ WS) |
| pub | `/wakeup_progress` | `{stage, ts, ...}` 단계 이벤트 | worker | main (→ WS) |
| pub | `/voice_command` | `sequence` (JSON 배열) | worker | **state_manager** (외부) |
| pub | `/voice_reply` | reply 문자열 | worker | voice_client (외부) |

`/voice_command`·`/voice_reply` 는 `voice_processing/voice_to_command.py` 와 동일한 포맷·QoS — state_manager 가 어느 노드가 publish 했는지 신경 안 씀.

## wakeup_worker 처리 흐름

1. `openwakeword(inference_framework="onnx")` 로 호출어 `wassup_homie.onnx` 감지 (threshold 0.3)
2. 감지 직후 PyAudio 스트림 → **최소 5초, 최대 10초**, peak amplitude 기반 무음 1초 시 종료
3. WAV → OpenAI Whisper (`language="ko"`) STT
4. STT 텍스트 → GPT-4o command parser (프롬프트는 `PROMPT_CONTENT` 상수, 액션 카탈로그 + 객체/위치 매핑)
5. 결과 `{stt, sequence, reply}` 를 `/stt_result` 에 publish, `sequence`만 분리해 `/voice_command`, `reply`만 분리해 `/voice_reply` 에 publish
6. 응답 후 마이크 버퍼 drain + `model.reset()` — API 호출 중 입력으로 인한 재트리거 방지

## 프론트엔드

| 파일 | 역할 |
|---|---|
| `frontend/index.html` | Rive (webgl2 2.37.6) 단일 페이지. WebSocket 으로 main 의 상태 수신, TTS 오디오 재생 |
| `frontend/riv_ai_button.riv` | 상태 머신: `wait_face` ↔ `smile_face` 전환은 Click Trigger 로 토글 |

- 캔버스에 `pointer-events: none` — 사용자 클릭이 Rive 상태를 흔들지 않음
- 브라우저 autoplay 정책 우회: document-level gesture listener 가 첫 클릭/키 입력 시 `<audio>` unlock
- `Fit.Cover` 로 뷰포트 전체. 배경 `#161616` 고정 (상태별 배경 변화 없음)

## TTS

- 엔드포인트: `GET /tts?text=...` → `gpt-4o-mini-tts` 음성 바이트 반환
- 프론트는 `/stt_result` 수신 시 `reply` 를 fetch → blob URL 로 `<audio>` 재생

## 외부 의존성 / 환경

| 변수 | 용도 |
|---|---|
| `OPENAI_API_KEY` | Whisper, GPT-4o, TTS 호출 — `~/.bashrc` 에 export |
| `ROS_DOMAIN_ID` / `RMW_IMPLEMENTATION` | state_manager (외부 PC) 와 일치해야 `/voice_command` 가 도달 |
| `MIC_DEVICE_INDEX` (선택) | PyAudio 디바이스 강제 지정 |
| `LLM_MODEL` (선택) | command parser 모델 override (기본 `gpt-4o`) |

## 실행

```bash
myros2-cobot2_client-restart   # 부모+자식 kill → 포트 8001 검증 → cloudflared 보장 → 재기동
```

- 로컬: `http://localhost:8001/`
- 공개: `https://cobot2.thatshoon.com/` (cloudflared `robo-chef` 터널의 `cobot2` 인그레스)

## 핵심 설계 결정

- **spawn 프로세스 분리**: rclpy + PyAudio + openwakeword 의 상태가 부모 FastAPI 의 asyncio loop 와 섞이지 않도록 격리. 마이크 핸들 누수도 차단.
- **동일 prompt/토픽 포맷**: 기존 `voice_processing` 노드와 인터페이스 호환 → state_manager 코드 무수정.
- **무음 종료 + 재트리거 방지**: 자연스러운 발화 길이 허용하면서 호출어 모델이 자기 음성에 반응하는 루프를 끊음.
- **Rive Click Trigger 동기화**: 상태 머신이 트리거 기반이라 부모(JS) 가 명시적 토글로 face 전환, 사용자 입력은 차단해 desync 회피.
