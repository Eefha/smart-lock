-- ============================================================
-- 共享行李锁 v5 迁移脚本
-- 执行前请先备份：mysqldump -umercury -p smart_lock > backup_before_v5.sql
-- 执行方式：mysql -umercury -p smart_lock < migrate_v5.sql
-- ============================================================

-- 【迁移1】lock_info.qr_code 允许为空
-- 原因：设备 Q0 首帧签到时后台还没有二维码编号，register_lock() 只能插入 NULL，
--       而原字段是 NOT NULL 且无默认值，MySQL 严格模式下报 1048，导致新设备永远注册不进后台。
-- 影响：仅放宽约束，不影响已有的 qr_code 数据和 /qrcode/resolve 查询逻辑。
ALTER TABLE `lock_info`
    MODIFY COLUMN `qr_code` VARCHAR(50) DEFAULT NULL COMMENT '二维码编号(10位纯数字，后台绑定后可扫码解析)';

-- 【迁移2】开锁确认索引
-- 原因：订单改为单轨后，L0/L1 回调需要按 (user_id, lock_id, status, unlock_status)
--       高频查询进行中的订单，原有索引只覆盖单列，加上复合索引避免全表扫描。
-- 影响：纯新增索引，不改字段、不改语义。
ALTER TABLE `order`
    ADD INDEX `idx_user_lock_status` (`user_id`, `lock_id`, `status`, `unlock_status`);

-- ============================================================
-- 说明：本次改动全部复用 order 表已有的字段，未新增业务字段。
--   status        0进行中 1已完成 2已取消   —— 语义不变
--   unlock_status 0失败   1成功   NULL未确认 —— 语义不变，此前从未被写入
--   pay_time                               —— 语义不变，此前由 prepay 直接置 status=1
-- ============================================================

-- 校验迁移结果
SELECT COLUMN_NAME, IS_NULLABLE, COLUMN_DEFAULT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'lock_info'
  AND COLUMN_NAME = 'qr_code';

SHOW INDEX FROM `order` WHERE Key_name = 'idx_user_lock_status';
