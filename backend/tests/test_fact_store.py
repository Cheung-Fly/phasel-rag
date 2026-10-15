"""fact_store 的单元测试。

只测不依赖外部服务（PG / LLM / embedding）的纯逻辑：
  - _parse_facts 对 LLM 输出的各种脏格式（markdown 包裹、夹说明文字、非 JSON、空）
  - FactStore 在 PG 不可用时的安全降级（所有方法返回空/False，不抛异常）

依赖 PG 的检索/写入/遗忘在宿主机验收脚本里走真实 HTTP 验证（见 .verify-d.sh）。
"""
import unittest

from app.fact_store import FACT_TYPES, _parse_facts


class ParseFactsTests(unittest.TestCase):
    def test_plain_json_array(self):
        raw = '[{"fact": "用户喜欢简洁回答", "type": "preference", "confidence": 0.9}]'
        facts = _parse_facts(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["fact"], "用户喜欢简洁回答")
        self.assertEqual(facts[0]["type"], "preference")

    def test_markdown_fenced(self):
        raw = '```json\n[{"fact": "用户主修计算机", "type": "topic", "confidence": 0.8}]\n```'
        facts = _parse_facts(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["type"], "topic")

    def test_wrapped_in_prose(self):
        raw = '好的，以下是抽取结果：[{"fact": "用户每周去图书馆", "type": "habit", "confidence": 0.7}] 以上。'
        facts = _parse_facts(raw)
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["type"], "habit")

    def test_empty_array(self):
        self.assertEqual(_parse_facts("[]"), [])

    def test_non_json(self):
        self.assertEqual(_parse_facts("没有什么值得记住的"), [])

    def test_empty_string(self):
        self.assertEqual(_parse_facts(""), [])
        self.assertEqual(_parse_facts(None), [])

    def test_filters_entries_without_fact(self):
        raw = '[{"fact": "", "type": "habit"}, {"type": "topic"}]'
        self.assertEqual(_parse_facts(raw), [])

    def test_not_a_list(self):
        self.assertEqual(_parse_facts('{"fact": "x"}'), [])

    def test_fact_types_constant(self):
        # 抽取与落库共用同一份取值，避免 LLM 抽出的类型落不进 CHECK 约束
        self.assertEqual(set(FACT_TYPES), {"preference", "habit", "topic"})


class FactStoreDegradedTests(unittest.TestCase):
    """PG 未配置 / pgvector 未装时，FactStore 必须安全降级而非抛异常。"""

    def test_unavailable_store_degrades(self):
        # 直接构造一个「未初始化成功」的 store（绕过 __init__ 里的 PG 连接）
        from app.fact_store import FactStore

        store = FactStore.__new__(FactStore)
        store._available = False
        store.s = None  # 降级路径不应触碰 settings

        self.assertFalse(store.available)
        self.assertEqual(store.search("u", "q"), [])
        self.assertEqual(store.list_facts("u"), [])
        self.assertFalse(store.forget("u", 1))
        self.assertEqual(store.upsert_facts("u", [], "s"), 0)


if __name__ == "__main__":
    unittest.main()
