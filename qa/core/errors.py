"""可向调用方展示的应用异常，隐藏凭据与底层服务响应。"""


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

