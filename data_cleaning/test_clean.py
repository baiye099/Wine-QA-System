"""针对会改变问答事实或筛选结果的边界情况进行回归测试。"""

import unittest

from clean import alcohol_pct, date_compatible, date_value, external_review_id, extract_flavors, is_catalog_candidate, number, redact_review, text, vintage_value, volume_ml
from validate import schema_errors


class CleaningRuleTests(unittest.TestCase):
    def test_volume_units_and_missing(self):
        self.assertEqual(volume_ml("0.75 L"), 750)
        self.assertEqual(volume_ml("75 cl"), 750)
        self.assertIsNone(volume_ml(None))
        self.assertIsNone(volume_ml("750左右"))
        self.assertIsNone(volume_ml(0))

    def test_alcohol_is_percent_number(self):
        self.assertEqual(alcohol_pct("14.2%vol"), 14.2)
        self.assertIsNone(alcohol_pct("未知"))
        self.assertIsNone(alcohol_pct(120))

    def test_non_vintage_differs_from_unknown(self):
        self.assertEqual(vintage_value({"is_non_vintage": True, "vintage": None}, 2026)[:2], (None, "non_vintage"))
        self.assertEqual(vintage_value({"is_non_vintage": False, "vintage": None}, 2026)[:2], (None, "unspecified"))
        self.assertEqual(vintage_value({"vintage": 2022}, 2026)[:2], (2022, "specified"))

    def test_nv_does_not_inherit_base_year(self):
        year, status, problem = vintage_value({"is_non_vintage": True, "vintage": 2021}, 2026)
        self.assertIsNone(year)
        self.assertEqual(status, "non_vintage")
        self.assertEqual(problem, "nv_year_conflict")

    def test_future_and_mixed_vintage_not_guessed(self):
        self.assertEqual(vintage_value({"vintage": 2030}, 2026)[0], None)
        self.assertEqual(vintage_value({"record_scope": "2019/2020随机发货"}, 2026)[:2], (None, "mixed"))
        self.assertEqual(vintage_value({"vintage_status": "mixed", "vintage": 2019}, 2026)[:2], (None, "mixed"))

    def test_date_precision_is_preserved(self):
        self.assertEqual(date_value("2025-09"), ("2025-09", "month"))
        self.assertEqual(date_value("2022"), ("2022", "year"))
        self.assertEqual(date_value("2025-02-30"), (None, "unknown"))
        self.assertEqual(date_value("昨天"), (None, "unknown"))
        self.assertEqual(date_value("/Date(0+0800)/"), ("1970-01-01", "day"))
        self.assertFalse(date_compatible("2022", "2026-10-01"))

    def test_missing_is_not_zero(self):
        self.assertIsNone(number("暂无"))
        self.assertIsNone(number("NaN"))
        self.assertIsNone(number("inf"))
        self.assertIsNone(number(True))
        self.assertEqual(number(0), 0)

    def test_markup_cleanup_preserves_negation(self):
        self.assertEqual(text("<p>不需要醒酒</p><script>alert(1)</script>"), "不需要醒酒")
        self.assertEqual(text("半甜\\r\\n不等于干型"), "半甜\n不等于干型")

    def test_unicode_line_separator_is_safe_in_jsonl(self):
        import json
        encoded = json.dumps({"text": text("第一段\u2028第二段\u2029第三段")}, ensure_ascii=False)
        self.assertEqual(len(encoded.splitlines()), 1)
        self.assertEqual(json.loads(encoded)["text"], "第一段\n第二段\n第三段")

    def test_directory_plus_unavailable_detail_is_still_candidate(self):
        raw = {"review_status": "仅目录收录，详情待补", "notes": "目录出现产品，详情获取失败"}
        self.assertTrue(is_catalog_candidate(raw, ["S001"], ["S001"]))
        self.assertTrue(is_catalog_candidate({}, ["S001"], ["S001"]))
        self.assertFalse(is_catalog_candidate({"review_status": "已核对页面字段"}, ["S003"], ["S001"]))

    def test_flavor_extraction_does_not_negate_meaning(self):
        self.assertEqual(extract_flavors("没有明显黑醋栗香气", ["黑醋栗"]), [])
        self.assertEqual(extract_flavors("黑樱桃和苹果香", ["樱桃", "黑樱桃", "苹果"]), ["苹果", "黑樱桃"])

    def test_review_contact_redaction(self):
        cleaned, changed = redact_review("电话13812345678，邮箱test@example.com，体验很好")
        self.assertTrue(changed)
        self.assertNotIn("13812345678", cleaned)
        self.assertNotIn("test@example.com", cleaned)
        self.assertIn("体验很好", cleaned)

    def test_platform_export_id_prefix_is_not_new_review(self):
        self.assertEqual(external_review_id({"review_id": "ctrip_734060188"}, "ctrip"), "734060188")
        self.assertEqual(external_review_id({"commentId": 734060188}, "ctrip"), "734060188")
        self.assertEqual(external_review_id({"comment_id": "734060188"}, "ctrip"), "734060188")
        self.assertIsNone(external_review_id({"id": "734060188"}, "ctrip"))
        self.assertIsNone(external_review_id({"review_id": "ctrip_734060188"}, "unknown"))

    def test_schema_rejects_string_boolean_and_nonfinite(self):
        self.assertTrue(schema_errors("false", {"type": "boolean"}))
        self.assertTrue(schema_errors(float("nan"), {"type": "number"}))
        self.assertTrue(schema_errors(True, {"type": "integer"}))


if __name__ == "__main__":
    unittest.main()
