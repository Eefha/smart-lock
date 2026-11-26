# 共享行李箱智能锁平台（pg_v5）

基于 TCP 长连接 + 微信小程序的共享智能锁管理系统。设备通过私有 TCP 协议上报心跳与开关锁状态，用户通过小程序扫码/支付/开锁，运营人员通过 Flask-Admin 后台管理设备、订单与数据。

## 功能

- 设备接入：TCP 长连接（`\xff\xff` 包头 + `\n` 分帧），支持粘包/半包处理、心跳在线检测、断线自动离线
- 小程序端：微信登录、设备绑定、模拟支付、开锁、设备状态查询、二维码解析
- 管理后台：仪表盘（在线率/订单趋势）、设备与二维码管理、订单筛选与 CSV/Excel 导出、单个/批量远程开锁、操作日志
- 订单状态机：建单 → 设备确认开锁 → 关锁结算，全程幂等

## 技术栈

| 层次 | 选型 |
| --- | --- |
| 语言 | Python 3（测试环境覆盖 3.8 / 3.14） |
| Web 框架 | Flask + Flask-Admin + Flask-SQLAlchemy + Flask-Login |
| 数据库 | MySQL 5.7+ / 8.0，pymysql + dbutils 连接池 |
| 设备通信 | 原生 socket + ThreadPoolExecutor（默认 100 worker） |
| 鉴权 | 自研 HMAC-SHA256 无状态令牌（不依赖 JWT/Redis），内部服务共享密钥 |
| 前端 | Bootstrap3 + jQuery + ECharts（CDN 引入） |
| 部署 | `nohup` 裸进程 + 自写 `service.sh`（无 Docker） |

## 目录结构

```
.
├── tcp_server.py          # 核心进程：TCP 服务(8002) + 小程序 API(8001)
├── admin_web.py           # 管理后台(8003)：Flask-Admin 后台 + 官网首页
├── config.py              # 全局配置与密钥自举（环境变量 > secrets.env > 自动生成）
├── db_utils.py            # 数据访问层：连接池、设备状态、订单状态机、审计日志
├── auth_utils.py          # 令牌签发/校验、内部接口密钥校验
├── create_table.py        # 数据库建表脚本
├── migrate_v5.sql         # v5 迁移：qr_code 可空 + 订单复合索引
├── migrate_v6.sql         # v6 迁移：电量百分比字段改为电压原值(0.01V)
├── service.sh             # 服务管理：start/stop/restart/status/logs
├── requirements.txt       # Python 依赖
├── secrets.env.example    # 配置样例（复制为 secrets.env 后填写）
├── templates/             # Jinja2 模板（官网首页 + 后台页面）
├── static/                # 静态资源
├── tests/                 # 无外部依赖的单元测试（桩掉 flask/pymysql）
└── logs/                  # 运行日志（.gitignore 已排除）
```

## 快速开始

### 1. 安装依赖

```bash
python3 -m pip install -r requirements.txt
```

### 2. 初始化数据库

先创建空数据库 `smart_lock`，然后配置 `secrets.env`（见下一步），再执行：

```bash
python3 create_table.py
```

如从旧版本升级，按 `migrate_v5.sql` → `migrate_v6.sql` 顺序执行（执行前先备份）。

### 3. 配置 secrets.env

```bash
cp secrets.env.example secrets.env
chmod 600 secrets.env
vi secrets.env
```

必须填写：`DB_PASSWORD`、`TOKEN_SECRET`、`INTERNAL_API_KEY`、`WX_APPID`、`WX_SECRET`。
`TOKEN_SECRET` / `INTERNAL_API_KEY` 可用如下命令生成：

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

> 重要：`tcp_server.py` 与 `admin_web.py` 必须读取同一份 `secrets.env`，否则后台调用开锁接口会 401。
> `ADMIN_INIT_PASSWORD` 留空时，系统会随机生成初始管理员口令并在日志中打印一次；请在首次登录后立即修改。

### 4. 启动服务

```bash
./service.sh start      # 启动 tcp_server.py + admin_web.py
./service.sh status     # 查看进程与端口
./service.sh logs       # 实时跟踪日志
./service.sh stop       # 停止
```

启动后端口：

| 端口 | 服务 |
| --- | --- |
| 8001 | 小程序 HTTP API（`tcp_server.py` 内的 Flask 线程） |
| 8002 | 设备 TCP 长连接 |
| 8003 | 管理后台 / 官网首页 |

## 核心设计约定

1. **只有 H0 心跳能写锁状态**：Q0 签到、L0/L1 回包、S5 查询都不覆盖 `is_locked`，避免"幽灵开锁"。
2. **电压存设备原值**（单位 0.01V，如 360 = 3.60V）：`None` 表示本次不写该列，避免 L0/L1 回包把电压清成 0；云端不做电量百分比推算。
3. **单轨订单**：一次开锁对应一条订单。`prepay` 建单并写 `pay_time`，`L0` 写 `unlock_status`，`L1` 关锁置 `status=1` 结算；SQL 带 `status=0` 守卫保证幂等。
4. **内部开锁隔离**：后台下发的开锁指令强制 `user_id=0`，避免管理员 ID 与用户 ID 撞号导致串单。
5. **密钥自举**：缺失密钥自动生成并持久化，多进程并发启动时以文件中首次写入的值为准，避免各自签发导致鉴权失败。
6. **在线状态双保险**：TCP 断开立即置离线 + 定时扫描超时（默认 10 分钟）兜底；连接被新会话接管时旧线程不会误置离线。

## 小程序 API（8001）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/wx/login` | 微信 code2session 换 openid，返回令牌 |
| POST | `/device/bind` | 绑定设备（需令牌） |
| GET | `/device/info` | 查询设备在线/锁定/电压状态（需令牌） |
| POST | `/pay/prepay` | 模拟支付建单（需令牌） |
| POST | `/pay/notify` | 支付回调占位 |
| POST | `/device/unlock` | 校验支付后下发 L0 开锁（需令牌） |
| GET | `/user/devices` | 用户绑定设备列表（需令牌） |
| GET | `/qrcode/resolve` | 二维码数字解析锁 ID（需令牌） |
| POST | `/open_lock` | 内部接口，需 `X-Internal-Key` 头，仅供后台调用 |

用户类接口用 `Authorization: Bearer <token>` 传递令牌；业务代码只信任令牌里的 `user_id`，请求体里的 `user_id` 仅做一致性校验。

## 测试

测试不依赖真实数据库或微信服务，全部通过桩对象隔离外部依赖：

```bash
cd tests
python test_config_env.py    # 配置加载与密钥自举
python test_auth_utils.py    # 令牌签验与越权拦截
python test_db_sql.py        # 生成的 SQL 文本与数据层约定
python test_tcp_logic.py     # 协议解析与订单状态机
```

## 安全提示

- `secrets.env` 含明文密钥，权限必须为 `600`，**切勿提交到版本库**。
- 当前支付为模拟实现（建单即视为已收款），`/pay/notify` 为空实现；接入真实支付前不可直接用于生产收费。
- 生产环境建议在 Flask 开发服务器前增加 Nginx 反向代理与 HTTPS。
- 默认管理员口令来自 `ADMIN_INIT_PASSWORD` 或随机生成，首次登录后请立即修改。
