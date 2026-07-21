"""
Server dieu khien xe (thay the vai tro cua ESP32 AP+WebServer trong main.cpp).

Kien truc:
- ESP32 (xe) la CLIENT socket.io, ket noi toi server nay ngay khi khoi dong.
- Trinh duyet (web dieu khien) cung la CLIENT socket.io.
- Server la trung tam: nhan lenh tu web (hoac tu bat ky nguon nao khac goi
  'web_command'), luu lich su 10 lenh gan nhat, chuyen tiep lenh xuong xe
  that (neu dang ket noi), va chay mot vong lap mo phong vi tri/huong xe
  (dead-reckoning) de ve len man hinh radar.

Chay: pip install -r requirements.txt && python app.py
Mo trinh duyet: http://<ip-may-chay-server>:5000
"""

import math
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config["SECRET_KEY"] = "xe-nhomphuc-secret"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ============================================================
# TRANG THAI XE (dung chung, bao ve boi lock vi co thread mo phong)
# ============================================================
state_lock = threading.Lock()
state = {
    "car_connected": False,
    "car_sid": None,
    "running": False,          # tuong ung dangChay ben firmware
    "speed": 120,                # 40-200, tuong ung bien tocDo
    "mode": "dung",             # tien / lui / retrai / rephai / xoaytrai / xoayphai / dung
    "prev_linear": "tien",      # huong thang gan nhat, de quay lai sau khi re
    "x": 300.0,
    "y": 300.0,
    "heading": 0.0,              # do, 0 = huong sang phai, tang = quay theo chieu kim dong ho
    "mic_connected": False,      # ESP32-S3 mic (Edge Impulse) co dang ket noi toi xe khong
    "android_connected": False,  # App Android co dang ket noi toi xe khong
}

HISTORY_LEN = 10
history = deque(maxlen=HISTORY_LEN)

# Log nhan dien THO cua mic (bao gom ca noise/tin cay thap), tach rieng
# khoi lich su 10 lenh (history) vi tan suat day dac hon nhieu
MIC_LOG_LEN = 30
mic_log = deque(maxlen=MIC_LOG_LEN)

# hang so mo phong - chinh cho khop voi xe that neu can
MAX_LINEAR_SPEED_PX_S = 90.0    # px/giay khi speed = 255
TURN_ARC_S = 2.1                 # tuong ung NHIP_RE = 2100ms trong firmware (da chinh de re ~90 do)
TURN_RATE_DEG_S = 55.0           # toc do doi huong khi re trai/phai (banh xe re quay tai cho)
ROTATE_RATE_DEG_S = 110.0        # toc do xoay tai cho (xoaytrai/xoayphai)
CANVAS_W, CANVAS_H = 600, 600

TEN_LENH = {
    "tien": "Tien", "lui": "Lui", "retrai": "Re trai", "rephai": "Re phai",
    "xoaytrai": "Xoay trai", "xoayphai": "Xoay phai", "dung": "Dung",
}


def snapshot():
    with state_lock:
        return {
            "car_connected": state["car_connected"],
            "running": state["running"],
            "speed": state["speed"],
            "mode": state["mode"],
            "x": state["x"], "y": state["y"], "heading": state["heading"],
            "mic_connected": state["mic_connected"],
            "android_connected": state["android_connected"],
        }


def them_lich_su(nhan, nguon):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "cmd": nhan, "source": nguon}
    history.appendleft(entry)
    socketio.emit("history_update", list(history))


def gui_xuong_xe(payload):
    with state_lock:
        connected, sid = state["car_connected"], state["car_sid"]
    if connected and sid:
        socketio.emit("command", payload, to=sid)


def ap_dung_lenh_dieu_khien(cmd, nguon):
    """Cap nhat trang thai theo mot lenh dieu khien huong (tien/lui/re/xoay/dung)."""
    if cmd not in TEN_LENH:
        return
    with state_lock:
        is_running = state["running"]
        if is_running:
            if cmd in ("tien", "lui"):
                state["prev_linear"] = cmd
            if cmd in ("retrai", "rephai"):
                state["_turn_deadline"] = time.time() + TURN_ARC_S
            state["mode"] = cmd
    if not is_running:
        socketio.emit("error_msg", {"msg": "Xe chua BAT DAU, khong nhan lenh."})
        return
    them_lich_su(TEN_LENH[cmd], nguon)
    gui_xuong_xe({"cmd": cmd})


# ============================================================
# ROUTES
# ============================================================
@app.route("/")
def index():
    return render_template("index.html")


# ============================================================
# SOCKET.IO — dang ky vai tro
# ============================================================
@socketio.on("connect")
def on_connect():
    pass


@socketio.on("register")
def on_register(data):
    role = (data or {}).get("role", "web")
    if role == "car":
        with state_lock:
            state["car_connected"] = True
            state["car_sid"] = request.sid
        socketio.emit("car_status", {"connected": True})
        emit("registered", {"ok": True, "role": "car"})
    else:
        emit("registered", {"ok": True, "role": "web"})
        emit("state_snapshot", snapshot())
        emit("history_update", list(history))
        emit("mic_log_update", list(mic_log))


@socketio.on("disconnect")
def on_disconnect():
    with state_lock:
        if request.sid == state["car_sid"]:
            state["car_connected"] = False
            state["car_sid"] = None
            state["running"] = False
            state["mode"] = "dung"
            state["mic_connected"] = False
            state["android_connected"] = False
            socketio.emit("car_status", {"connected": False})
            socketio.emit("running_status", {"running": False})
            socketio.emit("device_status_update", {
                "mic": False, "android": False,
            })


# ============================================================
# SOCKET.IO — lenh dieu khien tu bat ky nguon nao (web, app khac, script...)
# ============================================================
@socketio.on("start")
def on_start(data):
    nguon = (data or {}).get("source", "web")
    with state_lock:
        state["running"] = True
        state["speed"] = 120
        state["mode"] = "dung"
    them_lich_su("BAT DAU", nguon)
    gui_xuong_xe({"cmd": "start"})
    socketio.emit("running_status", {"running": True})


@socketio.on("stop")
def on_stop(data):
    nguon = (data or {}).get("source", "web")
    with state_lock:
        state["running"] = False
        state["mode"] = "dung"
    them_lich_su("KET THUC", nguon)
    gui_xuong_xe({"cmd": "stop"})
    socketio.emit("running_status", {"running": False})


@socketio.on("web_command")
def on_web_command(data):
    data = data or {}
    cmd = data.get("cmd", "dung")
    nguon = data.get("source", "web")
    ap_dung_lenh_dieu_khien(cmd, nguon)


@socketio.on("set_speed")
def on_set_speed(data):
    data = data or {}
    v = max(40, min(200, int(data.get("v", 120))))
    nguon = data.get("source", "web")
    with state_lock:
        state["speed"] = v
    them_lich_su(f"Toc do: {v}", nguon)
    gui_xuong_xe({"cmd": "speed", "v": v})
    socketio.emit("speed_update", {"v": v})


# Xe bao cao len day KHI no vua thuc thi mot lenh CUC BO (vi du nhan tu
# app Android qua AP rieng cua xe, khong di qua server nay). Muc dich: du
# lenh den tu dau, lich su 10 lenh gan nhat va vi tri mo phong tren web
# van dung va dong bo.
@socketio.on("car_report")
def on_car_report(data):
    data = data or {}
    loai = data.get("type")
    nguon = data.get("source", "esp32")

    if loai == "command":
        cmd = data.get("cmd", "dung")
        if cmd in TEN_LENH:
            with state_lock:
                if state["running"]:
                    if cmd in ("tien", "lui"):
                        state["prev_linear"] = cmd
                    if cmd in ("retrai", "rephai"):
                        state["_turn_deadline"] = time.time() + TURN_ARC_S
                    state["mode"] = cmd
            them_lich_su(TEN_LENH[cmd], nguon)
    elif loai == "start":
        with state_lock:
            state["running"] = True
            state["speed"] = 120
            state["mode"] = "dung"
        them_lich_su("BAT DAU", nguon)
        socketio.emit("running_status", {"running": True})
    elif loai == "stop":
        with state_lock:
            state["running"] = False
            state["mode"] = "dung"
        them_lich_su("KET THUC", nguon)
        socketio.emit("running_status", {"running": False})
    elif loai == "speed":
        v = max(40, min(200, int(data.get("v", 120))))
        with state_lock:
            state["speed"] = v
        them_lich_su(f"Toc do: {v}", nguon)
        socketio.emit("speed_update", {"v": v})
    elif loai == "device_status":
        # Xe bao cao trang thai ket noi cua mic (Edge Impulse) va app Android
        mic = bool(data.get("mic", False))
        android = bool(data.get("app", False))
        with state_lock:
            state["mic_connected"] = mic
            state["android_connected"] = android
        socketio.emit("device_status_update", {"mic": mic, "android": android})
    elif loai == "mic_log":
        # Log nhan dien THO cua mic - bao gom ca noise/tin cay thap, khong
        # gioi han 8 nhan nhu TEN_LENH, tach rieng khoi lich su 10 lenh
        entry = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "label": data.get("label", "?"),
            "conf": float(data.get("conf", 0)),
            "ms": int(data.get("ms", 0)),
            "db": float(data.get("db", 0)),
            "sent": bool(data.get("sent", False)),
        }
        mic_log.appendleft(entry)
        socketio.emit("mic_log_update", list(mic_log))
        return   # khong can emit lai state_snapshot cho loai nay

    socketio.emit("state_snapshot", snapshot())


# Neu xe that co cam bien (vi du encoder/IMU that) va muon gui vi tri that
# ve thay cho vi tri mo phong, no co the emit 'car_telemetry' voi {x,y,heading}
@socketio.on("car_telemetry")
def on_car_telemetry(data):
    data = data or {}
    with state_lock:
        if "x" in data:
            state["x"] = float(data["x"])
        if "y" in data:
            state["y"] = float(data["y"])
        if "heading" in data:
            state["heading"] = float(data["heading"]) % 360
    socketio.emit("state_snapshot", snapshot())


# ============================================================
# VONG LAP MO PHONG VI TRI (dead-reckoning) — chay nen, 20 lan/giay
# ============================================================
def vong_lap_mo_phong():
    dt = 0.05
    while True:
        time.sleep(dt)
        changed = False
        with state_lock:
            if state["running"]:
                mode = state["mode"]
                spd = state["speed"] / 200.0 * MAX_LINEAR_SPEED_PX_S
                rad = math.radians(state["heading"])

                if mode == "tien":
                    state["x"] += spd * dt * math.cos(rad)
                    state["y"] += spd * dt * math.sin(rad)
                    changed = True
                elif mode == "lui":
                    state["x"] -= spd * dt * math.cos(rad)
                    state["y"] -= spd * dt * math.sin(rad)
                    changed = True
                elif mode in ("retrai", "rephai"):
                    deadline = state.get("_turn_deadline", 0)
                    if time.time() < deadline:
                        sign = -1 if mode == "retrai" else 1
                        state["heading"] = (state["heading"] + sign * TURN_RATE_DEG_S * dt) % 360
                    else:
                        # het pha re -> tiep tuc theo huong thang truoc do (giong diTheoHuong())
                        state["mode"] = state["prev_linear"]
                    changed = True
                elif mode == "xoaytrai":
                    state["heading"] = (state["heading"] - ROTATE_RATE_DEG_S * dt) % 360
                    changed = True
                elif mode == "xoayphai":
                    state["heading"] = (state["heading"] + ROTATE_RATE_DEG_S * dt) % 360
                    changed = True

                # gioi han trong khung canvas (bat vao tuong)
                state["x"] = max(20, min(CANVAS_W - 20, state["x"]))
                state["y"] = max(20, min(CANVAS_H - 20, state["y"]))

        if changed:
            socketio.emit("state_snapshot", snapshot())


threading.Thread(target=vong_lap_mo_phong, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False
    )
