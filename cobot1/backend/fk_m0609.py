"""M0609 Forward Kinematics — joint(deg) → TCP(mm, deg).

기반: dsr_description2 / xacro.macro.m0609.blue.xacro 의 URDF origin/joint 체인.
admin UI 의 JS 구현과 동일한 규약:
    - 각 joint 회전축은 local +Z
    - URDF origin = T(xyz) · Rz(yaw) · Ry(pitch) · Rx(roll)
    - 반환 RPY 는 Rz(yaw)·Ry(pitch)·Rx(roll) 분해 (ZYX euler)
    - 위치는 m → mm 로 환산, 각도는 rad → deg

의존성 없음 (numpy 안 씀). math + list 만 사용.
"""

import math

# (rpy_rad, xyz_m) — joint_1 ~ joint_6
# rpy ±π/2 는 URDF 원본 xacro 의 정확한 π/2 이지만 일부 문서엔 1.571 로 근사됨.
# 여기서는 정밀도를 위해 math.pi/2 사용.
_PI2 = math.pi / 2.0

M0609_CHAIN = [
    ([0.0,    0.0,    0.0  ], [0.0,    0.0,    0.1345]),   # joint_1
    ([0.0,   -_PI2,  -_PI2 ], [0.0,    0.0062, 0.0   ]),   # joint_2
    ([0.0,    0.0,    _PI2 ], [0.411,  0.0,    0.0   ]),   # joint_3
    ([_PI2,   0.0,    0.0  ], [0.0,   -0.368,  0.0   ]),   # joint_4
    ([-_PI2,  0.0,    0.0  ], [0.0,    0.0,    0.0   ]),   # joint_5
    ([_PI2,   0.0,    0.0  ], [0.0,   -0.121,  0.0   ]),   # joint_6
]


def _mat_mul(A, B):
    """4×4 행렬 곱 (list of list)."""
    C = [[0.0]*4 for _ in range(4)]
    for i in range(4):
        for j in range(4):
            s = 0.0
            for k in range(4):
                s += A[i][k] * B[k][j]
            C[i][j] = s
    return C


def _origin(rpy, xyz):
    r, p, y = rpy
    cr, sr = math.cos(r), math.sin(r)
    cp, sp = math.cos(p), math.sin(p)
    cy, sy = math.cos(y), math.sin(y)
    return [
        [cy*cp, cy*sp*sr - sy*cr, cy*sp*cr + sy*sr, xyz[0]],
        [sy*cp, sy*sp*sr + cy*cr, sy*sp*cr - cy*sr, xyz[1]],
        [  -sp, cp*sr,            cp*cr,            xyz[2]],
        [  0.0, 0.0,              0.0,              1.0   ],
    ]


def _rotz(theta):
    c, s = math.cos(theta), math.sin(theta)
    return [
        [c, -s, 0.0, 0.0],
        [s,  c, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def compute_tcp_from_joint(joint_deg):
    """joint_deg (6, deg) → TCP dict {x, y, z (mm), rx, ry, rz (deg)}."""
    T = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]
    for i, (rpy, xyz) in enumerate(M0609_CHAIN):
        T = _mat_mul(T, _origin(rpy, xyz))
        theta_rad = math.radians(joint_deg[i] if i < len(joint_deg) else 0.0)
        T = _mat_mul(T, _rotz(theta_rad))

    r2d   = 180.0 / math.pi
    def clamp(v): return max(-1.0, min(1.0, v))
    pitch = math.asin(-clamp(T[2][0]))
    roll  = math.atan2(T[2][1], T[2][2])
    yaw   = math.atan2(T[1][0], T[0][0])

    return {
        "x":  T[0][3] * 1000.0,
        "y":  T[1][3] * 1000.0,
        "z":  T[2][3] * 1000.0,
        "rx": roll  * r2d,
        "ry": pitch * r2d,
        "rz": yaw   * r2d,
    }


if __name__ == "__main__":
    # 자체 검증 — DSR Fkin 결과와 비교
    # joint=[0,0,0,0,0,0] 에서 공식 Fkin 응답: XYZ=[0, 6.25, 1035]mm
    for j in ([0, 0, 0, 0, 0, 0], [0, 0, 90, 0, 90, 0], [45, -30, 60, 0, 45, 0]):
        tcp = compute_tcp_from_joint(j)
        print(f"joint={j} → "
              f"X={tcp['x']:.2f}mm Y={tcp['y']:.2f}mm Z={tcp['z']:.2f}mm "
              f"Rx={tcp['rx']:.2f}° Ry={tcp['ry']:.2f}° Rz={tcp['rz']:.2f}°")
