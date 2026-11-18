"""合并时保留旧事实与证据、拒绝ID碰撞的边界。"""

import copy
import unittest

from merge_general_knowledge import merge_record


class MergeKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.old = {"entity_id": "G_example", "entity_type": "GrapeVariety", "name": "赤霞珠",
                    "source_ids": ["S001"], "raw_locator": "data_raw/old.json#1",
                    "raw_locators": ["data_raw/old.json#1"], "review_status": "accepted", "notes": "旧证据"}
        self.new = {**self.old, "source_ids": ["GS001"], "raw_locator": "data_raw/new.json#1",
                    "raw_locators": ["data_raw/new.json#1"], "notes": "新增证据"}

    def test_shared_entity_retains_both_provenances_without_mutating_inputs(self):
        previous = copy.deepcopy((self.old, self.new))
        result = merge_record(self.old, self.new, "entities")
        self.assertEqual(set(result["source_ids"]), {"S001", "GS001"})
        self.assertEqual(set(result["raw_locators"]), {self.old["raw_locator"], self.new["raw_locator"]})
        self.assertEqual(result["raw_locator"], self.old["raw_locator"])
        self.assertEqual(set(result["merge_notes"]), {"旧证据", "新增证据"})
        self.assertEqual((self.old, self.new), previous)

    def test_same_id_with_another_type_or_name_is_rejected(self):
        for fields in [{"entity_type": "Concept"}, {"name": "另一品种"}]:
            with self.subTest(fields=fields):
                with self.assertRaisesRegex(ValueError, "同ID含义冲突"):
                    merge_record(self.old, {**self.new, **fields}, "entities")

    def test_same_knowledge_id_cannot_overwrite_text_or_scope(self):
        for fields in [{"text": "另一结论"}, {"fact_scope": "另一适用范围"}]:
            original = {"knowledge_id": "WK001", "text": "带条件的原结论", "fact_scope": "原适用范围"}
            with self.assertRaisesRegex(ValueError, "业务内容冲突"):
                merge_record(original, {**original, **fields}, "knowledge")

    def test_shared_alias_cannot_be_redirected_to_another_entity(self):
        original = {**self.old, "alias_id": "A_example", "alias": "赤霞珠"}
        incoming = {**self.new, "alias_id": "A_example", "alias": "赤霞珠", "entity_id": "G_another"}
        with self.assertRaisesRegex(ValueError, "同ID含义冲突"):
            merge_record(original, incoming, "aliases")


if __name__ == "__main__":
    unittest.main()
