"""终端执行分类器与安全策略测试（阶段 C 之后新增）。

覆盖三块：
1. **分类器纯函数** —— 只读 / 危险 / 永久拒绝三档，重点是「拼接符逃逸」
   和「只读命令里藏危险参数」这类反向用例。
2. **路径越界** —— cat /etc/passwd、find -exec、../ 逃逸必须归 forbidden。
3. **脱敏 / 截断** —— env 输出里的密钥值被遮蔽，超长输出被截断。

分类器是纯函数，不依赖数据库，全部可离线跑。
"""
import unittest

from app.shell_exec import (
    MAX_OUTPUT_CHARS,
    classify,
    classify_detail,
    redact_output,
    summarize,
    truncate_output,
)


class ClassifyTestCase(unittest.TestCase):
    def test_readonly_basics(self):
        for cmd in ["ls", "df -h", "free -m", "ps aux", "pwd", "date", "uptime",
                    "uname -a", "whoami", "id", "cat /work/x.txt", "du -sh /work"]:
            self.assertEqual(classify(cmd), "readonly", cmd)

    def test_dangerous_writes(self):
        for cmd in ["mkdir /work/foo", "touch /work/a.txt", "cp /work/a /work/b",
                    "mv /work/a /work/b", "tar -xzf /work/x.tar.gz", "unzip /work/x.zip"]:
            self.assertEqual(classify(cmd), "dangerous", cmd)

    def test_forbidden_destructive(self):
        for cmd in ["rm -rf /work/x", "dd if=/dev/zero of=/work/x", "mkfs.ext4 /dev/sda",
                    "shutdown -h now", "reboot", "docker ps", "ssh root@host",
                    "curl http://x.com", "kill -9 1", "pkill nginx"]:
            self.assertEqual(classify(cmd), "forbidden", cmd)

    def test_forbidden_metachar_concatenation(self):
        # 拼接符是最经典也最危险的逃逸，必须一律拒绝。
        for cmd in ["ls; rm -rf /", "ls && rm -rf /", "ls || cat /etc/passwd",
                    "echo hi | cat /etc/passwd", "echo $(cat /etc/passwd)",
                    "echo `whoami`", "ls > /work/out.txt", "cat /etc/passwd > /work/x"]:
            self.assertEqual(classify(cmd), "forbidden", cmd)

    def test_forbidden_path_escape_read(self):
        # 读路径命令必须强制路径落在 /work 内，否则 forbidden（不是 dangerous）。
        for cmd in ["cat /etc/passwd", "cat /app/.env", "grep key /etc/shadow",
                    "head /etc/hosts", "tail /var/log/syslog", "find / -name x",
                    "cat ../../etc/passwd", "cat /etc/../etc/passwd"]:
            self.assertEqual(classify(cmd), "forbidden", cmd)

    def test_find_restricted_flags(self):
        self.assertEqual(classify("find /work -name '*.txt'"), "readonly")
        # -exec / -delete 是 find 的逃逸口，必须拒绝。
        self.assertEqual(classify("find /work -exec rm {} \\;"), "forbidden")
        self.assertEqual(classify("find /work -delete"), "forbidden")

    def test_unknown_command_forbidden(self):
        # 默认拒绝：白名单外一律 forbidden。
        for cmd in ["python3 -c 'print(1)'", "bash /work/x.sh", "gcc x.c",
                    "pip install x", "apt-get install x", "vim /work/x"]:
            self.assertEqual(classify(cmd), "forbidden", cmd)

    def test_empty_command(self):
        self.assertEqual(classify(""), "forbidden")
        self.assertEqual(classify("   "), "forbidden")


class ClassifyDetailTestCase(unittest.TestCase):
    def test_detail_kind(self):
        self.assertEqual(classify_detail("ls")[0], "readonly")
        self.assertEqual(classify_detail("mkdir /work/x")[0], "dangerous")

    def test_forbidden_raises(self):
        # classify_detail 对 forbidden 抛 _Forbidden，classify 捕获后返回 "forbidden"。
        from app.shell_exec import _Forbidden

        with self.assertRaises(_Forbidden):
            classify_detail("rm -rf /")


class RedactTestCase(unittest.TestCase):
    def test_redact_key_value(self):
        text = "LLM_API_KEY=sk-abc123\nPOSTGRES_PASSWORD=supersecret\nUSER=admin"
        out = redact_output(text)
        self.assertNotIn("sk-abc123", out)
        self.assertNotIn("supersecret", out)
        self.assertIn("********", out)
        # 非敏感行不受影响。
        self.assertIn("USER=admin", out)

    def test_redact_colon_value(self):
        text = "password: hunter2\ntoken: abc"
        out = redact_output(text)
        self.assertNotIn("hunter2", out)
        self.assertNotIn("abc", out)

    def test_redact_no_match_untouched(self):
        text = "total 12\n-rw-r--r-- 1 user user 100 x.txt"
        self.assertEqual(redact_output(text), text)


class TruncateTestCase(unittest.TestCase):
    def test_truncate_long(self):
        long = "a" * (MAX_OUTPUT_CHARS + 100)
        out = truncate_output(long)
        self.assertLessEqual(len(out), MAX_OUTPUT_CHARS + 50)
        self.assertIn("截断", out)

    def test_truncate_short_untouched(self):
        self.assertEqual(truncate_output("hi"), "hi")


class SummarizeTestCase(unittest.TestCase):
    def test_summarize_templates(self):
        self.assertIn("创建目录", summarize("mkdir /work/foo"))
        self.assertIn("复制", summarize("cp /work/a /work/b"))
        self.assertIn("移动", summarize("mv /work/a /work/b"))
        self.assertIn("创建空文件", summarize("touch /work/a.txt"))

    def test_summarize_fallback(self):
        # 非模板命令：至少返回可读的「执行命令：xxx」。
        self.assertIn("执行命令", summarize("unknowncmd arg"))


class NpxSkillsTests(unittest.TestCase):
    def test_find_is_readonly(self):
        self.assertEqual(classify("npx skills find react testing"), "readonly")
        self.assertEqual(classify("npx -y skills find pdf --owner anthropics"), "readonly")

    def test_install_and_update_need_approval(self):
        self.assertEqual(classify("npx skills add vercel-labs/agent-skills@react-best-practices -g -y"), "dangerous")
        self.assertEqual(classify("npx --yes skills update"), "dangerous")

    def test_other_packages_and_tricks_are_forbidden(self):
        for cmd in (
            "npx",
            "npx evil-package",
            "npx -y evil skills find x",
            "npx --package evil skills find x",
            "npx -c 'id'",
            "npx skills",
            "npx skills run x",
            "npx skills add ../../x",
            "npx skills add a/b; id",
            "npx skills add $(id)",
            "npx skills find x | sh",
        ):
            self.assertEqual(classify(cmd), "forbidden", cmd)


if __name__ == "__main__":
    unittest.main()
