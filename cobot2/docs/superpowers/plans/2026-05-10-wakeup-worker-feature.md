# Wake-Word Worker + Rive Character UI — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-on wake-word listener (`wassup_homie`) as a separate process spawned by the FastAPI backend; on detection, record 5 s of audio, transcribe via OpenAI Whisper, and broadcast both events to ROS2 topics + WebSocket. Replace the frontend with a minimal Rive character UI that shows Sleep ↔ Awake transitions and overlays the transcribed text.

**Architecture:** Backend uses `multiprocessing.Process` to spawn `wakeup_worker.py`, which is a self-contained rclpy node. Worker publishes `/wakeup_status` + `/stt_result` to ROS2; the existing `ClientBridgeNode` in `main.py` already subscribes to both and forwards to WebSocket clients. Frontend is a single static `index.html` with a centered Rive canvas and a text overlay; it consumes the WebSocket and drives the `Boolean 1` state-machine input.

**Tech Stack:** Python 3 (FastAPI + uvicorn + rclpy + multiprocessing), `pyaudio`, `openwakeword`, `onnxruntime`, `scipy`, `numpy`, `openai` (Whisper), Rive `@rive-app/webgl2` (CDN, no build step).

**Spec:** [`docs/superpowers/specs/2026-05-10-wakeup-worker-design.md`](../specs/2026-05-10-wakeup-worker-design.md)

**Notes for the engineer:**
- `OPENAI_API_KEY` is exported in `~/.bashrc` — `os.getenv` picks it up when the backend is launched from an interactive shell. Do not load any `.env` file.
- The project at `/home/hoon/client_ui` is currently **not** a git repo. Task 0 initializes it so the per-task commits work; if you'd rather not version the project, skip the commits and the rest of the plan still works.
- Audio capture stays at 48 kHz mono Int16 to match the existing `voice_processing/MicController.py` pattern. Resampling to 16 kHz happens in-process before openwakeword inference.
- The ONNX wake-word model has a sidecar binary (`.onnx.data`); both files must sit in the same directory.

---

### Task 0: Initialize the project for version control

**Files:**
- Create: `/home/hoon/client_ui/.gitignore`

- [ ] **Step 1: Initialize the repo and add an ignore file**

```bash
cd /home/hoon/client_ui
git init -q
```

Write `/home/hoon/client_ui/.gitignore`:

```gitignore
__pycache__/
*.pyc
*.pyo
.venv/
venv/
.env
.DS_Store
node_modules/
```

- [ ] **Step 2: Stage and commit current state as the baseline**

```bash
cd /home/hoon/client_ui
git add -A
git commit -q -m "chore: baseline commit before wakeup-worker feature"
```

If git is already initialized for some reason, stop here and just commit any uncommitted state with the same message.

---

### Task 1: Download wake-word ONNX model files into the backend

**Files:**
- Create: `client_ui/backend/resource/wassup_homie.onnx`
- Create: `client_ui/backend/resource/wassup_homie.onnx.data`

The two files come verbatim from the `dev-kibeom/cobot2` `feature/voice` branch.

- [ ] **Step 1: Create the resource directory and download both files**

```bash
mkdir -p /home/hoon/client_ui/backend/resource
cd /home/hoon/client_ui/backend/resource
curl -fsSL -o wassup_homie.onnx \
  https://raw.githubusercontent.com/dev-kibeom/cobot2/feature/voice/src/cobot2/voice_processing/resource/wassup_homie.onnx
curl -fsSL -o wassup_homie.onnx.data \
  https://raw.githubusercontent.com/dev-kibeom/cobot2/feature/voice/src/cobot2/voice_processing/resource/wassup_homie.onnx.data
```

- [ ] **Step 2: Verify file sizes match the upstream**

Run: `ls -l /home/hoon/client_ui/backend/resource/`
Expected: `wassup_homie.onnx` ≈ 13 876 bytes, `wassup_homie.onnx.data` ≈ 200 704 bytes.

- [ ] **Step 3: Commit**

```bash
cd /home/hoon/client_ui
git add backend/resource/wassup_homie.onnx backend/resource/wassup_homie.onnx.data
git commit -q -m "feat: add wassup_homie wake-word ONNX model"
```

---

### Task 2: Copy the Rive character file into the frontend

**Files:**
- Create: `client_ui/frontend/riv_ai_button.riv`

- [ ] **Step 1: Copy from Downloads**

```bash
cp /home/hoon/Downloads/riv_ai_button.riv /home/hoon/client_ui/frontend/riv_ai_button.riv
```

- [ ] **Step 2: Verify file is present and the right size**

Run: `ls -l /home/hoon/client_ui/frontend/riv_ai_button.riv`
Expected: ~3 838 bytes.

- [ ] **Step 3: Commit**

```bash
cd /home/hoon/client_ui
git add frontend/riv_ai_button.riv
git commit -q -m "feat: add riv_ai_button character animation"
```

---

### Task 3: Verify Python dependencies (install `onnxruntime` if missing)

**Files:** none

`openwakeword` lazily imports `onnxruntime` only when an `.onnx` model is loaded. The existing voice_processing code uses `.tflite` so `onnxruntime` may be absent.

- [ ] **Step 1: Check whether `onnxruntime` is importable**

Run: `python3 -c "import onnxruntime; print(onnxruntime.__version__)"`

- If it prints a version → done, skip Step 2.
- If it raises `ModuleNotFoundError` → continue to Step 2.

- [ ] **Step 2: Install `onnxruntime`**

```bash
pip install onnxruntime
```

Re-run the import check from Step 1 and confirm a version prints.

- [ ] **Step 3: Sanity-check that the other libraries already exist**

Run: `python3 -c "import pyaudio, scipy, numpy, openai, openwakeword, rclpy; print('ok')"`
Expected: `ok`. If any are missing, `pip install` them — they are all already used by `cobot2/voice_processing` so this should not fire.

No commit for this task — it only verifies the host environment.

---

### Task 4: Implement `wakeup_worker.py`

**Files:**
- Create: `client_ui/backend/wakeup_worker.py`

This is the bulk of the backend work — a single ~140-line module that runs the full wake-word → record → STT → publish pipeline.

- [ ] **Step 1: Write the complete worker module**

Create `/home/hoon/client_ui/backend/wakeup_worker.py` with the following content:

```python
"""Wake-word + 5s record + Whisper STT worker.

Runs as a separate OS process spawned by client_ui/backend/main.py via
multiprocessing.Process. Publishes detection + transcription events to ROS2
topics, which the parent ClientBridgeNode forwards to WebSocket clients.

Standalone testing: python wakeup_worker.py
"""
import io
import json
import os
import sys
import time
import wave

import numpy as np
import pyaudio
import rclpy
from openai import OpenAI
from openwakeword.model import Model
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from scipy.signal import resample
from std_msgs.msg import String

# Resource paths
RESOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resource")
ONNX_PATH = os.path.join(RESOURCE_DIR, "wassup_homie.onnx")
MODEL_KEY = "wassup_homie"

# Mic / model rates
MIC_RATE = 48000
MIC_CHUNK = 12000
MIC_CHANNELS = 1
MIC_FMT = pyaudio.paInt16
MODEL_RATE = 16000

# Inference threshold passed *into* openwakeword.predict — it filters internally;
# our explicit gate is the per-call confidence check below.
PREDICT_INTERNAL_THRESHOLD = 0.1

# Defaults overridable via env
DEFAULT_WAKE_THRESHOLD = 0.5
DEFAULT_RECORD_SECONDS = 5


def _env_float(name, default):
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_int(name, default):
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _open_mic(device_index=None):
    """Open a 48kHz mono Int16 stream. Retries forever on OSError."""
    while True:
        audio = None
        try:
            audio = pyaudio.PyAudio()
            kwargs = dict(
                format=MIC_FMT,
                channels=MIC_CHANNELS,
                rate=MIC_RATE,
                input=True,
                frames_per_buffer=MIC_CHUNK,
            )
            if device_index is not None:
                kwargs["input_device_index"] = device_index
            stream = audio.open(**kwargs)
            return audio, stream
        except OSError as e:
            print(f"[wakeup_worker] mic open failed: {e!r}; retrying in 5s", file=sys.stderr)
            try:
                if audio is not None:
                    audio.terminate()
            except Exception:
                pass
            time.sleep(5)


def _record_to_wav_bytes(stream, seconds):
    """Capture `seconds` of audio from `stream` and return an in-memory WAV blob."""
    n_chunks = max(1, int(round(MIC_RATE * seconds / MIC_CHUNK)))
    frames = []
    for _ in range(n_chunks):
        frames.append(stream.read(MIC_CHUNK, exception_on_overflow=False))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(MIC_CHANNELS)
        wf.setsampwidth(2)  # int16
        wf.setframerate(MIC_RATE)
        wf.writeframes(b"".join(frames))
    return buf.getvalue()


def _transcribe(client, wav_bytes):
    """Returns transcribed text or None on failure."""
    try:
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.wav", wav_bytes, "audio/wav"),
        )
        return resp.text
    except Exception as e:
        print(f"[wakeup_worker] whisper failed: {e!r}", file=sys.stderr)
        return None


def run_worker():
    if not os.path.isfile(ONNX_PATH):
        print(f"[wakeup_worker] FATAL: model not found at {ONNX_PATH}", file=sys.stderr)
        return

    threshold = _env_float("WAKEUP_THRESHOLD", DEFAULT_WAKE_THRESHOLD)
    record_seconds = _env_int("RECORD_SECONDS", DEFAULT_RECORD_SECONDS)
    mic_device_env = os.getenv("MIC_DEVICE_INDEX")
    device_index = int(mic_device_env) if mic_device_env else None

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[wakeup_worker] OPENAI_API_KEY unset → STT disabled", file=sys.stderr)
        client = None
    else:
        client = OpenAI(api_key=api_key)

    rclpy.init()
    node = Node("wakeup_worker_node")
    qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)
    pub_wakeup = node.create_publisher(String, "/wakeup_status", qos)
    pub_stt = node.create_publisher(String, "/stt_result", qos)
    node.get_logger().info(
        f"wakeup_worker_node initialized (threshold={threshold}, "
        f"record_seconds={record_seconds}, stt_enabled={client is not None})"
    )

    audio, stream = _open_mic(device_index=device_index)
    model = Model(wakeword_models=[ONNX_PATH])
    node.get_logger().info("openwakeword model loaded; entering detection loop")

    try:
        while rclpy.ok():
            chunk_bytes = stream.read(MIC_CHUNK, exception_on_overflow=False)
            samples = np.frombuffer(chunk_bytes, dtype=np.int16)
            samples_16k = resample(samples, int(len(samples) * MODEL_RATE / MIC_RATE))
            outputs = model.predict(samples_16k, threshold=PREDICT_INTERNAL_THRESHOLD)
            confidence = float(outputs.get(MODEL_KEY, 0.0))
            if confidence > threshold:
                node.get_logger().info(f"wakeword detected (confidence={confidence:.2f})")
                pub_wakeup.publish(String(data=json.dumps({
                    "detected": True,
                    "model": MODEL_KEY,
                    "confidence": confidence,
                    "ts": time.time(),
                })))
                wav_bytes = _record_to_wav_bytes(stream, record_seconds)
                if client is not None:
                    text = _transcribe(client, wav_bytes)
                    if text is not None:
                        node.get_logger().info(f"stt_result: {text!r}")
                        pub_stt.publish(String(data=text))
    finally:
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        try:
            audio.terminate()
        except Exception:
            pass
        try:
            node.destroy_node()
        except Exception:
            pass
        try:
            rclpy.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    run_worker()
```

- [ ] **Step 2: Syntax-check by importing**

Run: `python3 -c "import sys; sys.path.insert(0, '/home/hoon/client_ui/backend'); import wakeup_worker; print('ok')"`
Expected: `ok` (no SyntaxError, no missing-module errors).

- [ ] **Step 3: Commit**

```bash
cd /home/hoon/client_ui
git add backend/wakeup_worker.py
git commit -q -m "feat: add wakeup worker (wake-word + 5s record + Whisper STT)"
```

---

### Task 5: Smoke-test the worker standalone

This is the only realistic "does it work" check before wiring it into the FastAPI backend. The worker needs a microphone and `OPENAI_API_KEY` to fully exercise; without them it should still start, log, and fail gracefully.

- [ ] **Step 1: Run the worker for ~10 seconds**

```bash
cd /home/hoon/client_ui/backend
timeout 10 python3 wakeup_worker.py 2>&1 | tee /tmp/wakeup_worker.smoke.log
```

The `timeout 10` kills the process after 10 s — expected, since the worker is an infinite loop.

- [ ] **Step 2: Verify expected log lines appeared**

Run: `grep -E "wakeup_worker_node initialized|openwakeword model loaded|entering detection loop" /tmp/wakeup_worker.smoke.log`

Expected output: at least the "initialized" and "openwakeword model loaded" lines. If `OPENAI_API_KEY` was unset, you should also see `OPENAI_API_KEY unset → STT disabled`.

If the mic was unavailable you'll see `mic open failed: ...; retrying in 5s` instead — that's still a successful smoke test (the retry path was reached).

- [ ] **Step 3: Optional — speak the wake phrase**

If you have a working mic and want to verify detection end-to-end:

```bash
cd /home/hoon/client_ui/backend
timeout 30 python3 wakeup_worker.py 2>&1
```

Within 30 s, say "what's up homie" close to the microphone. Expected log entries:
```
[INFO] wakeup_worker_node: wakeword detected (confidence=0.NN)
[INFO] wakeup_worker_node: stt_result: '<your speech>'
```

If it doesn't trigger, lower the threshold once: `WAKEUP_THRESHOLD=0.3 timeout 30 python3 wakeup_worker.py`.

No commit for this task — it only verifies behavior.

---

### Task 6: Wire the worker into `main.py`

**Files:**
- Modify: `client_ui/backend/main.py`

The current `main.py` (139 lines) needs three additions: an import, a process spawn after `rclpy.init`, and a teardown block in the `finally` of `main()`.

- [ ] **Step 1: Add the imports**

In `/home/hoon/client_ui/backend/main.py`, find the existing import block (lines 1–19) and replace it with the version below — only `multiprocessing` and the relative `wakeup_worker` import are new:

Replace:
```python
import json
import asyncio
import threading

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
```

With:
```python
import json
import asyncio
import multiprocessing
import os
import sys
import threading

# Make sibling module importable both when run as `python main.py`
# and as `python -m client_ui.backend.main`.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from wakeup_worker import run_worker  # noqa: E402

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from std_msgs.msg import String

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn
```

- [ ] **Step 2: Replace `def main()` to spawn and clean up the worker**

Find the existing `def main():` at the bottom of the file:

```python
def main():
    global loop

    rclpy.init()
    node = ClientBridgeNode()

    ros_thread = threading.Thread(target=ros_spin, args=(node,), daemon=True)
    ros_thread.start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    config = uvicorn.Config(app, host='0.0.0.0', port=8001, loop='asyncio', log_level='warning')
    server = uvicorn.Server(config)
    loop.run_until_complete(server.serve())
```

Replace it with:

```python
def main():
    global loop

    rclpy.init()
    node = ClientBridgeNode()

    wakeup_proc = multiprocessing.Process(
        target=run_worker, name='wakeup_worker', daemon=True,
    )
    wakeup_proc.start()
    print(f'[main] spawned wakeup_worker pid={wakeup_proc.pid}')

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
            print('[main] terminating wakeup_worker...')
            wakeup_proc.terminate()
            wakeup_proc.join(timeout=2)
            if wakeup_proc.is_alive():
                wakeup_proc.kill()
                wakeup_proc.join(timeout=1)
```

- [ ] **Step 3: Verify the file still parses**

Run: `python3 -c "import sys; sys.path.insert(0, '/home/hoon/client_ui/backend'); import main; print('ok')"`
Expected: `ok`. (This will not start uvicorn; it just imports the module.)

- [ ] **Step 4: Commit**

```bash
cd /home/hoon/client_ui
git add backend/main.py
git commit -q -m "feat: spawn wakeup_worker as a child process from backend main"
```

---

### Task 7: Replace the frontend with the minimal Rive character UI

**Files:**
- Modify (full rewrite): `client_ui/frontend/index.html`

The current 711-line `index.html` is replaced wholesale with a minimal page: black background, centered Rive character, text overlay above the character. No other UI elements per the spec.

- [ ] **Step 1: Overwrite `index.html`**

Replace the entire contents of `/home/hoon/client_ui/frontend/index.html` with:

```html
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>ROKEY</title>
  <style>
    html, body {
      margin: 0;
      padding: 0;
      width: 100%;
      height: 100%;
      background: #000;
      overflow: hidden;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    }
    body {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 24px;
    }
    #stt-overlay {
      color: #fff;
      font-size: 1.5rem;
      line-height: 1.4;
      text-align: center;
      max-width: 80vw;
      min-height: 1.4em;
      opacity: 0;
      transition: opacity 0.3s ease;
      pointer-events: none;
    }
    #stt-overlay.visible {
      opacity: 1;
    }
    #rive-canvas {
      width: 60vmin;
      height: 60vmin;
      display: block;
    }
  </style>
</head>
<body>
  <div id="stt-overlay" aria-live="polite"></div>
  <canvas id="rive-canvas"></canvas>

  <script src="https://unpkg.com/@rive-app/webgl2@2"></script>
  <script>
    const IDLE_RESET_MS = 8000;
    const STATE_MACHINE = "State Machine 1";
    const ARTBOARD = "Artboard";
    const AWAKE_INPUT = "Boolean 1";

    const canvas = document.getElementById("rive-canvas");
    const overlay = document.getElementById("stt-overlay");

    let awakeInput = null;
    let resetTimer = null;

    function setOverlay(text) {
      overlay.textContent = text || "";
      overlay.classList.toggle("visible", !!text);
    }

    function setAwake(awake) {
      if (awakeInput) awakeInput.value = !!awake;
    }

    function scheduleSleep() {
      if (resetTimer) clearTimeout(resetTimer);
      resetTimer = setTimeout(() => {
        setAwake(false);
        setOverlay("");
        resetTimer = null;
      }, IDLE_RESET_MS);
    }

    const r = new rive.Rive({
      src: "riv_ai_button.riv",
      canvas: canvas,
      autoplay: true,
      artboard: ARTBOARD,
      stateMachines: STATE_MACHINE,
      layout: new rive.Layout({
        fit: rive.Fit.Contain,
        alignment: rive.Alignment.Center,
      }),
      onLoad: () => {
        r.resizeDrawingSurfaceToCanvas();
        const inputs = r.stateMachineInputs(STATE_MACHINE) || [];
        awakeInput = inputs.find((i) => i.name === AWAKE_INPUT) || null;
        setAwake(false); // start in Sleep
      },
    });

    window.addEventListener("resize", () => r.resizeDrawingSurfaceToCanvas());

    // ── WebSocket ──
    const wsProto = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${wsProto}//${location.host}/ws/client`;

    function connectWs() {
      const ws = new WebSocket(wsUrl);
      ws.addEventListener("message", (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (data.type === "wakeup") {
          if (resetTimer) {
            clearTimeout(resetTimer);
            resetTimer = null;
          }
          setAwake(true);
          setOverlay("");
        } else if (data.type === "stt_result") {
          setAwake(true);
          setOverlay(data.text || "");
          scheduleSleep();
        }
      });
      ws.addEventListener("close", () => {
        // Auto-reconnect after 2 s
        setTimeout(connectWs, 2000);
      });
    }

    connectWs();
  </script>
</body>
</html>
```

- [ ] **Step 2: Quick static-load sanity check (no backend needed)**

```bash
cd /home/hoon/client_ui/frontend
python3 -m http.server 8765
```

Open `http://localhost:8765/` in a browser. Expected:
- Black screen with the Rive character centered, in Sleep state (closed eyes / asleep pulse).
- The console shows a WebSocket connection error to `ws://localhost:8765/ws/client` — that is fine, that endpoint only exists on port 8001 when the FastAPI backend runs. The auto-reconnect will keep retrying.

Stop the static server with Ctrl-C.

- [ ] **Step 3: Commit**

```bash
cd /home/hoon/client_ui
git add frontend/index.html
git commit -q -m "feat: replace frontend with minimal Rive character UI"
```

---

### Task 8: End-to-end manual verification

This is the final integration check. It exercises: backend boot → worker spawn → mic capture → wake-word inference → ROS2 publish → WS broadcast → Rive state change → STT response render.

- [ ] **Step 1: Boot the backend**

```bash
cd /home/hoon/client_ui
python3 backend/main.py
```

Expected stdout (within ~3 s):
```
[main] spawned wakeup_worker pid=NNNN
[INFO] [<ts>] [client_bridge_node]: ClientBridgeNode 시작
[INFO] [<ts>] [wakeup_worker_node]: wakeup_worker_node initialized (threshold=0.5, record_seconds=5, stt_enabled=True)
[INFO] [<ts>] [wakeup_worker_node]: openwakeword model loaded; entering detection loop
```

If `stt_enabled=False` appears, your `OPENAI_API_KEY` is not visible to this shell — ensure you launched from a terminal where `~/.bashrc` was sourced, or `export OPENAI_API_KEY=...` before launching.

- [ ] **Step 2: Open the frontend in a browser**

Navigate to `http://localhost:8001/`. Expected:
- Centered Rive character in Sleep state.
- Browser DevTools → Network shows the `ws/client` WebSocket connecting (status 101).

- [ ] **Step 3: Verify ROS2 topics independently**

In a second terminal (with the same ROS2 environment sourced):

```bash
ros2 topic echo /wakeup_status
```

…and in a third:

```bash
ros2 topic echo /stt_result
```

- [ ] **Step 4: Trigger the wake word and observe**

Say "what's up homie" near the mic. Expected within ~6 seconds:

1. Backend log: `wakeword detected (confidence=0.NN)`.
2. `/wakeup_status` echo terminal prints a JSON String with `"detected": true`.
3. Browser: character switches from Sleep to Awake (smile).
4. Backend log (5 s later): `stt_result: '<your speech>'`.
5. `/stt_result` echo terminal prints your text.
6. Browser: text overlay appears above the character with the transcription.
7. After 8 seconds idle, character returns to Sleep, overlay disappears.

- [ ] **Step 5: Test graceful shutdown**

In the backend terminal press Ctrl-C. Expected:
```
[main] terminating wakeup_worker...
```
…and the prompt returns within ~3 s. Confirm no orphan `wakeup_worker` processes remain:

```bash
pgrep -f wakeup_worker
```
Expected: no output.

- [ ] **Step 6: Test the missing-API-key path (optional but recommended)**

```bash
cd /home/hoon/client_ui
env -u OPENAI_API_KEY python3 backend/main.py
```

Expected backend log: `OPENAI_API_KEY unset → STT disabled`. Speak the wake word; the browser should still switch to Awake (the `wakeup` event fires) but no `stt_result` event is emitted, so the overlay stays empty and the character returns to Sleep after the idle timer.

Stop with Ctrl-C.

- [ ] **Step 7: Final commit (no code change — the plan is done)**

If any quick fixes were needed during integration testing (typos, missed env, log noise), commit them now with a focused message. Otherwise skip.

---

## Self-Review Notes

- **Spec coverage:** Tasks 1 (model files), 2 (rive file), 4 (worker), 6 (main.py wiring), 7 (frontend) cover every component in the spec's File Layout. The Error Matrix is exercised by Task 5 (mic-missing path on first run if applicable) and Task 8 Step 6 (missing API key).
- **Type / name consistency:** State machine name `"State Machine 1"`, artboard `"Artboard"`, input `"Boolean 1"` are taken from the live `.riv` strings dump and match between spec and frontend code. ROS2 topic names `/wakeup_status` and `/stt_result` match the existing `ClientBridgeNode` subscriptions in `main.py:55-57`. WebSocket payload `type` values (`"wakeup"`, `"stt_result"`) match the existing `_on_wakeup` and `_on_stt` emissions in `main.py:66, 78`.
- **Placeholders:** none. Every code step contains the full content; every command shows its expected output.
