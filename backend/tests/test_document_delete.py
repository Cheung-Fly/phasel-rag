"""delete_document 的回归测试。

这一组用例直接来自 2026-10-15 的真实事故：删除一个 owner 为 "shared" 的文档时，
接口返回 404（匹配到 0 个片段），但 uploads/ 下的源文件被【无条件】删掉了 ——
文档于是变成「向量还在、源文件没了」的死状态：既删不掉，也无法重新入库。

所以下面每个用例都同时在两层断言，缺一层就抓不到这类 bug：
  ① 返回值 —— 删了几个片段（接口语义）
  ② 磁盘副作用 —— 源文件还在不在（真正闯祸的那一层）
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import rag


class _FakeVectorStore:
    """只实现 delete_document 会用到的那两个方法，并在本地求值 where 条件。"""

    def __init__(self, chunks):
        # chunks: list[tuple[str, dict]] —— (chunk_id, metadata)
        self._chunks = list(chunks)
        self.delete_calls = []

    @staticmethod
    def _match(meta, where):
        for key, value in (where or {}).items():
            if key.startswith("$"):
                raise AssertionError(f"测试替身未实现的算子: {key}")
            if (meta or {}).get(key) != value:
                return False
        return True

    def get(self, where=None, include=None):
        ids, metadatas = [], []
        for chunk_id, meta in self._chunks:
            if self._match(meta, where):
                ids.append(chunk_id)
                metadatas.append(meta)
        return {"ids": ids, "metadatas": metadatas}

    def delete(self, ids):
        self.delete_calls.append(list(ids))
        doomed = set(ids)
        self._chunks = [c for c in self._chunks if c[0] not in doomed]

    def count(self):
        return len(self._chunks)


class DeleteDocumentTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.upload_dir = Path(self._tmp.name)
        self.settings = SimpleNamespace(upload_dir=str(self.upload_dir))

    def tearDown(self):
        self._tmp.cleanup()

    def _write_source(self, doc_id, suffix=".md", text="demo"):
        path = self.upload_dir / f"{doc_id}{suffix}"
        path.write_text(text, encoding="utf-8")
        return path

    def _run(self, store, doc_id, owner, *, allow_shared=False):
        with patch.object(rag, "get_settings", return_value=self.settings), patch.object(
            rag, "get_vectorstore", return_value=store
        ), patch.object(rag, "invalidate_corpus_cache"):
            return rag.delete_document(doc_id, owner, allow_shared=allow_shared)

    # ---------------- 缺陷一：删不掉时绝不能动源文件 ----------------

    def test_shared_doc_without_permission_keeps_vectors_and_source(self):
        """owner 是 shared、请求者不是它 → 返回 0，且【源文件必须还在】。

        这就是事故本身：旧实现在这条路径上把源文件删了。
        """
        doc_id = "doc-shared"
        source = self._write_source(doc_id)
        store = _FakeVectorStore(
            [
                ("c1", {"doc_id": doc_id, "owner_user_id": "shared"}),
                ("c2", {"doc_id": doc_id, "owner_user_id": "shared"}),
            ]
        )

        deleted = self._run(store, doc_id, "someone-else")

        self.assertEqual(deleted, 0)
        self.assertEqual(store.count(), 2, "没有权限时向量不该被删")
        self.assertTrue(source.exists(), "★ 源文件绝不能被删（事故根因）")

    def test_other_users_document_keeps_source(self):
        """文档归别人所有 → 向量与文件都不动。"""
        doc_id = "doc-other"
        source = self._write_source(doc_id)
        store = _FakeVectorStore(
            [("c1", {"doc_id": doc_id, "owner_user_id": "owner-b"})]
        )

        deleted = self._run(store, doc_id, "owner-a")

        self.assertEqual(deleted, 0)
        self.assertEqual(store.count(), 1)
        self.assertTrue(source.exists())

    # ---------------- 正向：有权限时要删干净 ----------------

    def test_own_document_is_fully_removed(self):
        doc_id = "doc-mine"
        source = self._write_source(doc_id)
        store = _FakeVectorStore(
            [
                ("c1", {"doc_id": doc_id, "owner_user_id": "owner-a"}),
                ("c2", {"doc_id": doc_id, "owner_user_id": "owner-a"}),
                ("c3", {"doc_id": "doc-other", "owner_user_id": "owner-b"}),
            ]
        )

        deleted = self._run(store, doc_id, "owner-a")

        self.assertEqual(deleted, 2)
        self.assertEqual(store.count(), 1, "别的文档不能被连带删除")
        self.assertFalse(source.exists(), "有权限时应连源文件一起删")

    def test_shared_document_is_removable_when_allow_shared(self):
        """本机单人模式：shared 就是主人自己的资料，必须可删（缺陷二）。"""
        doc_id = "doc-shared"
        source = self._write_source(doc_id)
        store = _FakeVectorStore(
            [("c1", {"doc_id": doc_id, "owner_user_id": "shared"})]
        )

        deleted = self._run(store, doc_id, "owner-a", allow_shared=True)

        self.assertEqual(deleted, 1)
        self.assertEqual(store.count(), 0)
        self.assertFalse(source.exists())

    # ---------------- 边界 ----------------

    def test_missing_document_only_cleans_orphan_file(self):
        """doc_id 完全不存在 → 清掉孤儿文件，保持 uploads 与知识库一致。"""
        doc_id = "doc-gone"
        source = self._write_source(doc_id)
        store = _FakeVectorStore([])

        deleted = self._run(store, doc_id, "owner-a")

        self.assertEqual(deleted, 0)
        self.assertFalse(source.exists(), "孤儿文件应被清理")

    def test_partial_ownership_keeps_source(self):
        """只删到一部分片段时【不能】删源文件，否则剩下的片段变成死文档。"""
        doc_id = "doc-mixed"
        source = self._write_source(doc_id)
        store = _FakeVectorStore(
            [
                ("c1", {"doc_id": doc_id, "owner_user_id": "owner-a"}),
                ("c2", {"doc_id": doc_id, "owner_user_id": "someone-else"}),
            ]
        )

        deleted = self._run(store, doc_id, "owner-a")

        self.assertEqual(deleted, 1)
        self.assertEqual(store.count(), 1, "无权限的片段应留下")
        self.assertTrue(source.exists(), "★ 片段未删净时源文件必须保留")

    def test_all_source_files_of_one_doc_are_removed(self):
        """同一 doc_id 因后缀变化留下多份源文件时，应全部清掉。"""
        doc_id = "doc-multi"
        md = self._write_source(doc_id, ".md")
        pdf = self._write_source(doc_id, ".pdf")
        store = _FakeVectorStore(
            [("c1", {"doc_id": doc_id, "owner_user_id": "owner-a"})]
        )

        deleted = self._run(store, doc_id, "owner-a")

        self.assertEqual(deleted, 1)
        self.assertFalse(md.exists())
        self.assertFalse(pdf.exists())

    def test_similar_doc_id_file_is_not_touched(self):
        """反向用例：doc_id 前缀相同的另一份文档，文件不能被误删。"""
        victim = self._write_source("doc-abc2")
        self._write_source("doc-abc")
        store = _FakeVectorStore(
            [("c1", {"doc_id": "doc-abc", "owner_user_id": "owner-a"})]
        )

        deleted = self._run(store, "doc-abc", "owner-a")

        self.assertEqual(deleted, 1)
        self.assertTrue(victim.exists(), "doc-abc.* 的 glob 不能命中 doc-abc2")


if __name__ == "__main__":
    unittest.main()
