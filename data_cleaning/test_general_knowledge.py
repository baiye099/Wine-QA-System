"""通用知识清洗中会影响回答含义、证据等级和已有业务ID的边界。"""

import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from clean import ROOT
from clean_general_knowledge import (
    RULES_PATH, build_tables, check_output_location, clean_card, find_overlaps, load_json,
)


class GeneralKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.rules = load_json(RULES_PATH)
        self.source = {"source_id": "GS001", "title": "Example", "publisher": "Example publisher",
                       "url": "https://example.org/wine", "accessed_date": "2026-11-11",
                       "published_at": None, "evidence_status": "page_read", "source_role": "primary"}
        self.card = {"knowledge_id": "WK999", "title": "干型糖度的条件", "question": "干型糖能超过4克/升吗？",
                     "text": "不一定。满足糖与总酸差值不超过2克/升等条件时，可放宽至9克/升。不能省略酸度条件。",
                     "category": "甜度与标签", "keywords": ["干型", "残糖"], "source_ids": ["GS001"],
                     "source_urls": [self.source["url"]], "applicability": "OIV定义语境，不能直接代替中国产品标签依据",
                     "language": "zh-CN", "accessed_date": "2026-11-11", "text_type": "editorial_paraphrase",
                     "review_status": "原包编辑核对，未经专家复审", "eligible_for_retrieval": True}
        self.package = ROOT / "data_raw" / "synthetic_general_fixture"

    def clean(self, card=None, evidence="reported_body_read"):
        source_map = {"GS001": {**self.source, "access_evidence_status": evidence}}
        return clean_card(card or self.card, source_map, self.rules, "fixture#WK999")

    def test_answer_retains_negation_threshold_and_jurisdiction_with_question(self):
        row = self.clean()
        for phrase in ["不一定", "不超过2克/升", "9克/升", "不能省略酸度条件", "不能直接代替中国产品标签依据"]:
            self.assertIn(phrase, row["text"])
        self.assertIn(row["question"], row["text"])
        self.assertEqual(row["text_type"], "faithful_summary")
        self.assertEqual(row["review_status_raw"], self.card["review_status"])
        self.assertEqual(row["external_verification"], "not_performed")

    def test_input_ineligibility_and_weak_evidence_each_block_retrieval(self):
        for card, evidence in [({**self.card, "eligible_for_retrieval": False}, "reported_body_read"),
                               (self.card, "unknown")]:
            with self.subTest(evidence=evidence):
                row = self.clean(card, evidence)
                self.assertEqual(row["review_status"], "review")
                self.assertFalse(row["retrieval_allowed"])
                self.assertIn("9克/升", row["answer"])

    def test_old_id_specific_rule_rejects_a_changed_title(self):
        raw = {**self.card, "knowledge_id": "WK073", "title": "另一条知识"}
        with self.assertRaisesRegex(ValueError, "旧分类规则不适用"):
            self.clean(raw)

    def test_source_urls_follow_sorted_source_ids(self):
        raw = {**self.card, "source_ids": ["GS002", "GS001"],
               "source_urls": ["https://example.org/two", self.source["url"]]}
        sources = {"GS001": {**self.source, "access_evidence_status": "reported_body_read"},
                   "GS002": {"url": "https://example.org/two", "access_evidence_status": "reported_body_read"}}
        row = clean_card(raw, sources, self.rules, "fixture")
        self.assertEqual(list(zip(row["source_ids"], row["source_urls"])),
                         [("GS001", self.source["url"]), ("GS002", "https://example.org/two")])

    def test_reported_page_read_does_not_become_an_original_web_snapshot(self):
        raw = {"sources": [self.source], "knowledge": [self.card], "aliases": []}
        original = copy.deepcopy(raw)
        tables, _ = build_tables(raw, self.package, self.rules)
        source = tables["sources"][0]
        self.assertEqual(source["evidence_form"], "collector_summary")
        self.assertIsNone(source["snapshot_path"])
        self.assertIsNone(source["published_at"])
        self.assertEqual(source["accessed_date"], "2026-11-11")
        self.assertEqual(raw, original)

    def test_related_chemical_is_only_a_retrieval_hint(self):
        card = {**self.card, "knowledge_id": "WK034", "title": "软木塞污染", "category": "常见缺陷与误区",
                "text": "软木塞污染常与TCA有关，不能与软木碎屑混淆。"}
        raw = {"sources": [self.source], "knowledge": [card], "aliases": [
            {"canonical": "软木塞污染", "aliases": ["TCA", "软木塞味"], "knowledge_ids": ["WK034"], "origin": "检索词典"}]}
        tables, audit = build_tables(raw, self.package, self.rules)
        self.assertNotIn("TCA", {a["alias"] for a in tables["aliases"]})
        self.assertEqual(audit["retrieval_alias_hints"][0]["knowledge_ids"], ["WK034"])
        self.assertTrue(all(r["relation_type"] in {"HAS_CHUNK", "ABOUT"} for r in tables["relations"]))

    def test_existing_knowledge_id_cannot_be_silently_reused(self):
        tables, _ = build_tables({"sources": [self.source], "knowledge": [self.card], "aliases": []}, self.package, self.rules)
        old = {name: [] for name in tables}
        old["knowledge"] = [{"knowledge_id": "WK999", "title": "已有完全不同知识"}]
        with patch("clean_general_knowledge.load_rows", side_effect=lambda p: old[p.stem]):
            with self.assertRaisesRegex(ValueError, "ID冲突"):
                find_overlaps(tables, ROOT / "data", self.rules)

    def test_output_cannot_overwrite_input_or_its_parent(self):
        base = ROOT / "data_cleaning" / "synthetic_paths"
        source = base / "raw"
        for output in [source, source / "nested", base]:
            with self.subTest(output=output):
                with self.assertRaises(ValueError):
                    check_output_location(output, source)
        check_output_location(base / "cleaned", source)


if __name__ == "__main__":
    unittest.main()
