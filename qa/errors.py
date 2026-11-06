"""可向调用方展示的错误，不携带连接凭据或原始服务响应。"""


class QAError(Exception):
    pass


class ConfigurationError(QAError):
    pass


class BackendUnavailable(QAError):
    pass


class DataVersionError(QAError):
    pass


class KnowledgeNotReady(QAError):
    pass


class ModelError(QAError):
    pass

