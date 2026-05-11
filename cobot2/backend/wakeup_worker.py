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
입력은 항상 한국어로 해석한다 (일본어/중국어 금지).
JSON만 출력. 다른 텍스트 금지.

[출력 형식]
{{"sequence":[{{"step":N,"action":"<액션>","params":{{"target":"<값>"}} 또는 {{}}}}],"reply":"한 문장"}}

[액션] — 의미와 발화 신호 (괄호 안은 트리거 표현)
- pick / pick_horizontal / pick_side (target): 객체 잡기. 잡는 방식은 객체별 카탈로그 고정. ("잡아", "들어", "가져와")
- place (target): 잡은 객체를 박스에 내려놓기. target = right_box | left_box. ("놔줘", "넣어", "옮겨", "박스에")
- pour (target): 잡은 객체를 target 객체에 붓기·뿌리기·쏟기. ("부어", "뿌려", "쏟아", "따라")
- trash (): 잡은 객체 버리기. ("버려", "치워", "쓰레기", "갖다 버려") — 별도 target 없음
- finding (target): target 위치를 시각으로 탐색·보고. ("어디 있어", "찾아줘", "위치", "어딨어")
- tap (target): 로봇 팔로 객체를 톡톡 두드려 "이거 여기 있어요" 라고 가리키는 손짓. ("가리켜", "짚어", "지적해", "여기라고 알려줘", "톡톡", "두드려") — 잡지 않은 상태 단독
- hello_bot (): 가벼운 인사 모션. ("안녕", "하이", "야", "헤이", 호명·인사만 있는 발화) — params 없음

[객체 → target(영어), 잡는 방식 고정]
사과→apple, 블록→toy_block, 배→pear, 오렌지→orange : pick
후추통→shaker, 바나나→banana : pick_horizontal
접시→plate : pick_side

[위치 → target(영어), place 전용]
오른쪽/오른편/오른쪽 박스→right_box, 왼쪽/왼편/왼쪽 박스→left_box

[규칙]
1. 객체별 잡는 방식은 카탈로그 고정. 발화의 "수직/수평/사이드" 단어는 무시.
2. 한 번에 한 물건만 잡는다 — 같은 sequence 내 다음 pick 전에 반드시 place/pour/trash 로 그리퍼 해제.
3. params 의 target 은 모두 영어 식별자. place 의 target 은 right_box/left_box 둘 중 하나로 고정. params 없는 액션(trash, hello_bot) 은 {{}} 로 출력.
4. finding vs tap 분별:
   - 질문형/탐색 ("어디 있어", "찾아줘") → finding
   - 손짓·지시 요청 ("가리켜", "짚어", "톡톡 쳐줘") → tap
   - 단순히 "이거 보여줘" 처럼 모호하면 tap 우선 (손짓이 더 직관적)
5. trash vs place 분별: "버려/치워/쓰레기" → trash. "오른쪽/왼쪽 박스에" → place. 발화에 "쓰레기통" 이라는 단어가 있어도 trash 액션으로 매핑 (별도 target 없음).
6. 카탈로그 외 객체/액션 요청 시 sequence=[], reply 에 거절 멘트.
7. reply 는 친근한 존댓말 1문장 ("~할게요", "~해드릴게요", "~네요"). 로봇답게 간결·자연스럽게.

[예시]
"사과 버려" → {{"sequence":[{{"step":1,"action":"pick","params":{{"target":"apple"}}}},{{"step":2,"action":"trash","params":{{}}}}],"reply":"네, 사과 버릴게요."}}

"오렌지 오른쪽 박스에 넣어줘" → {{"sequence":[{{"step":1,"action":"pick","params":{{"target":"orange"}}}},{{"step":2,"action":"place","params":{{"target":"right_box"}}}}],"reply":"네, 오렌지를 오른쪽 박스에 넣어드릴게요."}}

"후추통으로 접시에 뿌려" → {{"sequence":[{{"step":1,"action":"pick_horizontal","params":{{"target":"shaker"}}}},{{"step":2,"action":"pour","params":{{"target":"plate"}}}}],"reply":"네, 접시에 뿌려드릴게요."}}

"배 어디 있어?" → {{"sequence":[{{"step":1,"action":"finding","params":{{"target":"pear"}}}}],"reply":"배 찾아볼게요."}}

"바나나 좀 짚어봐" → {{"sequence":[{{"step":1,"action":"tap","params":{{"target":"banana"}}}}],"reply":"바나나 여기 있어요."}}

"안녕!" → {{"sequence":[{{"step":1,"action":"hello_bot","params":{{}}}}],"reply":"안녕하세요!"}}

"수박 가져와" → {{"sequence":[],"reply":"죄송해요, 수박은 아직 다루지 못해요."}}

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
    pub_debug = node.create_publisher(String, "/wakeup_debug", qos)
    pub_progress = node.create_publisher(String, "/wakeup_progress", qos)
    # Canonical voice-pipeline topics consumed by cobot_core.state_manager
    # (/voice_command, sequence-as-JSON-array) and voice_client (/voice_reply,
    # plain reply string). Format matches voice_to_command.py exactly.
    pub_voice_command = node.create_publisher(String, "/voice_command", qos)
    pub_voice_reply = node.create_publisher(String, "/voice_reply", qos)

    def emit_progress(stage, **extra):
        payload = {"stage": stage, "ts": time.time()}
        payload.update(extra)
        pub_progress.publish(String(data=json.dumps(payload, ensure_ascii=False)))
        extra_str = " ".join(f"{k}={v}" for k, v in extra.items())
        node.get_logger().info(
            f"📢 /wakeup_progress — {stage}" + (f" ({extra_str})" if extra_str else "")
        )
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
            pub_debug.publish(String(data=json.dumps({
                "confidence": confidence,
                "level": level,
                "threshold": threshold,
                "chunk": chunk_idx,
                "ts": time.time(),
            })))
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
                stt_data = json.dumps(payload, ensure_ascii=False)
                pub_stt.publish(String(data=stt_data))
                node.get_logger().info(
                    f"📢 /stt_result — reply={payload['reply']!r} "
                    f"(seq={len(payload['sequence'])} steps, {len(stt_data)} bytes)"
                )

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
