"""Client UI backend.

Bridges ROS 2 ↔ WebSocket and serves the Rive frontend. Spawns the wake-word
worker (`wakeup_worker.run_worker`) as a child process. Subscribes to the
worker's topics and forwards them to all connected WebSocket clients. Also
exposes a `/tts` endpoint that proxies OpenAI gpt-4o-mini-tts.
"""
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

from fastapi import FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import uvicorn

from openai import OpenAI

# ── WebSocket 연결 관리 ────────────────────────────
class WsManager:
    """`cid` 기반 dedup. 동일 client id 로 새 ws 가 붙으면 기존 ws 를 끊어
    같은 브라우저(localStorage 공유) 는 항상 1개의 연결만 유지된다.
    cid 없이 들어오는 구버전 프론트는 dedup 없이 그대로 누적 (호환 목적).
    """

    SUPERSEDED_CODE = 4000  # 4000-4999 = application-defined close codes

    def __init__(self):
        self._connections: list[WebSocket] = []
        self._by_cid: dict[str, WebSocket] = {}

    async def connect(self, ws: WebSocket, cid: str | None = None):
        await ws.accept()
        if cid:
            old = self._by_cid.pop(cid, None)
            if old is not None and old is not ws:
                self._connections = [c for c in self._connections if c is not old]
                try:
                    await old.close(code=self.SUPERSEDED_CODE, reason="superseded")
                except Exception:
                    pass
            self._by_cid[cid] = ws
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        self._connections = [c for c in self._connections if c is not ws]
        for k in [k for k, v in self._by_cid.items() if v is ws]:
            del self._by_cid[k]

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_text(json.dumps(data, ensure_ascii=False))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = WsManager()
loop: asyncio.AbstractEventLoop = None


# ── ROS2 구독 노드 ─────────────────────────────────
class ClientBridgeNode(Node):
    def __init__(self):
        super().__init__('client_bridge_node')

        qos = QoSProfile(depth=10, reliability=ReliabilityPolicy.RELIABLE)

        self.create_subscription(String, '/wakeup_status',   self._on_wakeup,          qos)
        self.create_subscription(String, '/stt_result',      self._on_stt,             qos)
        self.create_subscription(String, '/voice_reply',     self._on_voice_reply,     qos)
        # /wakeup_debug, /wakeup_progress 구독 비활성화 — 필요 시 주석 해제
        # self.create_subscription(String, '/wakeup_debug',    self._on_wakeup_debug,    qos)
        # self.create_subscription(String, '/wakeup_progress', self._on_wakeup_progress, qos)

        self.get_logger().info('ClientBridgeNode 시작')

    def _on_wakeup(self, msg: String):
        try:
            data = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        data['type'] = 'wakeup'
        self._emit(data)

    # /wakeup_debug, /wakeup_progress 핸들러 비활성화 — 필요 시 주석 해제
    # def _on_wakeup_debug(self, msg: String):
    #     try:
    #         data = json.loads(msg.data)
    #     except json.JSONDecodeError:
    #         return
    #     data['type'] = 'wakeup_debug'
    #     self._emit(data)
    #
    # def _on_wakeup_progress(self, msg: String):
    #     try:
    #         data = json.loads(msg.data)
    #     except json.JSONDecodeError:
    #         return
    #     data['type'] = 'wakeup_progress'
    #     self._emit(data)

    def _on_stt(self, msg: String):
        # /stt_result 는 Whisper raw transcription (사용자 발화 그대로) 만 들어옴.
        self._emit({'type': 'stt_result', 'text': msg.data})

    def _on_voice_reply(self, msg: String):
        # /voice_reply 는 GPT command parser 의 reply 문장. TTS 재생 대상.
        self._emit({'type': 'voice_reply', 'text': msg.data})

    def _emit(self, data: dict):
        if loop and not loop.is_closed():
            asyncio.run_coroutine_threadsafe(ws_manager.broadcast(data), loop)


# ── FastAPI 앱 ─────────────────────────────────────
app = FastAPI(title='Client UI Bridge', version='1.0.0')

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'frontend')

# OpenAI client for TTS endpoint. Plain `def` endpoints below run in a
# threadpool, so blocking calls here don't stall uvicorn's event loop.
_OPENAI_CLIENT = OpenAI() if os.getenv('OPENAI_API_KEY') else None
_TTS_MODEL = os.getenv('TTS_MODEL', 'gpt-4o-mini-tts')
_TTS_VOICE = os.getenv('TTS_VOICE', 'alloy')


@app.websocket('/ws/client')
async def ws_endpoint(websocket: WebSocket, cid: str | None = None):
    await ws_manager.connect(websocket, cid=cid)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


@app.get('/tts')
def tts_endpoint(text: str):
    if not text or not text.strip():
        raise HTTPException(400, 'text query param is required')
    if _OPENAI_CLIENT is None:
        raise HTTPException(503, 'OPENAI_API_KEY unset on server')
    try:
        resp = _OPENAI_CLIENT.audio.speech.create(
            model=_TTS_MODEL,
            voice=_TTS_VOICE,
            input=text,
            response_format='mp3',
        )
    except Exception as e:
        print(f'[tts] OpenAI call failed: {e!r}', file=sys.stderr)
        raise HTTPException(502, f'TTS failed: {type(e).__name__}')
    audio = resp.read() if hasattr(resp, 'read') else getattr(resp, 'content', b'')
    if not audio:
        raise HTTPException(502, 'TTS returned empty audio')
    return Response(
        content=audio,
        media_type='audio/mpeg',
        headers={'Cache-Control': 'no-store'},
    )


# 정적 파일은 WebSocket 라우트 뒤에 마운트 (라우트 우선순위)
if os.path.isdir(FRONTEND_DIR):
    app.mount('/', StaticFiles(directory=FRONTEND_DIR, html=True), name='static')


# ── 실행 진입점 ────────────────────────────────────
def ros_spin(node: Node):
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


def main():
    global loop

    rclpy.init()
    node = ClientBridgeNode()

    # Use 'spawn' start method (not the Linux default 'fork') so the child
    # starts with a fresh Python interpreter and does not inherit the parent's
    # already-initialized rclpy context — otherwise the worker's rclpy.init()
    # raises "Context.init() must only be called once".
    wakeup_ctx = multiprocessing.get_context('spawn')
    wakeup_proc = wakeup_ctx.Process(
        target=run_worker, name='wakeup_worker', daemon=True,
    )
    wakeup_proc.start()
    print(f'[main] spawned wakeup_worker pid={wakeup_proc.pid}', flush=True)

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
            print('[main] terminating wakeup_worker...', flush=True)
            wakeup_proc.terminate()
            wakeup_proc.join(timeout=2)
            if wakeup_proc.is_alive():
                wakeup_proc.kill()
                wakeup_proc.join(timeout=1)


if __name__ == '__main__':
    main()
