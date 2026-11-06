"""对照实际发布 SQL 验证别名快照，确保导入后的合法记录不会被误判。"""

import json
import re
import unittest
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import patch

from qa.config import ROOT, Settings
from qa.data import mysql_expected_tables, runtime_dataset
from qa.errors import DataVersionError
from qa.mysql_repository import MySQLRepository


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
        cls.expected = mysql_expected_tables(cls.dataset)["entity_alias"]
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

    def repository(self, rows):
        repo = MySQLRepository(Settings(), self.dataset)
        repo._connection = lambda: nullcontext(AliasConnection(rows))
        return repo

    def test_runtime_alias_records_match_published_sql_without_mutating_jsonl(self):
        self.assertEqual(set(self.exported), {r["alias_id"] for r in self.expected})
        for expected in self.expected:
            with self.subTest(alias_id=expected["alias_id"]):
                self.assertEqual(expected["raw_record"], self.exported[expected["alias_id"]])
                self.assertEqual(expected["raw_record"]["alias_key"], expected["alias_key"])
        self.assertTrue(all("alias_key" not in r for r in self.dataset.tables["aliases"]))

    def test_imported_aliases_pass_the_readiness_check(self):
        repo = self.repository(self.sql_rows())
        with patch("qa.mysql_repository.mysql_expected_tables", return_value={"entity_alias": self.expected}):
            repo.ensure_ready()
        self.assertIsNotNone(repo._checked_at)

    def test_actual_column_or_provenance_changes_are_still_rejected(self):
        for column in ("alias_key", "raw_record"):
            rows = deepcopy(self.sql_rows())
            rows[0][column] = "changed" if column == "alias_key" else json.dumps({"changed": True})
            repo = self.repository(rows)
            with self.subTest(column=column), patch("qa.mysql_repository.mysql_expected_tables", return_value={"entity_alias": self.expected}):
                with self.assertRaises(DataVersionError):
                    repo.ensure_ready()
                self.assertIsNone(repo._checked_at)

    def test_missing_aliases_are_still_rejected(self):
        repo = self.repository(self.sql_rows()[1:])
        with patch("qa.mysql_repository.mysql_expected_tables", return_value={"entity_alias": self.expected}):
            with self.assertRaises(DataVersionError):
                repo.ensure_ready()


if __name__ == "__main__":
    unittest.main()
