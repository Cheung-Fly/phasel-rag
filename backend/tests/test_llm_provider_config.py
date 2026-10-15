"""供应商凭证解耦的配置回归测试（2026-10-15）。

覆盖三类「改错了不会当场报错、只会在使用时报一个离配置很远的错误」的行为：

  1. 回退链：新变量 LLM_* / EMBEDDING_* 优先，旧变量 DASHSCOPE_* 仍可用；
  2. 解耦：chat 与 embedding 可以指向不同供应商（本次改造的核心目的）；
  3. 失败方向：全空时字段保持空串，工厂函数抛出指名道姓的 RuntimeError ——
     而不是拿一个「看起来能用」的地址去发请求，最后在对话里报个看不懂的 401。

全部离线：不触网、不读 .env 文件（显式 _env_file=None），只验证配置解析。
真实连通性由 deploy.sh 末尾的探针负责，两者分工不重叠。
"""
import os
import unittest
from unittest.mock import patch

from app import llm
from app.config import Settings

# 与本测试相关的环境变量。构造 Settings 前会把它们全部清掉，
# 否则开发机上真实存在的 .env / 已导出的变量会污染断言 —— 这是最难查的一类假绿。
_MANAGED = (
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "EMBEDDING_API_KEY",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "EMBEDDING_BATCH_SIZE",
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
)

# 旧版 config.py 曾把百炼地址寫成 dashscope_base_url 的字段默认值，
# 因此「只填了 DASHSCOPE_API_KEY」的旧 .env 依赖它生效。改造后该行为由
# _resolve_provider_credentials() 显式重建，这里用同一个常量做断言。
_LEGACY_DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _settings(**env: str) -> Settings:
    """在托管变量被清空的干净环境里构造 Settings，且不读 .env 文件。"""
    clean = {k: v for k, v in os.environ.items() if k not in _MANAGED}
    clean.update(env)
    with patch.dict(os.environ, clean, clear=True):
        return Settings(_env_file=None)


class ProviderResolutionTests(unittest.TestCase):
    def test_legacy_key_only_rebuilds_the_old_default_url(self):
        """旧 .env 只填了 DASHSCOPE_API_KEY：必须精确重建「内置百炼地址」的历史行为。"""
        s = _settings(DASHSCOPE_API_KEY="legacy-key")
        self.assertEqual(s.llm_api_key, "legacy-key")
        self.assertEqual(s.llm_base_url, _LEGACY_DASHSCOPE_URL)
        # 向量化未单独配置 ⇒ 跟随 chat
        self.assertEqual(s.embedding_api_key, "legacy-key")
        self.assertEqual(s.embedding_base_url, _LEGACY_DASHSCOPE_URL)

    def test_legacy_pair_is_used_as_fallback(self):
        s = _settings(
            DASHSCOPE_API_KEY="legacy-key",
            DASHSCOPE_BASE_URL="https://legacy.example/v1",
        )
        self.assertEqual(s.llm_api_key, "legacy-key")
        self.assertEqual(s.llm_base_url, "https://legacy.example/v1")

    def test_new_variables_win_over_legacy(self):
        s = _settings(
            LLM_API_KEY="new-key",
            LLM_BASE_URL="https://new.example/v1",
            DASHSCOPE_API_KEY="legacy-key",
            DASHSCOPE_BASE_URL="https://legacy.example/v1",
        )
        self.assertEqual(s.llm_api_key, "new-key")
        self.assertEqual(s.llm_base_url, "https://new.example/v1")

    def test_chat_and_embedding_may_point_to_different_providers(self):
        """解耦的核心断言：两条链路互不干扰（本次改造要证明的那件事）。"""
        s = _settings(
            LLM_API_KEY="chat-key",
            LLM_BASE_URL="https://chat.example/v1",
            EMBEDDING_API_KEY="embed-key",
            EMBEDDING_BASE_URL="https://embed.example/v1",
        )
        self.assertEqual(
            (s.llm_api_key, s.llm_base_url), ("chat-key", "https://chat.example/v1")
        )
        self.assertEqual(
            (s.embedding_api_key, s.embedding_base_url),
            ("embed-key", "https://embed.example/v1"),
        )

    def test_embedding_falls_back_to_chat_when_not_configured(self):
        """只配一家供应商时，EMBEDDING_* 留空应自动沿用 chat 那套。"""
        s = _settings(LLM_API_KEY="k", LLM_BASE_URL="https://chat.example/v1")
        self.assertEqual(s.embedding_api_key, "k")
        self.assertEqual(s.embedding_base_url, "https://chat.example/v1")

    def test_surrounding_whitespace_is_trimmed(self):
        """空格的经典后果是「key 明明对却 401」，必须在解析期就吃掉。"""
        s = _settings(LLM_API_KEY="  spaced-key  ", LLM_BASE_URL=" https://chat.example/v1 ")
        self.assertEqual(s.llm_api_key, "spaced-key")
        self.assertEqual(s.llm_base_url, "https://chat.example/v1")

    def test_nothing_configured_stays_empty(self):
        """不做静默兜底：全空时字段就是空串，由工厂函数显式报错。"""
        s = _settings()
        self.assertEqual(s.llm_api_key, "")
        self.assertEqual(s.llm_base_url, "")
        self.assertEqual(s.embedding_api_key, "")
        self.assertEqual(s.embedding_base_url, "")


class FactoryFailureDirectionTests(unittest.TestCase):
    """失败方向：宁可抛错，也不拿错误端点发请求。"""

    def _with(self, **env):
        return patch.object(llm, "get_settings", return_value=_settings(**env))

    def test_chat_raises_naming_both_variable_names(self):
        with self._with(), self.assertRaises(RuntimeError) as ctx:
            llm.get_chat_model()
        message = str(ctx.exception)
        # 迁移期用户可能只认得旧名，报错必须两个都提，否则会以为 key 丢了。
        self.assertIn("LLM_API_KEY", message)
        self.assertIn("DASHSCOPE_API_KEY", message)

    def test_embeddings_raises_naming_both_variable_names(self):
        with self._with(), self.assertRaises(RuntimeError) as ctx:
            llm.get_embeddings()
        message = str(ctx.exception)
        self.assertIn("EMBEDDING_API_KEY", message)
        self.assertIn("LLM_API_KEY", message)


class CrossProviderMismatchWarningTests(unittest.TestCase):
    """放行 + 标记：端点不同却沿用 chat 的 key，不阻断运行，但必须留下痕迹。"""

    def test_warns_when_embedding_endpoint_differs_but_key_is_inherited(self):
        with self.assertLogs("app.config", level="WARNING") as captured:
            s = _settings(
                LLM_API_KEY="chat-key",
                LLM_BASE_URL="https://chat.example/v1",
                EMBEDDING_BASE_URL="https://embed.example/v1",
            )
        self.assertEqual(s.embedding_api_key, "chat-key")  # 放行
        self.assertTrue(
            any("EMBEDDING_API_KEY" in line for line in captured.output),
            f"未产生预期的告警，实际日志：{captured.output}",
        )


if __name__ == "__main__":
    unittest.main()
