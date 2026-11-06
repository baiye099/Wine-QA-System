-- 只读校验与查询示例；首次导入空库时actual_count应等于expected。
USE `Wine_QA_System`;
SELECT 'source_document' AS table_name, 49 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `source_document`
UNION ALL
SELECT 'entity_registry' AS table_name, 170 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `entity_registry`
UNION ALL
SELECT 'region' AS table_name, 8 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `region`
UNION ALL
SELECT 'winery' AS table_name, 22 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `winery`
UNION ALL
SELECT 'grape_variety' AS table_name, 11 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `grape_variety`
UNION ALL
SELECT 'product' AS table_name, 47 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `product`
UNION ALL
SELECT 'product_grape' AS table_name, 51 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `product_grape`
UNION ALL
SELECT 'price_observation' AS table_name, 10 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `price_observation`
UNION ALL
SELECT 'entity_alias' AS table_name, 191 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `entity_alias`
UNION ALL
SELECT 'professional_review' AS table_name, 30 AS expected_on_fresh_database, COUNT(*) AS actual_count FROM `professional_review`;

-- 外键与别名类型检查；均应为0。
SELECT COUNT(*) AS orphan_products FROM product p LEFT JOIN winery w ON p.winery_id=w.winery_id WHERE w.winery_id IS NULL;
SELECT COUNT(*) AS invalid_alias_type FROM entity_alias a JOIN entity_registry e ON a.entity_id=e.entity_id WHERE a.entity_type<>e.entity_type;
SELECT COUNT(*) AS invalid_grape_registry FROM grape_variety g JOIN entity_registry e ON e.entity_id=g.grape_id WHERE e.entity_type<>'GrapeVariety';
SELECT COUNT(*) AS invalid_region_registry FROM region r JOIN entity_registry e ON e.entity_id=r.region_id WHERE e.entity_type<>'Region';

-- 当前批次应为0，参考报价不能被误当成可购买零售价。
SELECT COUNT(*) AS eligible_retail_price_count FROM v_eligible_retail_price;

-- 产品条件筛选：750ml干红；每个条件必须同时通过字段资格。
-- 当前数据快照应得到9条产品记录。
SELECT product_id,name,winery_name,vintage,vintage_status,volume_ml
FROM v_product_basic
WHERE strict_product_filter_eligible=1 AND color='red' AND sweetness='dry' AND volume_ml=750
  AND JSON_UNQUOTE(JSON_EXTRACT(field_filter_eligibility,'$.color'))='true'
  AND JSON_UNQUOTE(JSON_EXTRACT(field_filter_eligibility,'$.sweetness'))='true'
  AND JSON_UNQUOTE(JSON_EXTRACT(field_filter_eligibility,'$.volume_ml'))='true'
ORDER BY product_id;

-- 查询明确披露含赤霞珠的产品，不推断100%配比。
SELECT p.product_id,p.name,p.vintage,p.vintage_status,g.name AS grape_name,pg.proportion_pct
FROM product p JOIN product_grape pg ON pg.product_id=p.product_id
JOIN grape_variety g ON g.grape_id=pg.grape_id
WHERE g.name='赤霞珠' AND p.strict_product_filter_eligible=1
  AND JSON_UNQUOTE(JSON_EXTRACT(p.field_filter_eligibility,'$.grape_varieties'))='true'
ORDER BY p.product_id;
