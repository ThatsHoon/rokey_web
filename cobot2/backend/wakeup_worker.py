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
DEFAULT_WAKE_THRESHOLD = 0.3
DEFAULT_RECORD_SECONDS = 5            # minimum guaranteed record window
DEFAULT_RECORD_MAX_SECONDS = 10       # hard cap to bound runaway speech / open mics
DEFAULT_SILENCE_THRESHOLD = 0.2       # peak |amp|/32768 considered "no speech"
DEFAULT_SILENCE_DURATION = 1.0        # seconds of contiguous silence to end recording

# LLM used for the command-parser refinement step (matches voice_processing/get_keyword.py).
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o")

# Shared command-parser prompt — must match voice_to_command / text_to_command.
# Edit only here.
PROMPT_CONTENT = """
당신은 가정용 협동로봇의 음성 명령 파서다.
사용자 발화를 단위 동작 시퀀스로 분해하고, 자연스러운 한국어 reply를 함께 생성한다.
JSON만 출력. 다른 텍스트 금지.
 
[출력 형식]
{{
"sequence": [{{"step": N, "action": "<액션>", "params": {{"target": "<값>"}} 또는 {{}}}}],
"reply": "한 문장"
}}
 
[액션 카탈로그]
▸ pick_vertical(target)   : 물체를 수직으로 집는다.
▸ pick_horizontal(target) : 물체를 수평으로 집는다 (길쭉한 형태).
▸ pick_side(target)       : 물체를 사이드로 집는다 (그릇/접시 형태).
▸ finding(target)         : 물체 위치를 탐색한다. "어디있어", "찾아봐" 같은 단독 탐색 발화 시.
▸ place(target)           : 지정 박스에 내려놓는다. ★ 잡은 상태(pick류 직후)에서만 사용. target은 left_box 또는 right_box 만 가능.
▸ trash()                 : 잡은 물체를 쓰레기통(고정 위치)에 버린다. ★ 잡은 상태에서만 사용. target 없음.
▸ pour(target)            : 잡은 물체의 내용물을 target에 붓는다. ★ 잡은 상태에서만 사용. (place처럼 잡기 해제됨)
▸ shake()                 : 잡은 물체를 흔든다. ★ 잡은 상태에서만 사용.
▸ tap(target)             : 물체를 톡톡 두드린다. 잡을 필요 없음. (단독 동작)
▸ reset()                 : 홈 포지션으로 복귀한다 (그리퍼 열림). 모든 정상 시퀀스의 종료 동작.
 
[지원 객체 (params.target 키, 영어로 발행) + 잡는 방식 고정]
- "사과"      → "apple"      : pick_vertical (수직)
- "오렌지"    → "orange"     : pick_vertical (수직)
- "후추통"    → "shaker"     : pick_horizontal (수평)
- "접시"      → "plate"      : pick_side (사이드)
- "배"        → "pear"       : pick_vertical (수직)
- "바나나"    → "banana"     : pick_vertical (수직)
- "레고"      → "toy_block"  : pick_vertical (수직)
 
[지원 위치 (params.target 키, 영어로 발행)]
- "왼쪽 박스" / "왼쪽" → "left_box"
- "오른쪽 박스" / "오른쪽" → "right_box"
(※ "쓰레기통"은 별도 target이 아니라 trash() 액션을 사용한다)
 
[규칙]
1. 객체별 잡는 방식은 고정이다. 사용자 발화의 "수직"/"수평"/"사이드" 같은 단어와 무관하게 위 매핑을 따른다.
2. 모든 정상 시퀀스는 마지막에 reset 으로 종료한다 (단순 홈 복귀 / 단순 finding / 단순 tap 발화는 단독 가능).
3. 잡기 동작(pick_vertical / pick_horizontal / pick_side) 직후에는 반드시 place(target), trash(), pour(target), 또는 reset() 중 하나가 와야 다시 잡기 동작을 호출할 수 있다.
   (한 번에 한 물건만 잡을 수 있음. 다음 물건 잡기 전 들고 있던 것을 내려놓아야 함.)
4. place(target) 는 target이 반드시 "left_box" 또는 "right_box" 여야 한다. 그 외 값은 거절.
5. trash() 는 쓰레기통 전용 동작이므로 params는 {{}}. "쓰레기통에 버려"는 항상 trash() 로 매핑한다.
6. shake(), pour(target), place(target), trash() 는 직전에 잡은 상태여야 한다.
7. tap(target) 은 잡지 않은 상태로 단독 사용. tap 후엔 reset 으로 마무리.
8. params 는 항상 {{"target": ...}} 형식. 액션 자체가 인자 없으면 {{}}.
9. 객체는 영어로(apple/orange/shaker/plate/pear/banana/toy_block), 위치도 영어로(left_box/right_box) 발행.
10. 카탈로그에 없는 액션이나 지원하지 않는 값을 요청하면 sequence 는 [], reply 는 거절 멘트.
11. step 번호는 1부터 순차.
 
[예시]
사용자: "사과 버려줘"
{{"sequence":[{{"step":1,"action":"pick_vertical","params":{{"target":"apple"}}}},{{"step":2,"action":"trash","params":{{}}}},{{"step":3,"action":"reset","params":{{}}}}],"reply":"네, 사과를 쓰레기통에 버리겠습니다."}}
 
사용자: "오렌지 왼쪽 박스에 둬"
{{"sequence":[{{"step":1,"action":"pick_vertical","params":{{"target":"orange"}}}},{{"step":2,"action":"place","params":{{"target":"left_box"}}}},{{"step":3,"action":"reset","params":{{}}}}],"reply":"네, 오렌지를 왼쪽 박스에 두겠습니다."}}
 
사용자: "바나나 오른쪽에 놔줘"
{{"sequence":[{{"step":1,"action":"pick_vertical","params":{{"target":"banana"}}}},{{"step":2,"action":"place","params":{{"target":"right_box"}}}},{{"step":3,"action":"reset","params":{{}}}}],"reply":"네, 바나나를 오른쪽 박스에 두겠습니다."}}
 
사용자: "접시 잡아"
{{"sequence":[{{"step":1,"action":"pick_side","params":{{"target":"plate"}}}},{{"step":2,"action":"reset","params":{{}}}}],"reply":"네, 접시를 잡겠습니다."}}
 
사용자: "후추통 흔들어"
{{"sequence":[{{"step":1,"action":"pick_horizontal","params":{{"target":"shaker"}}}},{{"step":2,"action":"shake","params":{{}}}},{{"step":3,"action":"reset","params":{{}}}}],"reply":"네, 후추통을 흔들겠습니다."}}
 
사용자: "사과 흔들고 쓰레기통에 버려"
{{"sequence":[{{"step":1,"action":"pick_vertical","params":{{"target":"apple"}}}},{{"step":2,"action":"shake","params":{{}}}},{{"step":3,"action":"trash","params":{{}}}},{{"step":4,"action":"reset","params":{{}}}}],"reply":"네, 사과를 흔들고 쓰레기통에 버리겠습니다."}}
 
사용자: "사과 왼쪽 박스에 두고 바나나 오른쪽 박스에 둬"
{{"sequence":[{{"step":1,"action":"pick_vertical","params":{{"target":"apple"}}}},{{"step":2,"action":"place","params":{{"target":"left_box"}}}},{{"step":3,"action":"pick_vertical","params":{{"target":"banana"}}}},{{"step":4,"action":"place","params":{{"target":"right_box"}}}},{{"step":5,"action":"reset","params":{{}}}}],"reply":"네, 사과를 왼쪽 박스에, 바나나를 오른쪽 박스에 두겠습니다."}}
 
사용자: "오렌지랑 배 다 쓰레기통에 버려"
{{"sequence":[{{"step":1,"action":"pick_vertical","params":{{"target":"orange"}}}},{{"step":2,"action":"trash","params":{{}}}},{{"step":3,"action":"pick_vertical","params":{{"target":"pear"}}}},{{"step":4,"action":"trash","params":{{}}}},{{"step":5,"action":"reset","params":{{}}}}],"reply":"네, 오렌지와 배를 차례로 쓰레기통에 버리겠습니다."}}
 
사용자: "후추통으로 접시에 부어줘"
{{"sequence":[{{"step":1,"action":"pick_horizontal","params":{{"target":"shaker"}}}},{{"step":2,"action":"pour","params":{{"target":"plate"}}}},{{"step":3,"action":"reset","params":{{}}}}],"reply":"네, 후추통에 든 것을 접시에 부어드릴게요."}}
 
사용자: "레고 톡톡 두드려"
{{"sequence":[{{"step":1,"action":"tap","params":{{"target":"toy_block"}}}},{{"step":2,"action":"reset","params":{{}}}}],"reply":"네, 레고를 톡톡 두드리겠습니다."}}
 
사용자: "배 어디있어"
{{"sequence":[{{"step":1,"action":"finding","params":{{"target":"pear"}}}}],"reply":"배를 찾아볼게요."}}
 
사용자: "홈으로 가"
{{"sequence":[{{"step":1,"action":"reset","params":{{}}}}],"reply":"네, 홈 포지션으로 복귀하겠습니다."}}
 
사용자: "수박 가져와"
{{"sequence":[],"reply":"죄송합니다. 현재 지원하는 객체가 아니에요."}}
 
사용자: "사과 가운데 박스에 둬"
{{"sequence":[],"reply":"죄송합니다. 왼쪽 박스 또는 오른쪽 박스에만 둘 수 있어요."}}
 
사용자: "그냥 흔들어"
{{"sequence":[],"reply":"어떤 물건을 흔들까요?"}}
 
<사용자 입력>
"{user_input}"
"""




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


def _record_to_wav_bytes(stream, min_seconds, max_seconds, silence_threshold, silence_duration):
    """Record audio with speech-aware tail detection.

    Always captures at least `min_seconds`. After the minimum is reached, keeps
    reading chunks and stops once the peak amplitude has stayed below
    `silence_threshold` for `silence_duration` seconds — or when `max_seconds`
    is hit (hard cap). Returns (wav_bytes, elapsed_seconds, ended_by) where
    `ended_by` is "silence" or "max".
    """
    min_chunks = max(1, int(round(MIC_RATE * min_seconds / MIC_CHUNK)))
    max_chunks = max(min_chunks, int(round(MIC_RATE * max_seconds / MIC_CHUNK)))
    silence_chunks_needed = max(1, int(round(MIC_RATE * silence_duration / MIC_CHUNK)))

    frames = []
    silence_run = 0
    ended_by = "max"

    for i in range(max_chunks):
        chunk_bytes = stream.read(MIC_CHUNK, exception_on_overflow=False)
        frames.append(chunk_bytes)
        samples = np.frombuffer(chunk_bytes, dtype=np.int16)
        level = float(np.max(np.abs(samples)) / 32768.0) if samples.size else 0.0
        in_tail = (i + 1 >= min_chunks)
        if in_tail:
            if level < silence_threshold:
                silence_run += 1
            else:
                silence_run = 0
        # Live readout — same \r line style as the detection loop's [mic] feed
        # so the user can watch level vs silence_threshold in real time.
        print(
            f"\r[rec] chunk={i+1:3d}/{max_chunks} level={level:.3f} "
            f"sil_thr={silence_threshold:.3f} silence={silence_run}/{silence_chunks_needed} "
            f"phase={'tail' if in_tail else 'min '}     ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        if in_tail and silence_run >= silence_chunks_needed:
            ended_by = "silence"
            break

    print("", file=sys.stderr, flush=True)  # newline so the next [mic] line starts fresh
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(MIC_CHANNELS)
        wf.setsampwidth(2)  # int16
        wf.setframerate(MIC_RATE)
        wf.writeframes(b"".join(frames))
    elapsed_seconds = len(frames) * MIC_CHUNK / MIC_RATE
    return buf.getvalue(), elapsed_seconds, ended_by


def _transcribe(client, wav_bytes):
    """Returns transcribed text (Korean) or None on failure.

    Forces language="ko" so Whisper does not mis-detect Japanese/Chinese on
    short, accent-ambiguous Korean clips. The `prompt` further biases the
    decoder toward Korean orthography on borderline frames.
    """
    try:
        resp = client.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.wav", wav_bytes, "audio/wav"),
            language="ko",
            prompt="다음은 한국어 발화입니다. 일본어나 중국어가 아닙니다.",
        )
        return resp.text
    except Exception as e:
        print(f"[wakeup_worker] whisper failed: {e!r}", file=sys.stderr)
        return None


def _refine_to_command(client, transcription):
    """Run the shared command-parser prompt on `transcription`.

    Returns a dict with keys `sequence` (list) and `reply` (str) on success,
    or None on failure. Failures are non-fatal — the caller emits a fallback.
    """
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{
                "role": "user",
                "content": PROMPT_CONTENT.format(user_input=transcription),
            }],
            temperature=0.5,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content
        data = json.loads(content)
        if not isinstance(data, dict):
            raise ValueError(f"LLM returned non-object JSON: {type(data).__name__}")
        return data
    except Exception as e:
        print(f"[wakeup_worker] LLM refine failed: {e!r}", file=sys.stderr)
        return None


def run_worker():
    if not os.path.isfile(ONNX_PATH):
        print(f"[wakeup_worker] FATAL: model not found at {ONNX_PATH}", file=sys.stderr)
        return

    threshold = _env_float("WAKEUP_THRESHOLD", DEFAULT_WAKE_THRESHOLD)
    record_min_seconds = _env_int("RECORD_SECONDS", DEFAULT_RECORD_SECONDS)
    record_max_seconds = _env_int("RECORD_MAX_SECONDS", DEFAULT_RECORD_MAX_SECONDS)
    silence_threshold = _env_float("SILENCE_THRESHOLD", DEFAULT_SILENCE_THRESHOLD)
    silence_duration = _env_float("SILENCE_DURATION", DEFAULT_SILENCE_DURATION)
    device_index = _env_int("MIC_DEVICE_INDEX", None)

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
    # /wakeup_debug, /wakeup_progress 발행 비활성화 — 필요 시 주석 해제
    # pub_debug = node.create_publisher(String, "/wakeup_debug", qos)
    # pub_progress = node.create_publisher(String, "/wakeup_progress", qos)
    # Canonical voice-pipeline topics consumed by cobot_core.state_manager
    # (/voice_command, sequence-as-JSON-array) and voice_client (/voice_reply,
    # plain reply string). Format matches voice_to_command.py exactly.
    pub_voice_command = node.create_publisher(String, "/voice_command", qos)
    pub_voice_reply = node.create_publisher(String, "/voice_reply", qos)

    def emit_progress(stage, **extra):
        # /wakeup_progress 비활성화됨. 호출부는 그대로 두고 no-op 처리.
        # payload = {"stage": stage, "ts": time.time()}
        # payload.update(extra)
        # pub_progress.publish(String(data=json.dumps(payload, ensure_ascii=False)))
        # extra_str = " ".join(f"{k}={v}" for k, v in extra.items())
        # node.get_logger().info(
        #     f"📢 /wakeup_progress — {stage}" + (f" ({extra_str})" if extra_str else "")
        # )
        pass
    node.get_logger().info(
        f"wakeup_worker_node initialized (threshold={threshold}, "
        f"record={record_min_seconds}–{record_max_seconds}s, "
        f"silence={silence_threshold}/{silence_duration}s, "
        f"stt_enabled={client is not None})"
    )

    audio, stream = _open_mic(device_index=device_index)
    model = Model(wakeword_models=[ONNX_PATH], inference_framework="onnx")
    node.get_logger().info("openwakeword model loaded; entering detection loop")

    chunk_idx = 0
    try:
        while rclpy.ok():
            chunk_bytes = stream.read(MIC_CHUNK, exception_on_overflow=False)
            samples = np.frombuffer(chunk_bytes, dtype=np.int16)
            samples_16k = resample(samples, int(len(samples) * MODEL_RATE / MIC_RATE)).astype(np.int16)
            outputs = model.predict(samples_16k, threshold=PREDICT_INTERNAL_THRESHOLD)
            confidence = float(outputs.get(MODEL_KEY, 0.0))
            chunk_idx += 1
            # Debug telemetry — emit every chunk so the browser console shows live confidence
            # plus a coarse audio level (peak abs amplitude / 32768) for "is the mic alive" sanity.
            level = float(np.max(np.abs(samples)) / 32768.0) if samples.size else 0.0
            # /wakeup_debug 비활성화됨 — 필요 시 주석 해제
            # pub_debug.publish(String(data=json.dumps({
            #     "confidence": confidence,
            #     "level": level,
            #     "threshold": threshold,
            #     "chunk": chunk_idx,
            #     "ts": time.time(),
            # })))
            # Live status line on the parent's terminal — \r overwrites in place
            # so the readout updates ~4 Hz without scrolling. ROS log lines push
            # it down to a new line when they fire.
            print(
                f"\r[mic] chunk={chunk_idx:5d} level={level:.3f} "
                f"conf={confidence:.3f} wake_thr={threshold} "
                f"sil_thr={silence_threshold:.3f}     ",
                end="",
                file=sys.stderr,
                flush=True,
            )
            if confidence > threshold:
                pub_wakeup.publish(String(data=json.dumps({
                    "detected": True,
                    "model": MODEL_KEY,
                    "confidence": confidence,
                    "ts": time.time(),
                })))
                node.get_logger().info(
                    f"📢 /wakeup_status — detected (confidence={confidence:.3f}, model={MODEL_KEY})"
                )

                emit_progress("recording_started",
                              min_duration=record_min_seconds,
                              max_duration=record_max_seconds)
                t0 = time.time()
                wav_bytes, audio_seconds, ended_by = _record_to_wav_bytes(
                    stream,
                    min_seconds=record_min_seconds,
                    max_seconds=record_max_seconds,
                    silence_threshold=silence_threshold,
                    silence_duration=silence_duration,
                )
                emit_progress("recording_finished",
                              elapsed=round(time.time() - t0, 3),
                              audio_seconds=round(audio_seconds, 3),
                              ended_by=ended_by,
                              wav_bytes=len(wav_bytes))

                if client is None:
                    emit_progress("stt_skipped", reason="OPENAI_API_KEY unset")
                    continue

                emit_progress("transcribing")
                t0 = time.time()
                text = _transcribe(client, wav_bytes)
                if text is None:
                    emit_progress("transcribe_failed", elapsed=round(time.time() - t0, 3))
                    continue
                emit_progress("transcribed",
                              elapsed=round(time.time() - t0, 3),
                              transcription=text)
                node.get_logger().info(f"🗣  Whisper: {text!r}")

                emit_progress("refining", model=LLM_MODEL)
                t0 = time.time()
                refined = _refine_to_command(client, text)
                if refined is not None:
                    payload = {
                        "transcription": text,
                        "sequence": refined.get("sequence", []),
                        "reply": refined.get("reply", ""),
                    }
                    emit_progress("refined", elapsed=round(time.time() - t0, 3))
                else:
                    payload = {
                        "transcription": text,
                        "sequence": [],
                        "reply": text,
                    }
                    emit_progress("refine_failed", elapsed=round(time.time() - t0, 3))
                pub_stt.publish(String(data=payload["transcription"]))
                node.get_logger().info(f"📢 /stt_result — {payload['transcription']!r}")

                # Canonical fan-out matching voice_to_command.py exactly:
                # state_manager subscribes to /voice_command (sequence array),
                # voice_client subscribes to /voice_reply (reply string).
                seq_data = json.dumps(payload["sequence"], ensure_ascii=False)
                pub_voice_command.publish(String(data=seq_data))
                action_summary = ", ".join(
                    f"{s.get('step', '?')}:{s.get('action', '?')}"
                    for s in payload["sequence"]
                ) or "(empty)"
                node.get_logger().info(
                    f"📢 /voice_command — {len(payload['sequence'])} steps [{action_summary}]"
                )

                pub_voice_reply.publish(String(data=payload["reply"]))
                node.get_logger().info(f"📢 /voice_reply — {payload['reply']!r}")

                # Re-trigger guard: during the recording + Whisper + GPT calls,
                # PyAudio kept buffering ~3 s of mic audio. If we resume detection
                # immediately, those stale chunks (often containing the tail of
                # the user's speech or the AI's playback) re-fire the wake word.
                # Drain the buffer and reset openwakeword's feature history.
                drained = 0
                try:
                    while stream.get_read_available() >= MIC_CHUNK:
                        stream.read(MIC_CHUNK, exception_on_overflow=False)
                        drained += MIC_CHUNK
                except Exception:
                    pass
                try:
                    model.reset()
                except Exception:
                    pass
                emit_progress("ready", drained_frames=drained)
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
