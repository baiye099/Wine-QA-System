"""集中读取环境配置；导入模块时不连接数据库、不请求模型。"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from .errors import ConfigurationError
from .paths import ROOT


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["demo", "database"] = "demo"
    data_dir: Path = ROOT / "data"
    dataset: str = "wine-qa-system"
    snapshot: str = ""
    max_results: int = Field(default=10, ge=1, le=50)
    price_enabled: bool = False
    display_price_enabled: bool = True
    price_max_age_days: int = Field(default=30, ge=1, le=365)
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = "wine_qa_reader"
    mysql_password: SecretStr = Field(default=SecretStr(""), repr=False)
    mysql_database: str = "Wine_QA_System"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr = Field(default=SecretStr(""), repr=False)
    neo4j_database: str = "wine-qa-system"
    neo4j_vector_index: str = Field(default="qa_chunk_embedding", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
    llm_base_url: str = ""
    llm_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    llm_model: str = ""
    embedding_base_url: str = ""
    embedding_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    embedding_model: str = ""
    embedding_dimensions: int = Field(default=1024, ge=1, le=4096)
    embedding_encoding: str = "plain-v1"
    model_timeout_seconds: float = Field(default=30, gt=0, le=120)

    @model_validator(mode="after")
    def validate_models(self):
        if self.snapshot and not re.fullmatch(r"[0-9a-f]{64}", self.snapshot):
            raise ValueError("QA_SNAPSHOT 必须是 SHA256")
        for prefix in ("llm", "embedding"):
            url, model = getattr(self, prefix + "_base_url"), getattr(self, prefix + "_model")
            if bool(url) != bool(model):
                raise ValueError(f"{prefix} 的 BASE_URL 和 MODEL 必须同时配置")
            if url and not re.match(r"^https?://[^\s/]+", url):
                raise ValueError(f"{prefix} BASE_URL 必须是 HTTP(S) 地址")
        return self

    @property
    def embedding_profile(self) -> str:
        identity = f"{self.embedding_base_url.rstrip('/')}|{self.embedding_model}|{self.embedding_dimensions}|{self.embedding_encoding}"
        return hashlib.sha256(identity.encode()).hexdigest()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, env_file: Path | None = ROOT / ".env") -> "Settings":
        values = {}
        if env_file is not None and env_file.is_file():
            from dotenv import dotenv_values

            values.update({k: v for k, v in dotenv_values(env_file, interpolate=False).items() if v is not None})
        values.update(os.environ if env is None else env)
        names = {"backend": "QA_BACKEND", "data_dir": "QA_DATA_DIR", "dataset": "QA_DATASET",
                 "snapshot": "QA_SNAPSHOT", "max_results": "QA_MAX_RESULTS", "price_enabled": "QA_PRICE_ENABLED",
                 "price_max_age_days": "QA_PRICE_MAX_AGE_DAYS", "display_price_enabled": "QA_DISPLAY_PRICE_ENABLED"}
        kwargs = {}
        for field in cls.model_fields:
            value = values.get(names.get(field, field.upper()))
            if value is not None and (value != "" or field in {"snapshot", "mysql_password", "neo4j_password"}):
                kwargs[field] = value
        if "data_dir" in kwargs:
            path = Path(kwargs["data_dir"])
            kwargs["data_dir"] = path if path.is_absolute() else ROOT / path
        try:
            return cls(**kwargs)
        except ValidationError as exc:
            fields = ", ".join(sorted({str(e["loc"][0]) for e in exc.errors()}))
            raise ConfigurationError(f"配置格式不正确，请检查：{fields}。") from None

