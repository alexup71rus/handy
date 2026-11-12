"""Opt-in real Chrome regression: HANDY_BROWSER_LIVE=1, existing Chrome only.

Only the test's localhost page is read. Other tabs contribute metadata solely
for ID selection; the test closes its tab and its own MCP client afterwards.
"""

import contextlib
import http.server
import json
import os
import tempfile
import threading
import unittest

from folder_memory.browser_tool import BrowserTool


def fixture():
    rows = "".join(f"<tr><td>Plan {i}</td><td>{i * 10}</td></tr>" for i in range(45))
    return ('''<!doctype html><html lang="en"><meta charset="utf-8"><title>Handy browser fixture</title>
<nav>OUTSIDE_NAV_SECRET</nav><main id="fixture"><h1>Fixture overview</h1>
<p id="intro">Overview sentence. <a href="/help">Help guide<span aria-hidden="true">HIDDEN_LINK_SECRET</span></a><span aria-hidden="true">HIDDEN_SECRET</span></p>
<section id="plans" class="pricing card"><h2>Plans</h2><p>Basic plan and current pricing.</p>
<table id="prices"><caption>Plans and prices</caption><thead><tr><th>Plan</th><th>USD</th></tr></thead><tbody>'''
            + rows + '''</tbody></table></section><section id="notes"><h2>Notes</h2><p>Public note.</p>
<p class="duplicate">First alternative</p><p class="duplicate">Second alternative</p>
<form><label>Private label<input value="FORM_SECRET"></label></form><script>const SCRIPT_SECRET = 1;</script>
</section><section id="nested"><h2>Nested table</h2><table id="outer"><tr><td>Outer cell</td>
<td><table><tr><td>NESTED_CONTENT</td></tr></table></td></tr></table>
<table id="attributes"><tr><th scope="row">Weight</th><td>42 g</td></tr><tr><th scope="row">Colour</th><td>Green</td></tr></table></section>
</main></html>''').encode()


@unittest.skipUnless(os.environ.get("HANDY_BROWSER_LIVE") == "1", "requires explicitly enabled existing Chrome smoke")
class BrowserLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        payload = fixture()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.addClassCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cls.addClassCleanup(lambda: (server.shutdown(), thread.join(timeout=2)))
        temporary = tempfile.TemporaryDirectory(prefix="handy-browser-live-")
        cls.addClassCleanup(temporary.cleanup)
        cls.browser = BrowserTool(temporary.name)
        cls.addClassCleanup(cls.browser.shutdown)
        opened = cls.browser.execute({"operation": "open", "url": f"http://127.0.0.1:{server.server_port}/fixture"})
        if opened["status"] != "ok":
            raise AssertionError(opened["text"])
        cls.tab = opened["data"]["tab_id"]
        cls.binding = opened["data"]["connection_id"]

        def close_fixture():
            if cls.browser.client.ready:
                with contextlib.suppress(Exception), cls.browser.client.lock:
                    cls.browser.client._request("tools/call", {"name": "close_page", "arguments": {"pageId": int(cls.tab)}})

        cls.addClassCleanup(close_fixture)

    def read(self, **kwargs):
        return self.browser.execute({"operation": "read", "tab_id": self.tab,
                                     "connection_id": self.binding, **kwargs})

    def test_hierarchy_tables_links_sources_and_private_dom_exclusion(self):
        result = self.read(query="Basic plan")
        self.assertEqual(result["status"], "ok", result["text"])
        data = result["data"]
        encoded = json.dumps(data, ensure_ascii=False)
        for hidden in ("OUTSIDE_NAV_SECRET", "HIDDEN_SECRET", "HIDDEN_LINK_SECRET", "FORM_SECRET", "SCRIPT_SECRET"):
            self.assertNotIn(hidden, encoded)
        overview = next(section for section in data["sections"] if section["heading"] == "Fixture overview")
        plans = next(section for section in data["sections"] if section["heading"] == "Plans")
        self.assertEqual(plans["parent_id"], overview["id"])
        self.assertEqual(plans["source"]["id"], "plans")
        self.assertIn("pricing", plans["source"]["classes"])
        table = next(table for table in data["tables"] if table["source"]["id"] == "prices")
        self.assertEqual(table["section_id"], plans["id"])
        self.assertEqual(table["columns"], ["Plan", "USD"])
        self.assertEqual(table["row_count"], 45)
        self.assertEqual(len(table["rows"]), 30)
        self.assertTrue(table["partial"])
        self.assertTrue(data["links"][0]["url"].endswith("/help"))
        attributes = next(table for table in data["tables"] if table["source"]["id"] == "attributes")
        self.assertEqual(attributes["columns"], [])
        self.assertEqual(attributes["row_count"], 2)
        self.assertEqual(attributes["rows"][0], ["Weight", "42 g"])

    def test_css_xpath_missing_ambiguous_and_invalid_selection(self):
        for selector in ("#intro", '//*[@id="notes"]'):
            result = self.read(selector=selector)
            self.assertEqual(result["status"], "ok", result["text"])
            self.assertEqual(result["data"]["selection"]["selector"], selector)
        for selector, error in (("#missing", "selector_missing"), (".duplicate", "selector_ambiguous"),
                                ("div > > span", "invalid_selector")):
            result = self.read(selector=selector)
            self.assertEqual(result["status"], "needs_input", result["text"])
            self.assertEqual(result["data"]["code"], error)

    def test_serialized_budget_and_nested_table_cells_do_not_mix(self):
        small = self.read(max_chars=2000)
        self.assertEqual(small["status"], "ok", small["text"])
        self.assertLess(len(json.dumps(small["data"], ensure_ascii=False)), 2600)
        self.assertTrue(any(small["data"]["truncation"].values()))
        scoped = self.read(selector="#outer")
        self.assertEqual(scoped["status"], "ok", scoped["text"])
        outer, = scoped["data"]["tables"]
        self.assertEqual(outer["row_count"], 1)
        self.assertEqual(outer["rows"], [["Outer cell", ""]])
        self.assertTrue(outer["partial"])
        self.assertNotIn("NESTED_CONTENT", json.dumps(outer))


if __name__ == "__main__":
    unittest.main()
