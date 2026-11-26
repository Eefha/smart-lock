-- migrate_v6.sql
-- 目的：删除电量百分比推算，改用设备上报的电压原值。
-- 变更：lock_info.battery (TINYINT UNSIGNED, 电量%) -> lock_info.voltage (SMALLINT UNSIGNED, 0.01V)
-- 环境：MySQL 5.7+ / 8.0，InnoDB，utf8mb4
--
-- 务必先备份：
--   mysqldump -u mercy -p smart_lock lock_info > lock_info_backup_$(date +%F).sql
--
-- 说明：
--   1) 这是改名+拓宽，不是删列。原 battery 列取值 0~100，转成 SMALLINT 无损。
--   2) 旧 battery 里存的是云端估算的百分比，换算成电压没有任何物理意义
--      （39 会被读成 0.39V），所以迁移后统一置 NULL，表示"尚未上报电压"，
--      等设备下一次 Q0/H0/S5 心跳自动写入真实电压。

-- 1. 列改名并拓宽类型
ALTER TABLE `lock_info`
    CHANGE COLUMN `battery` `voltage` SMALLINT UNSIGNED DEFAULT NULL
    COMMENT '电池电压(单位0.01V, 如360=3.60V)';

-- 2. 清掉旧估算值，避免把 39 当成 0.39V 显示
UPDATE `lock_info` SET `voltage` = NULL;

-- 3. 校验：应当看到 voltage 这一列，类型 smallint unsigned，可空，默认 NULL
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'lock_info' AND COLUMN_NAME = 'voltage';

-- 4. 校验：确认 battery 列已不存在（返回 0 行即为正确）
SELECT COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'lock_info' AND COLUMN_NAME = 'battery';

-- 5. 迁移后观察：设备心跳到达后 voltage 应出现 300~420 区间的值
SELECT lock_id, is_online, is_locked, voltage, last_heartbeat
FROM lock_info
ORDER BY last_heartbeat DESC
LIMIT 20;

-- ==================== 回滚（仅在需要退回旧版代码时执行）====================
-- ALTER TABLE `lock_info`
--     CHANGE COLUMN `voltage` `battery` TINYINT UNSIGNED DEFAULT 0
--     COMMENT '电量百分比(0-100)';
-- UPDATE `lock_info` SET `battery` = 0;
