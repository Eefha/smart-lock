#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
共享行李箱智能锁 - 数据库表结构初始化（匹配现有系统设计）
环境：Python 3.8, pymysql, MySQL 5.7+
"""

import pymysql
from pymysql.constants import CLIENT

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME

# ==================== 数据库连接配置 ====================
# 与其余服务共用 config.py，口令只从环境变量 / secrets.env 读取，源码不落明文。
DB_CONFIG = {
    'host': DB_HOST,
    'port': DB_PORT,
    'user': DB_USER,
    'password': DB_PASSWORD,
    'database': DB_NAME,
    'charset': 'utf8mb4',
    'client_flag': CLIENT.MULTI_STATEMENTS,
}

# ==================== 建表 SQL 语句 ====================
CREATE_TABLES_SQL = """
-- 管理员表
CREATE TABLE IF NOT EXISTS `admin` (
    `id` INT AUTO_INCREMENT PRIMARY KEY COMMENT '管理员ID',
    `username` VARCHAR(50) NOT NULL UNIQUE COMMENT '登录用户名',
    `password` VARCHAR(255) NOT NULL COMMENT '密码哈希(bcrypt)',
    `name` VARCHAR(50) DEFAULT NULL COMMENT '姓名',
    `role` VARCHAR(20) DEFAULT NULL COMMENT '角色',
    `last_login` DATETIME DEFAULT NULL COMMENT '最后登录时间',
    `last_ip` VARCHAR(45) DEFAULT NULL COMMENT '最后登录IP',
    `status` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '状态 1启用 0禁用',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='管理员表';

-- 系统配置表
CREATE TABLE IF NOT EXISTS `config` (
    `id` INT AUTO_INCREMENT PRIMARY KEY COMMENT '配置ID',
    `config_key` VARCHAR(100) NOT NULL UNIQUE COMMENT '配置键',
    `config_value` TEXT COMMENT '配置值',
    `remark` VARCHAR(255) DEFAULT '' COMMENT '备注',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统配置表';

-- 锁信息表
CREATE TABLE IF NOT EXISTS `lock_info` (
    `id` INT AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    `lock_id` VARCHAR(20) NOT NULL UNIQUE COMMENT '锁唯一ID(如OM019)',
    `qr_code` VARCHAR(50) DEFAULT NULL COMMENT '二维码编号(10位纯数字，后台绑定后可扫码解析)',
    `name` VARCHAR(100) DEFAULT NULL COMMENT '锁名称',
    `location` VARCHAR(255) DEFAULT NULL COMMENT '安装位置',
    `price` DECIMAL(10,2) NOT NULL DEFAULT 1.00 COMMENT '使用价格(元)',
    `status` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '设备状态 1正常 0禁用',
    `is_online` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否在线 1在线 0离线',
    `is_locked` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否锁定 1锁定 0解锁',
    `voltage` SMALLINT UNSIGNED DEFAULT NULL COMMENT '电池电压(单位0.01V, 如360=3.60V)',
    `use_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '累计使用次数',
    `total_income` DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '累计收入',
    `last_heartbeat` DATETIME DEFAULT NULL COMMENT '最后心跳时间',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '入库时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
    INDEX idx_is_online (`is_online`),
    INDEX idx_is_locked (`is_locked`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='锁信息表';

-- 用户信息表
CREATE TABLE IF NOT EXISTS `user` (
    `id` INT AUTO_INCREMENT PRIMARY KEY COMMENT '用户ID',
    `openid` VARCHAR(128) NOT NULL UNIQUE COMMENT '微信openid',
    `unionid` VARCHAR(128) DEFAULT NULL COMMENT '微信unionid',
    `nickname` VARCHAR(100) DEFAULT '' COMMENT '昵称',
    `avatar_url` VARCHAR(500) DEFAULT '' COMMENT '头像URL',
    `gender` TINYINT DEFAULT 0 COMMENT '性别 0未知 1男 2女',
    `phone` VARCHAR(20) DEFAULT NULL COMMENT '手机号',
    `total_spent` DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT '累计消费金额',
    `total_unlocks` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '累计开锁次数',
    `session_key` VARCHAR(255) DEFAULT NULL COMMENT '微信session_key',
    `last_login_at` DATETIME DEFAULT NULL COMMENT '最后登录时间',
    `status` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '状态 1正常 0禁用',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户信息表';

-- 订单表
CREATE TABLE IF NOT EXISTS `order` (
    `id` INT AUTO_INCREMENT PRIMARY KEY COMMENT '订单ID',
    `order_no` VARCHAR(32) NOT NULL UNIQUE COMMENT '订单号',
    `user_id` INT NOT NULL COMMENT '用户ID',
    `lock_id` VARCHAR(20) NOT NULL COMMENT '锁ID(关联lock_info.lock_id)',
    `qr_code` VARCHAR(50) DEFAULT NULL COMMENT '二维码编号(冗余)',
    `amount` DECIMAL(10,2) NOT NULL DEFAULT 1.00 COMMENT '订单金额',
    `status` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '订单状态 0进行中 1已完成 2已取消',
    `unlock_status` TINYINT(1) DEFAULT NULL COMMENT '开锁状态 0失败 1成功',
    `pay_time` DATETIME DEFAULT NULL COMMENT '支付时间',
    `transaction_id` VARCHAR(64) DEFAULT NULL COMMENT '微信支付交易号',
    `unlock_time` DATETIME DEFAULT NULL COMMENT '开锁时间',
    `lock_time` DATETIME DEFAULT NULL COMMENT '关锁时间',
    `use_minutes` INT UNSIGNED DEFAULT 0 COMMENT '使用时长(分钟)',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    INDEX idx_user_id (`user_id`),
    INDEX idx_lock_id (`lock_id`),
    INDEX idx_order_no (`order_no`),
    INDEX idx_user_lock_status (`user_id`, `lock_id`, `status`, `unlock_status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='订单表';

-- 操作日志表
CREATE TABLE IF NOT EXISTS `operation_log` (
    `id` BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '日志ID',
    `log_type` VARCHAR(20) NOT NULL COMMENT '日志类型(heartbeat,lock,admin等)',
    `order_id` INT DEFAULT NULL COMMENT '关联订单ID',
    `user_id` INT DEFAULT NULL COMMENT '用户ID',
    `lock_id` VARCHAR(20) DEFAULT NULL COMMENT '锁ID',
    `qr_code` VARCHAR(50) DEFAULT NULL COMMENT '二维码编号',
    `admin_id` INT DEFAULT NULL COMMENT '管理员ID',
    `action` VARCHAR(50) NOT NULL COMMENT '动作描述(设备心跳/设备锁车/管理员解锁等)',
    `content` TEXT COMMENT '详细内容',
    `ip_address` VARCHAR(45) DEFAULT NULL COMMENT '操作IP',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录时间',
    INDEX idx_log_type (`log_type`),
    INDEX idx_created_at (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='操作日志表';
"""

def create_tables():
    """连接数据库并执行建表"""
    try:
        conn = pymysql.connect(**DB_CONFIG)
        print("数据库连接成功！")
        with conn.cursor() as cursor:
            statements = [s.strip() for s in CREATE_TABLES_SQL.split(';') if s.strip()]
            for stmt in statements:
                print(f"执行: {stmt[:80]}...")
                cursor.execute(stmt)
            conn.commit()
            print("所有表创建成功！")
    except pymysql.MySQLError as e:
        print(f"数据库错误: {e}")
    finally:
        if 'conn' in locals() and conn.open:
            conn.close()
            print("数据库连接已关闭。")

if __name__ == '__main__':
    create_tables()