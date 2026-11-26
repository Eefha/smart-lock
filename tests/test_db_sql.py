"""桩掉 pymysql/连接池，检查 db_utils 真正发给 MySQL 的 SQL 文本。"""
import os, sys, types

os.environ['TOKEN_SECRET'] = 'x'
os.environ['INTERNAL_API_KEY'] = 'y'

PG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PG)

sql_log = []


class FakeCursor:
    def __init__(self):
        self.rowcount = 1

    def execute(self, sql, params=None):
        sql_log.append((' '.join(sql.split()), params))

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def cursor(self):
        return FakeCursor()

    def commit(self):
        pass

    def close(self):
        pass


pymysql_mod = types.ModuleType('pymysql')
pymysql_mod.MySQLError = Exception
pymysql_mod.connect = lambda *a, **kw: FakeConn()
sys.modules['pymysql'] = pymysql_mod

dbutils_mod = types.ModuleType('dbutils')
pooled_mod = types.ModuleType('dbutils.pooled_db')
pooled_mod.PooledDB = lambda *a, **kw: types.SimpleNamespace(connection=lambda: FakeConn())
dbutils_mod.pooled_db = pooled_mod
sys.modules['dbutils'] = dbutils_mod
sys.modules['dbutils.pooled_db'] = pooled_mod

import db_utils as du

failures = []


def check(name, cond):
    print(('PASS  ' if cond else 'FAIL  ') + name)
    if not cond:
        failures.append(name)


def find(pattern):
    return [s for s in sql_log if pattern in s[0]]


# 1. Q0 心跳：不得出现 is_locked 赋值
sql_log.clear()
du.update_lock_heartbeat('866311062305127', 355)
q = sql_log[0][0]
check('Q0 型心跳不更新 is_locked', 'is_locked' not in q)
check('Q0 型心跳更新电压列', 'voltage = %s' in q)
check('电压原值入库无换算', sql_log[0][1] == [355, '866311062305127'])
check('Q0 型心跳置在线', 'is_online = 1' in q)
check('Q0 型心跳刷新心跳时间', 'last_heartbeat = NOW()' in q)

# 1b. 脏电压一律不写该列，但在线状态与心跳时间照常刷新
for bad in ('?', '', None, 'abc', '199', '6001', '0'):
    sql_log.clear()
    du.update_lock_heartbeat('866311062305127', bad)
    check(f'脏电压 {bad!r} 不写 voltage 列', 'voltage' not in sql_log[0][0])
    check(f'脏电压 {bad!r} 仍刷新在线与心跳', 'is_online = 1' in sql_log[0][0] and 'last_heartbeat = NOW()' in sql_log[0][0])

# 1c. 合理区间边界：2.00V 与 60.00V 必须能入库（低电压设备不能被误判成脏数据）
sql_log.clear()
du.update_lock_heartbeat('866311062305127', 200)
check('2.00V 下边界可入库', 'voltage = %s' in sql_log[0][0] and sql_log[0][1][0] == 200)
sql_log.clear()
du.update_lock_heartbeat('866311062305127', 6000)
check('60.00V 上边界可入库', sql_log[0][1][0] == 6000)

# 2. H0 心跳：必须带 is_locked
sql_log.clear()
du.update_lock_heartbeat('866311062305127', 355, 1)
check('H0 型心跳更新 is_locked', 'is_locked = %s' in sql_log[0][0])
check('H0 型心跳参数顺序正确', sql_log[0][1] == [355, 1, '866311062305127'])

# 3. L0/L1：voltage=None 时电压列完全不出现在 SET 子句里
sql_log.clear()
du.update_lock_heartbeat('866311062305127', is_locked=0)
check('L0 型更新不含 voltage 列（不清零电压）', 'voltage' not in sql_log[0][0])
check('L0 型更新写入锁状态', 'is_locked = %s' in sql_log[0][0])

# 4. 全 None 也要合法（只刷新在线与心跳）
sql_log.clear()
du.update_lock_heartbeat('866311062305127')
check('无字段更新仍是合法SQL', sql_log[0][0].startswith('UPDATE lock_info SET is_online = 1, last_heartbeat = NOW() WHERE'))

# 5. 离线只改 is_online，不碰锁状态
sql_log.clear()
du.set_lock_offline('866311062305127')
check('离线更新只置 is_online=0', sql_log[0][0] == 'UPDATE lock_info SET is_online = 0 WHERE lock_id = %s')
check('离线更新不触碰 is_locked', 'is_locked' not in sql_log[0][0])
sql_log.clear()
du.set_lock_offline(None)
check('空 lock_id 不产生SQL', len(sql_log) == 0)

# 6. 订单建单：status 必须是 0（进行中），不再是 1
sql_log.clear()
du.create_order(42, '866311062305127', '1234567890', 1.00,
                pay_time=__import__('datetime').datetime(2026, 10, 24, 12, 0, 0))
ins = find('INSERT INTO `order`')
check('已支付建单写入 pay_time', 'pay_time' in ins[-1][0])
check('建单 status=0 进行中', ", status, pay_time)" in ins[-1][0])
check('建单不再直接写 status=1', 'VALUES (%s, %s, %s, %s, %s, 0,' in ins[-1][0])
sql_log.clear()
du.create_order(42, '866311062305127', None, 1.00)
check('未支付建单走 unlock_time 分支', 'unlock_time' in find('INSERT INTO `order`')[-1][0])

# 7. 关锁结算必须带 status=0 守卫（幂等）
sql_log.clear()
du.finish_order('PAY1', None, 7)
check('结算SQL带 status=0 幂等守卫', 'WHERE order_no = %s AND status = 0' in sql_log[0][0])

# 8. 开锁确认SQL
sql_log.clear()
du.set_order_unlock_result('PAY1', True)
check('确认SQL只更新未完结订单', "AND status = 0" in sql_log[0][0])

# 9. 新设备注册：不得再写 battery 列，电压留空等首次心跳
sql_log.clear()
du.register_lock('866311062305127')
reg = [s for s in sql_log if s[0].startswith('INSERT INTO lock_info')]
check('注册SQL不再出现 battery 列', reg and 'battery' not in reg[-1][0])
check('注册SQL不预置 voltage（无数据即NULL）', reg and 'voltage' not in reg[-1][0])

# 10. 数据层彻底告别百分比换算（S6/G0 仅在日志里打印设备报的百分比，不入库）
src = open(os.path.join(PG, 'db_utils.py'), encoding='utf-8').read() \
    + open(os.path.join(PG, 'tcp_server.py'), encoding='utf-8').read() \
    + open(os.path.join(PG, 'admin_web.py'), encoding='utf-8').read()
check('数据层无 battery 列残留', 'battery' not in open(os.path.join(PG, 'db_utils.py'), encoding='utf-8').read())
check('估算函数已删除', 'estimate_battery_from_voltage' not in src)
check('后台按 0.01V 还原成伏特显示',
      __import__('re').search(r'voltage\s*/\s*100', open(os.path.join(PG, 'admin_web.py'), encoding='utf-8').read()) is not None)

# 11. 后台操作列：只按锁状态出按钮，不再混显在线/离线
admin_src = open(os.path.join(PG, 'admin_web.py'), encoding='utf-8').read()
actions_block = admin_src[admin_src.index("'actions': lambda"):admin_src.index("@expose('/unlock/")]
check('操作列不再显示在线状态', '离线' not in actions_block and 'is_online' not in actions_block)
check('操作列只看 is_locked', 'if model.is_locked' in actions_block)
check('已解锁时仍给出文字标识', '已解锁' in actions_block)

print()
print('ALL PASS' if not failures else f'{len(failures)} FAILED: {failures}')
sys.exit(1 if failures else 0)
