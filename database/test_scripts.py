"""离线测试；不创建或连接MySQL/Neo4j数据库。"""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if __name__ == "__main__" and not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database.generate_mysql_sql import render_data, sql_value
from database.import_neo4j import DATASET_NAME, LEGACY_DATASET_NAMES, apply_snapshot, build_plan, node_query, property_map, relationship_query
from qa.core.paths import ROOT
from qa.db.mysql_tables import build_tables
from qa.db.snapshot import load_dataset


class FakeResult(list):
    def single(self):
        if len(self) != 1:
            raise AssertionError("expected single result")
        return self[0]


class RecordingTransaction:
    def __init__(self, plan, fail_relationship=False, existing_nodes=None, existing_relations=None):
        self.plan = plan
        self.calls = []
        self.fail_relationship = fail_relationship
        self.existing_nodes = existing_nodes or []
        self.existing_relations = existing_relations or []

    def run(self, query, **parameters):
        self.calls.append((query, parameters))
        if "RETURN n.entity_id AS entity_id" in query:
            return FakeResult(self.existing_nodes)
        if "RETURN r.relation_id AS relation_id" in query:
            return FakeResult(self.existing_relations)
        if "AS imported" in query:
            count = len(parameters["rows"])
            if self.fail_relationship and "count(r)" in query:
                count -= 1
            return FakeResult([{"imported": count}])
        if "count(n) AS total" in query:
            return FakeResult([{"total": len(self.plan["nodes"])}])
        if "count(r) AS total" in query:
            return FakeResult([{"total": len(self.plan["relations"])}])
        raise AssertionError("unexpected query")


class ImportScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = load_dataset(ROOT / "data")
        cls.tables = build_tables(cls.ds)
        cls.plan = build_plan(cls.ds)

    def test_sql_text_preserves_quotes_backslashes_and_chinese(self):
        original = "宁夏 Emma's\\酒庄\n第二行"
        encoded = sql_value(original)
        payload = re.search(r"X'([0-9a-f]*)'", encoded)[1]
        self.assertEqual(bytes.fromhex(payload).decode("utf-8"), original)

    def test_json_literal_keeps_boolean_null_and_nested_data(self):
        original = {"eligible": False, "year": None, "sources": ["S001"], "detail": {"label": "中文"}}
        encoded = sql_value(original, is_json=True)
        payload = re.search(r"X'([0-9a-f]*)'", encoded)[1]
        self.assertEqual(json.loads(bytes.fromhex(payload).decode("utf-8")), original)
        self.assertEqual(sql_value(None), "NULL")

    def test_nonfinite_cannot_enter_sql(self):
        with self.assertRaises(ValueError):
            sql_value(math.inf)

    def test_mysql_expected_records_and_reference_prices(self):
        counts = {t.name: len(t.rows) for t in self.tables}
        self.assertEqual(counts["winery"], 22)
        self.assertEqual(counts["product"], 47)
        self.assertEqual(counts["product_grape"], 51)
        self.assertTrue(all(not r["current_retail_eligible"] and not r["strict_price_filter_eligible"] for t in self.tables if t.name == "price_observation" for r in t.rows))

    def test_sql_foreign_keys_have_preceding_rows(self):
        lookup = {t.name: t.rows for t in self.tables}
        registry = {r["entity_id"] for r in lookup["entity_registry"]}
        wineries = {r["winery_id"] for r in lookup["winery"]}
        products = {r["product_id"] for r in lookup["product"]}
        regions = {r["region_id"] for r in lookup["region"]}
        grapes = {r["grape_id"] for r in lookup["grape_variety"]}
        sources = {r["source_id"] for r in lookup["source_document"]}
        for p in lookup["product"]:
            self.assertIn(p["product_id"], registry)
            self.assertIn(p["winery_id"], wineries)
            self.assertTrue(p["region_id"] is None or p["region_id"] in regions)
        for r in lookup["product_grape"]:
            self.assertIn(r["product_id"], products)
            self.assertIn(r["grape_id"], grapes)
        for a in lookup["entity_alias"]:
            self.assertIn(a["entity_id"], registry)
        for p in lookup["price_observation"]:
            self.assertIn(p["product_id"], products)
            self.assertIn(p["source_id"], sources)

    def test_generated_columns_exist_in_schema(self):
        schema = (Path(__file__).parent / "01_schema_mysql.sql").read_text(encoding="utf-8")
        for table in self.tables:
            block = re.search(r"CREATE TABLE IF NOT EXISTS `" + table.name + r"` \((.*?)\n\) ENGINE", schema, re.S)[1]
            columns = set(re.findall(r"^  `([^`]+)` ", block, re.M))
            self.assertEqual(set(table.columns), columns)

    def test_sql_strings_fit_declared_column_lengths(self):
        schema = (Path(__file__).parent / "01_schema_mysql.sql").read_text(encoding="utf-8")
        for table in self.tables:
            block = re.search(r"CREATE TABLE IF NOT EXISTS `" + table.name + r"` \((.*?)\n\) ENGINE", schema, re.S)[1]
            limits = dict((name, int(size)) for name, size in re.findall(r"^  `([^`]+)` (?:VAR)?CHAR\((\d+)\)", block, re.M))
            for row in table.rows:
                for field, maximum in limits.items():
                    if row[field] is not None:
                        self.assertLessEqual(len(row[field]), maximum, f"{table.name}/{field}")

    def test_sql_generator_is_deterministic_and_upserts(self):
        first = render_data(self.ds, self.tables, "Wine_QA_System", 50)
        self.assertEqual(first, render_data(self.ds, self.tables, "Wine_QA_System", 50))
        self.assertIn("ON DUPLICATE KEY UPDATE", first)
        self.assertIn("START TRANSACTION;", first)
        self.assertNotIn("DROP TABLE", first)

    def test_neo4j_node_and_relationship_counts(self):
        self.assertEqual(len(self.plan["nodes"]), 581)
        self.assertEqual(len(self.plan["relations"]), 746)
        self.assertEqual(self.plan["summary"]["nodes_by_label"]["Chunk"], 228)
        self.assertEqual(self.plan["summary"]["dataset_name"], "wine-qa-system")

    def test_neo4j_properties_are_native_or_serialized(self):
        for record in self.plan["nodes"] + self.plan["relations"]:
            for value in record["properties"].values():
                self.assertNotIsInstance(value, dict)
                if isinstance(value, list):
                    self.assertTrue(all(isinstance(v, (str, int, float, bool)) for v in value))
        props = property_map({"nested": {"a": False}, "items": [{"a": 1}], "unknown": None})
        self.assertEqual(json.loads(props["nested_json"]), {"a": False})
        self.assertEqual(json.loads(props["items_json"]), [{"a": 1}])
        self.assertIsNone(props["unknown"])

    def test_queries_only_allow_fixed_identifiers(self):
        with self.assertRaises(ValueError):
            node_query("Product) DETACH DELETE n //")
        with self.assertRaises(ValueError):
            relationship_query("BAD_TYPE")
        self.assertIn("$rows", node_query("Product"))
        self.assertNotIn("西鸽", node_query("Product"))

    def test_text_changes_invalidate_prior_embeddings(self):
        query = node_query("Chunk")
        self.assertIn("n.text_hash = row.properties.text_hash", query)
        self.assertIn("REMOVE n.embedding", query)

    def test_transaction_orders_nodes_before_relationships(self):
        tx = RecordingTransaction(self.plan)
        result = apply_snapshot(tx, self.plan, 200)
        self.assertEqual(result["verified_node_count"], 581)
        node_positions = [i for i, (q, _) in enumerate(tx.calls) if "count(n) AS imported" in q]
        rel_positions = [i for i, (q, _) in enumerate(tx.calls) if "count(r) AS imported" in q]
        self.assertLess(max(node_positions), min(rel_positions))

    def node_metadata(self, node, owner, **changes):
        return {"entity_id": node["entity_id"], "dataset_name": owner, "entity_type": node["label"],
                "node_labels": ["QAEntity", node["label"]], "snapshot": self.plan["snapshot"],
                "same_record": False, **changes}

    def relation_metadata(self, relation, owner, **changes):
        return {"relation_id": relation["relation_id"], "dataset_name": owner,
                "relation_type": relation["relation_type"], "head_id": relation["head_id"], "tail_id": relation["tail_id"],
                "snapshot": self.plan["snapshot"], "same_record": False, **changes}

    def test_old_dataset_names_migrate_matching_nodes_and_relations(self):
        for old_name in LEGACY_DATASET_NAMES:
            with self.subTest(old_name=old_name):
                tx = RecordingTransaction(self.plan,
                    existing_nodes=[self.node_metadata(n, old_name) for n in self.plan["nodes"]],
                    existing_relations=[self.relation_metadata(r, old_name) for r in self.plan["relations"]])
                result = apply_snapshot(tx, self.plan, 200)
                self.assertEqual(result["ownership_migration"]["nodes_by_previous_dataset"], {old_name: 581})
                self.assertEqual(result["ownership_migration"]["relationships_by_previous_dataset"], {old_name: 746})
                for query, params in tx.calls:
                    if "AS imported" in query:
                        self.assertTrue(all(r["properties"]["qa_dataset"] == DATASET_NAME for r in params["rows"]))
                    self.assertNotIn("DELETE", query)

    def test_old_owner_accepts_identical_record_when_snapshot_differs(self):
        tx = RecordingTransaction(self.plan, existing_nodes=[self.node_metadata(self.plan["nodes"][0], "Wine_QA_System", snapshot="old-snapshot", same_record=True)])
        result = apply_snapshot(tx, self.plan, 200)
        self.assertEqual(result["ownership_migration"]["nodes_by_previous_dataset"], {"Wine_QA_System": 1})

    def test_old_owner_different_snapshot_and_record_is_rejected_before_writes(self):
        tx = RecordingTransaction(self.plan, existing_nodes=[self.node_metadata(self.plan["nodes"][0], "Wine_QA_System", snapshot="unrelated-snapshot")])
        with self.assertRaisesRegex(ValueError, "不能自动迁移"):
            apply_snapshot(tx, self.plan, 200)
        self.assertFalse(any("AS imported" in q for q, _ in tx.calls))

    def test_foreign_or_missing_ownership_still_rejected(self):
        for owner in ["other_project", None, ""]:
            with self.subTest(owner=owner):
                tx = RecordingTransaction(self.plan, existing_nodes=[self.node_metadata(self.plan["nodes"][0], owner)])
                with self.assertRaisesRegex(ValueError, "已有归属"):
                    apply_snapshot(tx, self.plan, 200)
                self.assertFalse(any("AS imported" in q for q, _ in tx.calls))

    def test_known_legacy_owner_cannot_bypass_type_check(self):
        tx = RecordingTransaction(self.plan, existing_nodes=[self.node_metadata(self.plan["nodes"][0], "Wine_QA_System", entity_type="Product", node_labels=["QAEntity", "Product"])])
        with self.assertRaisesRegex(ValueError, "预期类型"):
            apply_snapshot(tx, self.plan, 200)

    def test_known_legacy_owner_cannot_bypass_relationship_endpoint_check(self):
        tx = RecordingTransaction(self.plan, existing_relations=[self.relation_metadata(self.plan["relations"][0], "Wine_QA_System", tail_id="unrelated_id")])
        with self.assertRaisesRegex(ValueError, "类型或端点"):
            apply_snapshot(tx, self.plan, 200)
        self.assertFalse(any("AS imported" in q for q, _ in tx.calls))

    def test_current_owner_does_not_require_migration(self):
        tx = RecordingTransaction(self.plan,
            existing_nodes=[self.node_metadata(n, DATASET_NAME) for n in self.plan["nodes"]],
            existing_relations=[self.relation_metadata(r, DATASET_NAME) for r in self.plan["relations"]])
        result = apply_snapshot(tx, self.plan, 200)
        self.assertEqual(result["ownership_migration"], {"nodes_by_previous_dataset": {}, "relationships_by_previous_dataset": {}})

    def test_missing_relationship_endpoint_is_a_failure(self):
        tx = RecordingTransaction(self.plan, fail_relationship=True)
        with self.assertRaises(RuntimeError):
            apply_snapshot(tx, self.plan, 200)

    def test_dry_run_never_needs_connectivity(self):
        env = os.environ.copy()
        env.pop("NEO4J_DATABASE", None)
        result = subprocess.run([sys.executable, str(Path(__file__).parent / "import_neo4j.py"), "--dry-run", "--uri", "bolt://invalid-host.invalid:1"],
                                capture_output=True, text=True, encoding="utf-8", timeout=20, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertFalse(report["connected_database"])
        self.assertEqual(report["database"], "wine-qa-system")

    def test_changed_formal_file_is_rejected(self):
        work = ROOT / "work" / "database_tests"
        work.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="fixture_", dir=work))
        try:
            manifest = json.loads((ROOT / "data/manifest.json").read_text(encoding="utf-8"))
            (tmp / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (tmp / "sources.jsonl").write_text('{"tampered":true}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "manifest"):
                load_dataset(tmp)
        finally:
            # 只删除本测试已知的两个临时文件；不用递归删除。
            for filename in ["manifest.json", "sources.jsonl"]:
                (tmp / filename).unlink(missing_ok=True)
            tmp.rmdir()


if __name__ == "__main__":
    unittest.main()
