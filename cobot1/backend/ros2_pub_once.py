"""
ros2_pub_once.py — 구독자 없이 즉시 1회 발행하는 경량 스크립트.
Flask subprocess에서 호출된다.

사용:
  python3 ros2_pub_once.py <topic> <data_string>
"""
import sys, time, os

def main():
    if len(sys.argv) < 3:
        print("Usage: ros2_pub_once.py <topic> <data>", file=sys.stderr)
        sys.exit(1)

    topic = sys.argv[1]
    data  = sys.argv[2]

    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, DurabilityPolicy, ReliabilityPolicy
    from std_msgs.msg import String

    rclpy.init()
    node = rclpy.create_node(f"rc_pub_once_{os.getpid()}")

    # transient_local(latched) QoS — 늦게 연결된 구독자도 수신
    qos = QoSProfile(
        depth=1,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        reliability=ReliabilityPolicy.RELIABLE,
    )
    pub = node.create_publisher(String, topic, qos)

    msg      = String()
    msg.data = data

    # 연결 대기 후 발행 (3회 보장)
    for _ in range(3):
        pub.publish(msg)
        rclpy.spin_once(node, timeout_sec=0.08)
        time.sleep(0.08)

    node.destroy_node()
    rclpy.shutdown()
    print(f"[pub_once] {topic} 발행 완료")

if __name__ == "__main__":
    main()
