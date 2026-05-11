"""
ros2_srv_call.py — std_srvs/Trigger 서비스 호출 후 JSON 출력.
Flask subprocess에서 호출된다.

사용:
  python3 ros2_srv_call.py <service_name>
출력:
  (stdout) JSON 문자열 — response.message 또는 {"error":"..."}
"""
import sys
import json
import os


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: ros2_srv_call.py <service_name>"}))
        sys.exit(1)

    service_name = sys.argv[1]

    import rclpy
    from std_srvs.srv import Trigger

    rclpy.init()
    node = rclpy.create_node(f"rc_srv_client_{os.getpid()}")
    client = node.create_client(Trigger, service_name)

    # 서비스 대기 (최대 4초)
    if not client.wait_for_service(timeout_sec=4.0):
        print(json.dumps({"error": f"서비스 없음: {service_name}"}))
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
        sys.exit(1)

    future = client.call_async(Trigger.Request())
    rclpy.spin_until_future_complete(node, future, timeout_sec=5.0)

    if future.done():
        result = future.result()
        if result.success:
            # result.message 가 JSON 문자열
            print(result.message)
        else:
            print(json.dumps({"error": result.message}))
    else:
        print(json.dumps({"error": "timeout"}))

    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


if __name__ == "__main__":
    main()
