"""产品问价、预算与生成价来源边界的回归测试。"""

import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from qa.config import Settings
from qa.data import runtime_dataset
from qa.demo_repository import DemoRepository
from qa.filtering import attach_prices, matches_filters
from qa.mysql_repository import MySQLRepository
from qa.schemas import ProductFilters
from qa.service import QAService
from qa.session import ConversationState
from qa.understand import QueryUnderstander


class PriceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings()
        cls.dataset = runtime_dataset(cls.settings)

    def setUp(self):
        self.repo = DemoRepository(self.dataset, self.settings)
        self.service = QAService(self.repo, self.repo, self.settings)

    def test_product_question_returns_generated_amount_without_claiming_verified_price(self):
        response = self.service.ask("贺兰红赤霞珠干红葡萄酒N.28多少钱？", ConversationState())
        self.assertEqual(response.status, "ok")
        self.assertIn("展示价格：298 元（生成数据，非实时零售价）", response.text)
        self.assertNotIn("已核验单瓶报价", response.text)
        self.assertNotIn("不能据此判断预算", response.text)
        price_line = next(line for line in response.text.splitlines() if "展示价格：" in line)
        self.assertNotIn("[S", price_line)
        self.assertNotIn("eligible_price", response.products[0])

    def test_budget_query_respects_amount_and_preserves_other_hard_conditions(self):
        response = self.service.ask("找几款不超过300元的750毫升干红", ConversationState())
        self.assertEqual(response.status, "ok")
        expected = [p["product_id"] for p in self.dataset.tables["products"]
                    if p["strict_product_filter_eligible"] and p["price_cny"] <= 300
                    and p["field_filter_eligibility"].get("color") and p["color"] == "red"
                    and p["field_filter_eligibility"].get("sweetness") and p["sweetness"] == "dry"
                    and p["field_filter_eligibility"].get("volume_ml") and p["volume_ml"] == 750]
        self.assertEqual(response.product_ids, sorted(expected)[:self.settings.max_results])
        self.assertIn("预算按各产品展示的价格筛选", response.text)

    def test_display_prices_can_be_disabled_independently_of_retail_price_rules(self):
        settings = self.settings.model_copy(update={"display_price_enabled": False})
        repo = DemoRepository(self.dataset, settings)
        response = QAService(repo, repo, settings).ask("300元以下的葡萄酒", ConversationState())
        self.assertEqual(response.status, "not_ready")
        self.assertNotIn("display_price", repo.products(["P001"])[0])

    def test_unknown_untracked_or_changed_prices_do_not_satisfy_budget(self):
        original = self.repo.products(["P001"])[0]
        variants = []
        for amount in (None, 0, -1, True, float("nan"), 299):
            row = deepcopy(original)
            row["price_cny"] = amount
            variants.append(row)
        untracked = deepcopy(original)
        untracked.pop("null_fill")
        variants.append(untracked)
        for row in variants:
            with self.subTest(amount=row.get("price_cny")):
                attach_prices([row], [], self.settings)
                self.assertNotIn("display_price", row)
                self.assertFalse(matches_filters(row, ProductFilters(price_max=1000)))

    def test_verified_quote_wins_and_expired_quote_falls_back_to_labelled_display_price(self):
        settings = self.settings.model_copy(update={"price_enabled": True})
        row = deepcopy(self.dataset.tables["products"][0])
        row.update(record_scope="specific_variant", vintage_status="specified", volume_ml=750)
        row["field_filter_eligibility"]["volume_ml"] = True
        quote = {"price_id": "Q1", "product_id": row["product_id"], "amount_min": 250, "amount_max": 250,
                 "current_retail_eligible": True, "strict_price_filter_eligible": True, "conflict": False,
                 "currency": "CNY", "unit": "bottle", "pack_quantity": 1,
                 "verified_at": datetime.now(timezone.utc).isoformat()}
        attach_prices([row], [quote], settings)
        self.assertEqual(row["eligible_price"]["amount_min"], 250)
        self.assertNotIn("display_price", row)
        quote["verified_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        attach_prices([row], [quote], settings)
        self.assertNotIn("eligible_price", row)
        self.assertEqual(row["display_price"]["amount"], row["price_cny"])

    def test_mysql_budget_is_applied_before_result_limit_and_uses_raw_record_provenance(self):
        rows = []
        for pid, amount in [("P001", 900), ("P002", 198)]:
            row = deepcopy(self.dataset.tables["products"][0])
            row.update(product_id=pid, price_cny=amount, strict_product_filter_eligible=True)
            row["null_fill"]["fields"]["price_cny"]["after"] = amount
            raw = deepcopy(row)
            row.pop("null_fill")
            row["raw_record"] = raw
            rows.append(row)
        repo = MySQLRepository(self.settings, SimpleNamespace(tables={"products": rows}))

        def read(query, params=()):
            if "v_product_basic" in query:
                return deepcopy(rows[:params[-1]])
            if "product_grape" in query:
                return []
            self.fail("unexpected read")

        repo._read = read
        result = repo.filter_products(ProductFilters(price_max=200), None, limit=1)
        self.assertEqual([r["product_id"] for r in result], ["P002"])
        self.assertEqual(result[0]["display_price"]["amount"], 198)
        self.assertNotIn("raw_record", result[0])

    def test_known_price_questions_keep_structured_route_with_model_configured(self):
        class UnexpectedModel:
            def complete_json(self, messages):
                raise AssertionError("known price query must not call the model")

        service = QAService(self.repo, self.repo, self.settings, understander=QueryUnderstander(UnexpectedModel()))
        for question in ("贺兰红赤霞珠干红葡萄酒N.28多少钱？", "300元以下的葡萄酒"):
            response = service.ask(question, ConversationState())
            self.assertEqual(response.status, "ok")
            self.assertTrue(response.products)


if __name__ == "__main__":
    unittest.main()
