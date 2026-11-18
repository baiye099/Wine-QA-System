"""MySQL 连接、只读事务与返回值解码。"""

from __future__ import annotations

import json
from contextlib import closing, contextmanager
from decimal import Decimal
from typing import TYPE_CHECKING

from ..core.errors import BackendUnavailable

if TYPE_CHECKING:
    from ..core.config import Settings


JSON_COLUMNS = {"source_ids", "raw_record", "field_filter_eligibility", "field_filter_reasons"}


def decode_row(row: dict) -> dict:
    return {k: json.loads(v) if k in JSON_COLUMNS and isinstance(v, str) else float(v) if isinstance(v, Decimal) else v
            for k, v in row.items()}


def connect_mysql(settings: Settings, *, io_timeout: int = 10):
    import pymysql

    return pymysql.connect(host=settings.mysql_host, port=settings.mysql_port, user=settings.mysql_user,
                           password=settings.mysql_password.get_secret_value(), database=settings.mysql_database,
                           charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                           connect_timeout=5, read_timeout=io_timeout, write_timeout=io_timeout)


@contextmanager
def mysql_read_connection(settings: Settings):
    try:
        from pymysql import MySQLError
    except ImportError:
        raise BackendUnavailable("缺少 PyMySQL，请安装项目 requirements.txt。") from None
    try:
        with closing(connect_mysql(settings)) as connection:
            try:
                with connection.cursor() as cursor:
                    cursor.execute("START TRANSACTION READ ONLY")
                yield connection
            finally:
                try:
                    connection.rollback()
                except MySQLError:
                    pass
    except MySQLError as exc:
        raise BackendUnavailable("MySQL 连接或只读查询失败，请检查配置、表结构与只读账号权限。") from exc
