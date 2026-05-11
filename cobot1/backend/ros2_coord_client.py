"""
영속 ROS2 토픽 구독 — /robo_chef/coords

서비스 콜(양방향 DDS) 대신 토픽 구독(단방향 메인노드→백엔드)을 사용한다.
서비스 응답이 네트워크/방화벽/DDS endpoint 문제로 돌아오지 않는 환경에서도 동작.

메인노드 coord_service.py 가 2Hz로 발행하는 /robo_chef/coords (std_msgs/String JSON)를
구독해 최신 좌표를 캐시하고, get_coords() 로 제공한다.
"""

import json
import threading
import time
import logging

logger = logging.getLogger(__name__)

COORD_TOPIC  = "/robo_chef/coords"
STALE_SEC    = 5.0      # 이 시간 이상 갱신 없으면 stale 처리

_lock        = threading.Lock()
_cache: dict = {}        # 최신 수신 데이터
_last_recv   = 0.0       # monotonic 시간
_ready       = threading.Event()
_spin_thread: threading.Thread | None = None


def _spin_worker():
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from std_msgs.msg import String

    global _cache, _last_recv

    try:
        if not rclpy.ok():
            rclpy.init()

        node = rclpy.create_node("rc_coord_subscriber")

        def _on_coords(msg: String):
            global _last_recv
            try:
                data = json.loads(msg.data)
                with _lock:
                    _cache.update(data)
                _last_recv = time.monotonic()
                if not _ready.is_set():
                    _ready.set()
                    logger.info("[CoordSub] 첫 좌표 수신: %s", COORD_TOPIC)
            except Exception as e:
                logger.warning("[CoordSub] 파싱 오류: %s", e)

        node.create_subscription(String, COORD_TOPIC, _on_coords, 10)
        logger.info("[CoordSub] 구독 시작: %s", COORD_TOPIC)

        executor = SingleThreadedExecutor()
        executor.add_node(node)

        while rclpy.ok():
            executor.spin_once(timeout_sec=0.5)

    except Exception as e:
        logger.error("[CoordSub] spin_worker 예외: %s", e)
    finally:
        try:
            executor.shutdown(wait=False)
            node.destroy_node()
        except Exception:
            pass


def start():
    """Flask startup 시 한 번 호출"""
    global _spin_thread
    if _spin_thread and _spin_thread.is_alive():
        return
    _spin_thread = threading.Thread(
        target=_spin_worker, daemon=True, name="ros2_coord_sub"
    )
    _spin_thread.start()


def get_coords(timeout: float = 3.0) -> dict:
    """
    캐시된 최신 좌표 반환.
    아직 한 번도 수신하지 못했으면 최대 timeout 초 대기.
    마지막 수신 후 STALE_SEC 초 초과 시 stale 오류 반환.
    """
    if not _ready.is_set():
        if not _ready.wait(timeout=timeout):
            return {"error": f"좌표 토픽 미수신: {COORD_TOPIC} (메인노드 확인 필요)"}

    if time.monotonic() - _last_recv > STALE_SEC:
        return {"error": "좌표 데이터 stale (메인노드 연결 끊김)"}

    with _lock:
        return dict(_cache)
