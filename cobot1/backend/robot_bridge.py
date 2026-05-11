"""
ROS2 ↔ Flask 브릿지
subprocess로 ros2 CLI를 호출해 관절 상태를 읽고
서비스 콜로 로봇에 명령을 전송한다.
"""
import subprocess
import os
import json
import re
import time
import threading
import yaml

import config
import firebase_client as fb

# ROS2 환경 변수 (source 없이 subprocess에서 사용하기 위해 직접 설정)
_ROS2_ENV = None


def _get_ros2_env() -> dict:
    global _ROS2_ENV
    if _ROS2_ENV is not None:
        return _ROS2_ENV
    env = os.environ.copy()
    # ROS2 환경 변수 직접 주입
    env["ROS_DISTRO"]          = "humble"
    env["AMENT_PREFIX_PATH"]   = "/opt/ros/humble"
    env["ROS_PACKAGE_PATH"]    = "/opt/ros/humble/share"
    env["PYTHONPATH"]          = (
        "/opt/ros/humble/lib/python3.10/site-packages:"
        + env.get("PYTHONPATH", "")
    )
    env["PATH"]                = f"/opt/ros/humble/bin:{env['PATH']}"
    env["LD_LIBRARY_PATH"]     = (
        "/opt/ros/humble/lib:"
        + env.get("LD_LIBRARY_PATH", "")
    )
    _ROS2_ENV = env
    return env


def _run(cmd: list, timeout: int = 5) -> tuple[int, str, str]:
    """ROS2 명령 실행 후 (returncode, stdout, stderr) 반환"""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_get_ros2_env(),
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except Exception as e:
        return -1, "", str(e)


# ── Joint State 읽기 ───────────────────────────────────────────────────────────

def get_joint_states() -> list[float] | None:
    """
    /dsr01/joint_states 토픽에서 관절 각도 [deg] 읽기.
    ROS2 JointState 메시지의 position 필드 (rad) → deg 변환.
    """
    rc, out, err = _run(
        ["ros2", "topic", "echo", "--once",
         f"/{config.ROS2_NAMESPACE}/joint_states",
         "sensor_msgs/msg/JointState"],
        timeout=4,
    )
    if rc != 0 or not out:
        return None
    # position: [r1, r2, r3, r4, r5, r6]
    pos_match = re.search(r"position:\s*\[([\d\s.,eE+\-]+)\]", out)
    if not pos_match:
        return None
    try:
        rads = [float(v.strip()) for v in pos_match.group(1).split(",")]
        import math
        degs = [round(math.degrees(r), 3) for r in rads]
        return degs[:6]
    except Exception:
        return None


def get_current_tcp() -> dict | None:
    """
    /dsr01/aux_control/get_current_posx 서비스 호출로 TCP 위치 읽기.
    """
    rc, out, _ = _run(
        ["ros2", "service", "call",
         f"/{config.ROS2_NAMESPACE}/aux_control/get_current_posx",
         "dsr_msgs2/srv/GetCurrentPosx", "{}"],
        timeout=5,
    )
    if rc != 0 or not out:
        return None
    # pos 필드 파싱
    pos_match = re.search(r"pos:\s*\[([\d\s.,eE+\-]+)\]", out)
    if not pos_match:
        return None
    try:
        vals = [float(v.strip()) for v in pos_match.group(1).split(",")]
        if len(vals) >= 6:
            return {"x": vals[0], "y": vals[1], "z": vals[2],
                    "rx": vals[3], "ry": vals[4], "rz": vals[5]}
    except Exception:
        pass
    return None


def get_robot_mode() -> str:
    """로봇 모드 조회"""
    rc, out, _ = _run(
        ["ros2", "service", "call",
         f"/{config.ROS2_NAMESPACE}/system/get_robot_mode",
         "dsr_msgs2/srv/GetRobotMode", "{}"],
        timeout=5,
    )
    if rc != 0:
        return "UNKNOWN"
    if "AUTONOMOUS" in out or "1" in out:
        return "AUTONOMOUS"
    return "MANUAL"


# ── 로봇 명령 ─────────────────────────────────────────────────────────────────

def move_joint(positions: list[float], vel: float = 30, acc: float = 60,
               time_sec: float = 0, sync: int = 1) -> bool:
    """관절 공간 이동 (동기)"""
    pos_str = ", ".join(str(p) for p in positions)
    req = (
        f"{{pos: [{pos_str}], vel: {vel}, acc: {acc},"
        f" time: {time_sec}, radius: 0.0, mode: 0,"
        f" blend_type: 0, sync_type: {sync}}}"
    )
    rc, out, err = _run(
        ["ros2", "service", "call",
         f"/{config.ROS2_NAMESPACE}/motion/move_joint",
         "dsr_msgs2/srv/MoveJoint", req],
        timeout=30,
    )
    success = rc == 0 and "success: True" in out
    fb.push_log(
        "MOVE" if success else "WARN",
        f"move_joint {positions} → {'OK' if success else 'FAIL: '+err[:60]}",
        source="robot",
    )
    return success


def move_line(pos: list[float], vel: list[float] | None = None,
              acc: list[float] | None = None, ref: int = 0) -> bool:
    """직선 이동"""
    vel = vel or [100, 50]
    acc = acc or [200, 100]
    pos_str = ", ".join(str(p) for p in pos)
    vel_str = ", ".join(str(v) for v in vel)
    acc_str = ", ".join(str(a) for a in acc)
    req = (
        f"{{pos: [{pos_str}], vel: [{vel_str}], acc: [{acc_str}],"
        f" time: 0.0, radius: 0.0, ref: {ref},"
        f" mode: 0, blend_type: 0, sync_type: 1}}"
    )
    rc, out, err = _run(
        ["ros2", "service", "call",
         f"/{config.ROS2_NAMESPACE}/motion/move_line",
         "dsr_msgs2/srv/MoveLine", req],
        timeout=30,
    )
    return rc == 0 and "success: True" in out


def stop_robot() -> bool:
    """로봇 즉시 정지"""
    rc, _, _ = _run(
        ["ros2", "service", "call",
         f"/{config.ROS2_NAMESPACE}/motion/move_stop",
         "dsr_msgs2/srv/MoveStop",
         "{stop_mode: 1}"],
        timeout=5,
    )
    fb.push_log("WARN", "Emergency stop requested", source="system")
    return rc == 0


def set_robot_mode_autonomous() -> bool:
    rc, out, _ = _run(
        ["ros2", "service", "call",
         f"/{config.ROS2_NAMESPACE}/system/set_robot_mode",
         "dsr_msgs2/srv/SetRobotMode",
         "{robot_mode: 1}"],
        timeout=5,
    )
    return rc == 0 and "success: True" in out


# ── 레시피 실행 ───────────────────────────────────────────────────────────────

def execute_recipe_step(step: dict, locations: dict) -> bool:
    """레시피 sequence 항목 하나를 실행"""
    action = step.get("action", "")
    target = step.get("target", "")
    params = step.get("params", {})
    desc   = step.get("description", action)

    fb.push_log("COOK", f"Step {step.get('step')}: {desc}", source="robot")

    # 좌표 조회
    coord = _find_coord(target, locations)

    if action == "PICK_UP":
        if coord is None:
            return False
        success = move_line(coord + [0, 180, 0])
        if success:
            fb.push_log("GRIP", f"파지 완료: {target}", source="robot")
        return success

    elif action == "MOVE_TO":
        if coord is None:
            return False
        path = step.get("path", "JOINT_MOVE")
        if path == "JOINT_MOVE":
            # TCP 목표를 ikin으로 변환하는 대신 단순 movel 사용
            return move_line(coord + [0, 180, 0])
        return move_line(coord + [0, 180, 0])

    elif action == "PLACE":
        if coord is None:
            return True   # 대상 없으면 skip
        delay = params.get("release_delay", 0.5)
        success = move_line(coord + [0, 180, 0])
        if success:
            time.sleep(delay)
            fb.push_log("GRIP", f"투하 완료: {target}", source="robot")
        return success

    elif action == "WAIT":
        duration = params.get("duration", 1.0)
        time.sleep(duration)
        return True

    elif action == "HOME":
        return move_joint([0, 0, 90, 0, 90, 0])

    fb.push_log("WARN", f"알 수 없는 action: {action}", source="system")
    return False


def _find_coord(target: str, locations: dict) -> list[float] | None:
    """locations 딕셔너리에서 target 좌표 반환"""
    for category in locations.values():
        if isinstance(category, dict) and target in category:
            return category[target].get("coord")
    return None


# ── 상태 모니터 스레드 ─────────────────────────────────────────────────────────

_monitor_running = False
_monitor_thread: threading.Thread | None = None


def start_state_monitor():
    """백그라운드에서 로봇 상태를 Firebase에 주기적으로 업데이트"""
    global _monitor_running, _monitor_thread
    if _monitor_running:
        return
    _monitor_running = True
    _monitor_thread = threading.Thread(target=_monitor_loop, daemon=True)
    _monitor_thread.start()


def stop_state_monitor():
    global _monitor_running
    _monitor_running = False


def _monitor_loop():
    fb.push_log("INFO", "로봇 상태 모니터 시작", source="system")
    while _monitor_running:
        try:
            state: dict = {}

            joints = get_joint_states()
            if joints:
                state["joint_positions"] = joints

            tcp = get_current_tcp()
            if tcp:
                state["tcp_position"] = tcp

            if state:
                fb.update_robot_state(state)

        except Exception as e:
            pass  # 연결 실패 시 조용히 넘김

        time.sleep(config.STATE_UPDATE_INTERVAL)
