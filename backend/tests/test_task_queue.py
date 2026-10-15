"""后台任务队列的测试（阶段 B）。

覆盖的是**语义边界**而不是实现细节 —— 具体说，是那些一旦写错就会
「静默做错事」的地方（重复入库、任务凭空消失、取消不生效、跳过入库直接
把任务标成功），而不是「某个函数有没有被调用」。

四组测试：
  TaskQueueStoreTests   队列本身（领取 / 进度 / 终结 / 租约 / 重试上限）
  WorkerExecutionTests  worker 对「用户取消 / worker 停机 / 执行失败」
                        三种停止的区分处理（这三者该有的后续动作完全不同）
  UploadEndpointTests   上传接口的对外契约（202 = 已接受入队，以及降级路径）
  IngestProgressTests   入库的进度回调与协作式取消（向量库换成假的，不依赖网络）

前三组需要 PostgreSQL；最后一组不需要。
"""
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import psycopg
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import auth_store, config, main, rag, task_store, worker
from app.auth_store import AuthStore
from app.task_store import TaskCancelled, TaskStore


def _test_settings(**overrides):
    """测试用配置：独立加密密钥 + 可覆盖的任意字段。"""
    base = config.get_settings().model_copy(
        update={"credential_encryption_key": Fernet.generate_key().decode("ascii")}
    )
    return base.model_copy(update=overrides) if overrides else base


class _PQGCase(unittest.TestCase):
    """共享的 PG 前置：确认队列空闲 → 建两个测试用户 → 注入 store 单例。"""

    def setUp(self):
        # ★ 这两项检查必须在建立任何测试资源【之前】做：skipTest 抛出异常时
        #   unittest 不会调用 tearDown，之后再建的连接池与 patch 就没人回收了。
        #
        # 为什么要跟真实后台执行器互斥：worker 的轮询间隔只有 2 秒，它会领走
        # 测试刚入队的任务并真的去跑入库（产生真实 embedding 调用），
        # 断言于是变成随机结果 —— 那种「有时过有时不过」的测试比没有更糟。
        # 跑本套件前请先 `docker compose stop worker`；忘了的话这里会明确跳过。
        live_workers = self._live_worker_ids()
        if live_workers:
            self.skipTest(
                f"检测到正在运行的后台执行器 {live_workers}，测试无法独占队列"
                "（请先 docker compose stop worker）"
            )
        if self._queue_has_active_tasks():
            self.skipTest("任务队列里有真实任务（queued/running），测试领取会与之竞争")

        self.suffix = uuid.uuid4().hex[:12]
        self.username_a = f"task{self.suffix}a"
        self.username_b = f"task{self.suffix}b"
        self.password_a = "synthetic-task-password-a"
        self.password_b = "synthetic-task-password-b"
        self.settings = _test_settings(auth_mode="session")
        self._patches = [
            patch.object(auth_store, "get_settings", return_value=self.settings),
            patch.object(main, "get_settings", return_value=self.settings),
            patch.object(task_store, "get_settings", return_value=self.settings),
            patch.object(worker, "get_settings", return_value=self.settings),
        ]
        for item in self._patches:
            item.start()
        self.auth = AuthStore()
        auth_store._store = self.auth
        self.auth.create_user(self.username_a, self.password_a)
        self.auth.create_user(self.username_b, self.password_b)
        self.store = TaskStore()
        task_store._store = self.store
        self.owner_a = self.auth.authenticate(self.username_a, self.password_a, "test")
        self.owner_b = self.auth.authenticate(self.username_b, self.password_b, "test")
        self.assertIsNotNone(self.owner_a)
        self.assertIsNotNone(self.owner_b)

    @staticmethod
    def _live_worker_ids() -> list[str]:
        """最近 10 秒内有过心跳的 worker。

        窗口取 10 秒而不是租约时长（60 秒）：worker 空闲时每 2 秒、忙碌时至多
        每 5 秒心跳一次，10 秒足够识别活着的；窗口再大一点，就会让「刚停掉
        worker 就跑测试」被误判为仍在运行，白跳过一轮（停止后需要等窗口过去
        才不跳过，所以窗口越小越好用）。
        """
        dsn = config.get_settings().postgres_dsn
        if not dsn.strip():
            return []
        try:
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                rows = connection.execute(
                    "SELECT worker_id FROM task_worker_heartbeats "
                    "WHERE last_seen_at > now() - interval '10 seconds'"
                ).fetchall()
            return [row[0] for row in rows]
        except Exception:
            return []

    @staticmethod
    def _queue_has_active_tasks() -> bool:
        dsn = config.get_settings().postgres_dsn
        if not dsn.strip():
            return False
        try:
            with psycopg.connect(dsn, connect_timeout=5) as connection:
                row = connection.execute(
                    "SELECT count(*) FROM agent_tasks WHERE status IN ('queued', 'running')"
                ).fetchone()
            return bool(row and row[0])
        except Exception:
            # 表还不存在（从未初始化过）→ 视为空闲
            return False

    def delete_test_users(self):
        """删用户会级联删掉 agent_tasks（外键 ON DELETE CASCADE）。

        ★ 必须在关闭 TestClient 之前调用 —— TestClient 退出会触发应用 shutdown，
        把连接池一并关掉，之后 self.store._pool 就用不了了。
        """
        with self.store._pool.connection() as connection:
            connection.execute(
                "DELETE FROM app_users WHERE username = ANY(%s)",
                ([self.username_a, self.username_b],),
            )

    def teardown_stores_and_patches(self):
        task_store.close_task_store()
        auth_store.close_auth_store()
        for item in reversed(self._patches):
            item.stop()

    def tearDown(self):
        self.delete_test_users()
        self.teardown_stores_and_patches()


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class TaskQueueStoreTests(_PQGCase):
    def test_enqueue_claim_progress_and_complete(self):
        task = self.store.enqueue(
            "document_ingest", self.owner_a, {"doc_id": "d1", "filename": "a.txt"}
        )
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["attempts"], 0)

        claimed = self.store.claim_next("w1", 60, 3)
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["attempts"], 1)

        self.store.update_progress(task["task_id"], "w1", "向量化", 30, 120)
        mid = self.store.get(task["task_id"], self.owner_a)
        self.assertEqual(
            (mid["stage"], mid["progress_current"], mid["progress_total"]),
            ("向量化", 30, 120),
        )

        self.assertTrue(self.store.complete(task["task_id"], "w1", {"chunks": 120}))
        done = self.store.get(task["task_id"], self.owner_a)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"], {"chunks": 120})
        # 进度被推到满格：前端不该留下一个「成功但停在 30%」的进度条
        self.assertEqual(done["progress_current"], done["progress_total"])
        self.assertIsNotNone(done["finished_at"])

        # 终态之后不能再被领取
        self.assertIsNone(self.store.claim_next("w2", 60, 3))

    def test_tasks_are_scoped_to_their_owner(self):
        task = self.store.enqueue("document_ingest", self.owner_a, {"filename": "a.txt"})
        self.assertIsNone(self.store.get(task["task_id"], self.owner_b))
        self.assertEqual(self.store.list_for_owner(self.owner_b), [])
        self.assertEqual(len(self.store.list_for_owner(self.owner_a)), 1)
        # 别人的任务既不能取消，也不该因为「取消失败」而暴露它存在
        self.assertIsNone(self.store.cancel(task["task_id"], self.owner_b))

    def test_cancelling_a_queued_task_terminates_it_immediately(self):
        task = self.store.enqueue("document_ingest", self.owner_a, {"filename": "a.txt"})
        cancelled = self.store.cancel(task["task_id"], self.owner_a)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["stage"], "已取消")
        self.assertIsNotNone(cancelled["finished_at"])
        # 关键：已取消的任务永远不会被领取（否则就成了「点了取消还在跑」）
        self.assertIsNone(self.store.claim_next("w1", 60, 3))
        # 重复取消是幂等的：用户点两次不该得到第二种结果
        again = self.store.cancel(task["task_id"], self.owner_a)
        self.assertEqual(again["status"], "cancelled")

    def test_cancelling_a_running_task_is_cooperative(self):
        task = self.store.enqueue("document_ingest", self.owner_a, {"filename": "a.txt"})
        self.store.claim_next("w1", 60, 3)
        after = self.store.cancel(task["task_id"], self.owner_a)
        # 运行中的任务无法被抢占式打断，只能置标记、由 worker 在检查点停下 ——
        # 所以这里 status 必须【仍是 running】。若写成 cancelled，前端会显示
        # 「已取消」而向量库其实还在被写。
        self.assertEqual(after["status"], "running")
        self.assertTrue(after["cancel_requested"])
        # worker 的下一次心跳就该读到这个请求
        self.assertTrue(self.store.heartbeat(task["task_id"], "w1", 60))

    def test_only_an_expired_lease_can_be_reclaimed(self):
        task = self.store.enqueue("document_ingest", self.owner_a, {"filename": "a.txt"})
        self.store.claim_next("w1", 60, 3)
        # 租约在手时别的 worker 领不到 —— 这是「不重复执行」的核心保证
        self.assertIsNone(self.store.claim_next("w2", 60, 3))

        with self.store._pool.connection() as connection:
            connection.execute(
                "UPDATE agent_tasks SET lease_expires_at = now() - interval '1 second' "
                "WHERE task_id = %s",
                (task["task_id"],),
            )
        # 租约过期（上一个执行者已消失）才允许重新领取，且 attempts 递增
        reclaimed = self.store.claim_next("w2", 60, 3)
        self.assertEqual(reclaimed["task_id"], task["task_id"])
        self.assertEqual(reclaimed["attempts"], 2)
        self.assertEqual(reclaimed["status"], "running")

    def test_heartbeat_reports_lost_lease_as_none(self):
        task = self.store.enqueue("document_ingest", self.owner_a, {"filename": "a.txt"})
        self.store.claim_next("w1", 60, 3)
        self.assertIsNotNone(self.store.heartbeat(task["task_id"], "w1", 60))
        # 租约已易主：必须明确告诉调用方「你不持有它了」。返回 True/False
        # 都不行 —— 旧执行者会把结果写到一个已经归别人的任务上。
        self.assertIsNone(self.store.heartbeat(task["task_id"], "w2", 60))

    def test_repeatedly_interrupted_task_fails_instead_of_looping_forever(self):
        task = self.store.enqueue("document_ingest", self.owner_a, {"filename": "a.txt"})
        for attempt in range(3):
            claimed = self.store.claim_next(f"w{attempt}", 60, 3)
            self.assertIsNotNone(claimed, f"第 {attempt + 1} 次领取应当成功")
            self.assertEqual(claimed["attempts"], attempt + 1)
            with self.store._pool.connection() as connection:
                connection.execute(
                    "UPDATE agent_tasks SET lease_expires_at = now() - interval '1 second' "
                    "WHERE task_id = %s",
                    (task["task_id"],),
                )
        # 达到上限后：既不再可领取，也会被扫描判为失败。
        # 没有这一步，一个「每次都在同一处打崩 worker」的任务就是无限重启循环。
        dead = self.store.fail_exhausted(3)
        self.assertEqual(len(dead), 1)
        # 返回值必须带上任务本身，而不只是个计数：调用方要据此清理源文件。
        # 这条路径同样绕过了 worker 的执行流程，只给计数的话文件必然泄漏。
        self.assertEqual(dead[0]["task_id"], task["task_id"])
        self.assertEqual(dead[0]["payload"]["filename"], "a.txt")
        self.assertIsNone(self.store.claim_next("w9", 60, 3))
        final = self.store.get(task["task_id"], self.owner_a)
        self.assertEqual(final["status"], "failed")
        self.assertIn("反复中断", final["error"])

    def test_release_returns_task_to_queue_without_burning_an_attempt(self):
        task = self.store.enqueue("document_ingest", self.owner_a, {"filename": "a.txt"})
        self.store.claim_next("w1", 60, 3)
        self.store.update_progress(task["task_id"], "w1", "向量化", 10, 100)
        self.assertTrue(self.store.release(task["task_id"], "w1", "worker 重启，等待重新执行"))

        released = self.store.get(task["task_id"], self.owner_a)
        self.assertEqual(released["status"], "queued")
        # attempts 回退：优雅停机不是任务的错，不该消耗它的重试预算
        self.assertEqual(released["attempts"], 0)
        self.assertEqual(released["progress_current"], 0)
        self.assertIsNone(released["lease_expires_at"])

    def test_worker_heartbeat_marks_worker_online(self):
        # 不断言「表里只有我这一条」—— heartbeat 表是跨进程共享的运行态，
        # 真实 worker 随时可能在写。判断的应该是「我这一条有没有被算进来」。
        probe = f"probe-{uuid.uuid4().hex[:8]}"
        before = {w["worker_id"] for w in self.store.active_workers(60)}
        try:
            self.store.worker_heartbeat(probe)
            after = self.store.active_workers(60)
            self.assertIn(probe, {w["worker_id"] for w in after})
            # 既有记录不该因为我的写入而消失
            self.assertLessEqual(before, {w["worker_id"] for w in after})
            mine = next(w for w in after if w["worker_id"] == probe)
            self.assertLess(mine["seconds_ago"], 5)
            # 窗口为 0 秒时任何心跳都已过期（这正是「租约过期」的判据）
            self.assertEqual(self.store.active_workers(0), [])
        finally:
            # 不留痕：probe 行与本用户的清理无关，得自己删
            with self.store._pool.connection() as connection:
                connection.execute(
                    "DELETE FROM task_worker_heartbeats WHERE worker_id = %s", (probe,)
                )


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class WorkerExecutionTests(_PQGCase):
    """worker 对三种停止的区分处理。

    这三条分支最容易写错，且错了以后最难发现：
      · 把「停机」当成「取消」→ 任务凭空消失（用户以为在上传，其实什么都没发生）
      · 把「取消」当成「停机」→ 用户点了取消，任务却一直在队列里循环重跑
      · 把「失去租约」当成正常完成 → 两个 worker 互相覆盖结果
    """

    TASK_TYPE = "_test_executor"

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rag-task-test-"))
        self.source = self.tmpdir / "source.txt"
        self.source.write_text("synthetic source", encoding="utf-8")
        self.runner = worker.Worker()
        # 缩短心跳/租约相关参数，避免测试等待
        self.runner.settings = self.settings.model_copy(
            update={
                "task_lease_seconds": 10,
                "task_max_attempts": 3,
                "task_worker_poll_seconds": 0.05,
            }
        )

    def tearDown(self):
        worker.EXECUTORS.pop(self.TASK_TYPE, None)
        for leftover in self.tmpdir.glob("*"):
            leftover.unlink(missing_ok=True)
        self.tmpdir.rmdir()
        super().tearDown()

    def _enqueue(self, executor):
        worker.EXECUTORS[self.TASK_TYPE] = executor
        return self.store.enqueue(
            self.TASK_TYPE,
            self.owner_a,
            {"stored_path": str(self.source), "doc_id": "doc-x", "filename": "source.txt"},
        )

    def _run_one(self):
        self.assertTrue(self.runner.run_once(), "应当领取到一个任务")

    def test_successful_task_records_result_and_keeps_source_file(self):
        def executor(task, ctx):
            ctx.progress("向量化", 5, 5)
            return {"chunks": 5}

        task = self._enqueue(executor)
        self._run_one()
        done = self.store.get(task["task_id"], self.owner_a)
        self.assertEqual(done["status"], "succeeded")
        self.assertEqual(done["result"], {"chunks": 5})
        self.assertEqual(done["progress_current"], 5)
        # 成功时源文件要留着：delete_document() 会连它一起删。
        # 提前删掉会让「删文档」少删一样东西，留下一个不可见的残留。
        self.assertTrue(self.source.exists())

    def test_user_cancellation_terminates_and_cleans_up(self):
        def executor(task, ctx):
            ctx.request_cancel()  # 模拟心跳线程读到 cancel_requested
            raise TaskCancelled("stop")

        task = self._enqueue(executor)
        self._run_one()
        done = self.store.get(task["task_id"], self.owner_a)
        self.assertEqual(done["status"], "cancelled")
        self.assertFalse(self.source.exists(), "取消是终态，源文件应当被清理")

    def test_worker_shutdown_requeues_and_preserves_the_source_file(self):
        def executor(task, ctx):
            ctx.request_shutdown()  # 模拟 SIGTERM
            raise TaskCancelled("stop")

        task = self._enqueue(executor)
        self._run_one()
        back = self.store.get(task["task_id"], self.owner_a)
        # 停机既不是失败也不是取消：任务要回到队列，重启后接着跑
        self.assertEqual(back["status"], "queued")
        self.assertEqual(back["attempts"], 0)
        self.assertTrue(self.source.exists(), "停机重排必须保留源文件，否则重跑无从谈起")

    def test_failure_marks_task_failed_and_cleans_up(self):
        def executor(task, ctx):
            raise RuntimeError("synthetic embedding outage")

        task = self._enqueue(executor)
        self._run_one()
        done = self.store.get(task["task_id"], self.owner_a)
        self.assertEqual(done["status"], "failed")
        self.assertIn("synthetic embedding outage", done["error"])
        self.assertFalse(self.source.exists())


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class UploadEndpointTests(_PQGCase):
    """上传接口的对外契约：立即返回 task_id，入库交给后台。"""

    def setUp(self):
        super().setUp()
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rag-upload-test-"))
        # 上传目录换成临时目录：测试不该往真实的 uploads/ 里塞文件
        self.upload_settings = self.settings.model_copy(
            update={
                "auth_mode": "local",
                "upload_dir": str(self.tmpdir),
                "local_owner_username": self.username_a,
            }
        )
        self._extra = [
            patch.object(main, "get_settings", return_value=self.upload_settings),
            patch.object(task_store, "get_settings", return_value=self.upload_settings),
            patch.object(rag, "get_settings", return_value=self.upload_settings),
        ]
        for item in self._extra:
            item.start()
        main._local_owner_cache = None
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        main._local_owner_cache = None
        # 顺序不能改：先清数据，再关 TestClient（退出会触发 shutdown 关掉连接池）
        self.delete_test_users()
        for item in reversed(self._extra):
            item.stop()
        for leftover in self.tmpdir.glob("*"):
            leftover.unlink(missing_ok=True)
        self.tmpdir.rmdir()
        self.client.__exit__(None, None, None)
        self.teardown_stores_and_patches()

    def test_upload_enqueues_and_returns_a_task_id_immediately(self):
        response = self.client.post(
            "/api/documents/upload",
            files={"file": ("notes.txt", b"synthetic knowledge")},
        )
        # 202 = 已接受、尚未处理完。用 200 会让调用方以为「入库已经完成」
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body["mode"], "queued")
        self.assertEqual(body["status"], "queued")
        self.assertEqual(body["filename"], "notes.txt")
        self.assertTrue(body["task_id"])
        # 内部实现细节不外泄
        self.assertNotIn("stored_path", body["payload"])
        self.assertNotIn("locked_by", body)

        listing = self.client.get("/api/tasks").json()["tasks"]
        self.assertEqual([t["task_id"] for t in listing], [body["task_id"]])
        detail = self.client.get(f"/api/tasks/{body['task_id']}")
        self.assertEqual(detail.status_code, 200)
        # 任务状态是「此刻的进度」，被缓存下来就是一个永远不动的进度条
        self.assertEqual(detail.headers.get("cache-control"), "no-store")

        cancelled = self.client.post(f"/api/tasks/{body['task_id']}/cancel")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(self.client.get("/api/tasks/does-not-exist").status_code, 404)

    def test_upload_rejects_an_unsupported_type_before_queueing(self):
        response = self.client.post(
            "/api/documents/upload",
            files={"file": ("malware.exe", b"MZ synthetic")},
        )
        # 在入队前就拒掉：用户立刻拿到原因，而不是等任务跑起来才失败
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/api/tasks").json()["tasks"], [])

    def test_cancelling_a_queued_upload_removes_the_staged_file(self):
        """排队中取消必须把刚落盘的源文件删掉。

        ★ 回归用例（2026-10-14 端到端验收实测的泄漏）：
        排队中取消不经过 worker —— 任务在 queued 状态就被直接写成终态，
        worker 的 claim_next 永远不会领到它，于是清理代码根本没机会执行。
        实测在 uploads/ 遗留了 250,988 字节的孤儿文件；更麻烦的是它没有
        向量片段、不出现在文档列表里，用户连手动删除的入口都没有。
        断言刻意落在「磁盘上还剩什么」而不是「接口返回了什么」：
        接口返回 cancelled 一直是对的，泄漏发生在接口之外。
        """
        response = self.client.post(
            "/api/documents/upload",
            files={"file": ("queued-cancel.txt", b"synthetic knowledge")},
        )
        task_id = response.json()["task_id"]
        self.assertEqual(len(list(self.tmpdir.glob("*.txt"))), 1, "上传后应有一份落盘源文件")

        cancelled = self.client.post(f"/api/tasks/{task_id}/cancel")
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual(list(self.tmpdir.glob("*")), [], "排队中取消后不该留下孤儿文件")

    def test_upload_falls_back_to_synchronous_ingest_without_the_queue(self):
        with patch.object(main, "get_task_store_or_none", return_value=None), patch.object(
            rag,
            "ingest",
            return_value={
                "doc_id": "sync-doc",
                "filename": "notes.txt",
                "chunks": 3,
                "chars": 20,
            },
        ) as ingest:
            response = self.client.post(
                "/api/documents/upload",
                files={"file": ("notes.txt", b"synthetic knowledge")},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "sync")
        self.assertEqual(response.json()["chunks"], 3)
        # 降级路径必须真的调用同步入库，而不是假装成功
        self.assertEqual(ingest.call_args.args[1], "notes.txt")


class _FakeVectorStore:
    """最小的向量库替身：只记录写入与删除，不做任何真实嵌入。"""

    def __init__(self):
        self.added: list[str] = []
        self.deleted: list[str] = []

    def add_documents(self, documents, ids=None):
        self.added.extend(ids or [])

    def get(self, where=None, include=None):
        # 回滚逻辑用它查出「已写入的片段」
        return {"ids": [i for i in self.added if i not in self.deleted]}

    def delete(self, ids=None):
        self.deleted.extend(ids or [])

    def remaining(self):
        return [i for i in self.added if i not in self.deleted]


class IngestProgressTests(unittest.TestCase):
    """入库的进度回调与协作式取消。

    向量库换成假的：这两个能力的正确与否，与「embedding 是否真的算对了」无关，
    而真调一次云端接口会把这条用例变成网络依赖（CI 里必然失败）。
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp(prefix="rag-ingest-test-"))
        self.settings = config.get_settings().model_copy(
            update={
                "upload_dir": str(self.tmpdir),
                "embedding_batch_size": 1,  # 每片一批，好让「检查点之间」有多个批次
                "ingest_max_concurrent": 1,
            }
        )
        self.fake = _FakeVectorStore()
        self.patches = [
            patch.object(rag, "get_settings", return_value=self.settings),
            patch.object(rag, "get_vectorstore", return_value=self.fake),
            patch.object(rag, "invalidate_corpus_cache", return_value=None),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        for leftover in self.tmpdir.glob("*"):
            leftover.unlink(missing_ok=True)
        self.tmpdir.rmdir()

    @staticmethod
    def _text(paragraphs: int) -> bytes:
        # 每段远大于 chunk_size(800)，保证被切成多片
        return ("\n\n".join("段落" + "内容" * 500 for _ in range(paragraphs))).encode("utf-8")

    def test_progress_is_reported_per_stage_with_a_monotonic_counter(self):
        reports = []
        rag.ingest(
            self._text(3),
            "notes.txt",
            "owner",
            on_progress=lambda stage, current, total: reports.append(
                (stage, current, total)
            ),
        )
        stages = [stage for stage, _, _ in reports]
        self.assertIn("解析文件", stages)
        self.assertIn("切片中", stages)
        self.assertEqual(stages[-1], "完成")
        counts = [(current, total) for stage, current, total in reports if stage == "向量化"]
        self.assertTrue(counts, "向量化阶段必须上报进度")
        # 进度必须单调递增到满格，否则前端的进度条会来回跳
        self.assertEqual([c for c, _ in counts], sorted(c for c, _ in counts))
        self.assertEqual(counts[-1][0], counts[-1][1])
        self.assertGreater(counts[-1][1], 1)

    def test_cancellation_stops_at_a_checkpoint_and_rolls_back_partial_chunks(self):
        calls = {"n": 0}

        def should_cancel() -> bool:
            calls["n"] += 1
            return calls["n"] > 2  # 放行前两个检查点，让第一批写进去

        with self.assertRaises(TaskCancelled):
            rag.ingest(self._text(4), "notes.txt", "owner", should_cancel=should_cancel)
        self.assertEqual(len(self.fake.added), 1, "取消只应发生在检查点之间")
        self.assertEqual(self.fake.deleted, self.fake.added)
        # 半份文档留在库里比没有更糟：它会以「来源」的身份出现在回答里
        self.assertEqual(self.fake.remaining(), [])

    def test_synchronous_ingest_removes_the_source_file_on_failure(self):
        rag.ingest(self._text(1), "ok.txt", "owner")
        self.assertEqual(len(list(self.tmpdir.glob("*.txt"))), 1)

        with self.assertRaises(TaskCancelled):
            rag.ingest(self._text(1), "bad.txt", "owner", should_cancel=lambda: True)
        # 同步路径的约定：失败即删，避免 uploads/ 里堆积孤儿文件
        self.assertEqual(len(list(self.tmpdir.glob("*.txt"))), 1)

    def test_discarding_a_staged_file_is_tolerant_and_idempotent(self):
        """删源文件的工具函数要容忍空值、文件不存在、以及重复调用。

        它有四个调用点（worker 执行路径、排队中取消、入队失败回滚、同步入库失败），
        任何一处拿到空路径或重复调用都不该抛异常 —— 那会把一个「清理动作」
        变成新的失败源。重试与幂等取消在真实使用里都会重复走到它。
        """
        self.assertFalse(rag.discard_stored_file(None))
        self.assertFalse(rag.discard_stored_file(""))
        self.assertFalse(rag.discard_stored_file(str(self.tmpdir / "never-existed.txt")))

        target = self.tmpdir / "staged.txt"
        target.write_bytes(b"staged")
        self.assertTrue(rag.discard_stored_file(str(target)))
        self.assertFalse(target.exists())
        # 幂等：文件已经没了，再删一次应返回 False 而不是报错
        self.assertFalse(rag.discard_stored_file(str(target)))
        # 也接受 Path 而不是只接受字符串（两个调用点各有各的写法）
        as_path = self.tmpdir / "as-path.txt"
        as_path.write_bytes(b"staged")
        self.assertTrue(rag.discard_stored_file(as_path))
        self.assertFalse(as_path.exists())


if __name__ == "__main__":
    unittest.main()
