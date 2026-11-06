-- 宁夏葡萄酒智能问答系统；MySQL 8.0.19+ / 8.4。
-- 仅提供脚本，由用户执行。使用同版本结构可重复运行；不删除旧表或数据。
SET NAMES utf8mb4;
CREATE DATABASE IF NOT EXISTS `Wine_QA_System`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE `Wine_QA_System`;

CREATE TABLE IF NOT EXISTS `source_document` (
  `source_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `title` VARCHAR(512) NOT NULL,
  `publisher` VARCHAR(512) NULL,
  `url` TEXT NULL,
  `source_type` VARCHAR(64) NOT NULL,
  `evidence_form` VARCHAR(64) NOT NULL,
  `published_at` VARCHAR(40) NULL COMMENT '保留日期精度，不补造日期',
  `accessed_date` VARCHAR(40) NULL,
  `raw_file` TEXT NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`source_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 统一实体ID目录，使酒庄、产品、辅助概念的多类型别名也有真实外键。
CREATE TABLE IF NOT EXISTS `entity_registry` (
  `entity_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `entity_type` ENUM('Winery','Product','Region','GrapeVariety','Flavor','Concept') NOT NULL,
  `name` VARCHAR(512) NOT NULL,
  `source_ids` JSON NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`entity_id`),
  KEY `idx_entity_type_name` (`entity_type`, `name`(191)),
  CONSTRAINT `ck_entity_sources_array` CHECK (JSON_TYPE(`source_ids`) = 'ARRAY')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `region` (
  `region_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `name` VARCHAR(512) NOT NULL,
  `region_kind` ENUM('wine_region','administrative','unknown') NOT NULL,
  PRIMARY KEY (`region_id`),
  CONSTRAINT `fk_region_registry` FOREIGN KEY (`region_id`) REFERENCES `entity_registry` (`entity_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `winery` (
  `winery_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `name` VARCHAR(512) NOT NULL,
  `company_name` VARCHAR(512) NULL,
  `region_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  `location_raw` TEXT NULL,
  `address_raw` TEXT NULL,
  `address_normalized` TEXT NULL,
  `founded_year` SMALLINT UNSIGNED NULL,
  `company_founded_year` SMALLINT UNSIGNED NULL,
  `introduction` TEXT NULL,
  `website` TEXT NULL,
  `telephone` VARCHAR(128) NULL,
  `email` VARCHAR(320) NULL,
  `source_ids` JSON NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`winery_id`),
  KEY `idx_winery_name` (`name`(191)),
  CONSTRAINT `fk_winery_registry` FOREIGN KEY (`winery_id`) REFERENCES `entity_registry` (`entity_id`),
  CONSTRAINT `fk_winery_region` FOREIGN KEY (`region_id`) REFERENCES `region` (`region_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `grape_variety` (
  `grape_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `name` VARCHAR(512) NOT NULL,
  PRIMARY KEY (`grape_id`),
  KEY `idx_grape_name` (`name`(191)),
  CONSTRAINT `fk_grape_registry` FOREIGN KEY (`grape_id`) REFERENCES `entity_registry` (`entity_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `product` (
  `product_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `winery_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `name` VARCHAR(512) NOT NULL,
  `series` VARCHAR(255) NULL,
  `record_scope` ENUM('specific_variant','unspecified_product_page','catalog_candidate','mixed_variant') NOT NULL,
  `vintage` SMALLINT UNSIGNED NULL,
  `vintage_status` ENUM('specified','non_vintage','unspecified','mixed') NOT NULL,
  `volume_ml` DECIMAL(12,3) NULL,
  `color` ENUM('red','white','rose','other') NULL,
  `sweetness` ENUM('dry','semi_dry','semi_sweet','sweet','other') NULL,
  `sweetness_raw` VARCHAR(128) NULL,
  `is_sparkling` BOOLEAN NULL,
  `alcohol_pct_vol` DECIMAL(6,3) NULL,
  `region_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
  `grapes_complete` BOOLEAN NULL,
  `description` TEXT NULL,
  `price_cny` DECIMAL(14,2) NULL COMMENT '未核实当前价格不得补入历史或参考报价',
  `strict_product_filter_eligible` BOOLEAN NOT NULL,
  `field_filter_eligibility` JSON NOT NULL,
  `field_filter_reasons` JSON NOT NULL,
  `source_ids` JSON NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`product_id`),
  KEY `idx_product_name` (`name`(191)),
  KEY `idx_product_filter` (`color`, `sweetness`, `volume_ml`),
  KEY `idx_product_vintage` (`vintage`),
  CONSTRAINT `fk_product_registry` FOREIGN KEY (`product_id`) REFERENCES `entity_registry` (`entity_id`),
  CONSTRAINT `fk_product_winery` FOREIGN KEY (`winery_id`) REFERENCES `winery` (`winery_id`),
  CONSTRAINT `fk_product_region` FOREIGN KEY (`region_id`) REFERENCES `region` (`region_id`),
  CONSTRAINT `ck_product_vintage` CHECK ((`vintage_status` = 'specified' AND `vintage` IS NOT NULL) OR (`vintage_status` <> 'specified' AND `vintage` IS NULL)),
  CONSTRAINT `ck_product_volume` CHECK (`volume_ml` IS NULL OR `volume_ml` > 0),
  CONSTRAINT `ck_product_alcohol` CHECK (`alcohol_pct_vol` IS NULL OR `alcohol_pct_vol` BETWEEN 0 AND 100),
  CONSTRAINT `ck_product_sparkling` CHECK (`is_sparkling` IS NULL OR `is_sparkling` IN (0,1)),
  CONSTRAINT `ck_product_filter` CHECK (`strict_product_filter_eligible` IN (0,1)),
  CONSTRAINT `ck_product_filter_json` CHECK (JSON_TYPE(`field_filter_eligibility`) = 'OBJECT')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `product_grape` (
  `product_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `grape_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `proportion_pct` DECIMAL(7,4) NULL,
  `source_ids` JSON NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`product_id`, `grape_id`),
  CONSTRAINT `fk_pg_product` FOREIGN KEY (`product_id`) REFERENCES `product` (`product_id`),
  CONSTRAINT `fk_pg_grape` FOREIGN KEY (`grape_id`) REFERENCES `grape_variety` (`grape_id`),
  CONSTRAINT `ck_pg_proportion` CHECK (`proportion_pct` IS NULL OR `proportion_pct` BETWEEN 0 AND 100)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `price_observation` (
  `price_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `product_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `source_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `amount_min` DECIMAL(14,2) NOT NULL,
  `amount_max` DECIMAL(14,2) NOT NULL,
  `currency` CHAR(3) CHARACTER SET ascii NOT NULL,
  `unit` ENUM('bottle','box','case','other','unknown') NOT NULL,
  `pack_quantity` INT UNSIGNED NULL,
  `price_type` VARCHAR(64) NOT NULL,
  `price_type_raw` VARCHAR(255) NULL,
  `quote_date` VARCHAR(10) NULL,
  `quote_date_precision` ENUM('day','month','year','unknown') NOT NULL,
  `observed_date` VARCHAR(40) NULL,
  `verified_at` VARCHAR(40) NULL,
  `minimum_order_raw` VARCHAR(255) NULL,
  `tax_shipping_note` TEXT NULL,
  `current_retail_eligible` BOOLEAN NOT NULL,
  `strict_price_filter_eligible` BOOLEAN NOT NULL,
  `conflict` BOOLEAN NOT NULL,
  `display_notice` TEXT NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`price_id`),
  KEY `idx_price_product` (`product_id`, `currency`),
  CONSTRAINT `fk_price_product` FOREIGN KEY (`product_id`) REFERENCES `product` (`product_id`),
  CONSTRAINT `fk_price_source` FOREIGN KEY (`source_id`) REFERENCES `source_document` (`source_id`),
  CONSTRAINT `ck_price_amount` CHECK (`amount_min` > 0 AND `amount_max` >= `amount_min`),
  CONSTRAINT `ck_price_current` CHECK (`current_retail_eligible` IN (0,1)),
  CONSTRAINT `ck_price_strict` CHECK (`strict_price_filter_eligible` IN (0,1)),
  CONSTRAINT `ck_price_conflict` CHECK (`conflict` IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `entity_alias` (
  `alias_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `alias` VARCHAR(512) NOT NULL,
  `alias_key` VARCHAR(512) COLLATE utf8mb4_bin NOT NULL,
  `entity_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `entity_type` VARCHAR(32) NOT NULL,
  `alias_kind` VARCHAR(32) NOT NULL,
  `ambiguous` BOOLEAN NOT NULL,
  `source_ids` JSON NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`alias_id`),
  KEY `idx_alias_key` (`alias_key`(191)),
  KEY `idx_alias_entity` (`entity_id`),
  CONSTRAINT `fk_alias_entity` FOREIGN KEY (`entity_id`) REFERENCES `entity_registry` (`entity_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `professional_review` (
  `review_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `product_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `source_id` VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  `review_type` VARCHAR(64) NOT NULL,
  `summary` TEXT NULL,
  `score` DECIMAL(8,3) NULL,
  `score_scale` DECIMAL(8,3) NULL,
  `score_raw` TEXT NULL,
  `medal` VARCHAR(128) NULL,
  `competition_year` SMALLINT UNSIGNED NULL,
  `fact_scope` TEXT NOT NULL,
  `raw_record` JSON NOT NULL,
  PRIMARY KEY (`review_id`),
  KEY `idx_professional_product` (`product_id`),
  CONSTRAINT `fk_review_product` FOREIGN KEY (`product_id`) REFERENCES `product` (`product_id`),
  CONSTRAINT `fk_review_source` FOREIGN KEY (`source_id`) REFERENCES `source_document` (`source_id`),
  CONSTRAINT `ck_review_score` CHECK (`score` IS NULL OR (`score_scale` IS NOT NULL AND `score` BETWEEN 0 AND `score_scale`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE OR REPLACE VIEW `v_product_basic` AS
SELECT p.*, w.name AS winery_name, r.name AS region_name
FROM product AS p
JOIN winery AS w ON w.winery_id = p.winery_id
LEFT JOIN region AS r ON r.region_id = p.region_id;

-- 仅提供已具备零售资格、精确单瓶CNY报价的候选视图；时效窗口由应用按核验日控制。
-- 本批没有合格当前零售价，因此此视图预期为空。
CREATE OR REPLACE VIEW `v_eligible_retail_price` AS
SELECT price_id, product_id, amount_min AS price_cny, quote_date, verified_at, source_id
FROM (
  SELECT p.*, ROW_NUMBER() OVER (
    PARTITION BY product_id ORDER BY verified_at DESC, price_id DESC
  ) AS row_priority
  FROM price_observation AS p
  WHERE current_retail_eligible = 1 AND strict_price_filter_eligible = 1
    AND conflict = 0 AND currency = 'CNY' AND unit = 'bottle'
    AND amount_min = amount_max AND verified_at IS NOT NULL
) AS ranked
WHERE row_priority = 1;
