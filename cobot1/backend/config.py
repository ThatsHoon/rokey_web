# ──────────────────────────────────────────────
#  ROBO CHEF — Backend Configuration
#  여기에 Firebase 프로젝트 정보를 입력하세요
# ──────────────────────────────────────────────

# Firebase Admin SDK 서비스 계정 키 파일 경로
# Firebase Console → 프로젝트 설정 → 서비스 계정 → 새 비공개 키 생성
SERVICE_ACCOUNT_KEY = "serviceAccountKey.json"

# Firebase Realtime Database URL
# Firebase Console → Realtime Database → 데이터 탭 상단 URL
FIREBASE_DATABASE_URL = "https://robochef-5d9b6-default-rtdb.asia-southeast1.firebasedatabase.app"

# Flask 서버 설정
FLASK_HOST  = "0.0.0.0"
FLASK_PORT  = 5000
FLASK_DEBUG = False

# ROS2 설정
ROS2_NAMESPACE   = "dsr01"           # 두산 로봇 네임스페이스
ROS2_SOURCE_PATH = "/opt/ros/humble/setup.bash"   # ROS2 환경 소스 경로

# 로봇 상태 업데이트 주기 (초)
STATE_UPDATE_INTERVAL = 0.5          # 2Hz

# Firebase에 유지할 최대 로그 수
MAX_LOG_ENTRIES = 200

# ── ROS2 토픽 설정 ──────────────────────────────────────────
# 서브노드(이 PC) → 메인노드(로봇 팔 PC) 주문 전달 토픽
ROS2_ORDER_TOPIC   = "/robo_chef/order_request"   # 메인노드가 구독할 토픽 이름
ROS2_STATUS_TOPIC  = "/robo_chef/robot_status"    # 메인노드가 발행할 상태 토픽
ROS2_DOMAIN_ID     = 25                           # ROS_DOMAIN_ID (양쪽 동일해야 함)

# 주문 메시지 포맷
# data: '{"order_id":"...", "recipe_name":"...", "recipe_id":"...", "sequence":[...]}'
ORDER_MSG_FORMAT = "json"   # "json" | "simple" (simple = "recipe_name|recipe_id")
