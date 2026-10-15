"""analysis_exec 安全校验的单元测试（纯函数，不依赖 PG/LLM/沙箱）。"""
import unittest

from app.analysis_exec import (
    validate_analysis_script,
    validate_output_path,
    summarize_script,
)


class TestValidateAnalysisScript(unittest.TestCase):
    def test_empty_script_rejected(self):
        self.assertTrue(validate_analysis_script(""))
        self.assertTrue(validate_analysis_script("   "))

    def test_valid_script_passes(self):
        script = (
            "import pandas as pd\n"
            "import matplotlib.pyplot as plt\n"
            "df = pd.read_csv('sales.csv')\n"
            "print(df['amount'].mean())\n"
            "df.groupby('month')['amount'].sum().plot()\n"
            "plt.savefig('/work/chart.png')\n"
        )
        self.assertEqual(validate_analysis_script(script), "")

    def test_network_access_rejected(self):
        self.assertTrue(validate_analysis_script("import requests\nr = requests.get('http://x')"))
        self.assertTrue(validate_analysis_script("import urllib.request"))
        self.assertTrue(validate_analysis_script("import socket\ns = socket.socket()"))
        self.assertTrue(validate_analysis_script("import httpx"))
        # 大小写不敏感
        self.assertTrue(validate_analysis_script("import Requests"))

    def test_dangerous_builtin_rejected(self):
        self.assertTrue(validate_analysis_script("eval('print(1)')"))
        self.assertTrue(validate_analysis_script("exec('x=1')"))
        self.assertTrue(validate_analysis_script("import os\nos.system('rm -rf /')"))
        self.assertTrue(validate_analysis_script("import subprocess\nsubprocess.run(['ls'])"))
        self.assertTrue(validate_analysis_script("__import__('os')"))
        self.assertTrue(validate_analysis_script("open('/etc/passwd', 'w')"))

    def test_disallowed_import_rejected(self):
        self.assertTrue(validate_analysis_script("import os"))
        self.assertTrue(validate_analysis_script("import shutil"))
        self.assertTrue(validate_analysis_script("import subprocess"))
        # 白名单内的顶层模块允许
        self.assertEqual(validate_analysis_script("import pandas as pd"), "")
        self.assertEqual(validate_analysis_script("from pathlib import Path"), "")

    def test_os_path_import_rejected(self):
        # os 不在白名单，即使 from os.path 也拒（顶层模块是 os）
        self.assertTrue(validate_analysis_script("from os.path import join"))


class TestValidateOutputPath(unittest.TestCase):
    def test_within_work_passes(self):
        self.assertEqual(validate_output_path("/work/chart.png"), "")
        self.assertEqual(validate_output_path("chart.png"), "")
        self.assertEqual(validate_output_path("/work/sub/x.csv"), "")

    def test_escape_rejected(self):
        self.assertTrue(validate_output_path("/etc/passwd"))
        self.assertTrue(validate_output_path("/work/../../etc/passwd"))
        self.assertTrue(validate_output_path("../../etc/passwd"))


class TestSummarizeScript(unittest.TestCase):
    def test_detects_features(self):
        s = "import matplotlib.pyplot as plt\nplt.savefig('x.png')\ndf.groupby('m').sum()\ndf.mean()"
        summary = summarize_script(s)
        self.assertIn("图表", summary)
        self.assertIn("分组", summary)

    def test_fallback(self):
        self.assertEqual(summarize_script("print(1)"), "执行数据分析")


if __name__ == "__main__":
    unittest.main()
