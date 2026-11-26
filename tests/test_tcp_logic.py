"""
桩掉 flask / db_utils，验证 tcp_server 的协议解析与状态机行为：
粘包半包、Q0 不写锁状态、H0 写锁状态、L0/L1 订单流转、电压不被清零、断线置离线。
"""
import os, sys, types, importlib

os.environ['TOKEN_SECRET'] = 'test-secret'
os.environ['INTERNAL_API_KEY'] = 'internal-key'

PG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PG)

# ---------- 桩：flask ----------
flask_mod = types.ModuleType('flask')


class FakeFlask:
    def __init__(self, *a, **kw):
        pass

    def route(self, *a, **kw):
        def deco(f):
            return f
        return deco

    def run(self, *a, **kw):
        pass


flask_mod.Flask = FakeFlask
flask_mod.request = types.SimpleNamespace()
flask_mod.g = types.SimpleNamespace()
flask_mod.jsonify = lambda payload=None, **kw: dict(payload or {}, **kw)
sys.modules['flask'] = flask_mod

# ---------- 桩：db_utils（记录所有调用） ----------
calls = []
state = {
    'paid_order': None,      # get_paid_unopened_order 返回值
    'unlocked_order': None,  # get_unlocked_order 返回值
}

db_mod = types.ModuleType('db_utils')


def _rec(name):
    def fn(*a, **kw):
        calls.append((name, a, kw))
        return None
    return fn


db_mod.update_lock_heartbeat = lambda lock_id, voltage=None, is_locked=None: (
    calls.append(('update_lock_heartbeat', (lock_id, voltage, is_locked), {})) or True)
db_mod.register_lock = lambda lock_id, qr_code=None, name=None, location=None: (
    calls.append(('register_lock', (lock_id, qr_code), {})) or True)
db_mod.create_order = lambda *a, **kw: (calls.append(('create_order', a, kw)) or 'PAYSHOULDNOTBEUSED')
db_mod.get_paid_unopened_order = lambda uid, lock_id, within=5: (
    calls.append(('get_paid_unopened_order', (uid, lock_id, within), {}))
    # 模拟真实库语义：user.id 是自增主键，不可能存在 id=0 的用户，
    # 因此后台/运维发起的开锁（uid=0）永远查不到订单。
    or (None if int(uid) == 0 else state['paid_order']))
db_mod.set_order_unlock_result = lambda order_no, success: (
    calls.append(('set_order_unlock_result', (order_no, success), {})) or True)
db_mod.get_unlocked_order = lambda uid, lock_id: (
    calls.append(('get_unlocked_order', (uid, lock_id), {})) or state['unlocked_order'])
db_mod.finish_order = lambda order_no, lock_time, minutes: (
    calls.append(('finish_order', (order_no, minutes), {})) or True)
db_mod.log_operation = lambda *a, **kw: calls.append(('log_operation', a, kw))
db_mod.get_connection = lambda: None
db_mod.mark_offline_devices = lambda timeout_seconds=600: (
    calls.append(('mark_offline_devices', (timeout_seconds,), {})) or 0)
db_mod.set_lock_offline = lambda lock_id: (
    calls.append(('set_lock_offline', (lock_id,), {})) or True)
db_mod.upsert_user_by_openid = lambda openid, prefix='用户': (1, openid, 'nick')
sys.modules['db_utils'] = db_mod

# ---------- 桩：requests ----------
sys.modules['requests'] = types.SimpleNamespace(
    exceptions=types.SimpleNamespace(Timeout=Exception),
    get=lambda *a, **kw: types.SimpleNamespace(json=lambda: {}),
    post=lambda *a, **kw: types.SimpleNamespace(status_code=200, json=lambda: {'code': 0})
)

import tcp_server as ts

ts.logger.setLevel(50)

failures = []


def check(name, cond):
    print(('PASS  ' if cond else 'FAIL  ') + name)
    if not cond:
        failures.append(name)


def names():
    return [c[0] for c in calls]


def last(name):
    for c in reversed(calls):
        if c[0] == name:
            return c
    return None


IMEI = '866311062305127'


def feed(cmd_type, params):
    calls.clear()
    parts = ['*CMDS', 'OM', IMEI, '261024120000', cmd_type] + list(params)
    return ts.handle_command(parts, ('127.0.0.1', 1))


# ============ 1. Q0 签到：只更新电压，绝不能写锁状态 ============
resp = feed('Q0', ['380', '?'])
hb = last('update_lock_heartbeat')
check('Q0 调用了心跳更新', hb is not None)
check('Q0 未写 is_locked（防幽灵开锁）', hb and hb[1][2] is None)
check('Q0 电压原样透传给数据层(380=3.80V)', hb and str(hb[1][1]) == '380')
check('Q0 自动注册设备', 'register_lock' in names())
check('Q0 不回复设备', resp is None)

# ============ 2. H0 心跳：唯一能写锁状态的指令 ============
resp = feed('H0', ['1', '380', '16'])
hb = last('update_lock_heartbeat')
check('H0 写入 is_locked=1', hb and hb[1][2] == 1)
check('H0 电压原样透传给数据层', hb and str(hb[1][1]) == '380')
feed('H0', ['0', '380', '16', '无', '88'])
check('H0 不再采纳第5参数百分比（只存电压）', str(last('update_lock_heartbeat')[1][1]) == '380')

# 低电压设备必须原样上报电压，而不是被曲线压成 0
feed('H0', ['1', '325', '16'])
check('3.25V 低电压原样入库不截断', str(last('update_lock_heartbeat')[1][1]) == '325')

# ============ 2b. S5 查询回包：设备主动报电压，后台点一次即可刷新 ============
feed('S5', ['376', '15', '12', '1', '无', '88'])
hb = last('update_lock_heartbeat')
check('S5 刷新电压', hb and str(hb[1][1]) == '376')
check('S5 不写 is_locked（仍只信 H0）', hb and hb[1][2] is None)

# ============ 3. L0 开锁上报：单轨订单 + 电压不被清零 ============
state['paid_order'] = 'PAY1700000000042'
resp = feed('L0', ['0', '42', '1700000000'])
hb = last('update_lock_heartbeat')
check('L0 成功写 is_locked=0', hb and hb[1][2] == 0)
check('L0 未把电压覆盖为0（voltage=None）', hb and hb[1][1] is None)
check('L0 对账到已支付订单', last('set_order_unlock_result')[1] == ('PAY1700000000042', True))
check('L0 不再另建订单（消除双轨订单）', 'create_order' not in names())
check('L0 回复设备 Re,L0', resp and 'Re,L0' in resp)

state['paid_order'] = None
resp = feed('L0', ['0', '42', '1700000000'])
feed('L0', ['0', '42', '1700000000'])
check('无订单的开锁（后台/RFID）不产生假单', 'create_order' not in names())
check('无订单时仍更新锁状态', last('update_lock_heartbeat')[1][2] == 0)

state['paid_order'] = 'PAY1700000000042'
feed('L0', ['1', '42', '1700000000'])
check('L0 失败回执记录 unlock_status=0', last('set_order_unlock_result')[1] == ('PAY1700000000042', False))

# ============ 4. L1 关锁上报：结算并落锁 ============
state['unlocked_order'] = 'PAY1700000000042'
resp = feed('L1', ['42', '1700000000', '7'])
check('L1 结算订单', last('finish_order')[1] == ('PAY1700000000042', '7'))
check('L1 写 is_locked=1', last('update_lock_heartbeat')[1][2] == 1)
check('L1 未把电压覆盖为0', last('update_lock_heartbeat')[1][1] is None)
check('L1 回复设备 Re,L1', resp and 'Re,L1' in resp)

state['unlocked_order'] = None
feed('L1', ['42', '1700000000', '7'])
check('L1 无进行中订单时不误结算', 'finish_order' not in names())

# ============ 5. 粘包 / 半包 / 包头剥离 ============
class FakeSock:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.closed = False

    def recv(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        return b''

    def send(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True


calls.clear()
# 两帧粘在同一个 recv 里；其中一帧故意不含包头
s = FakeSock([
    b'\xff\xff*CMDS,OM,' + IMEI.encode() + b',261024120000,H0,1,380,16#\n'
    b'\xff\xff*CMDS,OM,' + IMEI.encode() + b',261024120000,Q0,380,#\n',
    b'\xff\xff*CMDS,OM,' + IMEI.encode() + b',261024',   # 半包
    b'120000,L0,0,42,1700#\n',                            # 后半包
])
ts.handle_client(s, ('127.0.0.1', 2))
# 三帧：H0 + Q0（粘包）+ L0（半包拼接），每帧都会触发一次状态更新
check('粘包被正确拆成多条指令', names().count('update_lock_heartbeat') == 3)
check('粘包中的Q0也被解析并注册', 'register_lock' in names())
check('粘包中Q0依旧不写锁状态', any(c[0] == 'update_lock_heartbeat' and c[1][2] is None for c in calls))
check('半包跨 recv 拼接成功', last('get_paid_unopened_order') is not None)
check('开锁回执已回复设备', any(b'Re,L0' in d for d in s.sent))

# ============ 6. 断线 / 重连接管 ============
calls.clear()
s = FakeSock([b'\xff\xff*CMDS,OM,' + IMEI.encode() + b',261024120000,H0,1,380,16#\n'])
ts.handle_client(s, ('127.0.0.1', 3))
check('断线后立即置离线', ('set_lock_offline', (IMEI,), {}) in calls)
check('断线后从连接表移除', IMEI not in ts.online_devices)

# 场景：设备重连，新连接已接管连接表，旧连接线程此刻才走进 finally。
# 此时绝不能置离线，否则后台会在设备明明在线时显示离线（幽灵离线）。
H0_FRAME = b'\xff\xff*CMDS,OM,' + IMEI.encode() + b',261024120000,H0,1,380,16#\n'
new_sock = FakeSock([])


class TakeoverSock(FakeSock):
    """在退出 recv 循环前，模拟同 IMEI 的新连接完成接管。"""

    def recv(self, n):
        data = super().recv(n)
        if not data:
            with ts.devices_lock:
                ts.online_devices[IMEI] = new_sock
        return data


calls.clear()
ts.handle_client(TakeoverSock([H0_FRAME]), ('127.0.0.1', 4))
check('接管后旧连接不误删新socket', ts.online_devices.get(IMEI) is new_sock)
check('接管后旧连接不误置离线（幽灵离线）', ('set_lock_offline', (IMEI,), {}) not in calls)

# 自己就是连接表里的当前 socket 时，断开必须置离线
with ts.devices_lock:
    ts.online_devices[IMEI] = new_sock
calls.clear()
real = FakeSock([H0_FRAME])
ts.handle_client(real, ('127.0.0.1', 5))
check('真实断开才置离线', ('set_lock_offline', (IMEI,), {}) in calls)
check('真实断开后连接表已清空', IMEI not in ts.online_devices)

# ============ 7. /open_lock 身份隔离（管理员ID绝不能当用户ID用） ============
import auth_utils as au

sent = []
ts.send_open_command = lambda imei, uid: (sent.append((imei, uid)) or (True, 'Command sent'))


class ApiReq:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers if headers is not None else {'X-Internal-Key': 'internal-key'}
        self.remote_addr = '10.0.0.9'

    def get_json(self):
        return self.payload


def call_open_lock(payload, headers=None):
    req = ApiReq(payload, headers)
    ts.request = req
    au.request = req
    calls.clear()
    sent.clear()
    result = ts.api_open_lock()
    # 成功路径返回裸 dict（HTTP 200），失败路径返回 (dict, 状态码)
    if isinstance(result, tuple):
        return result[0], result[1]
    return result, 200


resp, code = call_open_lock({'imei': IMEI, 'admin_id': 1})
check('后台开锁下发 user_id=0', sent == [(IMEI, 0)])
check('后台开锁 HTTP 200', code == 200 and resp['code'] == 0)
check('后台开锁日志带管理员身份', any(c[0] == 'log_operation' and c[2].get('admin_id') == 1 for c in calls))

call_open_lock({'imei': IMEI, 'user_id': 42})
check('用户开锁保留真实 user_id', sent == [(IMEI, 42)])

resp, code = call_open_lock({'imei': IMEI, 'user_id': 42}, headers={})
check('缺少内部密钥被拒绝', code == 403 and len(sent) == 0)

resp, code = call_open_lock({'user_id': 42})
check('缺少 imei 返回400', code == 400)

ts.send_open_command = lambda imei, uid: (False, f'Device {imei} not online')
resp, code = call_open_lock({'imei': IMEI, 'admin_id': 1})
check('设备离线返回404', code == 404 and resp['code'] == 404)
check('失败也留审计日志', any(c[0] == 'log_operation' and c[1][1] == 'API开锁失败' for c in calls))

# 端到端：后台下发的 L0 回包（uid=0）不得命中任何用户订单
ts.send_open_command = lambda imei, uid: (sent.append((imei, uid)) or (True, 'Command sent'))
state['paid_order'] = 'PAY1700000000042'
feed('L0', ['0', '0', '1700000000'])
check('后台开锁的回执不回写用户订单（防串单）', 'set_order_unlock_result' not in names())
check('后台开锁仍会更新锁状态', last('update_lock_heartbeat')[1][2] == 0)

print()
print('ALL PASS' if not failures else f'{len(failures)} FAILED: {failures}')
sys.exit(1 if failures else 0)
