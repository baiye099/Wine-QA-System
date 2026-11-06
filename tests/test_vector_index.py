"""基于 Neo4j 索引元数据检查跨版本兼容，同时保留真实配置冲突检查。"""

import unittest
from copy import deepcopy

from qa.config import Settings
from qa.errors import KnowledgeNotReady
from qa.neo4j_repository import validate_index


class VectorIndexTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(embedding_dimensions=4096)
        # 本机 Neo4j 2026.10.1 的 SHOW VECTOR INDEXES 返回大写 COSINE。
        self.index = {
            "state": "ONLINE",
            "labelsOrTypes": ["Chunk"],
            "properties": ["embedding"],
            "options": {"indexConfig": {
                "vector.dimensions": 4096,
                "vector.similarity_function": "COSINE",
                "vector.default_search_expansion_factor": 3.0,
                "vector.hnsw.m": 16,
                "vector.quantization.type": "BINARY",
                "vector.hnsw.ef_construction": 100,
            }},
        }

    def test_online_index_accepts_cosine_case_variations(self):
        for similarity in ("cosine", "COSINE", "Cosine"):
            with self.subTest(similarity=similarity):
                self.index["options"]["indexConfig"]["vector.similarity_function"] = similarity
                validate_index(self.index, self.settings)

    def test_different_or_missing_similarity_is_still_rejected(self):
        for similarity in ("euclidean", "EUCLIDEAN", "", None, 1):
            with self.subTest(similarity=similarity):
                self.index["options"]["indexConfig"]["vector.similarity_function"] = similarity
                with self.assertRaises(KnowledgeNotReady):
                    validate_index(self.index, self.settings)

    def test_wrong_dimensions_are_still_rejected(self):
        self.index["options"]["indexConfig"]["vector.dimensions"] = 1024
        with self.assertRaises(KnowledgeNotReady):
            validate_index(self.index, self.settings)

    def test_wrong_label_or_vector_property_is_still_rejected(self):
        for field, value in (("labelsOrTypes", ["Product"]), ("properties", ["other_embedding"])):
            index = deepcopy(self.index)
            index[field] = value
            with self.subTest(field=field), self.assertRaises(KnowledgeNotReady):
                validate_index(index, self.settings)

    def test_populating_index_is_only_accepted_before_readiness_check(self):
        self.index["state"] = "POPULATING"
        validate_index(self.index, self.settings, require_online=False)
        with self.assertRaises(KnowledgeNotReady):
            validate_index(self.index, self.settings)


if __name__ == "__main__":
    unittest.main()
