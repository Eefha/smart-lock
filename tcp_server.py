#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享行李箱智能锁 - TCP 服务器 + 小程序 API
支持500+设备并发，数据自动存入MySQL
"""

import sys
import socket
import threading
import time
import json
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, g, request, jsonify

# ==================== 日志配置 ====================
import logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

logger.info("=== tcp_server.py 开始执行 ===")

# ==================== 导入配置和数据库工具 ====================
from config import (
    TCP_HOST, TCP_PORT, MAX_WORKERS, API_PORT,
    HEARTBEAT_TIMEOUT_SECONDS, OFFLINE_SCAN_INTERVAL,
    PAY_VALID_MINUTES, WX_APPID, WX_SECRET, WX_CODE2SESSION_URL,
    INTERNAL_API_KEY
)
from db_utils import (
    update_lock_heartbeat, register_lock, create_order,
    get_paid_unopened_order, set_order_unlock_result, get_unlocked_order,
    finish_order, log_operation, get_connection,
    mark_offline_devices, set_lock_offline, upsert_user_by_openid
)
from auth_utils import (
    require_user, reject_client_user_mismatch, is_internal_request,
    issue_token, INTERNAL_KEY_HEADER
)

logger.info("=== 导入成功，开始初始化 ===")

# ==================== 全局设备管理 ====================
online_devices = {}  # imei -> socket
devices_lock = threading.Lock()

# ==================== 心跳超时检测线程 ====================
def check_offline_devices():
    """定期扫描，把超过 HEARTBEAT_TIMEOUT_SECONDS 没有心跳的设备标记为离线（作为物理断开未触发finally的兜底）"""
    while True:
        try:
            time.sleep(OFFLINE_SCAN_INTERVAL)
            affected = mark_offline_devices(timeout_seconds=HEARTBEAT_TIMEOUT_SECONDS)
            if affected > 0:
                logger.info(f"[心跳检测] 已将 {affected} 台超时设备标记为离线")
        except Exception as e:
            logger.error(f"[心跳检测] 异常: {e}")

# ==================== Flask API 实例 ====================
app = Flask(__name__)

# ---------- 开锁指令下发（内部函数，供本进程和后台管理调用） ----------
def send_open_command(imei, user_id):
    """
    通过内存中的长连接向设备下发 L0 开锁指令。
    返回 (成功与否, 提示信息)。设备是否真的打开由后续 L0 上报决定。
    """
    with devices_lock:
        sock = online_devices.get(imei)
    if not sock:
        logger.warning(f"下发开锁失败: 设备 {imei} 不在连接表中")
        set_lock_offline(imei)
        return False, f"Device {imei} not online"

    cmd = f"*CMDS,OM,{imei},{get_current_time()},L0,0,{user_id},{int(time.time())}#\n"
    try:
        sock.send(b'\xff\xff' + cmd.encode('utf-8'))
        logger.info(f"[指令] 向设备 {imei} 下发开锁指令成功")
        return True, "Command sent"
    except Exception as e:
        logger.error(f"[指令] 下发失败: {e}")
        with devices_lock:
            owned = online_devices.get(imei) == sock
            if owned:
                del online_devices[imei]
        if owned:
            set_lock_offline(imei)
        return False, str(e)

# ---------- 开锁 API（仅供管理后台等内部服务调用） ----------
@app.route('/open_lock', methods=['POST'])
def api_open_lock():
    if not is_internal_request():
        logger.warning(f"[API] 拒绝非内部来源的开锁请求: {request.remote_addr}")
        return jsonify({"code": 403, "msg": "Forbidden"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"code": 400, "msg": "Missing JSON"}), 400

    imei = data.get('imei')
    admin_id = data.get('admin_id')
    user_id = data.get('user_id', 0)

    if not imei:
        return jsonify({"code": 400, "msg": "Missing imei"}), 400

    # admin.id 和 user.id 是两张表各自的自增主键，同号不代表同一人。
    # 后台/运维发起的开锁不参与计费，一律下发 user_id=0（自增主键不可能是0），
    # 这样设备回传 L0 时永远匹配不到用户订单，避免误标别人的待确认订单。
    billing_uid = 0 if admin_id else user_id

    ok, msg = send_open_command(imei, billing_uid)
    if ok:
        log_operation('api', 'API开锁',
                      f'IMEI={imei}, 计费用户ID={billing_uid}, 管理员ID={admin_id or "-"}',
                      user_id=(billing_uid or None), lock_id=imei,
                      admin_id=admin_id, ip=data.get('ip'))
        return jsonify({"code": 0, "msg": msg})

    status_code = 404 if 'not online' in msg else 500
    log_operation('api', 'API开锁失败',
                  f'IMEI={imei}, 原因={msg}, 管理员ID={admin_id or "-"}',
                  lock_id=imei, admin_id=admin_id, ip=data.get('ip'))
    return jsonify({"code": status_code, "msg": msg}), status_code

# ---------- 小程序 API ----------

# 1. 微信登录（真实 code2session）
@app.route('/wx/login', methods=['POST'])
def wx_login():
    data = request.get_json()
    code = data.get('code')
    if not code:
        return jsonify({"code": 400, "msg": "Missing code"}), 400

    if not WX_APPID or not WX_SECRET:
        logger.error("微信登录失败: 未配置 WX_APPID / WX_SECRET")
        return jsonify({"code": 500, "msg": "WeChat credentials not configured"}), 500

    try:
        resp = requests.get(
            WX_CODE2SESSION_URL,
            params={
                'appid': WX_APPID,
                'secret': WX_SECRET,
                'js_code': code,
                'grant_type': 'authorization_code'
            },
            timeout=5
        )
        wx_data = resp.json()
    except Exception as e:
        logger.error(f"微信 code2session 请求异常: {e}")
        return jsonify({"code": 502, "msg": "WeChat service unavailable"}), 502

    openid = wx_data.get('openid')
    if not openid:
        logger.warning(f"微信 code2session 失败: errcode={wx_data.get('errcode')}, errmsg={wx_data.get('errmsg')}")
        return jsonify({"code": 401, "msg": "WeChat login failed"}), 401

    user = upsert_user_by_openid(openid)
    token = issue_token(user[0], openid)
    log_operation('user', '小程序登录', f'user_id={user[0]}', user_id=user[0])

    return jsonify({
        "code": 0,
        "data": {
            "user_id": user[0],
            "openid": user[1],
            "nickname": user[2],
            "token": token
        }
    })

# 2. 绑定设备
@app.route('/device/bind', methods=['POST'])
@require_user
def device_bind():
    data = request.get_json()
    if not data:
        return jsonify({"code": 400, "msg": "Missing JSON"}), 400

    mismatch = reject_client_user_mismatch(data.get('user_id'))
    if mismatch:
        return mismatch

    user_id = g.user_id
    lock_id = data.get('lock_id')
    if not lock_id:
        return jsonify({"code": 400, "msg": "Missing lock_id"}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM lock_info WHERE lock_id = %s", (lock_id,))
            if not cur.fetchone():
                return jsonify({"code": 404, "msg": "Device not found"}), 404

            cur.execute(
                "SELECT user_id FROM user_device_binding WHERE lock_id = %s AND status = 1",
                (lock_id,)
            )
            row = cur.fetchone()
            if row and row[0] != user_id:
                return jsonify({"code": 403, "msg": "Device already bound to another user"}), 403

            cur.execute(
                """INSERT INTO user_device_binding (user_id, lock_id, status) 
                   VALUES (%s, %s, 1) 
                   ON DUPLICATE KEY UPDATE status = 1""",
                (user_id, lock_id)
            )
            conn.commit()
    finally:
        conn.close()

    return jsonify({"code": 0, "msg": "Bind success"})

# 3. 获取设备信息
@app.route('/device/info', methods=['GET'])
@require_user
def device_info():
    lock_id = request.args.get('lock_id')
    if not lock_id:
        return jsonify({"code": 400, "msg": "Missing lock_id"}), 400

    mismatch = reject_client_user_mismatch(request.args.get('user_id'))
    if mismatch:
        return mismatch

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_online, is_locked, voltage, last_heartbeat FROM lock_info WHERE lock_id = %s",
                (lock_id,)
            )
            device = cur.fetchone()
            if not device:
                return jsonify({"code": 404, "msg": "Device not found"}), 404

            is_online, is_locked, voltage, last_heartbeat = device

            cur.execute(
                "SELECT user_id FROM user_device_binding WHERE lock_id = %s AND status = 1",
                (lock_id,)
            )
            bind_row = cur.fetchone()
            bound_user_id = bind_row[0] if bind_row else None

            is_bound = (bound_user_id == g.user_id) if bound_user_id else False
    finally:
        conn.close()

    return jsonify({
        "code": 0,
        "data": {
            "is_online": bool(is_online),
            "is_locked": bool(is_locked),
            "voltage": voltage,
            "last_heartbeat": last_heartbeat.strftime('%Y-%m-%d %H:%M:%S') if last_heartbeat else None,
            "bound_user_id": bound_user_id,
            "is_bound": is_bound
        }
    })

# 4. 发起支付（模拟）
@app.route('/pay/prepay', methods=['POST'])
@require_user
def prepay():
    data = request.get_json()
    if not data:
        return jsonify({"code": 400, "msg": "Missing JSON"}), 400

    mismatch = reject_client_user_mismatch(data.get('user_id'))
    if mismatch:
        return mismatch

    lock_id = data.get('lock_id')
    amount = data.get('amount', 1.00)

    if not lock_id:
        return jsonify({"code": 400, "msg": "Missing user_id or lock_id"}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM user_device_binding WHERE user_id = %s AND lock_id = %s AND status = 1",
                (g.user_id, lock_id)
            )
            if not cur.fetchone():
                return jsonify({"code": 403, "msg": "Device not bound"}), 403

            cur.execute(
                "SELECT is_online, is_locked, qr_code FROM lock_info WHERE lock_id = %s",
                (lock_id,)
            )
            device = cur.fetchone()
            if not device:
                return jsonify({"code": 404, "msg": "Device not found"}), 404
            if not device[0]:
                return jsonify({"code": 400, "msg": "Device offline"}), 400
            if not device[1]:
                return jsonify({"code": 400, "msg": "Device already unlocked"}), 400
            qr_code = device[2]
    finally:
        conn.close()

    # 建单即代表已收款（当前为模拟支付），status 保持 0 进行中，
    # 由设备 L0 上报写 unlock_status、L1 上报写 status=1 完成结算。
    order_no = create_order(g.user_id, lock_id, qr_code, amount, pay_time=datetime.now())
    if not order_no:
        return jsonify({"code": 500, "msg": "Order create failed"}), 500

    log_operation('order', '发起支付', f'订单号={order_no}, 金额={amount}', user_id=g.user_id, lock_id=lock_id)

    return jsonify({
        "code": 0,
        "data": {
            "order_no": order_no,
            "pay_status": "success"
        }
    })

# 5. 支付回调（模拟）
@app.route('/pay/notify', methods=['POST'])
def pay_notify():
    return jsonify({"code": 0, "msg": "ok"})

# 6. 开锁（支付成功后调用）
@app.route('/device/unlock', methods=['POST'])
@require_user
def device_unlock():
    data = request.get_json()
    if not data:
        return jsonify({"code": 400, "msg": "Missing JSON"}), 400

    mismatch = reject_client_user_mismatch(data.get('user_id'))
    if mismatch:
        return mismatch

    lock_id = data.get('lock_id')
    if not lock_id:
        return jsonify({"code": 400, "msg": "Missing user_id or lock_id"}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM user_device_binding WHERE user_id = %s AND lock_id = %s AND status = 1",
                (g.user_id, lock_id)
            )
            if not cur.fetchone():
                return jsonify({"code": 403, "msg": "Device not bound"}), 403

            cur.execute(
                "SELECT is_online, is_locked FROM lock_info WHERE lock_id = %s",
                (lock_id,)
            )
            device = cur.fetchone()
            if not device:
                return jsonify({"code": 404, "msg": "Device not found"}), 404
            if not device[0]:
                return jsonify({"code": 400, "msg": "Device offline"}), 400
            if not device[1]:
                return jsonify({"code": 400, "msg": "Device already unlocked"}), 400
    finally:
        conn.close()

    order_no = get_paid_unopened_order(g.user_id, lock_id, PAY_VALID_MINUTES)
    if not order_no:
        return jsonify({"code": 400, "msg": "No valid payment found"}), 400

    ok, msg = send_open_command(lock_id, g.user_id)
    if ok:
        log_operation('order', '下发开锁', f'订单号={order_no}', user_id=g.user_id, lock_id=lock_id)
        return jsonify({"code": 0, "msg": "Unlock success"})
    else:
        logger.warning(f"[API] 开锁下发失败: lock_id={lock_id}, 原因={msg}")
        return jsonify({"code": 500, "msg": "Unlock failed"}), 500

# 7. 获取用户绑定的设备列表
@app.route('/user/devices', methods=['GET'])
@require_user
def user_devices():
    mismatch = reject_client_user_mismatch(request.args.get('user_id'))
    if mismatch:
        return mismatch

    user_id = g.user_id
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT b.lock_id, l.is_online, l.is_locked, l.voltage, l.last_heartbeat
                   FROM user_device_binding b
                   JOIN lock_info l ON b.lock_id = l.lock_id
                   WHERE b.user_id = %s AND b.status = 1""",
                (user_id,)
            )
            rows = cur.fetchall()
            devices = []
            for row in rows:
                devices.append({
                    'lock_id': row[0],
                    'is_online': bool(row[1]),
                    'is_locked': bool(row[2]),
                    'voltage': row[3],
                    'last_heartbeat': row[4].strftime('%Y-%m-%d %H:%M:%S') if row[4] else None
                })
    finally:
        conn.close()

    return jsonify({"code": 0, "data": devices})

# 8. 通过二维码数字解析锁ID
@app.route('/qrcode/resolve', methods=['GET'])
@require_user
def qrcode_resolve():
    qr_code = request.args.get('qr_code')
    if not qr_code:
        return jsonify({"code": 400, "msg": "Missing qr_code"}), 400

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT lock_id, name, location FROM lock_info WHERE qr_code = %s AND status = 1",
                (qr_code,)
            )
            row = cur.fetchone()
            if row:
                return jsonify({
                    "code": 0,
                    "data": {
                        "lock_id": row[0],
                        "name": row[1],
                        "location": row[2]
                    }
                })
            else:
                return jsonify({"code": 404, "msg": "未找到该二维码对应的设备"}), 404
    finally:
        conn.close()

# ==================== TCP 处理核心 ====================
def get_current_time():
    return datetime.now().strftime("%y%m%d%H%M%S")

def build_response(response_str):
    if not response_str:
        return b''
    if not response_str.endswith('\n'):
        response_str += '\n'
    return b'\xff\xff' + response_str.encode('utf-8')

def handle_command(cmd_parts, client_addr):
    if len(cmd_parts) < 6:
        logger.warning(f"[{client_addr}] 指令格式错误: {cmd_parts}")
        return None

    header, vendor, imei, timestamp, cmd_type = cmd_parts[:5]
    params = cmd_parts[5:]

    logger.info(f"[{client_addr}] 收到指令: 类型={cmd_type}, IMEI={imei}, 时间={timestamp}, 参数={params}")

    if cmd_type == 'Q0':
        voltage = params[0] if len(params) > 0 else '?'
        register_lock(imei)
        update_lock_heartbeat(imei, voltage, is_locked=None)
        log_operation('heartbeat', '设备签到', f'电压={voltage}(0.01V)', lock_id=imei)
        logger.info(f"  Q0签到: 电压={voltage} (0.01V)")
        return None

    elif cmd_type in ('HO', 'H0'):
        lock_state = params[0] if len(params) > 0 else '?'
        voltage = params[1] if len(params) > 1 else '?'
        signal = params[2] if len(params) > 2 else '?'
        fault = params[3] if len(params) > 3 else '无'
        is_locked = 1 if lock_state == '1' else 0
        update_lock_heartbeat(imei, voltage, is_locked)
        log_operation('heartbeat', '设备心跳', f'状态={lock_state}, 电压={voltage}(0.01V), 信号={signal}, 故障={fault}', lock_id=imei)
        logger.info(f"  心跳: 状态={lock_state}, 电压={voltage} (0.01V), 信号={signal}, 故障={fault}")
        return None

    elif cmd_type == 'L0':
        result = params[0] if params else '?'
        uid_raw = params[1] if len(params) > 1 else '0'
        ts = params[2] if len(params) > 2 else '?'
        try:
            uid = int(uid_raw)
        except (ValueError, TypeError):
            uid = 0

        if result == '0':
            # 只给"已支付且未确认"的订单记开锁成功；管理后台/RFID 开锁没有订单，不产生假单。
            order_no = get_paid_unopened_order(uid, imei, PAY_VALID_MINUTES)
            if order_no:
                set_order_unlock_result(order_no, True)
            else:
                logger.info(f"  解锁返回: 无待确认订单（可能为后台或RFID开锁）")
            update_lock_heartbeat(imei, is_locked=0)
            log_operation('lock', '开锁成功', f'用户ID={uid}, 订单号={order_no}', user_id=uid, lock_id=imei)
            logger.info(f"  解锁返回: 结果={result}, 用户ID={uid}, 时间戳={ts}, 订单号={order_no}")
        else:
            order_no = get_paid_unopened_order(uid, imei, PAY_VALID_MINUTES)
            if order_no:
                set_order_unlock_result(order_no, False)
            log_operation('lock', '开锁失败', f'用户ID={uid}, 结果={result}', user_id=uid, lock_id=imei)
            logger.info(f"  解锁返回: 结果={result}, 用户ID={uid}, 时间戳={ts}")
        return f"*CMDS,OM,{imei},{timestamp},Re,L0#"

    elif cmd_type == 'L1':
        uid_raw = params[0] if params else '0'
        ts = params[1] if len(params) > 1 else '?'
        minutes = params[2] if len(params) > 2 else '?'
        try:
            uid = int(uid_raw)
        except (ValueError, TypeError):
            uid = 0

        order_no = get_unlocked_order(uid, imei)
        if order_no:
            finish_order(order_no, datetime.now(), minutes)
            log_operation('lock', '关锁完成', f'订单号={order_no}, 使用{minutes}分钟', user_id=uid, lock_id=imei)
            logger.info(f"  关锁上报: 用户ID={uid}, 时间戳={ts}, 骑行分钟={minutes}, 订单号={order_no}")
        else:
            log_operation('lock', '关锁上报', f'用户ID={uid}, 使用{minutes}分钟, 无进行中订单', user_id=uid, lock_id=imei)
            logger.warning(f"  关锁上报: 未找到用户 {uid} 的进行中订单")

        update_lock_heartbeat(imei, is_locked=1)
        return f"*CMDS,OM,{imei},{timestamp},Re,L1#"

    elif cmd_type == 'L3':
        status = params[0] if params else '?'
        logger.info(f"  开关机控制返回: 状态={status}")
        return None

    elif cmd_type == 'L5':
        op = params[0] if params else '?'
        result = params[1] if len(params) > 1 else '?'
        logger.info(f"  外部锁控制返回: 操作={op}, 结果={result}")
        return None

    elif cmd_type == 'L6':
        lock_state = params[0] if params else '?'
        sn = params[1] if len(params) > 1 else '?'
        logger.info(f"  电池信息: 锁状态={lock_state}, 电池SN={sn}")
        return None

    elif cmd_type == 'D0':
        if len(params) >= 4:
            loc_status = params[2] if len(params) > 2 else 'V'
            if loc_status == 'A':
                lat = params[3] if len(params) > 3 else ''
                lon = params[5] if len(params) > 5 else ''
                logger.info(f"  GPS定位有效: 纬度={lat}, 经度={lon}")
            else:
                logger.info(f"  GPS定位无效")
        else:
            logger.warning(f"  GPS定位数据不完整: {params}")
        return f"*CMDS,OM,{imei},{timestamp},Re,D0#"

    elif cmd_type == 'D1':
        interval = params[0] if params else '?'
        logger.info(f"  定位追踪设置: 间隔={interval}秒")
        return None

    elif cmd_type == 'S5':
        voltage = params[0] if params else '?'
        signal = params[1] if len(params) > 1 else '?'
        sat = params[2] if len(params) > 2 else '?'
        lock_state = params[3] if len(params) > 3 else '?'
        fault = params[4] if len(params) > 4 else '?'
        # S5 是"查询锁信息"的回包，带电压真值，直接刷新 voltage。
        # 目前没有任何接口主动下发查询，所以这条只在设备自报时生效；
        # 锁状态仍只由 H0 写（协议铁律），这里只记电压不覆盖 is_locked。
        update_lock_heartbeat(imei, voltage)
        logger.info(
            f"  锁信息: 电压={voltage}(0.01V), 信号={signal}, 卫星={sat}, 锁状态={lock_state}, 故障={fault}")
        return None

    elif cmd_type == 'S6':
        battery = params[0] if params else '?'
        mode = params[1] if len(params) > 1 else '?'
        speed = params[2] if len(params) > 2 else '?'
        charge = params[3] if len(params) > 3 else '?'
        fault = params[4] if len(params) > 4 else '?'
        mileage = params[5] if len(params) > 5 else '?'
        remain = params[6] if len(params) > 6 else '?'
        total = params[7] if len(params) > 7 else '?'
        logger.info(
            f"  电动车信息: 电量={battery}%, 模式={mode}, 车速={speed}km/h, 充电={charge}, 故障={fault}, 单次={mileage}(10m), 剩余={remain}(10m), 总={total}m")
        return None

    elif cmd_type == 'S7':
        alarm_move = params[0] if params else '?'
        alarm_w0 = params[1] if len(params) > 1 else '?'
        heartbeat = params[2] if len(params) > 2 else '?'
        gps_interval = params[3] if len(params) > 3 else '?'
        ble_enable = params[4] if len(params) > 4 else '?'
        s6_enable = params[5] if len(params) > 5 else '?'
        s6_interval = params[6] if len(params) > 6 else '?'
        logger.info(
            f"  锁功能设置: 报警触发={alarm_move}, W0触发={alarm_w0}, 心跳={heartbeat}s, 定位间隔={gps_interval}s, 蓝牙={ble_enable}, S6上传={s6_enable}, S6间隔={s6_interval}s")
        return None

    elif cmd_type == 'S4':
        speed_mode = params[0] if params else '?'
        cruise = params[1] if len(params) > 1 else '?'
        start_mode = params[2] if len(params) > 2 else '?'
        low_limit = params[3] if len(params) > 3 else '?'
        mid_limit = params[4] if len(params) > 4 else '?'
        high_limit = params[5] if len(params) > 5 else '?'
        throttle = params[6] if len(params) > 6 else '?'
        motor_lock = params[7] if len(params) > 7 else '?'
        headlight = params[8] if len(params) > 8 else '?'
        logger.info(
            f"  电动车设置: 速度模式={speed_mode}, 巡航={cruise}, 启动={start_mode}, 低速限={low_limit}, 中速限={mid_limit}, 高速限={high_limit}, 油门={throttle}, 锁电机={motor_lock}, 大灯={headlight}")
        return None

    elif cmd_type == 'S8':
        ring_times = params[0] if params else '?'
        reserved = params[1] if len(params) > 1 else '?'
        logger.info(f"  找车指令返回: 响铃次数={ring_times}")
        return None

    elif cmd_type == 'W0':
        alarm_type = params[0] if params else '?'
        logger.info(f"  报警: 类型={alarm_type}")
        return f"*CMDS,OM,{imei},{timestamp},Re,W0#"

    elif cmd_type == 'G0':
        ver = params[0] if params else '?'
        date = params[1] if len(params) > 1 else '?'
        logger.info(f"  固件信息: 版本={ver}, 编译日期={date}")
        return None

    elif cmd_type == 'G1':
        controller = params[0] if params else '?'
        battery = params[1] if len(params) > 1 else '?'
        bat_lock = params[2] if len(params) > 2 else '?'
        cable_lock = params[3] if len(params) > 3 else '?'
        tcp_ver = params[4] if len(params) > 4 else '?'
        logger.info(
            f"  外接固件: 控制器={controller}, 电池={battery}, 电池锁={bat_lock}, 钢缆锁={cable_lock}, TCP协议={tcp_ver}")
        return None

    elif cmd_type == 'U0':
        ver = params[0] if params else '?'
        dev_id = params[1] if len(params) > 1 else '?'
        date = params[2] if len(params) > 2 else '?'
        logger.info(f"  检测升级: 版本={ver}, 设备识别码={dev_id}, 编译日期={date}")
        return None

    elif cmd_type == 'U1':
        pkg_num = params[0] if params else '?'
        dev_id = params[1] if len(params) > 1 else '?'
        logger.info(f"  请求升级数据: 包序号={pkg_num}, 设备识别码={dev_id}")
        return None

    elif cmd_type == 'U2':
        dev_id = params[0] if params else '?'
        result = params[1] if len(params) > 1 else '?'
        logger.info(f"  升级结果通知: 设备={dev_id}, 结果={result}")
        return None

    elif cmd_type == 'K0':
        key = params[0] if params else '?'
        logger.info(f"  BLE通信KEY: {key}")
        return None

    elif cmd_type == 'SC':
        logger.info(f"  充电桩信息: {params}")
        return None

    elif cmd_type == 'S0':
        logger.info(f"  远程进入运输模式")
        return None

    elif cmd_type in ('I0', '10'):
        iccid = params[0] if params else '?'
        logger.info(f"  SIM卡ICCID: {iccid}")
        return None

    elif cmd_type == 'M0':
        mac = params[0] if params else '?'
        logger.info(f"  蓝牙MAC: {mac}")
        return None

    elif cmd_type == 'TS':
        logger.info(f"  透传指令: {params}")
        return None

    elif cmd_type == 'S1':
        logger.info(f"  重启指令")
        return None

    elif cmd_type == 'B0':
        found = params[0] if params else '?'
        logger.info(f"  Beacon验证: 是否搜索到={found}")
        return None

    elif cmd_type == 'C0':
        op = params[0] if params else '?'
        card = params[2] if len(params) > 2 else '?'
        logger.info(f"  RFID开锁请求: 操作={op}, 卡号={card}")
        return None

    elif cmd_type == 'C1':
        card = params[0] if params else '?'
        logger.info(f"  管理卡号: {card}")
        return None

    elif cmd_type == 'D2':
        logger.info(f"  WIFI定位信息: {params}")
        return None

    elif cmd_type in ('U5', 'U6'):
        logger.info(f"  HTTP升级指令: {cmd_type} {params}")
        return None

    else:
        log_operation('cmd', f'指令_{cmd_type}', f'参数={params}', lock_id=imei)
        logger.warning(f"  未知指令: {cmd_type}")
        return None

def handle_client(client_socket, client_addr):
    imei = None
    try:
        buffer = b''
        while True:
            data = client_socket.recv(4096)
            if not data:
                break
            buffer += data
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                line = line.strip()
                if not line:
                    continue
                if len(line) >= 2 and line[0] == 0xFF and line[1] == 0xFF:
                    line = line[2:]
                try:
                    cmd_str = line.decode('utf-8')
                except UnicodeDecodeError:
                    logger.warning(f"[{client_addr}] 非UTF-8数据，忽略")
                    continue
                if cmd_str.endswith('#'):
                    cmd_str = cmd_str[:-1]
                parts = cmd_str.split(',')
                if not parts:
                    continue

                if len(parts) >= 3:
                    cur_imei = parts[2]
                    if cur_imei and cur_imei != imei:
                        with devices_lock:
                            old_sock = online_devices.get(cur_imei)
                            if old_sock and old_sock != client_socket:
                                try:
                                    old_sock.close()
                                except:
                                    pass
                            online_devices[cur_imei] = client_socket
                            imei = cur_imei
                        logger.info(f"[{client_addr}] 设备 {imei} 已注册")

                response = handle_command(parts, client_addr)
                if response:
                    send_data = build_response(response)
                    client_socket.send(send_data)
                    logger.info(f"[{client_addr}] 回复: {response.strip()}")
    except Exception as e:
        logger.error(f"[{client_addr}] 异常: {e}")
    finally:
        if imei:
            with devices_lock:
                # 只有当连接表里仍挂着自己的 socket 时才清理并置离线。
                # 设备重连后旧线程才退出是常态，此时新会话已经接管，
                # 若仍然写离线，后台会出现设备明明在线却显示离线的"幽灵离线"。
                owned = online_devices.get(imei) == client_socket
                if owned:
                    del online_devices[imei]
            if owned:
                set_lock_offline(imei)
                logger.info(f"[{client_addr}] 设备 {imei} 已断开TCP连接并更新数据库为离线")
            else:
                logger.info(f"[{client_addr}] 设备 {imei} 的旧连接已退出，连接已被新会话接管，跳过离线更新")
        client_socket.close()
        logger.info(f"[断开] {client_addr} 连接已关闭")

def start_tcp_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((TCP_HOST, TCP_PORT))
    server.listen(500)
    logger.info(f"TCP服务器启动，监听 {TCP_HOST}:{TCP_PORT}")
    logger.info(f"API接口: http://0.0.0.0:{API_PORT}/open_lock")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        while True:
            client_sock, addr = server.accept()
            executor.submit(handle_client, client_sock, addr)

def run_flask():
    app.run(host='0.0.0.0', port=API_PORT, debug=False, threaded=True)

if __name__ == "__main__":
    logger.info("=== 进入 __main__ 块 ===")
    threading.Thread(target=check_offline_devices, daemon=True).start()
    logger.info(f"心跳超时检测线程已启动（阈值：{HEARTBEAT_TIMEOUT_SECONDS}秒 / 扫描间隔：{OFFLINE_SCAN_INTERVAL}秒）")
    threading.Thread(target=run_flask, daemon=True).start()
    start_tcp_server()