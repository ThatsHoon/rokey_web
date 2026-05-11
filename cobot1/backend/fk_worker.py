"""fk_worker — Firebase /robot_state.joint_positions → FK → /robot_state.tcp_position.

설계
  - 서브노드(현재 PC) 에서 상시 돌면서 joint_positions 을 폴링.
  - 변경이 있으면 m0609 FK 로 TCP 를 재계산해 `/robot_state.tcp_position` 에 merge-update.
  - firebase_admin listen 은 SSE 가 조용해지는 이슈가 있어 폴링 기반.
  - 기본 주기 2Hz — coord_service 의 /robot_state.joint_positions publish 주기와 동기.

실행
  python3 -u fk_worker.py
  (또는 app.py 기동 시 별도 스레드로 start_fk_worker() 호출)

의존성
  firebase-admin (pip install firebase-admin)
"""

import os
import sys
import time
from datetime import datetime, timezone

import firebase_admin
from firebase_admin import credentials, db

from fk_m0609 import compute_tcp_from_joint

DATABASE_URL    = "https://robochef-5d9b6-default-rtdb.asia-southeast1.firebasedatabase.app"
CRED_PATH       = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "serviceAccountKey.json")

POLL_HZ         = 2               # Hz — joint_positions 폴링 주기
ROUND_DECIMALS  = 3               # mm/deg 반올림 자리수


def _init_firebase():
    if firebase_admin._apps:
        return
    if not os.path.isfile(CRED_PATH):
        print(f"[fk_worker] ❌ credential 없음: {CRED_PATH}", flush=True)
        raise FileNotFoundError(CRED_PATH)
    firebase_admin.initialize_app(
        credentials.Certificate(CRED_PATH),
        {"databaseURL": DATABASE_URL},
    )


def _round(d):
    return {k: round(float(v), ROUND_DECIMALS) for k, v in d.items()}


def run():
    _init_firebase()
    joint_ref = db.reference("/robot_state/joint_positions")
    tcp_ref   = db.reference("/robot_state")

    interval      = 1.0 / POLL_HZ
    last_joint    = None
    err_streak    = 0
    push_count    = 0
    print(f"[fk_worker] 시작 — /robot_state.joint_positions → FK → tcp_position "
          f"@ {POLL_HZ}Hz", flush=True)

    while True:
        t0 = time.monotonic()
        try:
            j = joint_ref.get()
            if isinstance(j, list) and len(j) >= 6:
                key = tuple(round(float(x), 2) for x in j[:6])       # 소수 2자리 dedup
                if key != last_joint:
                    tcp = _round(compute_tcp_from_joint(j[:6]))
                    tcp_ref.update({
                        "tcp_position": tcp,
                        "tcp_updated_at": datetime.now(timezone.utc).isoformat(),
                    })
                    last_joint = key
                    push_count += 1
                    if push_count <= 3 or push_count % 50 == 0:
                        print(f"[fk_worker] #{push_count} joint={key} → "
                              f"X={tcp['x']:.1f} Y={tcp['y']:.1f} Z={tcp['z']:.1f}",
                              flush=True)
            if err_streak:
                err_streak = 0
        except Exception as e:
            err_streak += 1
            if err_streak in (1, 10, 100):
                print(f"[fk_worker] 예외 (x{err_streak}): {e}", flush=True)

        time.sleep(max(0.0, interval - (time.monotonic() - t0)))


def start_fk_worker_thread():
    """app.py 에서 별 스레드로 기동할 때 사용."""
    import threading
    t = threading.Thread(target=run, daemon=True, name="fk_worker")
    t.start()
    return t


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        print("\n[fk_worker] 종료", flush=True)
        sys.exit(0)
