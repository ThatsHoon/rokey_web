"""
Firebase에 초기 레시피 데이터를 업로드하는 스크립트.
실행: python3 recipe_seeder.py
"""
import firebase_client as fb

RECIPES = [
    {
        "recipe_id":    "KIMCHI_STEW_001",
        "recipe_name":  "김치찌개",
        "total_steps":  12,
        "locations": {
            "INGREDIENTS": {
                "KIMCHI": {"id": "ST_01", "coord": [450.0, -100.0, 50.0], "type": "tray"},
                "PORK":   {"id": "ST_02", "coord": [450.0,    0.0, 50.0], "type": "tray"},
                "TOFU":   {"id": "ST_03", "coord": [450.0,  100.0, 50.0], "type": "tray"},
                "WATER":  {"id": "ST_04", "coord": [450.0,  200.0, 50.0], "type": "tray"},
            },
            "TOOLS": {
                "POT":   {"id": "T_01", "coord": [200.0, 300.0,  0.0], "type": "jig"},
                "LADLE": {"id": "T_02", "coord": [100.0, 300.0, 20.0], "type": "holder"},
            },
            "COOKING_ZONE": {
                "INDUCTION": {
                    "id":           "CZ_01",
                    "coord":        [0.0, 500.0, 10.0],
                    "temp_control": True,
                }
            },
        },
        "sequence": [
            {"step": 1,  "action": "HOME",    "target": None,     "description": "홈 포지션 이동"},
            {"step": 2,  "action": "PICK_UP", "target": "KIMCHI", "params": {"force": 5.0, "speed": 0.5},  "description": "김치 트레이 픽업"},
            {"step": 3,  "action": "MOVE_TO", "target": "POT",    "path": "JOINT_MOVE",                    "description": "냄비 위치로 이동"},
            {"step": 4,  "action": "PLACE",   "target": "POT",    "params": {"release_delay": 1.0},        "description": "냄비에 김치 투하"},
            {"step": 5,  "action": "PICK_UP", "target": "PORK",   "params": {"force": 6.0, "speed": 0.5},  "description": "돼지고기 픽업"},
            {"step": 6,  "action": "MOVE_TO", "target": "POT",    "path": "JOINT_MOVE",                    "description": "냄비로 이동"},
            {"step": 7,  "action": "PLACE",   "target": "POT",    "params": {"release_delay": 0.8},        "description": "냄비에 돼지고기 투하"},
            {"step": 8,  "action": "PICK_UP", "target": "TOFU",   "params": {"force": 3.0, "speed": 0.3},  "description": "두부 픽업 (약한 파지)"},
            {"step": 9,  "action": "MOVE_TO", "target": "POT",    "path": "JOINT_MOVE",                    "description": "냄비로 이동"},
            {"step": 10, "action": "PLACE",   "target": "POT",    "params": {"release_delay": 0.5},        "description": "냄비에 두부 투하"},
            {"step": 11, "action": "WAIT",    "target": None,     "params": {"duration": 5.0},             "description": "조리 대기 (5초)"},
            {"step": 12, "action": "HOME",    "target": None,     "description": "완료 후 홈 포지션"},
        ],
    },
    {
        "recipe_id":    "RAMEN_001",
        "recipe_name":  "신라면",
        "total_steps":  8,
        "locations": {
            "INGREDIENTS": {
                "RAMEN_NOODLE": {"id": "ST_05", "coord": [450.0, -200.0, 50.0], "type": "tray"},
                "SOUP_POWDER":  {"id": "ST_06", "coord": [450.0, -300.0, 50.0], "type": "tray"},
                "WATER":        {"id": "ST_04", "coord": [450.0,  200.0, 50.0], "type": "tray"},
            },
            "TOOLS": {
                "POT":   {"id": "T_01", "coord": [200.0, 300.0,  0.0], "type": "jig"},
            },
            "COOKING_ZONE": {
                "INDUCTION": {
                    "id":           "CZ_01",
                    "coord":        [0.0, 500.0, 10.0],
                    "temp_control": True,
                }
            },
        },
        "sequence": [
            {"step": 1, "action": "HOME",    "target": None,          "description": "홈 포지션"},
            {"step": 2, "action": "PICK_UP", "target": "RAMEN_NOODLE","params": {"force": 4.0, "speed": 0.5}, "description": "라면 면 픽업"},
            {"step": 3, "action": "MOVE_TO", "target": "POT",         "path": "JOINT_MOVE",                   "description": "냄비로 이동"},
            {"step": 4, "action": "PLACE",   "target": "POT",         "params": {"release_delay": 0.8},        "description": "면 투입"},
            {"step": 5, "action": "PICK_UP", "target": "SOUP_POWDER", "params": {"force": 2.0, "speed": 0.4}, "description": "스프 픽업"},
            {"step": 6, "action": "MOVE_TO", "target": "POT",         "path": "JOINT_MOVE",                   "description": "냄비로 이동"},
            {"step": 7, "action": "PLACE",   "target": "POT",         "params": {"release_delay": 1.2},        "description": "스프 투입"},
            {"step": 8, "action": "HOME",    "target": None,           "description": "완료 후 홈"},
        ],
    },
]


def seed():
    print("Firebase 초기화 중...")
    fb.init()
    print(f"레시피 {len(RECIPES)}개 업로드 시작...")
    for recipe in RECIPES:
        recipe_id = recipe["recipe_id"]
        fb.upsert_recipe(recipe_id, recipe)
        print(f"  ✓ {recipe_id} — {recipe['recipe_name']}")
    print("완료!")


if __name__ == "__main__":
    seed()
