"""Browser observations stay structured, scoped to one tab and resource bounded."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

from folder_memory.browser_tool import BrowserError, BrowserTool, McpClient, canonical_url


CONNECTION = "a" * 32
PAGE = {"id": 7, "url": "https://shop.example/item", "title": "Shop", "selected": True}


def observation(url=PAGE["url"]):
    return {"schema_version": 1, "page": {"url": url, "title": "Shop", "language": "en"},
            "sections": [{"id": "section-0", "parent_id": None, "level": 0, "heading": "Shop", "blocks": []}],
            "tables": [], "links": [], "truncation": {"text": False, "tables": False}}


class FakeClient:
    connection_id = CONNECTION

    def __init__(self, pages=None, content=None, opened=None):
        self.pages = copy.deepcopy(pages if pages is not None else [PAGE])
        self.content = content or observation()
        self.opened, self.calls = opened, []

    def call(self, name, args, *, connection_id=""):
        self.calls.append((name, args, connection_id))
        if connection_id and connection_id != self.connection_id:
            raise BrowserError("stale connection")
        if name == "list_pages":
            data = {"pages": self.pages}
        elif name == "evaluate_script":
            data = {"result": self.content}
        elif name == "new_page":
            data = {"pages": self.opened}
        else:
            raise AssertionError(name)
        return {"structuredContent": data, "_connection_id": self.connection_id}

    def close(self, **_):
        pass


class BrowserToolTests(unittest.TestCase):
    def browser(self, **kwargs):
        client = FakeClient(**kwargs)
        return BrowserTool("unused", client=client), client

    def test_web_url_normalization_and_no_credentials_or_file_navigation(self):
        self.assertEqual(canonical_url("https://SHOP.example:443/цена?q=1#part"),
                         "https://shop.example/%D1%86%D0%B5%D0%BD%D0%B0?q=1#part")
        for url in ("file:///etc/passwd", "chrome://settings", "javascript:alert(1)",
                    "https://user:pass@shop.example", "https://shop.example\\@other.example", "https://shop.example/has space"):
            with self.subTest(url=url), self.assertRaises(BrowserError):
                canonical_url(url)

    def test_tab_inventory_is_metadata_only_and_omits_non_web_tabs(self):
        browser, client = self.browser(pages=[PAGE, {"id": 8, "url": "chrome://settings", "title": "Settings"}])
        result = browser.execute({"operation": "list"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["data"]["excluded_non_web_pages"], 1)
        self.assertEqual(result["data"]["pages"][0]["tab_id"], "7")
        self.assertEqual([call[0] for call in client.calls], ["list_pages"])

    def test_read_uses_exact_url_and_id_and_returns_structured_evidence(self):
        browser, client = self.browser(pages=[PAGE, {**PAGE, "id": 8}])
        result = browser.execute({"operation": "read", "url": PAGE["url"], "tab_id": "7", "connection_id": CONNECTION,
                                  "selector": "#prices", "query": "цена товара", "max_chars": 16000})
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["data"]["content_is_untrusted"])
        self.assertEqual(result["data"]["page"]["tab_id"], "7")
        self.assertEqual(result["data"]["source_ids_scope"], "this_observation")
        self.assertEqual([call[0] for call in client.calls], ["list_pages", "evaluate_script"])
        _, arguments, binding = client.calls[-1]
        self.assertEqual(binding, CONNECTION)
        self.assertEqual(arguments["pageId"], 7)
        self.assertEqual(arguments["dialogAction"], "")
        self.assertIn("location.href !== expected", arguments["function"])
        self.assertIn('"selector": "#prices"', arguments["function"])

    def test_duplicate_or_unspecified_tabs_ask_instead_of_guessing(self):
        for args in ({"url": PAGE["url"]}, {}):
            browser, client = self.browser(pages=[PAGE, {**PAGE, "id": 8, "selected": False}])
            result = browser.execute({"operation": "read", **args})
            self.assertEqual(result["status"], "needs_input")
            self.assertEqual(len(result["data"]["pages"]), 2)
            self.assertEqual(len(client.calls), 1)

    def test_id_and_url_cannot_silently_select_different_pages(self):
        browser, client = self.browser(pages=[PAGE, {**PAGE, "id": 8, "url": "https://other.example/"}])
        result = browser.execute({"operation": "read", "url": PAGE["url"], "tab_id": "8"})
        self.assertEqual(result["status"], "needs_input")
        self.assertEqual(len(client.calls), 1)

    def test_stale_tab_binding_never_reads_a_reused_id(self):
        browser, client = self.browser()
        result = browser.execute({"operation": "read", "tab_id": "7", "connection_id": "b" * 32})
        self.assertEqual(result["status"], "error")
        self.assertEqual([call[0] for call in client.calls], ["list_pages"])

    def test_invalid_or_extra_arguments_do_not_touch_browser(self):
        browser, client = self.browser()
        for args in ({"operation": "evaluate", "function": "alert(1)"}, {"operation": []},
                     {"operation": "read", "function": "alert(1)"}, {"operation": "read", "tab_id": True},
                     {"operation": "read", "connection_id": ""}, {"operation": "read", "max_chars": 100},
                     {"operation": "read", "max_chars": True}, {"operation": "open"},
                     {"operation": "open", "url": "file:///etc/passwd"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                browser.execute(args)
        self.assertEqual(client.calls, [])

    def test_page_races_and_selector_errors_are_explicit(self):
        for error in ("page_changed", "page_loading", "selector_missing", "selector_ambiguous", "invalid_selector"):
            browser, _ = self.browser(content={"error": error, "matches": 2})
            result = browser.execute({"operation": "read", "tab_id": "7"})
            self.assertEqual(result["status"], "needs_input", result)
            self.assertEqual(result["data"]["code"], error)
        browser, _ = self.browser(content=observation("https://other.example/"))
        self.assertEqual(browser.execute({"operation": "read", "tab_id": "7"})["status"], "error")

    def test_oversized_or_malformed_dom_result_is_not_forwarded(self):
        huge = observation()
        huge["sections"][0]["blocks"] = ["x" * 25000]
        for content in (huge, {"page": {}}, {**observation(), "tables": "bad"}):
            browser, _ = self.browser(content=content)
            result = browser.execute({"operation": "read", "tab_id": "7", "max_chars": 2000})
            self.assertEqual(result["status"], "error", result)

    def test_confirmed_open_reports_redirect_but_does_not_read_page(self):
        browser, client = self.browser(opened=[PAGE, {"id": 8, "url": "https://shop.example/redirect", "title": "Redirect"}])
        result = browser.execute({"operation": "open", "url": "https://shop.example/start"})
        self.assertEqual(result["status"], "ok")
        self.assertIs(result["data"]["opened"], True)
        self.assertIs(result["data"]["redirected"], True)
        self.assertEqual([call[0] for call in client.calls], ["list_pages", "new_page"])
        self.assertTrue(client.calls[-1][1]["background"])

    def test_ambiguous_open_does_not_claim_success_or_safe_repetition(self):
        browser, _ = self.browser(opened=[PAGE, {**PAGE, "id": 8}, {**PAGE, "id": 9}])
        result = browser.execute({"operation": "open", "url": PAGE["url"]})
        self.assertIsNone(result["data"]["opened"])
        browser, client = self.browser()
        client.call = lambda *args, **kwargs: (_ for _ in ()).throw(BrowserError("disconnected"))
        result = browser.execute({"operation": "open", "url": PAGE["url"]})
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["data"]["opened"])
        self.assertNotIn("performed", result["data"])


RPC_SERVER = r'''
import json, os, pathlib, sys, time
pathlib.Path(__file__).with_suffix('.pid').write_text(str(os.getpid()))
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request: continue
    method = request['method']
    if method == 'initialize': result = {'protocolVersion': '2024-11-05'}
    elif method == 'tools/list': result = {'tools': [{'name': name} for name in ('list_pages', 'new_page', 'evaluate_script')]}
    elif pathlib.Path(__file__).with_suffix('.silent').exists():
        time.sleep(10); continue
    else: result = {'structuredContent': {'pages': []}}
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
'''


class BrowserProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.entry = self.state / "mcp.py"
        self.entry.write_text(RPC_SERVER)
        self.client = McpClient({"enabled": True, "node": sys.executable, "entry": str(self.entry),
                                 "timeout_seconds": .3, "connect_timeout_seconds": .3, "idle_seconds": 1}, self.state)
        self.addCleanup(self.client.close, permanent=True)

    def assert_reaped(self, supervisor, worker):
        self.assertIsNotNone(supervisor.poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(worker, 0)

    def test_lazy_start_reuse_idle_reap_and_shutdown_reap_the_owned_worker(self):
        self.assertIsNone(self.client.process)
        self.client.call("list_pages", {})
        supervisor, worker = self.client.process, int(self.entry.with_suffix(".pid").read_text())
        first_binding = self.client.connection_id
        self.client.call("list_pages", {}, connection_id=first_binding)
        self.assertIs(self.client.process, supervisor)
        self.client.last_used = time.monotonic() - 5
        self.client.reap_idle()
        self.assert_reaped(supervisor, worker)
        self.assertIsNone(self.client.process)
        self.client.call("list_pages", {})
        supervisor, worker = self.client.process, int(self.entry.with_suffix(".pid").read_text())
        self.assertNotEqual(first_binding, self.client.connection_id)
        self.client.close(permanent=True)
        self.assert_reaped(supervisor, worker)
        with self.assertRaises(BrowserError):
            self.client.call("list_pages", {})

    def test_timeout_closes_supervisor_and_worker_without_waiting_for_mcp(self):
        self.client.start()
        supervisor, worker = self.client.process, int(self.entry.with_suffix(".pid").read_text())
        self.entry.with_suffix(".silent").touch()
        started = time.monotonic()
        with self.assertRaises(BrowserError):
            self.client.call("list_pages", {})
        self.assertLess(time.monotonic() - started, 4)
        self.assert_reaped(supervisor, worker)

    def test_stale_binding_is_rejected_without_starting_a_new_worker(self):
        with self.assertRaises(BrowserError):
            self.client.call("list_pages", {}, connection_id=CONNECTION)
        self.assertIsNone(self.client.process)
        self.assertFalse(self.entry.with_suffix(".pid").exists())


if __name__ == "__main__":
    unittest.main()
