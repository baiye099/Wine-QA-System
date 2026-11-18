# MySQL与Neo4j导入脚本说明

2026-11-16 已按用户审核后的自然风格方案补齐533处空值，并将主 `data` 同步到 MySQL `Wine_QA_System` 和 Neo4j `wine-qa-system`。两套数据库的实际字段已核对通过，剩余101处为空值方案中保留的日期、NV年份及非赛事奖项字段。记录数量保持：正式知识228条、MySQL来源84条、统一实体269条，Neo4j业务节点581个、关系746条。

实际结果见 `null_fill_execution_report.json`、`database_import_report.json`、`mysql_import_report.json`、`neo4j_import_report.json` 和 `backend_check_report.json`。逐字段填充值及来源见 `data/audit/null_fill_changes.jsonl`；业务字段中的自然名称与后台 `null_fill` 来源标记分开保存。
本次填充前的完整数据位于 `work/null_fill_20261116_a0e0cd5b72c5/baseline_data`，数据库原记录与228条原向量备份位于同级 `database_before`。
合并前完整数据位于 `work/data_merge_20261115_b97e7d737f92/baseline_data`，数据库原记录及原有向量备份位于该目录同级的 `database_before`。这些本地备份不公开。

2026-11-15 已按用户许可仅向SiliconFlow发送新增的90条知识文本，完成90条向量构建；历史结果保留在 `embedding_build_report.json`。本次空值填充未调用外部模型，228条知识正文、原向量及向量元数据前后哈希一致，当前全部就绪，结果见 `null_fill_execution_report.json` 和 `backend_check_report.json`。此前的3条本地检索链路检查不是独立问答质量评测。

## 1. 文件与执行顺序

本目录与 `data`、`data_raw`、`data_cleaning` 同级。

| 文件 | 用途 |
|---|---|
| `01_schema_mysql.sql` | 创建 Wine_QA_System 数据库、10张表、外键、索引及查询视图 |
| `02_data_mysql.sql` | 已根据当前正式JSONL生成的数据写入SQL，直接执行即可 |
| `03_verify_mysql.sql` | 只读数量、引用及筛选查询检查 |
| `import_neo4j.py` | 读取正式JSONL，导入节点、别名属性与关系 |
| `import_databases.py` | 使用本地配置同步两套数据库，导入前备份、事务内验证与导入后核对；默认只显示计划 |
| `requirements.txt` | Neo4j Python驱动依赖 |
| `dataset.py` | 共用的JSONL读取、manifest哈希与引用校验 |
| `generate_mysql_sql.py` | 数据更新后重新生成三份MySQL SQL；不执行数据库 |
| `test_scripts.py` | 离线测试，不访问数据库 |

脚本仅读取 `data` 根目录的正式JSONL；不导入 review、excluded、auxiliary、游客评论或审计数据。

## 2. MySQL导入

目标为 **MySQL 8.0.19及以上版本**，也可使用MySQL 8.4。使用了行别名形式的upsert、JSON列、窗口函数及CHECK约束，不以MySQL 5.7或MariaDB为兼容目标。

使用Navicat、MySQL Workbench、IDEA或MySQL命令行，按以下顺序执行整份文件：

1. `01_schema_mysql.sql`
2. `02_data_mysql.sql`
3. `03_verify_mysql.sql`

命令行示例，在PowerShell中启动客户端：

```powershell
mysql --host=127.0.0.1 --port=3306 --user=root --password --default-character-set=utf8mb4
```

进入MySQL提示符后执行：

```sql
SOURCE E:/PythonProject/NLP/QASystem/database/01_schema_mysql.sql;
SOURCE E:/PythonProject/NLP/QASystem/database/02_data_mysql.sql;
SOURCE E:/PythonProject/NLP/QASystem/database/03_verify_mysql.sql;
```

账号需要具备目标库的建库、建表、创建视图和写入权限。数据写入以事务组织；客户端遇错应停止并回滚，不要忽略错误继续执行最后的COMMIT。DDL与数据写入是两份脚本，不能视为一个跨文件原子事务。

### 表与本批数量

| 表 | 数量 | 内容 |
|---|---:|---|
| source_document | 84 | 来源登记，包括证据形式与原访问状态 |
| entity_registry | 269 | 酒庄、产品、产区、品种、风味与概念的统一ID目录 |
| region | 8 | 产区或行政区域，保留region_kind |
| winery | 22 | 酒庄基础信息，区分公司成立年 |
| grape_variety | 17 | 明确出现的葡萄品种 |
| product | 47 | 产品及年份、容量、参数、字段筛选资格 |
| product_grape | 51 | 有依据的产品与品种关系、已披露比例 |
| price_observation | 10 | 正式可追溯的参考报价，不是当前零售价 |
| entity_alias | 236 | 多类型别名，外键关联统一实体目录 |
| professional_review | 30 | 来源专业评价、赛事或商家描述，保持具体年份范围 |

`entity_registry`是共享ID及别名目标目录，其记录与具体业务表存在对应，不代表新增269款产品。

复杂元数据通过JSON列保留；`raw_record`保存用于导入的完整清洗记录及必要派生字段，原始位置仍由其中raw_locator追溯。不是重新保存网页原文。

文本在数据SQL中使用UTF-8十六进制表达式，因此文件中会看到 `CONVERT(X'...' USING utf8mb4)`。这是为了可靠保留中文、引号、反斜杠和换行；可读数据仍在原JSONL中。

### MySQL查询注意事项

- `product.price_cny`含经用户审核生成的展示金额，逐字段来源保存在 `raw_record.null_fill`；这些金额不是已核验的当前零售价。
- 2026-11-17 问答层已接入这些展示金额，产品问价和预算筛选默认可用，输出明确标为生成数据。`QA_DISPLAY_PRICE_ENABLED`控制展示金额，`QA_PRICE_ENABLED`独立控制已核验零售报价；本次接入未修改数据库。
- 当前零售价资格标志均为false，`v_eligible_retail_price`本批应为空。将来有已核验报价后，应用还需检查核验时间是否处于实际时效窗口。
- `v_product_basic`提供产品、酒庄和产区联合视图。
- 每个筛选条件必须同时检查 `field_filter_eligibility`；仅检查总资格不足以判断所有字段可用。
- `quote_date`保留年/月/日精度，使用VARCHAR而不是补造日期。
- `professional_review`中的评分和奖项仅对所关联产品年份有效。

`03_verify_mysql.sql`包含当前数据“750ml干红”的9条产品检查，以及含赤霞珠产品的查询。

## 3. Neo4j导入

目标为支持所用Cypher语法的 **Neo4j 5.x或后续版本**。使用官方Python Driver、MERGE、UNWIND和命名唯一约束，不需要APOC。脚本不会创建新的Neo4j数据库实例，连接的目标数据库应已存在。

在项目根目录执行：

```powershell
python -m pip install -r database/requirements.txt
python database/import_neo4j.py --dry-run
```

dry-run只读取本地JSONL并校验引用，不需要数据库运行，也不会尝试连接。

确认Neo4j已经启动后，执行实际导入：

```powershell
python database/import_neo4j.py --uri bolt://localhost:7687 --user neo4j --database wine-qa-system --report-file database/neo4j_import_report.json
```

程序会提示输入密码，输入不显示。也可通过 NEO4J_URI、NEO4J_USER、NEO4J_DATABASE、NEO4J_PASSWORD 环境变量提供配置；密码没有写入脚本、报告或命令行参数。

本项目实际Neo4j数据库名为 `wine-qa-system`，脚本默认值与上面的命令已同步。`--database wine-qa-system`选择要连接的库；节点/关系属性 `qa_dataset=wine-qa-system`标识库内数据的项目归属。两者概念不同，本项目使用相同名称。MySQL库名仍为Wine_QA_System。

如设置了NEO4J_DATABASE环境变量，其值会覆盖脚本内置默认值；显式传入 `--database wine-qa-system` 可以确保使用本项目目标库。dry-run输出的database字段会显示实际选择的库名。

### 节点与关系

当前实际导入 **581个业务节点、746条关系**。别名保存为节点属性，不额外创建236个别名节点。

| 标签 | 数量 | 业务ID属性 |
|---|---:|---|
| Document | 84 | source_id |
| Winery | 22 | winery_id |
| Product | 47 | product_id |
| Region | 8 | entity_id |
| GrapeVariety | 17 | entity_id |
| Flavor | 52 | entity_id |
| Concept | 123 | entity_id |
| Chunk | 228 | knowledge_id |

所有节点额外具有 `QAEntity` 标签与统一 `entity_id`，并建立唯一约束。这些ID与JSONL/MySQL一致，不使用Neo4j内部节点编号。

关系按 `relations.jsonl` 原样导入：HAS_CHUNK 233、ABOUT 303、PRODUCES 47、LOCATED_IN 22、ORIGINATES_FROM 13、MADE_FROM 51、HAS_FLAVOR 77。没有来源依据的区域层级不补建。

`aliases`为别名数组，`alias_keys`为移除空白并casefold后的匹配键。Product上的 `filter_color`、`filter_volume_ml` 等属性对应原字段资格。嵌套对象使用 `_json` 后缀字符串保存，如 `field_filter_eligibility_json`；Neo4j属性不直接保存嵌套map。

### 重复执行与失败处理

- 同ID使用MERGE/upsert更新，重复导入同一快照不应增加节点和关系。
- 数据节点、关系及数量校验在同一个受管理事务内，数量不符或出现错误会回滚本次数据事务。Schema约束和索引另行创建，不属于该数据事务。
- 写入前检查已有QAEntity的归属与类型，避免覆盖其他归属的同ID节点。
- `qa_dataset=wine-qa-system`标识脚本归属，`qa_snapshot`是正式JSONL的内容指纹。
- 为兼容本项目之前的归属标识Wine_QA_System和ningxia_wine_qa，导入时先检查当前计划中的同ID记录。节点类型/标签、关系类型/端点必须一致，且已有快照匹配，或完整规范记录一致，才允许随正常upsert更新为新标识。迁移与数据写入在同一事务中进行，不全库改名、不删除节点。
- 归属缺失、其他项目归属、旧名但数据指纹不符、类型/端点冲突仍会拒绝导入，并返回已有归属和具体原因。不要通过删除所有节点或去掉校验来处理。
- 成功报告增加dataset_name与ownership_migration；前者与database字段区分，后者记录本次更新了多少旧标识节点和关系。
- 脚本不删除旧节点/关系。以后导入的数据若减少，历史记录仍保留；需要查询当前批次时按输出报告的qa_snapshot过滤，或使用独立目标数据库维护单个数据版本。
- 本次只导入文本与图，不生成embedding或向量索引。后续如果Chunk正文发生变化，会清除旧的embedding及其相关标记，避免沿用过期向量；正文未变时保留。

### 导入后查询示例

```cypher
// 首次空图导入应看到上述标签对应数量。
MATCH (n:QAEntity)
RETURN n.entity_type AS entity_type, count(*) AS total
ORDER BY entity_type;

MATCH (:QAEntity)-[r]->(:QAEntity)
WHERE r.qa_dataset = 'wine-qa-system'
RETURN type(r) AS relation_type, count(*) AS total
ORDER BY relation_type;

// 指定产品及其来源知识。
MATCH (d:QAEntity:Document)-[:HAS_CHUNK]->(k:QAEntity:Chunk)-[:ABOUT]->(p:QAEntity:Product)
WHERE p.product_id = 'P001'
RETURN p.name, k.title, k.text, d.title, d.url;

// 对明确披露的品种关系进行查询，不推断配比。
MATCH (p:QAEntity:Product)-[r:MADE_FROM]->(g:QAEntity:GrapeVariety)
WHERE g.name = '赤霞珠' AND p.filter_grape_varieties = true
RETURN p.product_id, p.name, p.vintage, r.proportion_pct;
```

数据库里有多个历史快照时，上述示例还需增加 `qa_snapshot` 条件。脚本输出的实际导入报告会给出本批指纹。

### 改名后出现“业务ID已被其他归属/类型占用”

该错误表示目标库中已有同ID节点，但不满足归属或类型检查。首先确认命令选择的是实际项目库：本项目应使用 `--database wine-qa-system`。之前示例中的 `--database neo4j` 会选择另一个名为neo4j的库，不应继续使用。

若目标库正确，另一种可能是旧版本导入的数据仍使用旧qa_dataset标识。更新后的脚本按上面的限定条件识别同项目旧标识，并在导入时更新。可以使用本节前面的正确命令重新执行。

如果仍然报错，新的错误消息会区分实际归属、类型冲突或指纹不符。可在Neo4j Browser执行以下只读查询查看现状，再核对错误中的业务ID：

```cypher
MATCH (n:QAEntity)
RETURN n.qa_dataset AS data_owner, n.entity_type AS entity_type, count(*) AS total
ORDER BY data_owner, entity_type;
```

## 4. 数据更新后

先完成清洗并更新清洗manifest，再执行：

```powershell
python database/generate_mysql_sql.py
python database/import_neo4j.py --dry-run
```

SQL生成器仅生成文件，不连接MySQL。需要自定义库名时：

```powershell
python database/generate_mysql_sql.py --database my_wine_qa
```

这会同步调整三份SQL中的建库和USE库名。建表脚本针对首次创建或同版结构重复执行；如果目标已有不同表结构，需自行做结构迁移。SQL upsert不自动删除清洗结果已不再收录的旧记录。

## 5. 本地检查

```powershell
python -m unittest discover -s database -p test_scripts.py -v
```

覆盖中文/引号/反斜杠、JSON类型、列映射与长度、引用关系、Neo4j属性类型、固定标签/关系白名单、事务写入顺序、失败检测、dry-run不联网及数据哈希变化检测。

SQL已根据当前正式数据重新生成，两套真实数据库已经导入并验证。数据库导入离线测试23项、后端离线测试22项、清洗/合并/填充边界测试32项通过。

## 6. 统一导入工具

在已安装项目依赖的Python环境执行；本机使用 `E:/Anaconda/envs/nlpbase_GPU/python.exe`。

```powershell
python database/import_databases.py
```

默认只读取本地正式数据并显示计划，不连接数据库。实际导入使用 `--apply --backup-dir work/新的备份目录`，可通过 `--base-dir` 指定本次升级前的完整数据副本，检查数据库是否仍匹配旧版本。
工具使用本地 `.env`，不会将密码写入命令行或报告；写入采用参数绑定和同ID更新。只导入正式根目录JSONL，保留原有知识、产品及其字段。

MySQL写入和逐字段验证位于同一事务，Neo4j沿用现有单事务导入与归属检查。两套数据库不提供跨库原子提交；发生部分成功时应依据实际报告与备份处理。
`--build-vectors` 会调用已配置的外部向量服务，须在获得发送知识文本的许可后使用。单独构建向量仍可用 `python -m scripts.build_embeddings`。

## 官方实现参考

- SQL采用行别名形式的重复键更新：[MySQL INSERT ... ON DUPLICATE KEY UPDATE](https://dev.mysql.com/doc/refman/8.4/en/insert-on-duplicate.html)。
- Neo4j数据写入使用官方受管理事务：[Neo4j Python事务文档](https://neo4j.com/docs/python-manual/current/transactions/)。
- 属性唯一性由命名约束保障：[Neo4j约束文档](https://neo4j.com/docs/cypher-manual/current/schema/constraints/create-constraints/)。
