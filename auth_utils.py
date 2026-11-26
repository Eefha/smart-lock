# auth_utils.py
"""
小程序用户令牌与内部接口密钥校验。

令牌为 HMAC-SHA256 签名的无状态字符串（payload.signature），只用标准库实现，
不引入 JWT/Redis 等新依赖，也不改动现有数据库表结构。
"""

import base64
import hashlib
import hmac
import json
import time
from functools import wraps

from flask import g, jsonify, request

from config import INTERNAL_API_KEY, TOKEN_SECRET, TOKEN_TTL_HOURS

INTERNAL_KEY_HEADER = 'X-Internal-Key'
AUTH_HEADER_PREFIX = 'Bearer '


def _b64url_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def _b64url_decode(segment):
    padding = '=' * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


def _sign(payload_segment):
    signature = hmac.new(
        TOKEN_SECRET.encode('utf-8'),
        payload_segment.encode('ascii'),
        hashlib.sha256
    ).digest()
    return _b64url_encode(signature)


def issue_token(user_id, openid):
    """签发令牌，有效期 TOKEN_TTL_HOURS 小时。"""
    payload = {
        'uid': int(user_id),
        'openid': openid or '',
        'exp': int(time.time()) + TOKEN_TTL_HOURS * 3600
    }
    body = _b64url_encode(json.dumps(payload, separators=(',', ':')).encode('utf-8'))
    return f'{body}.{_sign(body)}'


def verify_token(token):
    """校验签名与有效期，通过返回 payload，否则返回 None。"""
    if not token or token.count('.') != 1:
        return None

    body, signature = token.split('.', 1)
    if not hmac.compare_digest(_sign(body), signature):
        return None

    try:
        payload = json.loads(_b64url_decode(body).decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    try:
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        payload['uid'] = int(payload['uid'])
    except (KeyError, TypeError, ValueError):
        return None

    return payload


def _token_from_request():
    header = request.headers.get('Authorization', '')
    if header.startswith(AUTH_HEADER_PREFIX):
        return header[len(AUTH_HEADER_PREFIX):].strip()
    return ''


def require_user(view_func):
    """
    装饰器：要求请求携带合法令牌，并把令牌里的 user_id 写入 flask.g.user_id。
    业务代码一律使用 g.user_id，不再信任请求体里的 user_id。
    """

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        payload = verify_token(_token_from_request())
        if not payload:
            return jsonify({'code': 401, 'msg': 'Unauthorized'}), 401
        g.user_id = payload['uid']
        g.openid = payload.get('openid', '')
        return view_func(*args, **kwargs)

    return wrapper


def reject_client_user_mismatch(client_user_id):
    """
    老版本小程序仍会在请求里带 user_id。与令牌一致时放行，不一致时拒绝，
    防止拿别人的 user_id 越权操作。返回 None 表示可继续，返回 Response 表示应直接返回。
    """
    if client_user_id is None or client_user_id == '':
        return None
    try:
        if int(client_user_id) == g.user_id:
            return None
    except (TypeError, ValueError):
        pass
    return jsonify({'code': 403, 'msg': 'user_id mismatch'}), 403


def is_internal_request():
    """判断是否为管理后台等受信内部服务发来的指令下发请求。"""
    provided = request.headers.get(INTERNAL_KEY_HEADER, '')
    if not provided:
        return False
    return hmac.compare_digest(provided, INTERNAL_API_KEY)
