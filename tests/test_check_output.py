import io
import json
import unittest
from unittest.mock import patch

from scripts.check_mcp import print_summary


class CheckOutputTests(unittest.TestCase):
    def test_summary_round_trips_on_english_windows_console(self):
        summary = {'warnings': ['卡片向量索引缺失'], 'target': 'C:/测试项目'}
        buffer = io.BytesIO()
        with io.TextIOWrapper(buffer, encoding='cp1252') as output:
            with patch('sys.stdout', output):
                print_summary(summary)
            output.flush()
            self.assertEqual(json.loads(buffer.getvalue().decode('cp1252')), summary)
