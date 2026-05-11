# Wake-Word Worker — Design Spec

- **Date:** 2026-05-10
- **Owner:** hoon
- **Target backend:** `client_ui/backend/main.py`
- **Status:** Approved (awaiting implementation plan)

## Goal

Add an always-on wake-word listener as a separate OS process spawned from the FastAPI backend (`client_ui/backend/main.py`). On detection of `wassup_homie` (openwakeword ONNX), record the next 5 seconds of microphone audio, transcribe it with OpenAI Whisper (forced Korean), refine the transcription through a GPT-4o command-parser prompt, and emit the structured result to:

1. The existing WebSocket clients of the backend.
2. The existing ROS2 `/stt_result` topic (payload is a JSON String).

The refined payload shape is `{"transcription": str, "sequence": [...], "reply": str}` — `transcription` is the raw Whisper output, `sequence` is the action plan for the robot controller, and `reply` is the natural-language Korean response shown above the character.

## Non-Goals

- Watchdog / auto-restart of the worker on crash (deferred to v2).
- Replacing the existing `voice_processing` ROS2 service-trigger flow. The new worker runs *in addition* to it; both publish to `/stt_result` independently.
- Multi-language wake-word ensembles, VAD-based dynamic recording length, or speaker identification.

## File Layout

```
client_ui/
├── backend/
│   ├── main.py                    # MODIFY — spawn/teardown wakeup worker process
│   ├── wakeup_worker.py           # NEW — wake-word + 5s record + Whisper STT loop
│   └── resource/                  # NEW
│       ├── wassup_homie.onnx
│       └── wassup_homie.onnx.data
└── frontend/
    ├── index.html                 # REWRITE — minimal Rive character UI
    └── riv_ai_button.riv          # NEW (copy from /home/hoon/Downloads/)
```

The two `wassup_homie.onnx*` files are downloaded verbatim from
`https://raw.githubusercontent.com/dev-kibeom/cobot2/feature/voice/src/cobot2/voice_processing/resource/`.
The ONNX file references its sidecar `.data` blob — both must live in the same directory.

## Process Topology

```
[uvicorn process — main.py]                 [wakeup worker — wakeup_worker.py]
  ├─ FastAPI app                              ├─ rclpy node "wakeup_worker_node"
  ├─ ws_manager (WebSocket fan-out)           ├─ pyaudio stream (48kHz, mono, paInt16)
  ├─ ClientBridgeNode (rclpy)                 ├─ openwakeword Model(wassup_homie.onnx)
  │   └─ subscribes:                          └─ loop:
  │      /wakeup_status ──┐                       1. read chunk → resample 16k → predict
  │      /stt_result ─────┤                       2. confidence > WAKEUP_THRESHOLD?
  │                       │                          ├─ publish /wakeup_status
  │  (broadcast to WS) ◄──┘                          ├─ record RECORD_SECONDS of audio
  └─ multiprocessing.Process(daemon=True)             ├─ wav buffer → Whisper API
                                                      └─ publish /stt_result
                                                   3. loop
```

**IPC:** ROS2 topics only. Worker publishes `/wakeup_status` and `/stt_result`; `ClientBridgeNode` already subscribes to both and broadcasts to WebSocket. No `multiprocessing.Queue` is needed — the existing topic bridge satisfies "WS broadcast + ROS2 publish" simultaneously.

## Worker (`wakeup_worker.py`) — Behavior

### Entry points

- `def run_worker() -> None` — used by `main.py` as the `target=` of `multiprocessing.Process`.
- `if __name__ == "__main__": run_worker()` — allows standalone testing via `python wakeup_worker.py` without launching the FastAPI backend.

### Initialization sequence

1. Read environment (no `.env` file required — `OPENAI_API_KEY` lives in `~/.bashrc`):
   - `OPENAI_API_KEY` — required for Whisper. If absent, log a warning and proceed with STT disabled (wake events still publish; transcription step is skipped).
   - `WAKEUP_THRESHOLD` (default `0.5`).
   - `RECORD_SECONDS` (default `5`).
   - `MIC_DEVICE_INDEX` (default unset → PyAudio default).
   - `LLM_MODEL` (default `gpt-4o`).
2. `rclpy.init()` → create `Node("wakeup_worker_node")`.
3. Create publishers (QoS depth 10, RELIABLE — match `ClientBridgeNode`):
   - `/wakeup_status` — `std_msgs/String`, JSON payload.
   - `/stt_result` — `std_msgs/String`, plain text payload.
4. Resolve resource path: `<wakeup_worker.py dir>/resource/wassup_homie.onnx`. Fail fast if missing.
5. Open `pyaudio.PyAudio()` and a `paInt16` mono stream at **48000 Hz**, `frames_per_buffer=12000`. Matches the existing `MicConfig` in `voice_processing/MicController.py`.
6. Build `openwakeword.model.Model(wakeword_models=[<onnx path>])`.

If step 5 fails (mic missing), log and sleep 5 s, then retry indefinitely. Other init failures are fatal — process exits and parent logs the death.

### Detection loop

```
while rclpy.ok():
    chunk = stream.read(12000, exception_on_overflow=False)        # ~0.25s @ 48kHz
    samples_16k = scipy.signal.resample(np.frombuffer(chunk, np.int16),
                                        int(len(chunk_int16) * 16000 / 48000))
    confidence = model.predict(samples_16k, threshold=0.1)["wassup_homie"]
    if confidence > WAKEUP_THRESHOLD:
        publish_wakeup_status(confidence)                          # 1
        wav_bytes = record_wav(stream, RECORD_SECONDS)             # 2
        text = transcribe_whisper(wav_bytes)                       # 3 (None if disabled/failed)
        if text:
            publish_stt_result(text)                               # 4
```

Notes:

- **Re-trigger guarding** — during step 2, the stream is being read for recording, so the detection loop does not see new chunks until it returns. Implicit guard, no extra flag needed.
- **`record_wav`** consumes `RECORD_SECONDS * 48000 / 12000` ≈ 20 chunks at 5 s, accumulates into `bytes`, wraps in a `wave` container at 48 kHz mono Int16 (Whisper accepts any common sample rate).
- **`transcribe_whisper`** uses the `openai` SDK: `OpenAI().audio.transcriptions.create(model="whisper-1", file=...)`. Wraps the in-memory wav as a tuple `("audio.wav", buf, "audio/wav")`. Catches all `OpenAIError` and network exceptions, returns `None` on failure with a logged warning.

### Message formats

`/wakeup_status` payload (JSON, encoded as a `String` msg.data):

```json
{
  "detected": true,
  "model": "wassup_homie",
  "confidence": 0.74,
  "ts": 1746864000.123
}
```

`/stt_result` payload — JSON-encoded String:

```json
{
  "transcription": "사과를 쓰레기통에 버려줘",
  "sequence": [
    {"step": 1, "action": "pick", "params": {"target": "apple"}},
    {"step": 2, "action": "place", "params": {"target": "쓰레기통"}},
    {"step": 3, "action": "reset", "params": {}}
  ],
  "reply": "네, 사과를 쓰레기통에 버리겠습니다."
}
```

If the GPT refinement step fails (network error, malformed JSON), the worker still publishes a fallback payload `{"transcription": <text>, "sequence": [], "reply": <text>}` so the UI can degrade gracefully to showing the raw transcription.

`ClientBridgeNode._on_stt` in `main.py` parses the JSON and emits a flattened WS frame:

```json
{
  "type": "stt_result",
  "text": "<reply (or transcription if reply missing)>",
  "transcription": "<raw whisper text>",
  "sequence": [...]
}
```

If `msg.data` is not JSON, the bridge falls back to forwarding it verbatim as `text` (preserves backwards compatibility with bare-text producers). The frontend just reads `data.text` for display and `data.sequence` if it needs the action plan.

## Frontend (`client_ui/frontend/index.html`) — Rive Character

Replaces the existing 711-line `index.html` entirely. Only required visual elements:

1. A full-window black background with the Rive character (`riv_ai_button.riv`) centered.
2. A text overlay positioned **directly above the character** that displays the most recent Whisper transcription. Hidden when empty.

Nothing else — no headers, footers, status indicators, or styling beyond what's needed to make the two elements legible.

### `.riv` file structure (decoded from `riv_ai_button.riv`)

- **Artboard:** `Artboard`
- **State machine:** `State Machine 1`
- **Inputs:**
  - `Boolean 1` (Boolean) — drives `Sleep ↔ Awake` transition. `false` = sleeping, `true` = awake/smile.
  - `Click Trigger` (Trigger) — unused by this UI; the wake-word path drives `Boolean 1` directly.
- **Animation states:** `Sleep`, `Asleep pulse`, `Awake`, `Awake pulse`, `Timeline 2`.

### Runtime

- Use `@rive-app/webgl2` from unpkg (no build step). `Layout(Fit.Contain, Alignment.Center)`, `useDevicePixelRatio` via `resizeDrawingSurfaceToCanvas` on load + window resize.
- Initial state: `Boolean 1 = false` (Sleep).
- WebSocket client connects to `ws://<host>:8001/ws/client` and dispatches on `data.type`:
  - `wakeup` → set `Boolean 1 = true` (smile/awake), clear the text overlay.
  - `stt_result` → render `data.text` in the overlay, then schedule a return to sleep after `IDLE_RESET_MS` (default `8000` ms) — sets `Boolean 1 = false` and clears the overlay.
- The same auto-reset timer cancels and restarts if a new `wakeup` arrives mid-display.

### Why no separate "recording" indicator

The user spec says only sleep/smile + response text. The 5-second recording window is implicit between the `wakeup` and `stt_result` frames; the smile state covers it visually. Adding a third UI state ("listening") would be scope creep.

## `main.py` — Modifications (Minimal)

```python
import multiprocessing
from wakeup_worker import run_worker

def main():
    global loop
    rclpy.init()
    node = ClientBridgeNode()

    wakeup_proc = multiprocessing.Process(target=run_worker, daemon=True)
    wakeup_proc.start()

    ros_thread = threading.Thread(target=ros_spin, args=(node,), daemon=True)
    ros_thread.start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config = uvicorn.Config(app, host='0.0.0.0', port=8001, loop='asyncio', log_level='warning')
    server = uvicorn.Server(config)
    try:
        loop.run_until_complete(server.serve())
    finally:
        if wakeup_proc.is_alive():
            wakeup_proc.terminate()
            wakeup_proc.join(timeout=2)
```

`daemon=True` ensures the worker dies with the parent even on hard kill. The `terminate/join` block covers graceful shutdown (Ctrl-C, uvicorn SIGTERM).

## Configuration Surface

| Env var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | (required for STT) | Whisper auth — sourced from `~/.bashrc` already. |
| `WAKEUP_THRESHOLD` | `0.5` | Lower = more sensitive, more false positives. |
| `RECORD_SECONDS` | `5` | Length of post-wake recording. |
| `MIC_DEVICE_INDEX` | unset | PyAudio device override; unset uses system default. |

No code change required to retune; restart the backend after editing env.

## Dependencies

Already used elsewhere in the cobot2 / client_ui ecosystem:

- `openwakeword`
- `pyaudio`
- `scipy`, `numpy`
- `openai`
- `rclpy`, `std_msgs`

Possibly missing on this host:

- `onnxruntime` — required by openwakeword for `.onnx` models (the existing `.tflite` model uses `tflite_runtime` instead).

The implementation step verifies `pip list` and installs `onnxruntime` if absent.

## Error Matrix

| Failure | Worker behavior | Backend visibility |
|---|---|---|
| `OPENAI_API_KEY` unset | Warn at startup; skip transcription step. Wake events still emitted. | `/wakeup_status` arrives, `/stt_result` does not. |
| Mic device unavailable | Log + sleep 5 s + retry; never gives up. | Worker stays alive, no events until mic returns. |
| ONNX file missing | Log + exit (fatal). | Parent logs worker death; v1 does not respawn. |
| Whisper API error / timeout | Log warning, drop this utterance, return to wake-word loop. | `/wakeup_status` arrives, `/stt_result` does not for this utterance. |
| Worker process killed externally | No effect on parent. | Parent stops getting wake events; v1 does not detect/respawn. |
| 5 s window picks up partial speech | Out of scope — fixed-window v1. | Whisper transcribes whatever was captured. |

## Testing Strategy

- **Unit-ish smoke test:** run `python wakeup_worker.py` standalone and verify console logs for "wakeup detected" and STT result.
- **Integrated test:** start backend (`python -m client_ui.backend.main` or however it's currently launched), connect a WebSocket client to `ws://localhost:8001/ws/client`, speak the wake phrase, expect a `{type:"wakeup", ...}` then a `{type:"stt_result", text:"..."}` frame within ~6 s.
- **ROS2 test:** `ros2 topic echo /wakeup_status` and `/stt_result` while triggering — both must produce messages from `wakeup_worker_node`.
- **Frontend test:** open `http://localhost:8001/` in Chrome — character renders centered in Sleep state. Trigger wake-word → character switches to Awake, overlay clears. STT result → overlay shows transcribed text. After `IDLE_RESET_MS` → returns to Sleep, overlay clears.
- **Negative paths:** unset `OPENAI_API_KEY` and confirm wake events still flow; pull the mic and confirm worker logs and recovers.

## Out of Scope (Future Work)

- Watchdog/respawn loop in `main.py`.
- VAD-based dynamic recording length (instead of fixed 5 s).
- Multiple wake words / state-machine routing.
- Pushing intermediate audio levels to UI (visualizer).

## Open Questions — None

All resolved during brainstorming:
- Wake-word file: `wassup_homie.onnx` (single `m`).
- Refinement API: OpenAI Whisper STT.
- Backend target: `client_ui/backend/main.py`.
- Result transport: ROS2 publish (existing bridge auto-broadcasts to WS).
- Threshold: `0.5`. Record duration: `5 s`. Both env-overridable.
