"""对照实际发布 SQL 验证别名快照，确保导入后的合法记录不会被误判。"""

import json
import re
import unittest
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from unittest.mock import patch

from qa.core.config import Settings
from qa.core.errors import DataVersionError
from qa.core.paths import ROOT
from qa.db.mysql_tables import build_tables
from qa.db.snapshot import runtime_dataset
from qa.repository.mysql import MySQLRepository


class AliasCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query):
        if query != "SELECT * FROM `entity_alias`":
            raise AssertionError(f"unexpected query: {query}")

    def fetchall(self):
        return self.rows


class AliasConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return AliasCursor(self.rows)


class MySQLAliasSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dataset = runtime_dataset(Settings())
        cls.alias_table = next(t for t in build_tables(cls.dataset) if t.name == "entity_alias")
        cls.expected = cls.alias_table.rows
        sql = (ROOT / "database" / "02_data_mysql.sql").read_text(encoding="utf-8-sig")
        cls.exported = {}
        for encoded in re.findall(r"CAST\(CONVERT\(X'([0-9a-f]+)' USING utf8mb4\) AS JSON\)", sql, re.IGNORECASE):
            record = json.loads(bytes.fromhex(encoded).decode("utf-8"))
            if isinstance(record, dict) and "alias_id" in record and "alias_key" in record:
                cls.exported[record["alias_id"]] = record

    def sql_rows(self):
        columns = ("alias_id", "alias", "alias_key", "entity_id", "entity_type", "alias_kind", "ambiguous", "source_ids")
        return [{**{k: record[k] for k in columns}, "source_ids": json.dumps(record["source_ids"]),
                 "raw_record": json.dumps(record, ensure_ascii=False)} for record in self.exported.values()]

    @contextmanager
    def repository(self, rows):
        with (patch("qa.repository.mysql.mysql_read_connection", return_value=nullcontext(AliasConnection(rows))),
              patch("qa.repository.mysql.build_tables", return_value=[self.alias_table])):
            yield MySQLRepository(Settings(), self.dataset)

    def test_runtime_alias_records_match_published_sql_without_mutating_jsonl(self):
        self.assertEqual(set(self.exported), {r["alias_id"] for r in self.expected})
        for expected in self.expected:
            with self.subTest(alias_id=expected["alias_id"]):
                self.assertEqual(expected["raw_record"], self.exported[expected["alias_id"]])
                self.assertEqual(expected["raw_record"]["alias_key"], expected["alias_key"])
        self.assertTrue(all("alias_key" not in r for r in self.dataset.tables["aliases"]))

    def test_imported_aliases_pass_the_readiness_check(self):
        with self.repository(self.sql_rows()) as repo:
            repo.ensure_ready()
            self.assertIsNotNone(repo._checked_at)

    def test_actual_column_or_provenance_changes_are_still_rejected(self):
        for column in ("alias_key", "raw_record", "alias_kind"):
            rows = deepcopy(self.sql_rows())
            if column == "alias_kind":
                rows[0].pop(column)
            else:
                rows[0][column] = "changed" if column == "alias_key" else json.dumps({"changed": True})
            with self.subTest(column=column), self.repository(rows) as repo:
                with self.assertRaises(DataVersionError):
                    repo.ensure_ready()
                self.assertIsNone(repo._checked_at)

    def test_missing_aliases_are_still_rejected(self):
        with self.repository(self.sql_rows()[1:]) as repo:
            with self.assertRaises(DataVersionError):
                repo.ensure_ready()


if __name__ == "__main__":
    unittest.main()
