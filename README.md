# Wine-QA-System · 宁夏葡萄酒智能问答系统

**当前版本 v0.2.0（本地开发版）。** 使用 Streamlit、Python、MySQL 和 Neo4j，面向酒庄、葡萄酒产品、条件筛选和知识问答。

问答后端按入口、服务、数据访问和数据库分包。产品问价与预算筛选使用已审核的展示价格，并在回答中注明来源性质。泛化列表问题仍可能要求补充条件。

- [版本说明](docs/releases/v0.2.0.md) · [变更记录](CHANGELOG.md)
- [系统架构设计](docs/系统架构设计.md) · [后端框架说明](docs/后端框架说明.md)
- [公开发布与数据范围](docs/公开发布与数据范围.md)

## 快速运行

在仓库根目录、Python 3.11+ 环境中执行：

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python -m qa --interactive
```

模板默认 `QA_BACKEND=demo`，仅读取正式清洗数据，不连接数据库、不调用模型。`:new` 清空会话，`:quit` 退出。也可以执行单次查询或启动最小页面：

```powershell
python -m qa "介绍一下西鸽酒庄"
python -m qa "找几款750毫升的干红" --json
python -m streamlit run app.py
```

demo 用于验证规则覆盖范围内的查询和会话流程，不模拟已经就绪的知识语义检索。

## 数据库与模型配置

编辑本地 `.env`，将 `QA_BACKEND` 改为 `database`，填写 `MYSQL_*`、`NEO4J_*`、`LLM_*` 和 `EMBEDDING_*`。真实密码和 API Key 只放在本地 `.env`，不要写进示例、文档或 Git 提交。

- MySQL 使用 `Wine_QA_System`。应用执行只读查询，并核对实际字段与正式数据。
- Neo4j 使用配置中的实际数据库名；本版开发环境已将项目数据导入 `wine-qa-system`。
- `QA_DATASET=wine-qa-system` 是库内的数据归属标识，不会切换或迁移实际数据库。
- 大模型与 Embedding 分别配置；供应商可以不同。当前适配器兼容 `/chat/completions` JSON 模式与 `/embeddings` 浮点向量协议。
- `EMBEDDING_DIMENSIONS` 必须与服务实际输出一致。当前适配器校验维度，不向接口发送降维参数；例如 Qwen3-Embedding-8B 原生为 4096 维。

MySQL/Neo4j 导入方法见 [数据库说明](database/README.md)。导入 Neo4j 时明确指定实际目标库：

```powershell
python database/import_neo4j.py --database wine-qa-system --dry-run
python database/import_neo4j.py --database wine-qa-system --report-file database/neo4j_import_report.json
```

导入脚本不自动读取 `.env`，会提示输入密码；也可读取进程环境变量。应用启动不会自动建表、导入数据或写入向量。

配置完成后，按需执行：

```powershell
# 只读检查两库；不请求模型
python -m scripts.check_backend --online

# 离线写入知识向量并创建索引，会调用配置的 Embedding 服务
python -m scripts.build_embeddings --batch-size 16

# 要求知识向量与索引完整就绪；不请求模型
python -m scripts.check_backend --online --require-vectors
```

构建批次大小应遵守实际模型服务的限制。数据库模式失败不会自动切换 demo；更换文本或模型后需更新对应向量。

## 当前能力与限制

| 范围 | v0.2.0 状态 |
|---|---|
| 指定酒庄/产品资料 | 已提供查询与来源整理路径，需继续做真实问句验收 |
| 类型、容量、单一品种等硬条件 | 校验字段资格与实际值；未知值不当作匹配 |
| 连续追问 | 提供条件继承/删除与上轮展示顺序定位 |
| 知识问答 | 提供向量、召回、证据生成接口；就绪不等于答案质量达标 |
| 泛化列表查询 | 存在缺口，尚未修复 |
| 产品问价、价格预算筛选 | 支持已审核的生成展示价格；已核验零售价仍受独立资格与时效检查 |
| 风味等偏好排序 | 仅保存偏好，尚未启用有依据的排序 |

### 价格问答

可以直接问“贺兰红赤霞珠干红葡萄酒N.28多少钱？”、“300元以下的葡萄酒”，或接着问“第二款多少钱？”。
数据库模式与demo模式使用同一取价规则；先选符合资格且未过期的已核验报价，否则使用与逐字段审核记录一致的 `product.price_cny`。
生成值显示为“展示价格：298 元（生成数据，非实时零售价）”，预算依据各产品实际展示的价格筛选，未知价格不当作匹配。

`QA_DISPLAY_PRICE_ENABLED=true` 默认启用本次审核的生成展示价格；设为false可关闭。
`QA_PRICE_ENABLED=false` 仍控制已核验零售报价，未改动数据库中的核验标记或时间。
产品问价和明确预算的规则解析直接走结构化查询；这两类已识别问句不调用外部模型，也不从知识摘要猜价格。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s database -p test_scripts.py -v
python -m unittest discover -s data_cleaning -p 'test_*.py' -v
```

本版通过 29 项后端、23 项数据库工具和 32 项清洗测试。本地验收覆盖 MySQL、Neo4j、已有向量检索和 Streamlit 问价页面。测试范围见 [版本说明](docs/releases/v0.2.0.md)。

## 项目结构

```text
app.py                 Streamlit 启动文件
qa/                    问答应用，python -m qa 从此启动
  router/              页面与命令行交互
  service/             问题理解、问答编排与回答生成
  schemas/             查询、响应和证据的 Pydantic 契约
  models/              会话、数据快照与业务规则
  repository/          事实与知识仓库，负责具体查询
  db/                  连接、事务、快照及 MySQL 字段映射
  clients/             外部大模型和 Embedding 接口
  core/                配置、异常、路径和文本处理
  bootstrap.py         根据配置组装服务
scripts/               向量构建和按需诊断
tests/                 后端回归用例
database/              MySQL SQL、Neo4j 导入和离线测试
data_cleaning/         原始资料清洗与验证工具
data/                  正式清洗数据、Schema、manifest 与报告
docs/                  架构、数据规范和版本说明
CHANGELOG.md           版本变更记录
VERSION                当前版本号
```

原始采集、游客评论、待审材料、审计明细和本地配置不随公开仓库发布，详见 [数据范围](docs/公开发布与数据范围.md)。
