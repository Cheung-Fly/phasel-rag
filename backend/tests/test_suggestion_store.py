"""suggestion_store 的单元测试。

只测不依赖外部服务（PG / LLM）的纯逻辑：
  - _parse_suggestions 对 LLM 输出的各种脏格式
  - _overlap_ratio 去重近似的边界
  - SuggestionStore 在 PG 不可用时的安全降级

依赖 PG/LLM 的生成/采纳/忽略在宿主机验收脚本里走真实 HTTP 验证。
"""
import unittest

from app.suggestion_store import SUGGESTION_STATUSES, _overlap_ratio, _parse_suggestions


class ParseSuggestionsTests(unittest.TestCase):
    def test_plain_json_array(self):
        raw = '[{"suggestion": "帮你整理复习计划", "reason": "在备考", "kind": "study"}]'
        s = _parse_suggestions(raw)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["suggestion"], "帮你整理复习计划")
        self.assertEqual(s[0]["kind"], "study")

    def test_markdown_fenced(self):
        raw = '```json\n[{"suggestion": "x", "kind": "job"}]\n```'
        s = _parse_suggestions(raw)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["kind"], "job")

    def test_empty_array(self):
        self.assertEqual(_parse_suggestions("[]"), [])

    def test_non_json(self):
        self.assertEqual(_parse_suggestions("没有可建议的"), [])

    def test_empty(self):
        self.assertEqual(_parse_suggestions(""), [])
        self.assertEqual(_parse_suggestions(None), [])

    def test_filters_without_suggestion(self):
        raw = '[{"reason": "x"}, {"suggestion": ""}]'
        self.assertEqual(_parse_suggestions(raw), [])

    def test_not_a_list(self):
        self.assertEqual(_parse_suggestions('{"suggestion": "x"}'), [])

    def test_statuses_constant(self):
        self.assertEqual(
            set(SUGGESTION_STATUSES), {"pending", "accepted", "dismissed", "expired"}
        )


class OverlapRatioTests(unittest.TestCase):
    def test_identical_is_one(self):
        self.assertAlmostEqual(_overlap_ratio("整理本周复习计划", "整理本周复习计划"), 1.0)

    def test_completely_different_is_zero(self):
        self.assertLess(_overlap_ratio("整理复习计划", "帮我查一下天气"), 0.5)

    def test_similar_is_high(self):
        # 「帮你整理本周复习计划」与「整理本周的复习计划」应判为同类
        self.assertGreaterEqual(
            _overlap_ratio("帮你整理本周复习计划", "整理本周的复习计划"), 0.5
        )

    def test_empty_returns_zero(self):
        self.assertEqual(_overlap_ratio("", "x"), 0.0)
        self.assertEqual(_overlap_ratio("x", ""), 0.0)
        self.assertEqual(_overlap_ratio("", ""), 0.0)


class SuggestionStoreDegradedTests(unittest.TestCase):
    def test_unavailable_store_degrades(self):
        from app.suggestion_store import SuggestionStore

        store = SuggestionStore.__new__(SuggestionStore)
        store._available = False
        store.s = None

        self.assertFalse(store.available)
        self.assertEqual(store.generate("u"), [])
        self.assertEqual(store.list_pending("u"), [])
        self.assertIsNone(store.get("u", "id"))
        self.assertIsNone(store.accept("u", "id"))
        self.assertIsNone(store.dismiss("u", "id"))


if __name__ == "__main__":
    unittest.main()
