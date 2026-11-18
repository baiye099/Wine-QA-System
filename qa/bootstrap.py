"""按应用配置组装问答服务、仓库和模型客户端。"""

from .clients.http import HTTPChatModel, HTTPEmbeddingModel
from .core.config import Settings
from .db.snapshot import runtime_dataset
from .repository.demo import DemoRepository
from .repository.mysql import MySQLRepository
from .repository.neo4j import Neo4jRepository
from .service.answer import AnswerBuilder
from .service.chat import QAService
from .service.understand import QueryUnderstander


def build_service(settings: Settings | None = None) -> QAService:
    settings = settings or Settings.from_env()
    dataset = runtime_dataset(settings)
    if settings.backend == "demo":
        facts = knowledge = DemoRepository(dataset, settings)
    else:
        facts = MySQLRepository(settings, dataset)
        knowledge = Neo4jRepository(settings, dataset)
    chat = HTTPChatModel(settings) if settings.llm_model else None
    embedding = HTTPEmbeddingModel(settings) if settings.embedding_model else None
    return QAService(facts, knowledge, settings, QueryUnderstander(chat), AnswerBuilder(chat), embedding)
