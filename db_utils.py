# db_utils.py
"""
数据库底层工具：连接池、设备心跳/在线状态、订单状态机、操作日志。

三条铁律（改这里之前务必读）：
1. 只有 H0 心跳能写 is_locked；Q0 签到不上报锁状态，必须传 is_locked=None。
2. voltage=None 表示"本次不改动电压列"，供 L0/L1 这类不含电压的指令使用，
   避免把设备刚上报的电压覆盖成 0。电压一律存设备原值（单位 0.01V，360=3.60V），
   云端不做任何电量百分比推算——设备报什么存什么，报不出来就不写。
3. 一次开锁只对应一条订单：prepay 建单并写 pay_time，L0 写 unlock_status 确认，
   L1 关锁时置 status=1 完成结算。
"""

import pymysql
import time
from dbutils.pooled_db import PooledDB
from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, DB_POOL_SIZE

# 电压合理区间（单位 0.01V）：低于 2.00V 或高于 60.00V 视为脏数据，不入库
VOLTAGE_MIN = 200
VOLTAGE_MAX = 6000

pool = PooledDB(
    creator=pymysql,
    maxconnections=DB_POOL_SIZE,
    mincached=5,
    maxcached=20,
    blocking=True,
    host=DB_HOST,
    port=DB_PORT,
    user=DB_USER,
    password=DB_PASSWORD,
    database=DB_NAME,
    charset='utf8mb4'
)


def get_connection():
    return pool.connection()


def safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _normalize_voltage(value):
    """
    电压容错：返回可入库的整数（0.01V），不合格则返回 None 表示这一列不写。
    存 None 而不是 0，是为了区分"设备没报电压"和"电压为 0"，
    避免一次异常回包把最后一次可信读数冲掉。
    """
    if value is None or value == '' or value == '?' or not str(value).isdigit():
        return None
    voltage = int(value)
    if voltage < VOLTAGE_MIN or voltage > VOLTAGE_MAX:
        return None
    return voltage


# ==================== 设备状态 ====================
def update_lock_heartbeat(lock_id, voltage=None, is_locked=None):
    """
    更新设备在线状态与心跳时间。

    voltage=None  -> 不更新 voltage 列（L0/L1 回包没有电压字段）
    is_locked=None -> 不更新 is_locked 列（Q0 签到绝不上报锁状态）
    """
    set_clauses = ['is_online = 1', 'last_heartbeat = NOW()']
    params = []

    if voltage is not None:
        clean_voltage = _normalize_voltage(voltage)
        if clean_voltage is not None:
            set_clauses.append('voltage = %s')
            params.append(clean_voltage)

    if is_locked is not None:
        set_clauses.append('is_locked = %s')
        params.append(1 if int(is_locked) == 1 else 0)

    params.append(lock_id)
    sql = f"UPDATE lock_info SET {', '.join(set_clauses)} WHERE lock_id = %s"

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            affected = cur.rowcount
            conn.commit()
            return affected > 0
    finally:
        conn.close()


def set_lock_offline(lock_id):
    """
    TCP 连接断开时立即置离线。锁状态保持不变：设备最后一次 H0 上报的状态才是可信的，
    断线不能推断它被打开或打开了。
    """
    if not lock_id:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE lock_info SET is_online = 0 WHERE lock_id = %s", (lock_id,))
            affected = cur.rowcount
            conn.commit()
            return affected > 0
    finally:
        conn.close()


def mark_offline_devices(timeout_seconds=600):
    """
    将超过 timeout_seconds 秒没有心跳的设备标记为离线。
    默认 600 秒（10分钟），给设备心跳留出足够的容错空间，防止网络波动被误杀。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                UPDATE lock_info 
                SET is_online = 0 
                WHERE is_online = 1 
                AND (last_heartbeat IS NULL 
                     OR TIMESTAMPDIFF(SECOND, last_heartbeat, NOW()) > %s)
            """
            cur.execute(sql, (timeout_seconds,))
            affected = cur.rowcount
            conn.commit()
            return affected
    finally:
        conn.close()


def register_lock(lock_id, qr_code=None, name=None, location=None):
    """设备首次签到时自动入库；qr_code 允许为 NULL，由后台二维码管理后续绑定。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM lock_info WHERE lock_id = %s", (lock_id,))
            if cur.fetchone():
                return True
            sql = """
                INSERT INTO lock_info 
                (lock_id, qr_code, name, location, price, status, is_online, is_locked) 
                VALUES (%s, %s, %s, %s, 1.00, 1, 1, 1)
            """
            cur.execute(sql, (lock_id, qr_code, name, location))
            conn.commit()
            return True
    finally:
        conn.close()


# ==================== 订单状态机 ====================
def create_order(user_id, lock_id, qr_code, amount, pay_time=None):
    """
    建单：status=0（进行中），unlock_status=NULL（设备尚未确认开锁）。
    pay_time 传入即表示已支付，可供开锁；不传则由设备回调补记（历史行为）。
    """
    uid = safe_int(user_id, 0)
    order_no = f"PAY{int(time.time())}{uid % 1000:03d}"
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            if pay_time is None:
                sql = """
                    INSERT INTO `order` (order_no, user_id, lock_id, qr_code, amount, status, unlock_time)
                    VALUES (%s, %s, %s, %s, %s, 0, NOW())
                """
                cur.execute(sql, (order_no, uid, lock_id, qr_code, amount))
            else:
                sql = """
                    INSERT INTO `order` (order_no, user_id, lock_id, qr_code, amount, status, pay_time)
                    VALUES (%s, %s, %s, %s, %s, 0, %s)
                """
                cur.execute(sql, (order_no, uid, lock_id, qr_code, amount, pay_time))
            conn.commit()
            return order_no
    finally:
        conn.close()


def get_paid_unopened_order(user_id, lock_id, within_minutes=5):
    """
    取"已支付、设备还没确认开锁"的订单号，用于开锁前置校验和 L0 回调对账。
    条件：status=0 进行中、pay_time 非空、unlock_status IS NULL、且在 within_minutes 内。
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_no FROM `order`
                WHERE user_id = %s AND lock_id = %s
                  AND status = 0
                  AND pay_time IS NOT NULL
                  AND unlock_status IS NULL
                  AND pay_time > DATE_SUB(NOW(), INTERVAL %s MINUTE)
                ORDER BY pay_time DESC
                LIMIT 1
                """,
                (safe_int(user_id, 0), lock_id, int(within_minutes))
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def set_order_unlock_result(order_no, success):
    """写 unlock_status：1 设备确认开锁成功，0 设备返回开锁失败。"""
    if not order_no:
        return False
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                UPDATE `order`
                SET unlock_status = %s, unlock_time = NOW()
                WHERE order_no = %s AND status = 0
            """
            cur.execute(sql, (1 if success else 0, order_no))
            affected = cur.rowcount
            conn.commit()
            return affected > 0
    finally:
        conn.close()


def get_unlocked_order(user_id, lock_id):
    """取"设备已确认开锁、尚未关锁结算"的订单号，用于 L1 关锁上报。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT order_no FROM `order`
                WHERE user_id = %s AND lock_id = %s AND status = 0 AND unlock_status = 1
                ORDER BY unlock_time DESC
                LIMIT 1
                """,
                (safe_int(user_id, 0), lock_id)
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def finish_order(order_no, lock_time, use_minutes):
    """关锁结算：置 status=1 已完成。WHERE 带 status=0，重复上报不会被二次计费。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                UPDATE `order` 
                SET status = 1, lock_time = %s, use_minutes = %s 
                WHERE order_no = %s AND status = 0
            """
            cur.execute(sql, (lock_time, use_minutes, order_no))
            affected = cur.rowcount
            conn.commit()
            return affected > 0
    finally:
        conn.close()


# ==================== 用户与日志 ====================
def upsert_user_by_openid(openid, nickname_prefix='用户'):
    """按 openid 查用户，不存在则创建，返回 (id, openid, nickname)。"""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, openid, nickname FROM user WHERE openid = %s", (openid,))
            user = cur.fetchone()
            if not user:
                cur.execute(
                    "INSERT INTO user (openid, nickname, created_at) VALUES (%s, %s, NOW())",
                    (openid, f"{nickname_prefix}_{openid[:8]}")
                )
                conn.commit()
                cur.execute("SELECT id, openid, nickname FROM user WHERE openid = %s", (openid,))
                user = cur.fetchone()
            return user
    finally:
        conn.close()


def log_operation(log_type, action, content, user_id=None, lock_id=None, admin_id=None, ip=None):
    uid = safe_int(user_id) if user_id is not None else None
    aid = safe_int(admin_id) if admin_id is not None else None
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            sql = """
                INSERT INTO operation_log 
                (log_type, action, content, user_id, lock_id, admin_id, ip_address) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """
            cur.execute(sql, (log_type, action, content, uid, lock_id, aid, ip))
            conn.commit()
    finally:
        conn.close()


def authenticate_admin(username, password_hash):
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM admin WHERE username = %s AND password = %s AND status = 1",
                        (username, password_hash))
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()
