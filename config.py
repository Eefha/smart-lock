# config.py
# ============================================================
# 共享行李锁 v5 配置
#
# 取值优先级：真实环境变量 > 同目录 secrets.env > 首次启动自动生成的随机密钥。
#
# 2026-10-27 应急调整：TOKEN_SECRET / INTERNAL_API_KEY 缺失时不再阻断启动，
# 而是自动生成 32 字节随机密钥、写入 secrets.env 并复用，同时在 stderr 打告警。
# 这样做的原因：服务必须能起来（裸 nohup、无运维），而把密钥写死在源码里
# 等于任何人都能伪造用户令牌、任何人能调 /open_lock 开锁。
#
# 仍然支持的正式做法（有空时回补，不改代码）：
#   cp secrets.env.example secrets.env && chmod 600 secrets.env && vi secrets.env
#   python3 -c "import secrets; print('TOKEN_SECRET=' + secrets.token_hex(32))"
#
# 约定：
#   - secrets.env 里 KEY=VALUE 一行一条，值不要加行内注释（密码可能含 #，不自动剥离）；
#   - KEY= 这种空值会被忽略，等价于"没配"，所以 DB_PASSWORD 要么填对要么整行注释；
#   - tcp_server 与 admin_web 必须读同一份 secrets.env（同目录即自动一致），
#     否则后台开锁会 401。
# ============================================================

import os
import secrets as _secrets

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(CONFIG_DIR, 'secrets.env')

# 已自动生成的密钥缓存，用于在 stderr 只提示一次
_AUTO_GENERATED = []


def _load_local_secrets(path=SECRETS_FILE):
    """
    把同目录 secrets.env 里的键值补进 os.environ。

    只补环境变量里还没有的键，所以 `export TCP_PORT=9002 python3 tcp_server.py`
    这类临时覆盖依然生效。文件不存在时静默跳过。
    """
    if not os.path.isfile(path):
        return 0

    loaded = 0
    with open(path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            key = key.strip()
            if key.startswith('export '):
                key = key[len('export '):].strip()
            value = value.strip().strip('"').strip("'")
            if not key or not value:
                continue
            if key not in os.environ:
                os.environ[key] = value
                loaded += 1
    return loaded


def _first_value_in_file(name, path=SECRETS_FILE):
    """读取 secrets.env 中该键第一次出现的值（用于并发启动时对齐密钥）。"""
    if not os.path.isfile(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, value = line.partition('=')
            if key.strip() == name:
                value = value.strip().strip('"').strip("'")
                return value or None
    return None


def _bootstrap_secret(name, usage):
    """
    密钥缺失时自动生成并持久化，返回最终生效的值。

    两个进程可能同时首次启动，所以先追加再回头读"第一条该键的值"：
    谁先写进去，两边就都用谁的值，不会各签各的密钥导致后台开锁 401。
    """
    value = os.getenv(name, '').strip()
    if value:
        return value

    generated = _secrets.token_hex(32)
    try:
        fd = os.open(SECRETS_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(f"{name}={generated}\n")
    except OSError:
        # 落盘失败也不能挡住启动：本次用内存里的随机值，重启后会重新生成
        pass
    else:
        _AUTO_GENERATED.append((name, usage))
        stored = _first_value_in_file(name)
        if stored:
            value = stored
            os.environ[name] = value
            return value

    os.environ[name] = generated
    return generated


def _warn_after_bootstrap():
    if not _AUTO_GENERATED:
        return
    import sys
    lines = ['', '=' * 60,
             '[config] 已自动生成并持久化以下密钥（写入 %s）：' % SECRETS_FILE]
    for name, usage in _AUTO_GENERATED:
        lines.append('    %s  （用途：%s）' % (name, usage))
    lines += [
        '服务可正常运行；密钥为强随机值且保存在本地文件，未进入源码。',
        '有空时建议手工换成自己的值（换完已签发的小程序令牌会失效，需重新登录）：',
        '    vi secrets.env && chmod 600 secrets.env',
        '=' * 60, '']
    sys.stderr.write('\n'.join(lines) + '\n')
    sys.stderr.flush()


def _warn_if_loose_permissions(path=SECRETS_FILE):
    """密钥文件权限过松只告警不阻断：阻断了服务就停摆，反而更危险。"""
    if not os.path.isfile(path):
        return
    try:
        mode = oct(os.stat(path).st_mode & 0o777)[-3:]
        if mode != '600':
            import sys
            sys.stderr.write(
                "[config] 警告：%(path)s 权限为 %(mode)s，建议 chmod 600（文件中含明文密钥）\n"
                % {'path': path, 'mode': mode}
            )
    except OSError:
        pass


# 必须在下面任何 os.getenv 之前执行，否则读不到值
_load_local_secrets()

# ==================== 数据库配置 ====================
# 口令不再内置默认值：必须经由真实环境变量或同目录 secrets.env 提供。
# 未配置时打印告警并留空，连接池取连接时直接失败——宁可显式报错，也不用弱口令连库。
DB_HOST = os.getenv('DB_HOST', '127.0.0.1')   # 本地连接，不要改成域名
DB_PORT = int(os.getenv('DB_PORT', '3306'))
DB_USER = os.getenv('DB_USER', 'mercy')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')
DB_NAME = os.getenv('DB_NAME', 'smart_lock')
DB_POOL_SIZE = int(os.getenv('DB_POOL_SIZE', '20'))

if not DB_PASSWORD:
    import sys
    sys.stderr.write(
        "[config] 警告：DB_PASSWORD 未配置，数据库连接会失败。\n"
        "         请在同目录 secrets.env 写入 DB_PASSWORD=真实密码（chmod 600），\n"
        "         或通过环境变量导出后再启动服务。\n"
    )
    sys.stderr.flush()

# ==================== TCP 服务配置 ====================
TCP_HOST = os.getenv('TCP_HOST', '0.0.0.0')
TCP_PORT = int(os.getenv('TCP_PORT', '8002'))
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '100'))          # 线程池大小（处理并发连接）
API_PORT = int(os.getenv('API_PORT', '8001'))               # 小程序 API 端口

# ==================== 离线判定 ====================
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv('HEARTBEAT_TIMEOUT_SECONDS', '600'))  # 10 分钟
OFFLINE_SCAN_INTERVAL = int(os.getenv('OFFLINE_SCAN_INTERVAL', '60'))           # 60 秒扫一次

# ==================== 小程序登录鉴权 ====================
TOKEN_SECRET = _bootstrap_secret('TOKEN_SECRET', '签发/校验小程序用户令牌')
TOKEN_TTL_HOURS = int(os.getenv('TOKEN_TTL_HOURS', '168'))  # 令牌有效期，默认 7 天

# 微信小程序凭证（未配置时 /wx/login 会返回明确错误，其余接口不受影响）
WX_APPID = os.getenv('WX_APPID', '')
WX_SECRET = os.getenv('WX_SECRET', '')
WX_CODE2SESSION_URL = 'https://api.weixin.qq.com/sns/jscode2session'

# 后端服务之间调用的共享密钥（管理后台 -> 8001 /open_lock）
INTERNAL_API_KEY = _bootstrap_secret('INTERNAL_API_KEY', '管理后台调用 /open_lock 的下发密钥')

_warn_after_bootstrap()
_warn_if_loose_permissions()

# ==================== 业务参数 ====================
DEFAULT_PRICE = float(os.getenv('DEFAULT_PRICE', '1.00'))
PAY_VALID_MINUTES = int(os.getenv('PAY_VALID_MINUTES', '5'))  # 支付后多久内允许开锁

# ==================== Flask-Admin 后台配置 ====================
ADMIN_HOST = os.getenv('ADMIN_HOST', '0.0.0.0')
ADMIN_PORT = int(os.getenv('ADMIN_PORT', '8003'))
SECRET_KEY = os.getenv('FLASK_SECRET_KEY', '') or TOKEN_SECRET
# 首次启动创建初始管理员时使用的口令；留空则随机生成并在后台日志打印一次。
# 未在 secrets.env 配置 ADMIN_INIT_PASSWORD 时不会写死任何默认口令。
ADMIN_INIT_PASSWORD = os.getenv('ADMIN_INIT_PASSWORD', '')

# ==================== API 基础地址（仅用于展示） ====================
API_BASE_URL = os.getenv('API_BASE_URL', 'https://www.anan.uno')
