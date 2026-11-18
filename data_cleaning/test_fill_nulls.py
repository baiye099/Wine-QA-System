"""验证已批准的空值填充不会覆盖原值或改变无年份酒等业务含义。"""

import copy
import unittest

from clean import ID_KEYS
from fill_nulls import REVISION, field_is_filled, fill_field, validate_preservation


class NullFillTests(unittest.TestCase):
    def test_non_null_business_value_is_preserved(self):
        row = {"product_id": "P001", "color": "white"}
        before = copy.deepcopy(row)
        journal = []
        self.assertFalse(fill_field(journal, "product", row, "color", "red"))
        self.assertEqual(row, before)
        self.assertEqual(journal, [])

    def test_false_value_is_a_valid_traced_fill(self):
        row = {"product_id": "P001", "is_sparkling": None}
        journal = []
        fill_field(journal, "product", row, "is_sparkling", False)
        self.assertTrue(field_is_filled(row, "is_sparkling"))
        self.assertIs(journal[0]["before"], None)
        self.assertIs(journal[0]["after"], False)

    def test_generic_synthetic_flag_cannot_bypass_validation(self):
        self.assertFalse(field_is_filled({"color": "red", "synthetic": True}, "color"))
        self.assertFalse(field_is_filled({"color": "red", "null_fill": {"revision": REVISION, "fields": {"color": {"origin": "generated"}}}}, "color"))

    def test_filled_value_changed_without_lineage_is_rejected(self):
        row = {"product_id": "P001", "price_cny": None}
        fill_field([], "product", row, "price_cny", 298)
        self.assertTrue(field_is_filled(row, "price_cny"))
        row["price_cny"] = 888
        self.assertFalse(field_is_filled(row, "price_cny"))

    def test_non_vintage_status_cannot_be_replaced(self):
        before = {t: [] for t in ID_KEYS}
        before["products"] = [{"product_id": "P055", "vintage": None, "vintage_status": "non_vintage", "grape_varieties": []}]
        after = copy.deepcopy(before)
        fill_field([], "product", after["products"][0], "vintage", 2022)
        after["products"][0]["vintage_status"] = "specified"
        with self.assertRaisesRegex(ValueError, "已有非空值发生变化"):
            validate_preservation(before, after)

    def test_existing_company_name_cannot_change_during_synchronization(self):
        before = {t: [] for t in ID_KEYS}
        before["wineries"] = [{"winery_id": "W001", "company_name": "原有公司"}]
        after = copy.deepcopy(before)
        after["wineries"][0]["company_name"] = "另一公司"
        with self.assertRaisesRegex(ValueError, "已有非空值发生变化"):
            validate_preservation(before, after)


if __name__ == "__main__":
    unittest.main()
