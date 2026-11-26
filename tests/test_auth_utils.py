"""桩掉 flask 依赖，单独验证 auth_utils 的令牌签验逻辑。"""
import os, sys, types, json

os.environ['TOKEN_SECRET'] = 'test-secret-0123456789abcdef'
os.environ['INTERNAL_API_KEY'] = 'internal-key-abc'

PG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PG)

fake_flask = types.ModuleType('flask')
fake_flask.g = types.SimpleNamespace()
fake_flask.jsonify = lambda payload=None, **kw: dict(payload or {}, **kw)
fake_flask.request = types.SimpleNamespace(headers={})
sys.modules['flask'] = fake_flask

import auth_utils as au


class Req:
    def __init__(self, headers):
        self.headers = headers


def with_request(headers):
    """auth_utils 在导入时绑定了 flask.request，测试里同步替换两处引用。"""
    req = Req(headers)
    au.request = req
    fake_flask.request = req
    return req


def check(name, cond):
    print(('PASS' if cond else 'FAIL') + f'  {name}')
    if not cond:
        check.failed = True


check.failed = False

# 1. 正常签发与校验
t = au.issue_token(42, 'oABCDEF1234567890')
p = au.verify_token(t)
check('roundtrip uid', p and p['uid'] == 42)
check('roundtrip openid', p and p['openid'] == 'oABCDEF1234567890')

# 2. 篡改 payload / 签名 / 畸形输入
body, sig = t.split('.', 1)
check('篡改payload被拒绝', au.verify_token(body[:-2] + 'AA' + '.' + sig) is None)
check('篡改signature被拒绝', au.verify_token(body + '.' + 'x' * len(sig)) is None)
check('畸形令牌被拒绝', au.verify_token('abcdef') is None)
check('空令牌被拒绝', au.verify_token('') is None)
check('None被拒绝', au.verify_token(None) is None)

# 3. 过期
expired_body = au._b64url_encode(json.dumps({'uid': 7, 'openid': 'x', 'exp': 1}).encode())
check('过期令牌被拒绝', au.verify_token(expired_body + '.' + au._sign(expired_body)) is None)

# 4. 换密钥后旧令牌必须失效
old_secret = au.TOKEN_SECRET
au.TOKEN_SECRET = 'another-secret'
check('其他密钥签发的令牌被拒绝', au.verify_token(t) is None)
au.TOKEN_SECRET = old_secret

# 5. require_user 装饰器
@au.require_user
def view():
    return 'ok:' + str(au.g.user_id)


au.g = fake_flask.g

with_request({'Authorization': 'Bearer ' + t})
check('require_user 放行并注入 g.user_id', view() == 'ok:42')

with_request({})
check('require_user 无令牌返回401', view() == ({'code': 401, 'msg': 'Unauthorized'}, 401))

with_request({'Authorization': 'Bearer ' + t[:-4] + 'ZZZZ'})
check('require_user 伪造令牌返回401', view() == ({'code': 401, 'msg': 'Unauthorized'}, 401))

with_request({'Authorization': 'Token ' + t})
check('require_user 非Bearer头返回401', view() == ({'code': 401, 'msg': 'Unauthorized'}, 401))

# 6. 越权 user_id 拦截
with_request({'Authorization': 'Bearer ' + t})
view()
check('client user_id 一致放行', au.reject_client_user_mismatch(42) is None)
check('client user_id 字符串一致放行', au.reject_client_user_mismatch('42') is None)
check('client user_id 不一致拦截', au.reject_client_user_mismatch('99') == ({'code': 403, 'msg': 'user_id mismatch'}, 403))
check('client user_id 非数字拦截', au.reject_client_user_mismatch('abc') == ({'code': 403, 'msg': 'user_id mismatch'}, 403))
check('client user_id 缺失放行(以令牌为准)', au.reject_client_user_mismatch(None) is None)

# 7. 内部密钥
au.INTERNAL_API_KEY = 'internal-key-abc'
with_request({'X-Internal-Key': 'internal-key-abc'})
check('内部密钥正确放行', au.is_internal_request() is True)
with_request({'X-Internal-Key': 'internal-key-abd'})
check('内部密钥错误拒绝', au.is_internal_request() is False)
with_request({})
check('内部密钥缺失拒绝', au.is_internal_request() is False)
au.INTERNAL_API_KEY = ''
with_request({'X-Internal-Key': ''})
check('服务端未配置密钥时一律拒绝', au.is_internal_request() is False)

print('\n' + ('ALL PASS' if not check.failed else 'SOME FAILED'))
sys.exit(1 if check.failed else 0)
