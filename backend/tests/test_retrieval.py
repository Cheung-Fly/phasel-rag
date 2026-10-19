import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import retrieval
from app.retrieval import hybrid_search, is_confident, retrieve, tokenize


def document(text, chunk_index):
    return SimpleNamespace(
        page_content=text,
        metadata={"doc_id": "doc", "chunk_index": chunk_index, "filename": "校园指南"},
    )


class HybridRetrievalTests(unittest.TestCase):
    def test_tokenizer_keeps_chinese_bigrams_and_numeric_terms(self):
        self.assertEqual(tokenize("成都东软学院 2026"), ["成都", "都东", "东软", "软学", "学院", "2026"])

    def test_lexical_rank_can_promote_exact_evidence_over_dense_rank_one(self):
        semantic_only = document("校园交通与出行安排", 1)
        exact_match = document("学校办学理念：教育创造学生价值", 0)

        hits = hybrid_search(
            "学校教育理念的那句话是什么？",
            [(semantic_only, 0.51), (exact_match, 0.49)],
            [semantic_only, exact_match],
            top_k=2,
            candidate_k=4,
        )

        self.assertEqual(hits[0][0].metadata["chunk_index"], 0)
        self.assertGreaterEqual(hits[0][3], 0.3)

    def test_unanswerable_attribute_has_low_lexical_coverage(self):
        principal = document("现任校长：张应辉；现任党委书记：赖廷谦", 0)

        hits = hybrid_search(
            "学校副校长的手机号码是多少？",
            [(principal, 0.397)],
            [principal],
            top_k=1,
            candidate_k=4,
        )

        self.assertLess(hits[0][3], 0.3)
        self.assertFalse(is_confident(hits[0][1], hits[0][3], 0.42, 0.3))

    def test_strong_lexical_evidence_can_keep_a_paraphrase_below_vector_threshold(self):
        self.assertTrue(is_confident(0.334, 0.43, 0.42, 0.3))
        self.assertFalse(is_confident(0.208, 0.0, 0.42, 0.3))

    def test_rrf_deduplicates_documents_returned_by_both_retrievers(self):
        match = document("校训：精勤博学，学以致用", 0)
        hits = hybrid_search(
            "学校校训是什么？",
            [(match, 0.8)],
            [match],
            top_k=4,
            candidate_k=4,
        )

        self.assertEqual(len(hits), 1)

    def test_retrieval_filters_both_vector_and_lexical_candidates_by_owner(self):
        shared = document("学校校园指南 shared evidence", 0)
        owned_a = document("A用户的私有校园资料 evidence", 1)
        owned_a.metadata["owner_user_id"] = "owner-a"
        owned_b = document("B用户的私有校园资料 evidence", 2)
        owned_b.metadata["owner_user_id"] = "owner-b"
        shared.metadata["owner_user_id"] = "shared"

        class ScopedStore:
            documents = [shared, owned_a, owned_b]

            @staticmethod
            def _matches(document, where):
                allowed = {
                    item["owner_user_id"]
                    for item in where["$or"]
                }
                return document.metadata["owner_user_id"] in allowed

            def similarity_search_with_relevance_scores(self, question, k, filter):
                return [
                    (item, 0.9)
                    for item in self.documents
                    if self._matches(item, filter)
                ][:k]

            def get(self, where, include):
                matches = [
                    item for item in self.documents if self._matches(item, where)
                ]
                result = {"ids": [str(i) for i in range(len(matches))]}
                if "documents" in include:
                    result["documents"] = [item.page_content for item in matches]
                if "metadatas" in include:
                    result["metadatas"] = [item.metadata for item in matches]
                return result

        for owner, private_doc in (("owner-a", owned_a), ("owner-b", owned_b)):
            hits = retrieve(
                ScopedStore(),
                "校园资料 evidence",
                top_k=5,
                candidate_k=5,
                min_relevance=0,
                min_lexical_coverage=0,
                max_lexical_chunks=100,
                owner_user_id=owner,
            )
            returned_owners = {hit[0].metadata["owner_user_id"] for hit in hits}
            self.assertEqual(returned_owners, {"shared", owner})
            self.assertIn(private_doc, [hit[0] for hit in hits])

    def test_cached_corpus_refreshes_when_vector_count_changes(self):
        class Store:
            documents = [
                {"id": "a", "text": "first evidence", "metadata": {"filename": "a.md"}},
            ]

            class Collection:
                owner = None

                def count(self):
                    return len(self.owner.documents)

            def __init__(self):
                self._collection = self.Collection()
                self._collection.owner = self

            def get(self, where, include):
                result = {"ids": [item["id"] for item in self.documents]}
                if "documents" in include:
                    result["documents"] = [item["text"] for item in self.documents]
                if "metadatas" in include:
                    result["metadatas"] = [item["metadata"] for item in self.documents]
                return result

        store = Store()
        retrieval.invalidate_corpus_cache()
        try:
            first = retrieval._load_corpus(store, "owner", 10)
            store.documents.append(
                {"id": "b", "text": "second evidence", "metadata": {"filename": "b.md"}}
            )
            second = retrieval._load_corpus(store, "owner", 10)
            self.assertEqual(len(first.documents), 1)
            self.assertEqual(len(second.documents), 2)
        finally:
            retrieval.invalidate_corpus_cache()

    def test_cached_corpus_refreshes_same_size_changes_after_ttl(self):
        class Store:
            documents = [
                {"id": "a", "text": "old evidence", "metadata": {"filename": "a.md"}},
            ]

            class Collection:
                owner = None

                def count(self):
                    return len(self.owner.documents)

            def __init__(self):
                self._collection = self.Collection()
                self._collection.owner = self

            def get(self, where, include):
                result = {"ids": [item["id"] for item in self.documents]}
                if "documents" in include:
                    result["documents"] = [item["text"] for item in self.documents]
                if "metadatas" in include:
                    result["metadatas"] = [item["metadata"] for item in self.documents]
                return result

        store = Store()
        retrieval.invalidate_corpus_cache()
        try:
            with patch.object(retrieval.time, "monotonic", side_effect=[10.0, 26.0, 26.0]):
                first = retrieval._load_corpus(store, "owner", 10)
                store.documents[0]["text"] = "new evidence"
                second = retrieval._load_corpus(store, "owner", 10)
            self.assertEqual(first.documents[0].page_content, "old evidence")
            self.assertEqual(second.documents[0].page_content, "new evidence")
        finally:
            retrieval.invalidate_corpus_cache()


if __name__ == "__main__":
    unittest.main()
