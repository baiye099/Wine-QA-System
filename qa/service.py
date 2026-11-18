"""一次问答的编排入口；页面只需 ask(question, state)。"""

from __future__ import annotations

from pydantic import ValidationError

from .answer import AnswerBuilder
from .config import Settings
from .errors import BackendUnavailable, DataVersionError, KnowledgeNotReady, ModelError
from .filtering import matches_filters
from .models import EmbeddingModel
from .repositories import FactRepository, KnowledgeRepository
from .schemas import EntityRef, ParsedQuery, ProductFilters, QAResponse
from .session import ConversationState
from .understand import QueryUnderstander


class QAService:
    def __init__(self, facts: FactRepository, knowledge: KnowledgeRepository, settings: Settings,
                 understander: QueryUnderstander | None = None, answer: AnswerBuilder | None = None,
                 embedding: EmbeddingModel | None = None):
        self.facts, self.knowledge, self.settings = facts, knowledge, settings
        self.understander = understander or QueryUnderstander()
        self.answer, self.embedding = answer or AnswerBuilder(), embedding

    def ask(self, question: str, state: ConversationState) -> QAResponse:
        question = question.strip()
        if not question or len(question) > 2000:
            return QAResponse(status="clarification", text="请输入 1–2000 字的问题。")
        preferences = []
        try:
            self.facts.ensure_ready()
            parsed = self.understander.analyze(question, state, self.facts.aliases())
            preferences = list(dict.fromkeys((state.preferences if parsed.inherit_filters else []) + parsed.preferences))
            response = self._run(question, parsed, state)
            response.notices = list(dict.fromkeys(parsed.notices + response.notices))
            if response.status == "ok":
                self._attach_sources(response)
                if response.intent != "knowledge":
                    response.text = self.answer.facts(response)
        except ValidationError:
            response = QAResponse(status="clarification", text="条件格式、单位或范围未通过校验，请明确产品参数或简化问题。")
        except KnowledgeNotReady as exc:
            response = QAResponse(status="not_ready", intent="knowledge", text=str(exc))
        except ModelError as exc:
            response = QAResponse(status="not_ready", intent="knowledge", text=str(exc))
        except (BackendUnavailable, DataVersionError) as exc:
            response = QAResponse(status="error", text=str(exc))
        state.record(question, response, preferences)
        return response

    def _resolve(self, parsed: ParsedQuery, state: ConversationState) -> tuple[list[EntityRef], str | None]:
        ids = list(dict.fromkeys(parsed.entity_ids))
        if parsed.result_position is not None:
            if parsed.result_position > len(state.last_results):
                return [], "上轮展示结果中没有这一序号，请按实际展示顺序选择。"
            ids = [state.last_results[parsed.result_position - 1]]
        elif parsed.references_current and not ids:
            ids = list(state.active_entity_ids)
            refs = self.facts.entities(ids)
            products = [e for e in refs if e.entity_type == "Product"]
            if products:
                ids = [e.entity_id for e in products]
            if not ids or len(ids) != 1:
                return [], "当前指代不明确，请指定产品、酒庄名称或“第几款”。"
        refs = self.facts.entities(ids)
        if len(refs) != len(ids):
            return [], "部分对象未在当前 MySQL 实体目录中找到，请提供已收录的名称或 ID。"
        return refs, None

    def _filters(self, parsed: ParsedQuery, state: ConversationState) -> ProductFilters:
        values = state.filters.active() if parsed.inherit_filters else {}
        for field in parsed.remove_filters:
            values.pop(field, None)
        values.update(parsed.filters.active())
        filters = ProductFilters(**values)
        for field, kind in (("winery_id", "Winery"), ("region_id", "Region"), ("grape_id", "GrapeVariety")):
            entity_id = getattr(filters, field)
            if entity_id and not any(r.entity_type == kind for r in self.facts.entities([entity_id])):
                raise ValueError(f"未知的{field}")
        return filters

    def _related(self, entity_ids: list[str], response: QAResponse) -> None:
        try:
            response.evidence = self.knowledge.related(entity_ids)
        except (BackendUnavailable, DataVersionError) as exc:
            response.notices.append("基础事实已返回，图谱补充资料暂不可用：" + str(exc))

    def _run(self, question: str, parsed: ParsedQuery, state: ConversationState) -> QAResponse:
        if parsed.intent == "clarify" or parsed.clarification:
            return QAResponse(status="clarification", intent="clarify", text=parsed.clarification or "请明确查询对象和条件。")
        refs, error = self._resolve(parsed, state)
        if error:
            return QAResponse(status="clarification", text=error)
        product_ids = [r.entity_id for r in refs if r.entity_type == "Product"]
        winery_ids = [r.entity_id for r in refs if r.entity_type == "Winery"]
        if parsed.intent == "filter":
            try:
                filters = self._filters(parsed, state)
            except ValueError:
                return QAResponse(status="clarification", text="筛选条件中的实体类型或数值范围不正确，请明确品种、酒庄或区域。")
            response = QAResponse(status="empty", intent="filter", effective_filters=filters,
                                  text="当前正式数据没有满足全部条件的记录，未自动放宽条件。")
            if ((filters.price_min is not None or filters.price_max is not None)
                    and not self.settings.price_enabled and not self.settings.display_price_enabled):
                response.status = "not_ready"
                response.text = "价格筛选尚未启用。已保留预算条件，请启用已核验报价或系统展示价格后再查询。"
                return response
            scope_ids = state.last_results if parsed.scope == "last_results" else None
            if parsed.scope == "last_results" and not scope_ids:
                return QAResponse(status="clarification", text="当前没有可限定的上轮产品结果，请先查询产品。")
            rows = self.facts.filter_products(filters, scope_ids, self.settings.max_results)
            # 最终展示再校验，不能让查询适配器或图谱跳过硬条件。
            rows = [r for r in rows if matches_filters(r, filters)]
            response.products = rows
            response.active_entity_ids = [p["product_id"] for p in rows]
            response.status = "ok" if rows else "empty"
            return response
        if parsed.intent in {"product", "winery"}:
            if (parsed.intent == "product" and len(product_ids) != 1) or (parsed.intent == "winery" and len(product_ids) + len(winery_ids) != 1):
                return QAResponse(status="clarification", text="请指定唯一的产品或酒庄名称；重名时补充年份、容量或 ID。")
            response = QAResponse(status="ok", text="", intent=parsed.intent)
            if product_ids:
                response.products = self.facts.products(product_ids)
                if parsed.intent == "winery":
                    winery_ids = [p["winery_id"] for p in response.products]
            if parsed.intent == "winery":
                response.wineries = self.facts.wineries(winery_ids)
            response.active_entity_ids = product_ids + winery_ids
            if not response.products and not response.wineries:
                response.status, response.text = "empty", "当前数据未收录该对象的详细资料。"
                return response
            self._related(response.active_entity_ids, response)
            return response
        self.knowledge.ensure_vector_ready()
        if self.embedding is None:
            raise KnowledgeNotReady("Embedding 模型未配置，知识语义问答尚未就绪。")
        vector = self.embedding.embed([question])[0]
        evidence = self.knowledge.search(vector, [r.entity_id for r in refs], parsed.category, min(self.settings.max_results, 5))
        if not evidence:
            return QAResponse(status="empty", intent="knowledge", text="当前可检索资料中未找到适用的知识证据。")
        # Chunk 中若提到产品，必须在事实库存在，不能以图中镜像替代 MySQL 产品。
        graph_entity_ids = {e for k in evidence for e in k.entity_ids}
        mysql_ids = {e.entity_id for e in self.facts.entities(list(graph_entity_ids))}
        evidence = [k for k in evidence if not any(e in graph_entity_ids - mysql_ids for e in k.entity_ids)]
        if not evidence:
            raise DataVersionError("图谱知识涉及当前 MySQL 中不存在的产品，已阻止展示。")
        text, evidence, notices = self.answer.knowledge(question, evidence)
        return QAResponse(status="ok", intent="knowledge", text=text, evidence=evidence, notices=notices,
                          active_entity_ids=[r.entity_id for r in refs])

    def _attach_sources(self, response: QAResponse) -> None:
        ids = [s for p in response.products + response.wineries for s in p.get("source_ids", [])]
        ids += [s for k in response.evidence for s in k.source_ids]
        ids += [g for p in response.products for grape in p.get("grape_varieties", []) for g in grape.get("source_ids", [])]
        ids += [p["eligible_price"]["source_id"] for p in response.products if p.get("eligible_price")]
        ids = list(dict.fromkeys(ids))
        response.sources = self.facts.sources(ids)
        if {s.source_id for s in response.sources} != set(ids):
            raise DataVersionError("回答存在缺失的来源登记，已阻止展示。")

    def close(self) -> None:
        self.facts.close()
        if self.knowledge is not self.facts:
            self.knowledge.close()


def build_service(settings: Settings | None = None) -> QAService:
    from .data import runtime_dataset
    from .demo_repository import DemoRepository
    from .models import HTTPChatModel, HTTPEmbeddingModel
    from .mysql_repository import MySQLRepository
    from .neo4j_repository import Neo4jRepository

    settings = settings or Settings.from_env()
    dataset = runtime_dataset(settings)
    if settings.backend == "demo":
        facts = knowledge = DemoRepository(dataset, settings)
    else:
        facts, knowledge = MySQLRepository(settings, dataset), Neo4jRepository(settings, dataset)
    chat = HTTPChatModel(settings) if settings.llm_model else None
    embedding = HTTPEmbeddingModel(settings) if settings.embedding_model else None
    return QAService(facts, knowledge, settings, QueryUnderstander(chat), AnswerBuilder(chat), embedding)
