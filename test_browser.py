"""Local simulated-site integration tests; never claims a live CDE pass."""
import argparse
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

import crawler
from test_crawler import fixture


class BrowserFlowTests(unittest.TestCase):
    def test_pagination_popup_resume_dedup_export(self):
        from openpyxl import load_workbook
        counts = Counter()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if getattr(self.server, "reject", False):
                    self.send_response(400)
                    self.end_headers()
                    return
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                if parts.path.endswith("prosearch.dhtml"):
                    text = '''<form action="/list"><input aria-label="适应症" name="keyword">
                    <select><option>所有</option></select><select><option>进行中</option></select>
                    <button>查询</button></form>'''
                elif parts.path == "/list":
                    page = int(query.get("page", ["1"])[0])
                    numbers = [1, 2] if page == 1 else [3]
                    rows = ''.join(f'<tr><td>{i}</td><td><a target="_blank" href="/detail?ctr=CTR2026000{i}">CTR2026000{i}</a></td><td>进行中 招募中</td><td>测试药</td><td>测试肿瘤</td><td>合成测试项目</td></tr>' for i in numbers)
                    text = f'<table>{rows}</table><p>当前第 {page} 页，共 2 页，共 3 条记录</p>'
                    if page == 1:
                        text += '<a href="/list?page=2">Next</a>'
                elif parts.path == "/detail":
                    ctr = query["ctr"][0]
                    counts[ctr] += 1
                    status = "已完成" if ctr.endswith("3") else "进行中 （招募中）"
                    content = fixture(ctr, status).replace('<html><body>', '').replace('</body></html>', '')
                    text = '<button onclick="document.getElementById(\'details\').hidden=false">公示的试验信息</button><div id="details" hidden>' + content + '</div>'
                else:
                    text = ""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(text.encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        previous_base = crawler.BASE
        crawler.BASE = f"http://127.0.0.1:{server.server_port}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = Path(tmp) / "config.json"
                config.write_text(json.dumps({"keywords": ["瘤", "癌"], "delay_seconds": 2, "timeout_seconds": 10, "max_retries": 1}), encoding="utf-8")
                args = argparse.Namespace(config=str(config), output=str(Path(tmp) / "data"), export_only=False, keywords=None, refresh=False, channel="chrome", non_interactive=True, limit=1)
                self.assertEqual(crawler.collect(args), 0)
                args.limit = 0
                self.assertEqual(crawler.collect(args), 0)
                self.assertEqual(counts, {"CTR20260001": 1, "CTR20260002": 1, "CTR20260003": 1})
                book = load_workbook(Path(args.output) / "肿瘤项目.xlsx")
                self.assertEqual(book["进行中项目"].max_row, 3)
                self.assertEqual(book["状态已变更"].max_row, 2)
                self.assertEqual(book["待补采"].max_row, 1)
                self.assertTrue(all(row[3] == "是" and row[4] == 3 for row in book["检索覆盖"].iter_rows(min_row=2, values_only=True)))
                self.assertIn("已完成", book["采集说明"]["B6"].value)
                book.close()
                server.reject = True
                config.write_text(json.dumps({"keywords": ["瘤", "癌"], "delay_seconds": 2, "timeout_seconds": 0.5, "max_retries": 1}), encoding="utf-8")
                args.output = str(Path(tmp) / "blocked")
                self.assertEqual(crawler.collect(args), 2)
                blocked = load_workbook(Path(args.output) / "肿瘤项目.xlsx")
                self.assertIn("未完成", blocked["采集说明"]["B6"].value)
                self.assertIn("400", blocked["检索覆盖"]["F2"].value)
                blocked.close()
        finally:
            crawler.BASE = previous_base
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
