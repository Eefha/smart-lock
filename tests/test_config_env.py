"""config.py 的密钥加载行为：环境变量优先、secrets.env 兜底、空值不覆盖、缺失自动生成并落盘复用。"""
import os, shutil, subprocess, sys, tempfile

PG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PG)

# 本文件的用例要在干净环境里反复调用 _load_local_secrets，
# 先给进程自身补上密钥，否则 import config 会被 fail-fast 拦下。
os.environ.setdefault('TOKEN_SECRET', 'self_test_token')
os.environ.setdefault('INTERNAL_API_KEY', 'self_test_internal')

import config

failures = []
WATCH = ('TOKEN_SECRET', 'INTERNAL_API_KEY', 'DB_PASSWORD', 'TCP_PORT',
         'FLASK_SECRET_KEY', 'WX_APPID', 'EMPTY_ON_PURPOSE')


def check(name, cond):
    print(('PASS  ' if cond else 'FAIL  ') + name)
    if not cond:
        failures.append(name)


def load(text, pre_env=None):
    """
    在隔离的环境变量下调用 _load_local_secrets，
    返回 (加载条数, 加载瞬间的环境变量快照)，退出前恢复原环境。
    """
    workdir = tempfile.mkdtemp()
    path = os.path.join(workdir, 'secrets.env')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)

    saved = dict(os.environ)
    try:
        for key in WATCH:
            os.environ.pop(key, None)
            if pre_env and key in pre_env:
                os.environ[key] = pre_env[key]
        count = config._load_local_secrets(path)
        snapshot = {key: os.environ.get(key) for key in WATCH}
        return count, snapshot
    finally:
        os.environ.clear()
        os.environ.update(saved)
        shutil.rmtree(workdir, ignore_errors=True)


# 1. 正常键值能被读进来
count, snap = load("TOKEN_SECRET=aaa\nINTERNAL_API_KEY=bbb\n")
check('secrets.env 的密钥被加载', count == 2 and snap['TOKEN_SECRET'] == 'aaa' and snap['INTERNAL_API_KEY'] == 'bbb')

# 2. 真实环境变量优先，不被文件覆盖
count, snap = load("TCP_PORT=8002\n", pre_env={'TCP_PORT': '9002'})
check('已有环境变量不被文件覆盖', snap['TCP_PORT'] == '9002' and count == 0)

# 3. 注释/空行/没有等号的行一律忽略
count, snap = load("# 这是注释\n\n   \nNOT_A_LINE\nTOKEN_SECRET=only_one\n")
check('注释与无效行被忽略', count == 1 and snap['TOKEN_SECRET'] == 'only_one')

# 4. 空值等于没配（防止 DB_PASSWORD= 把密码覆盖成空串）
count, snap = load("DB_PASSWORD=\nEMPTY_ON_PURPOSE=\nTOKEN_SECRET=x\n")
check('空值不写入环境', snap['DB_PASSWORD'] is None and snap['EMPTY_ON_PURPOSE'] is None)
check('空值不影响同文件其它键', snap['TOKEN_SECRET'] == 'x')

# 5. 值里含 # 不能被截断（密码常见）
count, snap = load("TOKEN_SECRET=p@ss#w0rd\n")
check('值中的 # 被完整保留', snap['TOKEN_SECRET'] == 'p@ss#w0rd')

# 6. 引号与 export 前缀容错
count, snap = load('export TOKEN_SECRET="quoted_value"\n')
check('export 前缀与引号被剥掉', snap['TOKEN_SECRET'] == 'quoted_value')

# 7. 文件不存在时静默跳过，不抛异常
missing = os.path.join(tempfile.mkdtemp(), 'nope.env')
try:
    check('文件不存在返回0不抛错', config._load_local_secrets(missing) == 0)
except Exception as e:
    check(f'文件不存在返回0不抛错（{e}）', False)

# 8. 应急模式：干净沙箱里既没有环境变量也没有 secrets.env，必须能启动
sandbox = tempfile.mkdtemp()
shutil.copy(os.path.join(PG, 'config.py'), sandbox)
clean_env = {k: v for k, v in os.environ.items() if k not in ('TOKEN_SECRET', 'INTERNAL_API_KEY')}
clean_env['PYTHONIOENCODING'] = 'utf-8'
PRINT = ('import config;'
         'print(config.TOKEN_SECRET);'
         'print(config.INTERNAL_API_KEY);'
         'print(len(config.TOKEN_SECRET));'
         'print(config.SECRET_KEY == config.TOKEN_SECRET)')
run_kwargs = dict(cwd=sandbox, env=clean_env, capture_output=True,
                  encoding='utf-8', errors='replace')
r = subprocess.run([sys.executable, '-c', PRINT], **run_kwargs)
out_lines = (r.stdout or '').strip().splitlines()
check('缺密钥时不再阻断启动', r.returncode == 0)
check('自动生成 64 位十六进制密钥', len(out_lines) == 4 and out_lines[2] == '64')
check('生成的密钥不是占位字符串', len(out_lines) == 4 and out_lines[0] not in ('', 'your-secret-key-here', 'changeme'))
check('两个密钥互不相同', len(out_lines) == 4 and out_lines[0] != out_lines[1])
check('FLASK_SECRET_KEY 缺省时回退用 TOKEN_SECRET', len(out_lines) == 4 and out_lines[3] == 'True')
check('启动时打了告警', '自动生成' in (r.stderr or ''))

# 9. 生成的密钥必须落盘复用：第二次启动要拿到同一个值，否则令牌重启即失效
gen_path = os.path.join(sandbox, 'secrets.env')
check('自动生成了 secrets.env 文件', os.path.isfile(gen_path))
first = out_lines[0]
r2 = subprocess.run([sys.executable, '-c', PRINT], **run_kwargs)
second = (r2.stdout or '').strip().splitlines()
check('第二次启动复用同一密钥', r2.returncode == 0 and second and second[0] == first)
body = open(gen_path, encoding='utf-8').read().strip().splitlines()
check('文件里只有两行密钥未被重复追加', len(body) == 2)

# 10. 真实环境变量依然优先，且不覆盖已落盘的密钥
env3 = dict(clean_env)
env3['TOKEN_SECRET'] = 'from_real_env'
r3 = subprocess.run([sys.executable, '-c', PRINT], cwd=sandbox, env=env3,
                    capture_output=True, encoding='utf-8', errors='replace')
l3 = (r3.stdout or '').strip().splitlines()
check('真实环境变量优先于文件', r3.returncode == 0 and l3 and l3[0] == 'from_real_env')
check('优先时不回写覆盖文件', open(gen_path, encoding='utf-8').read().splitlines()[0].startswith('TOKEN_SECRET=' + first))

shutil.rmtree(sandbox, ignore_errors=True)

print()
print('ALL PASS' if not failures else f'{len(failures)} FAILED: {failures}')
sys.exit(1 if failures else 0)
