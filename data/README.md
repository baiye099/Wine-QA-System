# 清洗数据结构与使用说明

数据版本：1.0.0；评估日期：2026-11-06。

目录与 `data_raw`、`data_cleaning` 同级。所有业务和审计数据均为 UTF-8 JSON/JSONL，文档使用 Markdown。JSONL 每个非空行是一个 JSON 对象；null 表示未知，数组是真实数组，数值不夹带单位。

## 文件与实际数量

| 文件 | 正式 accepted | 待核验 review | 排除 excluded |
|---|---:|---:|---:|
| `aliases.jsonl` | 191 | 57 | 0 |
| `entities.jsonl` | 101 | 11 | 0 |
| `knowledge.jsonl` | 138 | 47 | 0 |
| `price_observations.jsonl` | 10 | 23 | 0 |
| `products.jsonl` | 47 | 40 | 0 |
| `professional_reviews.jsonl` | 30 | 4 | 0 |
| `relations.jsonl` | 538 | 203 | 0 |
| `sources.jsonl` | 49 | 0 | 0 |
| `wineries.jsonl` | 22 | 3 | 0 |

根目录同名 JSONL 只含 accepted；review/ 与 excluded/ 中的同名文件保存对应候选或排除记录，不能一起加载进正式问答。空文件表示该状态为0条。

`sources.jsonl` 是来源登记，包含弱证据/不可访问来源以支持追溯；登记记录 accepted 不代表其事实已核验。

## 主文件结构

| 文件 | 主键与内容 | 使用方式 |
|---|---|---|
| wineries.jsonl | winery_id；name、company_name、region_id、location_raw、地址、founded_year、company_founded_year、introduction、联系方式 | MySQL酒庄表；按ID到Neo4j关联 |
| products.jsonl | product_id；winery_id、name、series、年份/年份状态、容量、颜色、甜度、酒精度、品种列表、描述、字段筛选资格 | MySQL产品主数据；price_cny本批保持null |
| price_observations.jsonl | price_id；product_id、金额上下限、币种、单位、报价类型、报价/观察日期、起订条件、资格 | 参考报价，不能用作当前零售价 |
| entities.jsonl | entity_id；entity_type、name、region_kind | Region可生成MySQL产区表；其余为Neo4j实体 |
| aliases.jsonl | alias_id；alias、entity_id、entity_type、alias_kind、ambiguous | 实体名称匹配；一个名称允许多个目标 |
| knowledge.jsonl | knowledge_id；title、text、category、entity_ids、text_type、fact_scope、retrieval_allowed | 正式RAG文本；后续再生成embedding |
| relations.jsonl | relation_id；head_id、relation_type、tail_id、proportion_pct | Neo4j边，使用业务ID而非内部节点ID |
| sources.jsonl | source_id；来源名、URL、输入文件、原访问状态、证据形式、时间、dataset_role | 答案来源与数据追溯 |
| professional_reviews.jsonl | review_id；product_id、评价类型、摘要、原评分与满分、奖项年、fact_scope | 附加专业/商家描述；不是消费者购买评论 |

完整可机读字段约束见 `schema.json` 的 `$defs`；每个主JSONL按同名定义逐行验证。附加字段允许存在，跨文件引用与业务条件由 `data_cleaning/validate.py` 检查。

## 通用追溯与状态

主记录保留 source_ids、raw_locator、raw_locators、review_status、notes。raw_locator采用 `data_raw/文件#原ID或记录位置`。external_verification=not_performed表示本次没有重新访问网页。
accepted仅表示本轮本地清洗检查通过；不能解释成网站真实性认证、库存确认或当前价格保证。多来源不能精确归因的字段在audit/field_evidence.jsonl中标为record_level_candidates，不任意指定一个来源。

## 字段值与筛选规则

- color：red/white/rose/other/null；sweetness：dry/semi_dry/semi_sweet/sweet/other/null。Brut Nature保留在sweetness_raw，不据此换算静止酒残糖分类。
- vintage_status：specified/non_vintage/unspecified/mixed。specified才有确定vintage；NV和未知年份不同。specific_variant表示资料指定年份或NV的记录，不保证已取得完整可购买SKU。
- volume_ml为单容器毫升；alcohol_pct_vol=14.2表示14.2%vol；未知不填750或常见酒精度。
- grape_varieties为明确且无冲突的披露列表，各项含entity_id、name、proportion_pct和source_ids；未知配比为null。完整百分比确实合计100时grapes_complete才可置true，不补比例。
- 每次条件筛选同时检查 strict_product_filter_eligible、field_filter_eligibility[目标字段]和值本身。主记录accepted不代表所有字段可筛选。field_filter_reasons解释缺失、来源归因、特殊配方等原因。
- 未指定年份的官网页面只在该记录范围内使用，不为所有年份继承参数、评分或风味。
- price_cny全部为空；价格只在独立观察表中。current_retail_eligible与strict_price_filter_eligible本批均false。参考报价必须带性质、币种、日期/日期不明、单位和起订条件。
- is_sparkling未知为null；没有“起泡”字样不能反推为false。P043含米酒的特殊配方保留介绍、停用普通葡萄酒筛选。

## 图关系与节点映射

来源S...→Document；酒庄W...→Winery；产品P...→Product；entities按entity_type建节点；知识K.../KP_.../KR_...→Chunk。关系包含PRODUCES、LOCATED_IN、ORIGINATES_FROM、MADE_FROM、HAS_FLAVOR、HAS_CHUNK、ABOUT；PART_OF仅在有明确依据时建立，本批不猜测区域层级。
HAS_FLAVOR来自该具体酒款/年份的来源描述，未自动传播到同品牌或其他年份。风味字面标签不代表量化强度。HAS_CHUNK为来源追溯边，faithful_summary仍表示采集摘要。

## 辅助评论

原始评论输入共9725行，确认重复合并后3137条；计数包含多个导出载体，不能当作独立消费者总数。
`auxiliary/visitor_reviews.jsonl` 保存规范化、规则脱敏的待审评论，`auxiliary/review_sources.jsonl` 保存其本地来源；低信息量记录在 `excluded/visitor_reviews.jsonl`。评论未进入正式knowledge或产品风味图。
字段包括review_id、text、平台、酒庄原始标签/可确定的winery_id、日期及精度、source_ids、source_urls、raw_locators、original_records和content_hash。product_id固定null，approved_usage为空。姓名、用户等级、IP位置等显式用户字段不导出；正文做手机号/邮箱/证件号/IP规则脱敏，未声称完成所有个人信息识别。
仅同平台外部ID且正文一致、同采集版本ID且正文一致、兼容导出唯一对应副本被合并。无法确认身份的相同正文标为duplicate_candidate并保留。

## 审计与报告

| 文件 | 内容 |
|---|---|
| audit/quality_issues.jsonl | 继承及新增问题、字段、来源、处理与严重程度 |
| audit/field_evidence.jsonl | 原值/规范值、字段来源粒度与转换决策 |
| audit/id_mapping.jsonl | 原始ID与规范ID映射；保留原ID也登记 |
| audit/duplicates.jsonl | 确认重复的原位置、规范ID和合并理由 |
| audit/duplicate_candidates.jsonl | 相同正文但身份不能确认的候选 |
| audit/derived_records.jsonl | 旧RAG派生记录不重导入、按规范主数据重建说明 |
| audit/input_checks.jsonl | JSON/CSV/SQLite版本核对与评论输入数量 |
| audit/review_decisions.jsonl | 评论保留/排除及合并来源行数 |
| quality_report.json | 实际记录数、字段缺失/可用数、价格与证据分布、限制 |
| manifest.json | 原始文件及输出文件SHA256、版本与读取方式 |
| validation_report.json | 结构、Schema、外键、筛选条件及原始文件完整性检查结果 |

manifest的输出哈希不包括manifest本身和validation_report，避免自引用；后两者通过重新运行验证脚本检查。

## 读取示例

```python
import json
from pathlib import Path

def read_jsonl(path):
    with Path(path).open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]

products = read_jsonl('data/products.jsonl')
matched = [p for p in products
           if p['strict_product_filter_eligible']
           and all(p['field_filter_eligibility'].get(k, False)
                   for k in ['color', 'sweetness', 'volume_ml'])
           and p['color'] == 'red' and p['sweetness'] == 'dry'
           and p['volume_ml'] == 750]
```

脚本示例只筛选数据已支持的字段。当前价格预算查询应返回“当前零售价数据不足”，不能自行把参考报价填入产品price_cny。
