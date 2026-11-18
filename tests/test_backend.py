"""围绕筛选合规、指代、会话隔离和证据边界的回归用例。"""

import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from qa.answer import AnswerBuilder
from qa.config import Settings
from qa.data import runtime_dataset
from qa.demo_repository import DemoRepository
from qa.errors import DataVersionError, ModelError
from qa.filtering import eligible_price, matches_filters
from qa.models import validate_vector
from qa.mysql_repository import compile_product_query
from qa.schemas import KnowledgeChunk, ProductFilters
from qa.service import QAService, build_service
from qa.session import ConversationState


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.settings = Settings()
        cls.dataset = runtime_dataset(cls.settings)

    def setUp(self):
        self.repo = DemoRepository(self.dataset, self.settings)
        self.service = QAService(self.repo, self.repo, self.settings)
        self.state = ConversationState()

    def test_filter_inheritance_and_ordinal_use_actual_display_order(self):
        first = self.service.ask("找几款750毫升的干红", self.state)
        self.assertEqual(first.status, "ok")
        self.assertEqual(len(first.products), 9)
        second = self.service.ask("要含赤霞珠的", self.state)
        self.assertEqual(second.status, "ok")
        self.assertEqual(second.effective_filters.volume_ml, 750)
        self.assertEqual(second.effective_filters.color, "red")
        target = second.products[1]
        third = self.service.ask("第二款是哪家酒庄的？", self.state)
        self.assertEqual(third.status, "ok")
        self.assertEqual(third.products[0]["product_id"], target["product_id"])
        self.assertEqual(third.wineries[0]["winery_id"], target["winery_id"])

    def test_remove_volume_and_reset_do_not_affect_other_sessions(self):
        other = ConversationState()
        self.service.ask("找几款750毫升的干红", self.state)
        response = self.service.ask("容量不限", self.state)
        self.assertIsNone(response.effective_filters.volume_ml)
        self.assertEqual(response.effective_filters.color, "red")
        self.assertEqual(other.last_results, [])
        self.assertEqual(other.messages, [])
        self.state.reset()
        self.assertEqual(self.state.context()["filters"], {})
        self.assertEqual(self.state.last_results, [])

    def test_budget_is_not_silently_removed_on_next_turn(self):
        response = self.service.ask("找几款不超过100元的干红", self.state)
        self.assertEqual(response.status, "empty")
        self.assertEqual(response.products, [])
        following = self.service.ask("要含赤霞珠的", self.state)
        self.assertEqual(following.status, "empty")
        self.assertEqual(following.effective_filters.price_max, 100)

    def test_unknown_field_is_not_treated_as_match(self):
        product = deepcopy(self.dataset.tables["products"][0])
        product["field_filter_eligibility"]["volume_ml"] = False
        self.assertFalse(matches_filters(product, ProductFilters(volume_ml=750)))
        product["volume_ml"] = None
        self.assertFalse(matches_filters(product, ProductFilters(volume_ml=750)))

    def test_contains_grape_does_not_prove_one_hundred_percent(self):
        product = deepcopy(self.dataset.tables["products"][0])
        grape = product["grape_varieties"][0]["entity_id"]
        product["grapes_complete"] = False
        product["grape_varieties"][0]["proportion_pct"] = None
        self.assertTrue(matches_filters(product, ProductFilters(grape_id=grape)))
        self.assertFalse(matches_filters(product, ProductFilters(grape_id=grape, grape_only=True)))

    def test_unconfigured_knowledge_clears_unrelated_filter_context(self):
        self.service.ask("找750毫升干红", self.state)
        response = self.service.ask("什么是单宁？", self.state)
        self.assertEqual(response.status, "not_ready")
        self.assertEqual(response.evidence, [])
        self.assertEqual(self.state.current_topic, "knowledge")
        self.assertEqual(self.state.filters.active(), {})

    def test_ambiguous_alias_and_ambiguous_pronoun_require_clarification(self):
        ambiguous = self.service.ask("介绍一下摩塞尔", self.state)
        self.assertEqual(ambiguous.status, "clarification")
        self.service.ask("找750毫升干红", self.state)
        response = self.service.ask("这款是哪家酒庄的？", self.state)
        self.assertEqual(response.status, "clarification")

    def test_empty_last_results_never_mean_search_everything(self):
        response = self.service.ask("在这几款中选干红", self.state)
        self.assertEqual(response.status, "clarification")
        self.assertEqual(response.products, [])

    def test_contract_rejects_query_fields_and_invalid_numbers(self):
        for values in ({"order_by": "DROP TABLE product"}, {"volume_ml": -750}, {"price_min": 200, "price_max": 100}, {"alcohol_max": float("nan")}):
            with self.assertRaises(ValidationError):
                ProductFilters(**values)
        query, params = compile_product_query(ProductFilters(winery_id="W001"), ["P001' OR 1=1 --"], 10)
        self.assertNotIn("OR 1=1", query)
        self.assertIn("P001' OR 1=1 --", params)

    def test_old_or_mismatched_price_is_not_eligible(self):
        settings = self.settings.model_copy(update={"price_enabled": True})
        now = datetime.now(timezone.utc)
        product = {"record_scope": "specific_variant", "vintage_status": "specified", "volume_ml": 750,
                   "field_filter_eligibility": {"volume_ml": True}}
        quote = {"current_retail_eligible": True, "strict_price_filter_eligible": True, "conflict": False,
                 "currency": "CNY", "unit": "bottle", "pack_quantity": 1, "amount_min": 100, "amount_max": 100,
                 "verified_at": now.isoformat()}
        self.assertTrue(eligible_price(quote, product, settings, now))
        for changes in ({"verified_at": (now - timedelta(days=31)).isoformat()}, {"unit": "box"}, {"currency": "USD"}, {"conflict": True}):
            self.assertFalse(eligible_price({**quote, **changes}, product, settings, now))

    def test_vector_dimensions_and_values_are_validated(self):
        for vector in ([0, 0], [float("nan"), 1], [1], [True, 1]):
            with self.assertRaises(ModelError):
                validate_vector(vector, 2)

    def test_foreign_or_fabricated_citations_fall_back_to_evidence(self):
        class WrongModel:
            def complete_json(self, messages):
                return '{"claims":[{"text":"这是虚构内容","knowledge_ids":["FOREIGN"]}]}'

        evidence = KnowledgeChunk(knowledge_id="K1", title="储存", text="资料说明应避光储存。", fact_scope="一般储存建议",
                                  category="storage", entity_ids=[], source_ids=["S1"])
        text, used, notices = AnswerBuilder(WrongModel()).knowledge("如何储存？", [evidence])
        self.assertIn(evidence.text, text)
        self.assertNotIn("虚构内容", text)
        self.assertEqual(used, [evidence])
        self.assertTrue(notices)

    def test_snapshot_mismatch_blocks_service_construction(self):
        with self.assertRaises(DataVersionError):
            build_service(self.settings.model_copy(update={"snapshot": "0" * 64}))


if __name__ == "__main__":
    unittest.main()

