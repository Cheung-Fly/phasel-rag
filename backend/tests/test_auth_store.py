import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from fastapi.testclient import TestClient

from app import auth_store, config, main, rag, retrieval
from app.auth_store import AuthStore, _hash_password, _verify_password, password_is_valid


class AuthStoreTests(unittest.TestCase):
    def test_application_password_is_scrypt_hashed_and_verified(self):
        encoded = _hash_password("a-long-project-login-password")

        self.assertTrue(encoded.startswith("scrypt$"))
        self.assertTrue(_verify_password("a-long-project-login-password", encoded))
        self.assertFalse(_verify_password("incorrect-project-password", encoded))
        self.assertFalse(_verify_password("a-long-project-login-password", None))

    def test_project_login_password_length_policy(self):
        self.assertFalse(password_is_valid("short"))
        self.assertTrue(password_is_valid("a" * 12))
        self.assertFalse(password_is_valid("a" * 1025))

    def test_vault_fails_closed_without_or_with_invalid_key(self):
        store = AuthStore.__new__(AuthStore)
        store._encryption_key = ""
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            store._fernet()

        store._encryption_key = "not-a-fernet-key"
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            store._fernet()

    def test_vault_accepts_a_generated_fernet_key(self):
        store = AuthStore.__new__(AuthStore)
        store._encryption_key = Fernet.generate_key().decode("ascii")

        self.assertIsInstance(store._fernet(), Fernet)


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class CredentialIsolationIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.suffix = uuid.uuid4().hex[:12]
        self.username_a = f"test{self.suffix}a"
        self.username_b = f"test{self.suffix}b"
        self.password_a = "synthetic-test-project-password-a"
        self.password_b = "synthetic-test-project-password-b"
        # ★ 2026-10-14：本类验证的是「多用户会话隔离」，所以必须把认证模式
        #   显式钉死成 session。否则当部署里 AUTH_MODE=local 时，_current_user
        #   会无视 cookie 直接返回本机主人，下面全部 401 断言都会变成 200 ——
        #   测试红了却分不清是「隔离真的坏了」还是「只是模式变了」。
        test_settings = config.get_settings().model_copy(
            update={
                "credential_encryption_key": Fernet.generate_key().decode("ascii"),
                "auth_mode": "session",
            }
        )
        self.original_auth_store_get_settings = auth_store.get_settings
        self.original_main_get_settings = main.get_settings
        self.auth_store_settings_patch = patch.object(
            auth_store, "get_settings", return_value=test_settings
        )
        # main 侧也要 patch：模式是由 main._auth_mode() 读 get_settings() 决定的，
        # 只 patch auth_store 不足以让接口回到 session 语义。
        self.main_settings_patch = patch.object(
            main, "get_settings", return_value=test_settings
        )
        self.auth_store_settings_patch.start()
        self.main_settings_patch.start()
        self.store = AuthStore()
        auth_store._store = self.store
        self.store.create_user(self.username_a, self.password_a)
        self.store.create_user(self.username_b, self.password_b)
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        # 顺序不能改：先清数据，再关 TestClient（退出时会触发 shutdown，
        # 把 auth store 的连接池一并关掉，之后 self.store._pool 就用不了了）。
        with self.store._pool.connection() as connection:
            connection.execute(
                "DELETE FROM app_users WHERE username = ANY(%s)",
                ([self.username_a, self.username_b],),
            )
        self.client.__exit__(None, None, None)
        auth_store.close_auth_store()
        self.main_settings_patch.stop()
        self.auth_store_settings_patch.stop()
        self.assertIs(auth_store.get_settings, self.original_auth_store_get_settings)
        self.assertIs(main.get_settings, self.original_main_get_settings)

    def test_credentials_are_encrypted_and_scoped_to_the_logged_in_owner(self):
        self.assertEqual(self.client.get("/api/credentials").status_code, 401)
        login_a = self.client.post(
            "/api/auth/login",
            json={"username": self.username_a, "password": self.password_a},
        )
        self.assertEqual(login_a.status_code, 200)
        self.assertIn("httponly", login_a.headers["set-cookie"].lower())
        self.assertIn("samesite=strict", login_a.headers["set-cookie"].lower())

        sample = {
            "service_key": "synthetic-campus-service",
            "label": "temporary-test-record",
            "site_url": "https://study.example.edu/login",
            "full_name": "Synthetic Test Person",
            "student_id": "TEST000001",
            "account": "synthetic-account-001",
            "password": "synthetic-campus-password-001",
        }
        created = self.client.post("/api/credentials", json=sample)
        self.assertEqual(created.status_code, 200)
        credential_id = created.json()["credential_id"]
        invalid_url = {**sample, "site_url": "http://study.example.edu/login"}
        self.assertEqual(
            self.client.post("/api/credentials", json=invalid_url).status_code,
            422,
        )
        listing = self.client.get("/api/credentials").json()["credentials"]
        self.assertEqual(len(listing), 1)
        self.assertTrue(
            {"full_name", "student_id", "account", "password"}.isdisjoint(listing[0])
        )

        with self.store._pool.connection() as connection:
            ciphertexts = connection.execute(
                """
                SELECT site_url_ciphertext, full_name_ciphertext, student_id_ciphertext,
                       account_ciphertext, password_ciphertext
                FROM encrypted_credentials
                WHERE owner_user_id = (
                    SELECT user_id FROM app_users WHERE username = %s
                ) AND credential_id = %s
                """,
                (self.username_a, credential_id),
            ).fetchone()
        self.assertIsNotNone(ciphertexts)
        self.assertEqual(
            [self.store._fernet().decrypt(value.encode()).decode() for value in ciphertexts],
            [
                sample["site_url"],
                sample["full_name"],
                sample["student_id"],
                sample["account"],
                sample["password"],
            ],
        )
        self.assertTrue(set(sample.values()).isdisjoint(ciphertexts))

        # ★ 默认脱敏（2026-10-14）：详情接口不再回传口令明文。
        detail = self.client.get(f"/api/credentials/{credential_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.headers.get("cache-control"), "no-store")
        self.assertEqual(detail.json()["site_url"], sample["site_url"])
        self.assertFalse(detail.json()["password_revealed"])
        self.assertNotEqual(detail.json()["password"], sample["password"])
        # 脱敏要彻底：明文不得出现在响应体的任何位置
        self.assertNotIn(sample["password"], detail.text)

        # 只有显式 reveal 才拿得到明文，且同样禁止缓存
        revealed = self.client.post(f"/api/credentials/{credential_id}/reveal")
        self.assertEqual(revealed.status_code, 200)
        self.assertEqual(revealed.headers.get("cache-control"), "no-store")
        self.assertTrue(revealed.json()["password_revealed"])
        self.assertEqual(revealed.json()["password"], sample["password"])
        self.assertEqual(revealed.json()["site_url"], sample["site_url"])

        # 审计必须能区分「读了脱敏元数据」与「把口令取了出来」
        with self.store._pool.connection() as connection:
            audit_actions = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT action FROM credential_audit
                    WHERE credential_id = %s ORDER BY audit_id
                    """,
                    (credential_id,),
                ).fetchall()
            ]
        self.assertIn("create", audit_actions)
        self.assertIn("read", audit_actions)
        self.assertIn("reveal", audit_actions)

        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)

        login_b = self.client.post(
            "/api/auth/login",
            json={"username": self.username_b, "password": self.password_b},
        )
        self.assertEqual(login_b.status_code, 200)
        self.assertEqual(self.client.get("/api/credentials").json()["credentials"], [])
        self.assertEqual(self.client.get(f"/api/credentials/{credential_id}").status_code, 404)
        # 取明文的接口同样不能越权：别人连「存在与否」都不该探到
        self.assertEqual(
            self.client.post(f"/api/credentials/{credential_id}/reveal").status_code, 404
        )
        self.assertEqual(self.client.put(f"/api/credentials/{credential_id}", json=sample).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/credentials/{credential_id}").status_code, 404)
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)

    def test_documents_retrieval_and_chat_history_are_owner_scoped(self):
        self.assertEqual(self.client.get("/api/documents").status_code, 401)
        self.assertEqual(self.client.get("/api/sessions").status_code, 401)
        self.assertEqual(self.client.post("/api/chat", json={"question": "test"}).status_code, 401)

        self.client.post(
            "/api/auth/login",
            json={"username": self.username_a, "password": self.password_a},
        )
        store = rag.get_vectorstore()._collection
        embedding = [0.0] * config.get_settings().embedding_dim
        doc_a = f"tenant-test-{self.suffix}-a"
        doc_b = f"tenant-test-{self.suffix}-b"
        doc_shared = f"tenant-test-{self.suffix}-shared"
        ids = [f"{doc_a}-0", f"{doc_b}-0", f"{doc_shared}-0"]
        owner_a = self.store.authenticate(self.username_a, self.password_a, "test")
        owner_b = self.store.authenticate(self.username_b, self.password_b, "test")
        self.assertIsNotNone(owner_a)
        self.assertIsNotNone(owner_b)
        # ★ 2026-10-14 阶段 B：上传的契约从「同步入库并返回结果」变为
        #   「落盘 + 入队，立即返回 task_id」。所以这里不再断言 rag.ingest 被调用，
        #   而是断言任务行确实落在 owner_a 名下 —— 隔离语义没有变，只是承载它的
        #   东西从「一次函数调用」变成「一条任务记录」。
        upload_response = self.client.post(
            "/api/documents/upload",
            files={"file": ("synthetic.txt", b"synthetic upload")},
        )
        self.assertEqual(upload_response.status_code, 202)
        uploaded = upload_response.json()
        self.assertEqual(uploaded["mode"], "queued")
        self.assertEqual(uploaded["status"], "queued")
        with self.store._pool.connection() as connection:
            task_owner = connection.execute(
                "SELECT owner_user_id FROM agent_tasks WHERE task_id = %s",
                (uploaded["task_id"],),
            ).fetchone()
        self.assertIsNotNone(task_owner)
        self.assertEqual(task_owner[0], owner_a)
        # 任务不会被 worker 领走（测试进程里没有 worker），所以落盘的源文件
        # 要在这里手动清掉，否则 uploads/ 会慢慢堆垃圾。
        Path(
            config.get_settings().upload_dir,
            f"{uploaded['payload']['doc_id']}.txt",
        ).unlink(missing_ok=True)
        with patch.object(
            rag,
            "answer",
            return_value={
                "answer": "synthetic response",
                "sources": [],
                "session_id": "synthetic",
            },
        ) as answer:
            chat_response = self.client.post(
                "/api/chat",
                json={"question": "synthetic question"},
            )
        self.assertEqual(chat_response.status_code, 200)
        self.assertEqual(answer.call_args.args[0], "synthetic question")
        self.assertEqual(answer.call_args.args[2], owner_a)

        store.add(
            ids=ids,
            embeddings=[embedding, embedding, embedding],
            documents=["synthetic owner A", "synthetic owner B", "synthetic shared"],
            metadatas=[
                {"doc_id": doc_a, "filename": "synthetic-a.txt", "chunk_index": 0, "owner_user_id": owner_a},
                {"doc_id": doc_b, "filename": "synthetic-b.txt", "chunk_index": 0, "owner_user_id": owner_b},
                {"doc_id": doc_shared, "filename": "synthetic-shared.txt", "chunk_index": 0, "owner_user_id": "shared"},
            ],
        )
        session_a = f"tenant-test-session-{self.suffix}-a"
        session_b = f"tenant-test-session-{self.suffix}-b"
        student_session_a = f"student_{self.suffix}_a"
        memory = main.get_memory()
        memory.append_for_user(owner_a, session_a, "synthetic A question", "synthetic A answer")
        memory.append_for_user(
            owner_a,
            student_session_a,
            "synthetic student question",
            "synthetic student answer",
        )
        memory.append_for_user(owner_b, session_b, "synthetic B question", "synthetic B answer")
        retrieval.invalidate_corpus_cache()

        try:
            corpus_a = retrieval._load_corpus(
                store,
                owner_a,
                config.get_settings().retrieval_max_lexical_chunks,
            )
            self.assertEqual(
                {item.metadata["doc_id"] for item in corpus_a.documents}
                & {doc_a, doc_b, doc_shared},
                {doc_a, doc_shared},
            )
            docs_a = self.client.get("/api/documents").json()["documents"]
            self.assertEqual(
                {item["doc_id"] for item in docs_a} & {doc_a, doc_b},
                {doc_a},
            )
            self.assertIn(doc_shared, {item["doc_id"] for item in docs_a})
            self.assertEqual(self.client.delete(f"/api/documents/{doc_b}").status_code, 404)
            self.assertEqual(self.client.delete(f"/api/documents/{doc_shared}").status_code, 404)
            listed_sessions_a = self.client.get("/api/sessions").json()["sessions"]
            self.assertEqual([item["session_id"] for item in listed_sessions_a], [session_a])
            listed_student_sessions_a = self.client.get(
                "/api/sessions?assistant_mode=student"
            ).json()["sessions"]
            self.assertEqual(
                [item["session_id"] for item in listed_student_sessions_a],
                [student_session_a],
            )
            self.assertEqual(
                self.client.get(
                    f"/api/chat/{student_session_a}/history"
                ).status_code,
                404,
            )
            self.assertEqual(
                self.client.get(
                    f"/api/chat/{student_session_a}/history?assistant_mode=student"
                ).status_code,
                200,
            )
            self.assertEqual(self.client.get(f"/api/chat/{session_b}/history").status_code, 404)
            self.assertEqual(self.client.delete(f"/api/chat/{session_b}/history").status_code, 404)
            self.assertEqual(self.client.delete("/api/sessions").json()["cleared"], 1)
            self.assertEqual(
                [
                    item["session_id"]
                    for item in self.client.get(
                        "/api/sessions?assistant_mode=student"
                    ).json()["sessions"]
                ],
                [student_session_a],
            )

            self.client.post("/api/auth/logout")
            self.client.post(
                "/api/auth/login",
                json={"username": self.username_b, "password": self.password_b},
            )
            retrieval.invalidate_corpus_cache()
            corpus_b = retrieval._load_corpus(
                store,
                owner_b,
                config.get_settings().retrieval_max_lexical_chunks,
            )
            self.assertEqual(
                {item.metadata["doc_id"] for item in corpus_b.documents}
                & {doc_a, doc_b, doc_shared},
                {doc_b, doc_shared},
            )
            docs_b = self.client.get("/api/documents").json()["documents"]
            self.assertEqual(
                {item["doc_id"] for item in docs_b} & {doc_a, doc_b},
                {doc_b},
            )
            self.assertIn(doc_shared, {item["doc_id"] for item in docs_b})
            listed_sessions_b = self.client.get("/api/sessions").json()["sessions"]
            self.assertEqual([item["session_id"] for item in listed_sessions_b], [session_b])
            self.assertEqual(self.client.get(f"/api/chat/{session_a}/history").status_code, 404)
            self.assertEqual(self.client.delete(f"/api/documents/{doc_a}").status_code, 404)
        finally:
            store.delete(ids=ids)
            retrieval.invalidate_corpus_cache()
            with memory._pg_pool.connection() as connection:
                connection.execute(
                    "DELETE FROM sessions WHERE session_id = ANY(%s)",
                    ([session_a, session_b],),
                )


@unittest.skipUnless(os.environ.get("POSTGRES_DSN"), "requires the configured PostgreSQL service")
class LocalSingleUserModeTests(unittest.TestCase):
    """AUTH_MODE=local：免登录、身份恒定、登录接口关闭。

    与上一个类刻意互为对照 —— 同样是「不带任何 cookie 请求受保护接口」，
    session 模式必须 401、local 模式必须 200。这条对照关系就是本次改造的
    全部语义，值得被测试钉住，而不是靠人肉点一遍才发现某天回滚了。
    """

    def setUp(self):
        self.suffix = uuid.uuid4().hex[:12]
        self.owner_username = f"local{self.suffix}"
        test_settings = config.get_settings().model_copy(
            update={
                "credential_encryption_key": Fernet.generate_key().decode("ascii"),
                "auth_mode": "local",
                "local_owner_username": self.owner_username,
            }
        )
        self.original_auth_store_get_settings = auth_store.get_settings
        self.original_main_get_settings = main.get_settings
        self.auth_store_settings_patch = patch.object(
            auth_store, "get_settings", return_value=test_settings
        )
        self.main_settings_patch = patch.object(
            main, "get_settings", return_value=test_settings
        )
        self.auth_store_settings_patch.start()
        self.main_settings_patch.start()
        self.store = AuthStore()
        auth_store._store = self.store
        # 进程内主人缓存必须清掉：否则上一个用例解析出的身份会被带进来，
        # 测试之间就产生了一条看不见的耦合。
        main._local_owner_cache = None
        self.client = TestClient(main.app)
        self.client.__enter__()

    def tearDown(self):
        main._local_owner_cache = None
        with self.store._pool.connection() as connection:
            connection.execute(
                "DELETE FROM app_users WHERE username = %s",
                (self.owner_username,),
            )
        self.client.__exit__(None, None, None)
        auth_store.close_auth_store()
        self.main_settings_patch.stop()
        self.auth_store_settings_patch.stop()
        self.assertIs(auth_store.get_settings, self.original_auth_store_get_settings)
        self.assertIs(main.get_settings, self.original_main_get_settings)

    def test_protected_endpoints_are_reachable_without_any_login(self):
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200)
        self.assertTrue(me.json()["authenticated"])
        self.assertEqual(me.json()["username"], self.owner_username)
        # 对照 session 模式的 401：这里既没有 cookie 也没有登录动作，
        # 但受保护接口必须照常可达。
        credentials = self.client.get("/api/credentials")
        self.assertEqual(credentials.status_code, 200)
        self.assertEqual(credentials.json()["credentials"], [])
        self.assertEqual(
            self.client.get("/api/sessions?assistant_mode=school").status_code, 200
        )

    def test_local_owner_is_stable_and_created_exactly_once(self):
        first = self.client.get("/api/auth/me").json()["user_id"]
        # 手动清缓存，强制再走一次 ensure_local_owner。幂等性靠
        # ON CONFLICT DO NOTHING + 回查保证，这里验证它确实没造出第二行 ——
        # 否则每次重启都会多一个 user，旧数据会「看起来消失」。
        main._local_owner_cache = None
        second = self.client.get("/api/auth/me").json()["user_id"]
        self.assertEqual(first, second)
        with self.store._pool.connection() as connection:
            count = connection.execute(
                "SELECT count(*) FROM app_users WHERE username = %s",
                (self.owner_username,),
            ).fetchone()
        self.assertEqual(count[0], 1)

    def test_login_and_logout_are_disabled_in_local_mode(self):
        self.assertEqual(
            self.client.post(
                "/api/auth/login",
                json={
                    "username": self.owner_username,
                    "password": "irrelevant-synthetic-password",
                },
            ).status_code,
            404,
        )
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 404)


if __name__ == "__main__":
    unittest.main()
