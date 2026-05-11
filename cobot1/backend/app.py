"""
ROBO CHEF — Flask Backend Server
포트 5000에서 실행. Firebase + ROS2 브릿지 역할.
"""
import threading
import time
import os
import sys
import json
import subprocess

from flask import Flask, jsonify, request, abort
from flask_cors import CORS

import firebase_client as fb
import robot_bridge as rb
import config
import ros2_coord_client as coord_client


# ── ROS2 토픽 발행 헬퍼 (subprocess 방식) ────────────────────────

def _ros2_publish_order(order_id: str, order: dict, recipe: dict) -> bool:
    """
    std_msgs/String 토픽으로 레시피를 발행한다.
    ros2_order_publisher.py 가 실행 중이면 Firebase 큐를 통해 처리되지만,
    이 함수는 Flask에서 직접 subprocess로 one-shot 발행하는 fallback이다.
    """
    if config.ORDER_MSG_FORMAT == "simple":
        msg_data = (
            f"{recipe.get('recipe_name', recipe_id)}"
            f"|{recipe.get('recipe_id', '?')}"
        )
    else:
        payload = {
            "order_id":    order_id,
            "recipe_id":   recipe.get("recipe_id", ""),
            "recipe_name": recipe.get("recipe_name", ""),
            "total_steps": recipe.get("total_steps", 0),
            "locations":   recipe.get("locations", {}),
            "sequence":    recipe.get("sequence", []),
        }
        msg_data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # YAML-safe 이스케이프 (작은따옴표 안에 감쌈)
    yaml_str = f"data: '{msg_data.replace(chr(39), chr(39)+chr(39))}'"

    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(config.ROS2_DOMAIN_ID)

    # ros2_pub_once.py: 구독자 유무와 관계없이 즉시 발행 후 종료
    pub_script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ros2_pub_once.py")
    cmd = ["python3", pub_script, config.ROS2_ORDER_TOPIC, msg_data]

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5, env=env)
        success = r.returncode == 0
        if success:
            print(f"[ROS2] 발행 완료: {config.ROS2_ORDER_TOPIC}")
        else:
            print(f"[ROS2] 발행 실패 (rc={r.returncode}): {r.stderr[:120]}")
    except subprocess.TimeoutExpired:
        print("[ROS2] 발행 timeout")
        success = False
    except Exception as e:
        print(f"[ROS2] 발행 예외: {e}")
        success = False

    # Firebase 로그 & 상태 업데이트
    level = "INFO" if success else "WARN"
    fb.push_log(level,
        f"ROS2 발행 {'성공' if success else '실패'}: "
        f"{recipe.get('recipe_name')} → {config.ROS2_ORDER_TOPIC}",
        source="flask_ros2")

    if success:
        fb.update_order_status(order_id, "published")
        fb.update_robot_state({
            "robot_status":   "COOKING",
            "current_recipe": recipe.get("recipe_id", ""),
            "current_step":   0,
        })

    return success

app = Flask(__name__)
CORS(app)


# ── Firebase & 로봇 초기화 ─────────────────────────────────────────────────────

def startup():
    fb.init()
    fb.push_log("INFO", "Flask 백엔드 서버 시작", source="system")
    fb.update_robot_state({
        "robot_status":   "IDLE",
        "gripper_status": "OPEN",
        "speed_scale":    30,
        "current_recipe": None,
        "current_step":   0,
        "error_code":     "NONE",
        "mode":           "STANDBY",
        "joint_positions": [0, 0, 90, 0, 90, 0],
        "tcp_position":   {"x": 0, "y": 0, "z": 0, "rx": 0, "ry": 0, "rz": 0},
    })
    rb.start_state_monitor()
    coord_client.start()   # 영속 ROS2 서비스 클라이언트 시작
    # _start_order_watcher() — ros2_order_publisher.py 가 전담, 중복 발행 방지


# ── 주문 감시 스레드 ───────────────────────────────────────────────────────────

_processing_orders: set = set()


def _start_order_watcher():
    """Firebase /orders 를 1초마다 폴링해 pending 주문 처리"""
    def _watcher():
        while True:
            try:
                orders = fb.get_orders(limit=20) or {}
                for order_id, order in orders.items():
                    if (order.get("status") == "pending"
                            and order_id not in _processing_orders):
                        _processing_orders.add(order_id)
                        t = threading.Thread(
                            target=_process_order,
                            args=(order_id, order),
                            daemon=True,
                        )
                        t.start()
            except Exception as e:
                fb.push_log("WARN", f"Order watcher error: {e}", source="system")
            time.sleep(1)

    threading.Thread(target=_watcher, daemon=True).start()


def _process_order(order_id: str, order: dict):
    """주문 → 레시피 실행 → 상태 업데이트"""
    recipe_id = order.get("recipe_id")
    fb.push_log("INFO", f"주문 수락: {order_id} / 레시피: {recipe_id}", source="system")
    fb.update_order_status(order_id, "accepted")

    recipe = fb.get_recipe(recipe_id)
    if not recipe:
        fb.push_log("WARN", f"레시피를 찾을 수 없음: {recipe_id}", source="system")
        fb.update_order_status(order_id, "failed")
        _processing_orders.discard(order_id)
        return

    fb.update_order_status(order_id, "processing", started_at=fb.now_iso())
    fb.update_robot_state({
        "robot_status":   "COOKING",
        "current_recipe": recipe_id,
        "current_step":   0,
    })
    fb.push_log("COOK", f"레시피 실행 시작: {recipe.get('recipe_name')}", source="robot")

    sequence  = recipe.get("sequence", [])
    locations = recipe.get("locations", {})
    failed    = False

    for step in sequence:
        step_num = step.get("step", 0)
        fb.update_robot_state({"current_step": step_num})
        ok = rb.execute_recipe_step(step, locations)
        if not ok:
            fb.push_log("ERROR", f"Step {step_num} 실패", source="robot")
            failed = True
            break

    final_status = "completed" if not failed else "failed"
    fb.update_order_status(order_id, final_status, completed_at=fb.now_iso())
    fb.update_robot_state({
        "robot_status":   "IDLE" if not failed else "ERROR",
        "current_recipe": None,
        "current_step":   0,
    })
    fb.push_log(
        "OK" if not failed else "ERROR",
        f"주문 {order_id} {'완료' if not failed else '실패'}",
        source="system",
    )
    _processing_orders.discard(order_id)


# ─────────────────────────────────────────────
#  REST API
# ─────────────────────────────────────────────

# ── /api/recipes ─────────────────────────────

@app.route("/api/recipes", methods=["GET"])
def list_recipes():
    return jsonify(fb.get_all_recipes())


@app.route("/api/recipes/<recipe_id>", methods=["GET"])
def get_recipe(recipe_id):
    r = fb.get_recipe(recipe_id)
    if r is None:
        abort(404, "Recipe not found")
    return jsonify(r)


@app.route("/api/recipes", methods=["POST"])
def create_recipe():
    data = request.get_json(force=True)
    recipe_id = data.get("recipe_id")
    if not recipe_id:
        abort(400, "recipe_id required")
    fb.upsert_recipe(recipe_id, data)
    return jsonify({"ok": True, "recipe_id": recipe_id}), 201


# ── /api/orders ──────────────────────────────

@app.route("/api/orders", methods=["GET"])
def list_orders():
    limit = int(request.args.get("limit", 50))
    return jsonify(fb.get_orders(limit))


@app.route("/api/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    o = fb.get_order(order_id)
    if o is None:
        abort(404, "Order not found")
    return jsonify(o)


@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.get_json(force=True)
    recipe_id = data.get("recipe_id")
    items     = data.get("items", [])
    total     = data.get("total", 0)
    if not recipe_id:
        abort(400, "recipe_id required")
    order_id = fb.create_order(recipe_id, items, total)
    fb.push_log("INFO", f"새 주문 접수: {order_id}", source="kiosk")
    # ROS2 발행은 ros2_order_publisher.py 가 Firebase 감시 후 1회 전담

    return jsonify({"ok": True, "order_id": order_id}), 201


@app.route("/api/orders/<order_id>/status", methods=["PATCH"])
def update_order_status(order_id):
    data   = request.get_json(force=True)
    status = data.get("status")
    if not status:
        abort(400, "status required")
    fb.update_order_status(order_id, status)
    return jsonify({"ok": True})


# ── /api/robot ───────────────────────────────

@app.route("/api/robot/state", methods=["GET"])
def robot_state():
    return jsonify(fb.get_robot_state())


@app.route("/api/robot/coords", methods=["GET"])
def get_robot_coords():
    """
    영속 ROS2 클라이언트로 /robo_chef/get_coords 서비스 호출.
    subprocess 방식 대신 process 내 persistent node를 사용해
    DDS participant 반복 생성/소멸 문제를 방지한다.
    반환: { "joint":[j1..j6], "tcp":[x,y,z,rx,ry,rz], "timestamp":"...", "source":"ros2" }
    """
    data = coord_client.get_coords(timeout=5.0)
    if "error" in data:
        return jsonify(data), 503
    data["source"] = "ros2"
    return jsonify(data)


@app.route("/api/robot/stop", methods=["POST"])
def robot_stop():
    ok = rb.stop_robot()
    fb.update_robot_state({"robot_status": "IDLE", "current_step": 0, "current_recipe": None})
    return jsonify({"ok": ok})


@app.route("/api/robot/home", methods=["POST"])
def robot_home():
    threading.Thread(
        target=lambda: rb.move_joint([0, 0, 90, 0, 90, 0]),
        daemon=True,
    ).start()
    fb.push_log("MOVE", "홈 포지션으로 이동", source="system")
    return jsonify({"ok": True})


@app.route("/api/robot/mode/autonomous", methods=["POST"])
def set_autonomous():
    ok = rb.set_robot_mode_autonomous()
    fb.update_robot_state({"mode": "AUTONOMOUS" if ok else "MANUAL"})
    return jsonify({"ok": ok})


# ── /api/logs ────────────────────────────────

@app.route("/api/logs", methods=["GET"])
def get_logs():
    limit = int(request.args.get("limit", 50))
    return jsonify(fb.get_recent_logs(limit))


@app.route("/api/logs", methods=["POST"])
def add_log():
    """ROS2 노드 등 외부에서 로그를 전송할 엔드포인트"""
    data = request.get_json(force=True)
    fb.push_log(
        data.get("level", "INFO"),
        data.get("message", ""),
        data.get("source", "external"),
    )
    return jsonify({"ok": True}), 201


# ── Health check ─────────────────────────────

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "robo-chef-backend"})


# ── ROS2 토픽 발행 (수동 트리거 / 테스트용) ──

@app.route("/api/ros2/publish_order", methods=["POST"])
def ros2_publish_order():
    """
    주문 ID를 받아 해당 레시피를 ROS2 토픽으로 즉시 발행.
    Body: { "order_id": "ORD_XXXXX" }
    """
    data     = request.get_json(force=True)
    order_id = data.get("order_id")
    if not order_id:
        abort(400, "order_id required")

    order = fb.get_order(order_id)
    if not order:
        abort(404, "Order not found")

    recipe_id = order.get("recipe_id")
    recipe    = fb.get_recipe(recipe_id) if recipe_id else None
    if not recipe:
        abort(404, "Recipe not found")

    ok = _ros2_publish_order(order_id, order, recipe)
    return jsonify({"ok": ok, "topic": config.ROS2_ORDER_TOPIC})


@app.route("/api/ros2/status", methods=["GET"])
def ros2_status():
    """ROS2 토픽 설정 및 상태 반환."""
    import subprocess, os
    env = os.environ.copy()
    env["ROS_DOMAIN_ID"] = str(config.ROS2_DOMAIN_ID)
    # 토픽 목록 확인 (타임아웃 2초)
    try:
        r = subprocess.run(
            ["ros2", "topic", "list"],
            capture_output=True, text=True, timeout=3, env=env
        )
        topics = r.stdout.strip().split("\n") if r.returncode == 0 else []
    except Exception:
        topics = []

    return jsonify({
        "order_topic":   config.ROS2_ORDER_TOPIC,
        "status_topic":  config.ROS2_STATUS_TOPIC,
        "domain_id":     config.ROS2_DOMAIN_ID,
        "msg_format":    config.ORDER_MSG_FORMAT,
        "active_topics": topics,
    })


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    startup()
    app.run(
        host=config.FLASK_HOST,
        port=config.FLASK_PORT,
        debug=config.FLASK_DEBUG,
        use_reloader=False,
    )
