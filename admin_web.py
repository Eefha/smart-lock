#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享行李箱智能锁 - 管理后台（含设备指令下发 + 中文界面 + UI优化）
"""

import datetime
import io
import csv
import requests
import logging
from flask import Flask, redirect, url_for, render_template, flash, request, Response, jsonify, send_file
from flask_admin import Admin, AdminIndexView, expose, BaseView
from flask_admin.contrib.sqla import ModelView
from flask_admin.actions import action
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import func
from markupsafe import Markup
from config import (DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, SECRET_KEY,
                    API_PORT, INTERNAL_API_KEY, ADMIN_HOST, ADMIN_PORT,
                    ADMIN_INIT_PASSWORD)
from auth_utils import INTERNAL_KEY_HEADER
import openpyxl
from openpyxl.styles import Font, Alignment
from openpyxl.utils import get_column_letter

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 向 8001 下发设备指令时使用的内部地址与共享密钥
OPEN_LOCK_URL = f'http://127.0.0.1:{API_PORT}/open_lock'
INTERNAL_HEADERS = {INTERNAL_KEY_HEADER: INTERNAL_API_KEY}


def _client_ip():
    """
    取管理员真实IP写进操作日志。
    后台经本机 Nginx 反代时 remote_addr 恒为 127.0.0.1，此时才回退读 X-Forwarded-For，
    避免直接信任客户端可伪造的转发头。
    """
    remote = request.remote_addr or ''
    if remote in ('127.0.0.1', '::1'):
        forwarded = request.headers.get('X-Forwarded-For', '')
        if forwarded:
            return forwarded.split(',')[0].strip()[:45]
    return remote[:45]


def _audit(action, content, admin_id=None):
    """把后台自身的登录/登出等管理动作写进 operation_log，created_at 交给数据库默认值。"""
    try:
        db.session.add(OperationLog(log_type='admin', action=action, content=content,
                                    admin_id=admin_id, ip_address=_client_ip()))
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.error(f"写入操作日志失败: {e}")

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['SQLALCHEMY_DATABASE_URI'] = f'mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['FLASK_ADMIN_SWATCH'] = 'cosmo'
db = SQLAlchemy(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# ==================== 模型定义 ====================
class AdminUser(db.Model, UserMixin):
    __tablename__ = 'admin'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(50))
    role = db.Column(db.String(20))
    last_login = db.Column(db.DateTime)
    last_ip = db.Column(db.String(45))
    status = db.Column(db.SmallInteger, default=1)
    created_at = db.Column(db.DateTime, default=datetime.datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

class LockInfo(db.Model):
    __tablename__ = 'lock_info'
    id = db.Column(db.Integer, primary_key=True)
    lock_id = db.Column(db.String(20), unique=True)
    qr_code = db.Column(db.String(50), unique=True)
    name = db.Column(db.String(100))
    location = db.Column(db.String(255))
    price = db.Column(db.Numeric(10,2), default=1.00)
    status = db.Column(db.SmallInteger, default=1)
    is_online = db.Column(db.SmallInteger, default=0)
    is_locked = db.Column(db.SmallInteger, default=1)
    voltage = db.Column(db.SmallInteger)
    use_count = db.Column(db.Integer, default=0)
    total_income = db.Column(db.Numeric(10,2), default=0.00)
    last_heartbeat = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.datetime.now, onupdate=datetime.datetime.now)

class User(db.Model):
    __tablename__ = 'user'
    id = db.Column(db.Integer, primary_key=True)
    openid = db.Column(db.String(128), unique=True)
    nickname = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    total_spent = db.Column(db.Numeric(10,2), default=0)
    total_unlocks = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime)

class Order(db.Model):
    __tablename__ = 'order'
    id = db.Column(db.Integer, primary_key=True)
    order_no = db.Column(db.String(32), unique=True)
    user_id = db.Column(db.Integer)
    lock_id = db.Column(db.String(20))
    qr_code = db.Column(db.String(50))
    amount = db.Column(db.Numeric(10,2))
    status = db.Column(db.SmallInteger)
    unlock_status = db.Column(db.SmallInteger)
    pay_time = db.Column(db.DateTime)
    unlock_time = db.Column(db.DateTime)
    lock_time = db.Column(db.DateTime)
    use_minutes = db.Column(db.Integer)
    created_at = db.Column(db.DateTime)

class OperationLog(db.Model):
    __tablename__ = 'operation_log'
    id = db.Column(db.BigInteger, primary_key=True)
    log_type = db.Column(db.String(20))
    action = db.Column(db.String(50))
    content = db.Column(db.Text)
    user_id = db.Column(db.Integer)
    lock_id = db.Column(db.String(20))
    admin_id = db.Column(db.Integer)
    ip_address = db.Column(db.String(45))
    created_at = db.Column(db.DateTime)

@login_manager.user_loader
def load_user(user_id):
    return AdminUser.query.get(int(user_id))

# ==================== 仪表盘 ====================
class MyAdminIndexView(AdminIndexView):
    @expose('/')
    @login_required
    def index(self):
        today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + datetime.timedelta(days=1)
        total_devices = LockInfo.query.count()
        online_devices = LockInfo.query.filter_by(is_online=1).count()

        # 今日订单：只统计已完成（status=1），与今日收入口径一致
        today_orders = Order.query.filter(
            Order.created_at >= today, Order.created_at < tomorrow, Order.status == 1
        ).count()
        # 今日进行中订单（用于补充显示，可选）
        today_pending = Order.query.filter(
            Order.created_at >= today, Order.created_at < tomorrow, Order.status == 0
        ).count()
        today_income = db.session.query(func.sum(Order.amount)).filter(
            Order.created_at >= today, Order.created_at < tomorrow, Order.status == 1
        ).scalar() or 0

        return self.render('admin/index.html',
                           total_devices=total_devices,
                           online_devices=online_devices,
                           today_orders=today_orders,
                           today_pending=today_pending,
                           today_income=today_income)

    @expose('/chart-data')
    @login_required
    def chart_data(self):
        total = LockInfo.query.count()
        online = LockInfo.query.filter_by(is_online=1).count()
        offline = total - online

        today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        dates = []
        counts = []
        for i in range(6, -1, -1):
            day_start = today - datetime.timedelta(days=i)
            day_end = day_start + datetime.timedelta(days=1)
            cnt = Order.query.filter(
                Order.created_at >= day_start, Order.created_at < day_end, Order.status == 1
            ).count()
            dates.append(day_start.strftime('%m-%d'))
            counts.append(cnt)

        return jsonify({
            'online_rate': {'online': online, 'offline': offline, 'total': total},
            'order_trend': {'dates': dates, 'counts': counts}
        })

# ==================== 设备管理（含指令下发） ====================
class LockInfoView(ModelView):
    column_list = ['lock_id', 'qr_code', 'name', 'location', 'is_online', 'is_locked', 'voltage', 'last_heartbeat', 'actions']
    # 电压由设备上报，不允许人工填写，否则后台会造出设备从没报过的读数
    form_excluded_columns = ['voltage']
    column_searchable_list = ['lock_id', 'qr_code', 'name']
    column_filters = ['is_online', 'is_locked', 'location']
    can_create = True
    can_edit = True
    can_delete = True
    column_labels = {
        'lock_id': '锁ID',
        'qr_code': '二维码数字',
        'name': '名称',
        'location': '位置',
        'is_online': '在线',
        'is_locked': '锁定',
        'voltage': '电压(V)',
        'last_heartbeat': '最后心跳',
        'actions': '操作'
    }

    column_formatters = {
        'voltage': lambda view, context, model, name: (
            f'{model.voltage / 100.0:.2f}' if model.voltage is not None
            else Markup('<span class="label label-default">无数据</span>')
        ),
        'actions': lambda view, context, model, name: (
            Markup(
                f'''
                <form action="{view.get_url('.unlock', lock_id=model.lock_id)}" method="POST" style="display:inline;">
                    <button type="submit"
                            class="btn btn-sm btn-success"
                            style="font-size:13px;padding:5px 14px;border-radius:5px;"
                            onclick="return confirm('确定要向设备 {model.lock_id} 下发开锁指令吗？')">
                        🔓 开锁
                    </button>
                </form>
                '''
            )
            if model.is_locked
            else Markup('<span class="label label-default">已解锁</span>')
        )
    }

    @expose('/unlock/<lock_id>', methods=['POST'])
    @login_required
    def unlock(self, lock_id):
        try:
            resp = requests.post(
                OPEN_LOCK_URL,
                json={'imei': lock_id, 'admin_id': current_user.id or 0, 'ip': _client_ip()},
                headers=INTERNAL_HEADERS,
                timeout=10
            )
            if resp.status_code == 200 and resp.json().get('code') == 0:
                flash(f'设备 {lock_id} 开锁指令已下发，等待设备回传确认', 'success')
            else:
                # 操作列现在只按锁状态出按钮，设备离线时点得动，
                # 这里要把后端给的 msg 单独取出来，而不是把整段 JSON 甩给用户。
                try:
                    reason = resp.json().get('msg') or resp.text
                except ValueError:
                    reason = resp.text
                flash(f'开锁失败：{reason}', 'danger')
        except Exception as e:
            flash(f'开锁异常：{str(e)}', 'danger')
        return redirect(url_for('.index_view'))

    @action('unlock_selected', '批量开锁', '确定要向选中的设备下发开锁指令吗？')
    def action_unlock_selected(self, ids):
        logger.info(f"批量开锁开始，选中设备主键: {ids}")
        if not ids:
            flash('未选中任何设备', 'warning')
            return

        success = 0
        fail = 0
        failed_details = []

        for pk in ids:
            lock = LockInfo.query.get(pk)
            if not lock:
                fail += 1
                failed_details.append(f'主键{pk}不存在')
                continue
            if not lock.is_online:
                fail += 1
                failed_details.append(f'{lock.lock_id} 离线')
                continue
            try:
                resp = requests.post(
                    OPEN_LOCK_URL,
                    json={'imei': lock.lock_id, 'admin_id': current_user.id or 0, 'ip': _client_ip()},
                    headers=INTERNAL_HEADERS,
                    timeout=10
                )
                if resp.status_code == 200 and resp.json().get('code') == 0:
                    success += 1
                else:
                    fail += 1
                    failed_details.append(f'{lock.lock_id} API返回失败')
            except requests.exceptions.Timeout:
                fail += 1
                failed_details.append(f'{lock.lock_id} 请求超时')
            except Exception as e:
                fail += 1
                failed_details.append(f'{lock.lock_id} 异常: {str(e)}')

        msg = f'批量开锁完成：成功 {success} 台，失败 {fail} 台'
        if failed_details:
            msg += '，失败详情：' + '；'.join(failed_details[:5])
            if len(failed_details) > 5:
                msg += f' 等{len(failed_details)}个'
        flash(msg, 'success' if fail == 0 else 'warning')
        logger.info(msg)

# ==================== 二维码管理 ====================
class QRCodeView(ModelView):
    column_list = ['qr_code', 'lock_id', 'name', 'location', 'created_at']
    column_labels = {
        'qr_code': '二维码数字',
        'lock_id': '锁ID',
        'name': '设备名称',
        'location': '位置',
        'created_at': '添加时间'
    }
    column_searchable_list = ['qr_code', 'lock_id']
    can_create = True
    can_edit = False
    can_delete = True
    list_template = 'admin/qrcode_list.html'

    def validate_form(self, form):
        qr_code = form.data.get('qr_code')
        lock_id = form.data.get('lock_id')
        if qr_code and (not qr_code.isdigit() or len(qr_code) != 10):
            flash('二维码数字必须为10位纯数字', 'danger')
            return False
        if lock_id and (not lock_id.isdigit() or len(lock_id) != 15):
            flash('锁ID必须为15位数字（IMEI格式）', 'danger')
            return False
        existing = LockInfo.query.filter_by(qr_code=qr_code).first()
        if existing:
            flash(f'二维码数字 {qr_code} 已存在，不能重复添加', 'danger')
            return False
        return super().validate_form(form)

    def on_model_change(self, form, model, is_created):
        if is_created:
            if model.price is None: model.price = 1.00
            if model.status is None: model.status = 1
            if model.is_online is None: model.is_online = 0
            if model.is_locked is None: model.is_locked = 1
        db.session.commit()

    @expose('/import', methods=['GET', 'POST'])
    @login_required
    def import_view(self):
        if request.method == 'POST':
            file = request.files.get('file')
            if not file:
                flash('请选择文件', 'danger')
                return redirect(url_for('.import_view'))
            overwrite = request.form.get('overwrite') == 'on'
            try:
                if file.filename.endswith('.csv'):
                    stream = io.StringIO(file.stream.read().decode('utf-8'))
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                elif file.filename.endswith(('.xlsx', '.xls')):
                    wb = openpyxl.load_workbook(file)
                    ws = wb.active
                    headers = [cell.value for cell in ws[1]]
                    rows = []
                    for row in ws.iter_rows(min_row=2, values_only=True):
                        rows.append(dict(zip(headers, row)))
                else:
                    flash('不支持的文件格式，请上传 CSV 或 Excel 文件', 'danger')
                    return redirect(url_for('.import_view'))
            except Exception as e:
                flash(f'文件解析失败: {str(e)}', 'danger')
                return redirect(url_for('.import_view'))

            if not rows:
                flash('文件为空或无有效数据', 'warning')
                return redirect(url_for('.import_view'))

            success_count = 0
            skip_count = 0
            error_count = 0
            errors = []
            overwrite_count = 0

            for row in rows:
                qr_code = str(row.get('qr_code') or row.get('二维码数字') or '').strip()
                lock_id = str(row.get('lock_id') or row.get('锁ID') or '').strip()
                name = str(row.get('name') or row.get('设备名称') or '').strip()
                location = str(row.get('location') or row.get('位置') or '').strip()

                if not qr_code or not lock_id:
                    error_count += 1
                    errors.append(f'缺少二维码数字或锁ID: {row}')
                    continue
                if not qr_code.isdigit() or len(qr_code) != 10:
                    error_count += 1
                    errors.append(f'二维码数字 "{qr_code}" 不是10位纯数字')
                    continue
                if not lock_id.isdigit() or len(lock_id) != 15:
                    error_count += 1
                    errors.append(f'锁ID "{lock_id}" 不是15位数字（IMEI格式）')
                    continue

                existing = LockInfo.query.filter_by(qr_code=qr_code).first()
                if existing:
                    if overwrite:
                        existing.lock_id = lock_id
                        existing.name = name if name else None
                        existing.location = location if location else None
                        db.session.commit()
                        success_count += 1
                        overwrite_count += 1
                    else:
                        skip_count += 1
                    continue

                lock = LockInfo.query.filter_by(lock_id=lock_id).first()
                if lock:
                    lock.qr_code = qr_code
                    lock.name = name if name else None
                    lock.location = location if location else None
                    db.session.commit()
                    success_count += 1
                else:
                    new_lock = LockInfo(
                        lock_id=lock_id, qr_code=qr_code,
                        name=name if name else None,
                        location=location if location else None,
                        price=1.00, status=1, is_online=0, is_locked=1
                    )
                    db.session.add(new_lock)
                    db.session.commit()
                    success_count += 1

            msg = f'导入完成：成功 {success_count} 条（其中覆盖 {overwrite_count} 条），跳过 {skip_count} 条，错误 {error_count} 条'
            if errors:
                msg += '，错误详情: ' + '；'.join(errors[:5])
                if len(errors) > 5:
                    msg += f' 等{len(errors)}个'
            flash(msg, 'success' if error_count == 0 else 'warning')
            return redirect(url_for('.import_view'))
        return self.render('admin/qrcode_import.html')

# ==================== 订单管理 ====================
ORDER_STATUS_LABELS = {0: ('进行中', 'warning'), 1: ('已完成', 'success'), 2: ('已取消', 'default')}
UNLOCK_STATUS_LABELS = {None: ('待设备确认', 'warning'), 0: ('开锁失败', 'danger'), 1: ('已开锁', 'success')}


def _status_badge(mapping, value):
    label, kind = mapping.get(value, ('未知', 'default'))
    return Markup(f'<span class="label label-{kind}">{label}</span>')


class OrderView(ModelView):
    column_list = ['order_no', 'user_id', 'lock_id', 'amount', 'status', 'unlock_status',
                   'pay_time', 'unlock_time', 'lock_time', 'use_minutes']
    column_searchable_list = ['order_no', 'user_id', 'lock_id']
    column_filters = ['status', 'unlock_status', 'created_at', 'user_id', 'lock_id']
    can_create = False
    can_edit = False
    can_delete = True
    column_labels = {
        'order_no': '订单号', 'user_id': '用户ID', 'lock_id': '锁ID',
        'amount': '金额(元)', 'status': '订单状态', 'unlock_status': '设备确认',
        'pay_time': '支付时间', 'unlock_time': '开锁时间',
        'lock_time': '关锁时间', 'use_minutes': '时长(分钟)'
    }
    column_formatters = {
        'status': lambda v, c, m, n: _status_badge(ORDER_STATUS_LABELS, m.status),
        'unlock_status': lambda v, c, m, n: _status_badge(UNLOCK_STATUS_LABELS, m.unlock_status),
        'pay_time': lambda v, c, m, n: m.pay_time.strftime('%Y-%m-%d %H:%M:%S') if m.pay_time else '-',
        'unlock_time': lambda v, c, m, n: m.unlock_time.strftime('%Y-%m-%d %H:%M:%S') if m.unlock_time else '-',
        'lock_time': lambda v, c, m, n: m.lock_time.strftime('%Y-%m-%d %H:%M:%S') if m.lock_time else '-'
    }

    @expose('/export/<format>')
    @login_required
    def export(self, format):
        filters = request.args
        query = Order.query
        if 'user_id' in filters and filters['user_id']:
            query = query.filter(Order.user_id == filters['user_id'])
        if 'status' in filters and filters['status']:
            query = query.filter(Order.status == int(filters['status']))
        unlock_filter = filters.get('unlock_status')
        if unlock_filter == 'none':
            query = query.filter(Order.unlock_status.is_(None))
        elif unlock_filter in ('0', '1'):
            query = query.filter(Order.unlock_status == int(unlock_filter))
        orders = query.all()

        export_headers = ['订单号', '用户ID', '锁ID', '金额', '订单状态', '设备确认',
                          '支付时间', '开锁时间', '关锁时间', '使用时长(分钟)']
        status_map = {0: '进行中', 1: '已完成', 2: '已取消'}
        unlock_map = {None: '待设备确认', 0: '开锁失败', 1: '已开锁'}

        def export_row(o):
            return [
                o.order_no, o.user_id, o.lock_id, float(o.amount),
                status_map.get(o.status, '未知'),
                unlock_map.get(o.unlock_status, '未知'),
                o.pay_time.strftime('%Y-%m-%d %H:%M:%S') if o.pay_time else '',
                o.unlock_time.strftime('%Y-%m-%d %H:%M:%S') if o.unlock_time else '',
                o.lock_time.strftime('%Y-%m-%d %H:%M:%S') if o.lock_time else '',
                o.use_minutes or 0
            ]

        if format == 'csv':
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(export_headers)
            for o in orders:
                writer.writerow(export_row(o))
            response = Response(output.getvalue(), mimetype='text/csv')
            response.headers['Content-Disposition'] = f'attachment; filename=orders_{datetime.datetime.now().strftime("%Y%m%d")}.csv'
            return response

        elif format == 'excel':
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = '订单数据'
            for col, title in enumerate(export_headers, 1):
                cell = ws.cell(row=1, column=col, value=title)
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal='center')
            for row, o in enumerate(orders, 2):
                for col, value in enumerate(export_row(o), 1):
                    ws.cell(row=row, column=col, value=value)
            for col in range(1, len(export_headers) + 1):
                ws.column_dimensions[get_column_letter(col)].width = 18
            output = io.BytesIO()
            wb.save(output)
            output.seek(0)
            return send_file(output, download_name=f'orders_{datetime.datetime.now().strftime("%Y%m%d")}.xlsx',
                             as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        return '不支持的格式', 400

# ==================== 用户管理和日志 ====================
class UserView(ModelView):
    column_list = ['id', 'openid', 'nickname', 'phone', 'total_spent', 'total_unlocks', 'created_at']
    column_searchable_list = ['openid', 'nickname', 'phone']
    can_create = False
    can_edit = True

class LogView(ModelView):
    column_list = ['log_type', 'action', 'lock_id', 'user_id', 'admin_id', 'ip_address', 'content', 'created_at']
    column_searchable_list = ['log_type', 'action', 'lock_id']
    column_filters = ['log_type', 'action', 'lock_id', 'admin_id']
    can_create = False
    can_edit = False
    can_delete = False
    column_labels = {
        'log_type': '类型', 'action': '动作', 'lock_id': '锁ID', 'user_id': '用户ID',
        'admin_id': '管理员ID', 'ip_address': '来源IP', 'content': '详情', 'created_at': '时间'
    }

class LogoutView(BaseView):
    @expose('/')
    @login_required
    def index(self):
        return redirect(url_for('logout'))

# ==================== Admin 初始化 ====================
admin = Admin(
    app,
    name='共享锁管理平台',
    index_view=MyAdminIndexView(url='/admin'),
    template_mode='bootstrap3',
    url='/admin',
    base_template='custom_admin_base.html'
)

admin.add_view(LockInfoView(LockInfo, db.session, name='设备管理', endpoint='lockinfo'))
admin.add_view(QRCodeView(LockInfo, db.session, name='二维码管理', endpoint='qrcode'))
admin.add_view(OrderView(Order, db.session, name='订单管理', endpoint='order'))
admin.add_view(UserView(User, db.session, name='用户管理', endpoint='user'))
admin.add_view(LogView(OperationLog, db.session, name='操作日志', endpoint='log'))
admin.add_view(LogoutView(name='退出登录', endpoint='logoutview'))

# ==================== 官网首页 ====================
@app.route('/')
def company_home():
    return render_template('index.html')

# ==================== 登录 ====================
def _safe_next(target):
    """只允许站内相对路径，堵住 /login?next=https://外部域 的开放重定向。"""
    if not target or not target.startswith('/') or target.startswith('//'):
        return '/admin'
    return target


@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect('/admin')
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = AdminUser.query.filter_by(username=username, status=1).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            user.last_login = datetime.datetime.now()
            user.last_ip = _client_ip()
            db.session.commit()
            _audit('管理员登录', f'用户名={username}', admin_id=user.id)
            flash('登录成功', 'success')
            return redirect(_safe_next(request.args.get('next')))
        else:
            # 用户名不存在或密码错误统一提示，并留审计记录便于排查撞库
            _audit('登录失败', f'用户名={username}',
                   admin_id=(user.id if user else None))
            flash('用户名或密码错误', 'danger')
    return render_template('admin/login.html')

@app.route('/logout')
@login_required
def logout():
    _audit('管理员登出', f'用户名={current_user.username}', admin_id=current_user.id)
    logout_user()
    flash('已退出', 'info')
    return redirect('/')

# ==================== 初始化管理员 ====================
def init_admin():
    with app.app_context():
        if not AdminUser.query.filter_by(username='admin').first():
            import secrets as _secrets
            # 口令来源优先级：secrets.env / 环境变量 ADMIN_INIT_PASSWORD > 随机生成。
            # 随机生成时只打印一次到启动日志，提示运维尽快登录改密；源码不再写死默认口令。
            initial_password = ADMIN_INIT_PASSWORD.strip()
            generated = not initial_password
            if generated:
                initial_password = _secrets.token_urlsafe(12)
            hashed = generate_password_hash(initial_password)
            admin = AdminUser(username='admin', password=hashed, name='超级管理员', role='super', status=1)
            db.session.add(admin)
            db.session.commit()
            print('✅ 初始管理员已创建: admin')
            if generated:
                print('=' * 60)
                print('⚠️  未配置 ADMIN_INIT_PASSWORD，已随机生成初始口令（仅此一次显示）：')
                print('    ' + initial_password)
                print('   请登录后立即修改；口令未写入源码和仓库，重启不会再次打印。')
                print('=' * 60)
            else:
                print('   口令来自 ADMIN_INIT_PASSWORD 配置。')

if __name__ == '__main__':
    init_admin()
    app.run(host=ADMIN_HOST, port=ADMIN_PORT, debug=False)