import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import rag


class RefusalSessionTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            retrieval_top_k=4,
            retrieval_candidate_k=12,
            retrieval_min_relevance=0.42,
            retrieval_min_lexical_coverage=0.3,
            retrieval_max_lexical_chunks=10_000,
        )
        self.memory = Mock()
        self.patches = [
            patch.object(rag, "get_settings", return_value=self.settings),
            patch.object(rag, "current_date_iso", return_value="2026-10-09"),
            patch.object(rag, "get_vectorstore", return_value=Mock()),
            patch.object(rag, "retrieve", return_value=[]),
            patch("app.memory.get_memory", return_value=self.memory),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()

    def test_non_streaming_refusal_is_saved_to_the_session(self):
        result = rag.answer("unanswerable question", "session-a", "owner-a")

        self.assertEqual(result["session_id"], "session-a")
        self.assertEqual(
            result["answer"],
            "知识库中未找到足够相关的内容，暂时无法可靠回答。",
        )
        self.memory.append_for_user.assert_called_once_with(
            "owner-a",
            "session-a",
            "unanswerable question",
            result["answer"],
        )

    def test_streaming_refusal_is_saved_to_the_session(self):
        events = list(rag.answer_stream("unanswerable question", "session-b", "owner-b"))

        self.assertEqual(
            events,
            [
                {"type": "sources", "sources": []},
                {
                    "type": "token",
                    "text": "知识库中未找到足够相关的内容，暂时无法可靠回答。",
                },
            ],
        )
        self.memory.append_for_user.assert_called_once_with(
            "owner-b",
            "session-b",
            "unanswerable question",
            events[1]["text"],
        )


if __name__ == "__main__":
    unittest.main()
