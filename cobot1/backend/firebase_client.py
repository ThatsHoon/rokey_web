"""
Firebase Admin SDK 초기화 및 공통 헬퍼
"""
import os
import firebase_admin
from firebase_admin import credentials, db
from datetime import datetime, timezone
import uuid

import config

_initialized = False


def init():
    global _initialized
    if _initialized:
        return
    key_path = os.path.join(os.path.dirname(__file__), config.SERVICE_ACCOUNT_KEY)
    cred = credentials.Certificate(key_path)
    firebase_admin.initialize_app(cred, {
        "databaseURL": config.FIREBASE_DATABASE_URL
    })
    _initialized = True


def ref(path: str):
    return db.reference(path)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Recipes ───────────────────────────────────────────────────────────────────

def get_all_recipes() -> dict:
    return ref("/recipes").get() or {}


def get_recipe(recipe_id: str) -> dict | None:
    return ref(f"/recipes/{recipe_id}").get()


def upsert_recipe(recipe_id: str, data: dict):
    ref(f"/recipes/{recipe_id}").set(data)


# ── Orders ────────────────────────────────────────────────────────────────────

def create_order(recipe_id: str, items: list, total: int) -> str:
    order_id = f"ORD_{uuid.uuid4().hex[:8].upper()}"
    ref(f"/orders/{order_id}").set({
        "recipe_id":  recipe_id,
        "items":      items,
        "total":      total,
        "status":     "pending",
        "order_time": now_iso(),
        "started_at":    None,
        "completed_at":  None,
    })
    return order_id


def update_order_status(order_id: str, status: str, **kwargs):
    data = {"status": status, **kwargs}
    ref(f"/orders/{order_id}").update(data)


def get_orders(limit: int = 50) -> dict:
    # order_by_child 는 Firebase 인덱스 필요 → 전체 조회 후 Python 정렬
    all_orders = ref("/orders").get() or {}
    sorted_orders = dict(
        sorted(all_orders.items(),
               key=lambda x: x[1].get("order_time", "") if isinstance(x[1], dict) else "")
    )
    items = list(sorted_orders.items())
    return dict(items[-limit:]) if len(items) > limit else sorted_orders


def get_order(order_id: str) -> dict | None:
    return ref(f"/orders/{order_id}").get()


# ── Robot State ───────────────────────────────────────────────────────────────

def update_robot_state(state: dict):
    ref("/robot_state").update({**state, "last_updated": now_iso()})


def get_robot_state() -> dict:
    return ref("/robot_state").get() or {}


# ── Logs ──────────────────────────────────────────────────────────────────────

def push_log(level: str, message: str, source: str = "system"):
    log_id = f"LOG_{uuid.uuid4().hex[:12]}"
    ref(f"/logs/{log_id}").set({
        "timestamp": now_iso(),
        "level":     level,
        "message":   message,
        "source":    source,
    })
    # 최대 MAX_LOG_ENTRIES 유지
    _prune_logs()


def _prune_logs():
    logs = ref("/logs").order_by_key().get()
    if logs and len(logs) > config.MAX_LOG_ENTRIES:
        keys = sorted(logs.keys())
        for old_key in keys[: len(logs) - config.MAX_LOG_ENTRIES]:
            ref(f"/logs/{old_key}").delete()


def get_recent_logs(limit: int = 50) -> list:
    logs = ref("/logs").order_by_key().limit_to_last(limit).get() or {}
    return sorted(logs.values(), key=lambda x: x.get("timestamp", ""))
