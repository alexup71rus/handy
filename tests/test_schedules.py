from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from folder_memory.http_client import _connect_public, _request, bounded_request
from folder_memory.schedule_actions import start_worker
from folder_memory.schedule_worker import notify_desktop
from folder_memory.scheduled_price import Price, PriceUnavailable, check_price
from folder_memory.schedules import ScheduleConflict, Schedules, validate_schedule


URL = "https://store.example/test"
MINUTE = {"count": 1, "unit": "minute"}
XPATH = '//*[@id="__nuxt"]/div[2]/main/div[1]/div[2]/div/section[1]/div[2]/p/span'
PUBLIC_LIMITS = {"public_web": True, "response_bytes": 1024, "same_origin_redirects": 2, "timeout_seconds": 2}


def html_response(body, **changes):
    return {"url": URL, "status": 200, "http_ok": True, "truncated": False,
            "content_type": "text/html; charset=utf-8", "body": body, **changes}


def product_page(offers, *, name="Watch", url=URL):
    product = {"@context": "https://schema.org", "@type": "Product", "name": name, "url": url, "offers": offers}
    return f'<html><script type="application/ld+json">{json.dumps(product)}</script><main><h1>{name}</h1></main></html>'


def offer(price, currency="RUB", **extra):
    return {"@type": "Offer", "price": price, "priceCurrency": currency, **extra}


class PriceExtractionTests(unittest.TestCase):
    def price(self, response, *, xpath="", currency=""):
        request = Mock(return_value=response)
        price = check_price(validate_schedule(URL, MINUTE, "any", xpath=xpath, currency=currency), request=request)
        self.assertEqual(request.call_args.args[0]["method"], "GET")
        self.assertTrue(request.call_args.args[1]["public_web"])
        return price

    def test_decimal_literal_and_semantic_product_binding(self):
        price = self.price(html_response(product_page(offer("100.01"))))
        self.assertEqual((price.amount, price.currency), (Decimal("100.01"), "RUB"))
        self.assertEqual(price.source_key, self.price(html_response(product_page(offer("101.37")))).source_key)
        self.assertNotEqual(price.source_key, self.price(html_response(product_page(offer("100.01"), name="Bracelet"))).source_key)

    def test_xpath_to_nested_price_ignores_other_costs_and_hidden_text(self):
        html = """
<div id="__nuxt">
  <div>Shipping 990 ₽</div>
  <div><main><div><div>Previous price 15 990 ₽</div>
    <div><div><section><div>Delivery</div>
      <div><p><span><i hidden>100 ₽</i>12&#8239;490,50 ₽</span></p></div>
    </section></div></div>
  </div></main></div>
</div>"""
        price = self.price(html_response(html), xpath=XPATH)
        self.assertEqual((price.amount, price.currency), (Decimal("12490.50"), "RUB"))

    def test_ambiguous_missing_and_deliberately_wrong_inputs_abstain(self):
        for response, xpath, currency in [
            (html_response(product_page([offer("50"), offer("60")])), "", ""),
            (html_response(product_page(offer("50", eligibleTransactionVolume={"price": 100}))), "", ""),
            (html_response(product_page(offer("50", validForMemberTier={"@id": "#gold"}))), "", ""),
            (html_response(product_page(offer("50", availability="https://schema.org/OutOfStock"))), "", ""),
            (html_response('<template>' + product_page(offer("50")) + '</template>'), "", ""),
            (html_response(product_page(offer("50", "EUR"))), "", "RUB"),
            (html_response(product_page(offer("50"), url="https://store.example/other").replace("<h1>Watch", "<h1>Shop")), "", ""),
            (html_response(product_page({"@type": "AggregateOffer", "lowPrice": "50", "highPrice": "60", "priceCurrency": "RUB"})), "", ""),
            (html_response(product_page(offer("50")), truncated=True), "", ""),
            (html_response(product_page(offer("50")), status=403, http_ok=False), "", ""),
            (html_response(product_page(offer("50")), status=206), "", ""),
            (html_response(product_page(offer("50")), content_type="application/json"), "", ""),
            (html_response('<span id="price">$15</span>'), '//*[@id="price"]', ""),
            (html_response('<span id="price">15 ₽ / month</span>'), '//*[@id="price"]', ""),
            (html_response('<span id="price" hidden>15 ₽</span>'), '//*[@id="price"]', ""),
            (html_response('<span id="price">15 ₽</span><span id="price">16 ₽</span>'), '//*[@id="price"]', ""),
            (html_response('<span id="new-price">15 ₽</span>'), '//*[@id="price"]', ""),
        ]:
            with self.subTest(response=response, xpath=xpath, currency=currency), self.assertRaises(PriceUnavailable):
                self.price(response, xpath=xpath, currency=currency)

    def test_explicit_currency_can_disambiguate_symbol_or_number(self):
        price = self.price(html_response('<span id="price">$1,250.01</span>'), xpath='//*[@id="price"]', currency="USD")
        self.assertEqual(price.amount, Decimal("1250.01"))
        self.assertEqual(self.price(html_response('<span id="price">18.99</span>'), xpath='//*[@id="price"]', currency="EUR").currency, "EUR")

    def test_settings_validate_before_network(self):
        for url in ("file:///tmp/test", "http://me:password@store.example", "http://localhost", "http://foo.local",
                    "http://127.0.0.1", "http://127.1", "http://0177.0.0.1", "http://192.168.1.1",
                    "http://169.254.169.254", "http://100.64.0.1", "http://[::1]", "http://[::ffff:8.8.8.8]",
                    "http://[fe80::2%25en0]", "http://[64:ff9b::7f00:1]", "http://[2002:7f00:1::]",
                    "http://[2001::7f00:1]", "http://[fec0::1]", "https://store.example/#private"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_schedule(url, MINUTE, "down")
        for interval in ({}, {"count": False, "unit": "minute"}, {"count": 0, "unit": "day"},
                         {"count": 1, "unit": "second"}, {"count": 121, "unit": "month"}):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                validate_schedule(URL, interval, "up")
        for xpath in ('//span[1]', '//*[@id="price"]//span', '//*[@id="price"]/span[0]', '//*[@id="price"]/span/text()', '//*[@id="price"] | //*'):
            with self.subTest(xpath=xpath), self.assertRaises(ValueError):
                validate_schedule(URL, MINUTE, "any", xpath=xpath)
        self.assertEqual(validate_schedule("https://магазин.example/цены", MINUTE, "any")["url"],
                         "https://xn--80aairftm.example/%D1%86%D0%B5%D0%BD%D1%8B")


class PublicFetchTests(unittest.TestCase):
    @staticmethod
    def address(ip, port=443):
        return socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)

    def test_rejects_mixed_public_and_private_dns_results_before_connect(self):
        with patch("folder_memory.http_client.socket.getaddrinfo", return_value=[self.address("8.8.8.8"), self.address("127.0.0.1")]), \
                patch("folder_memory.http_client.socket.socket") as make_socket:
            with self.assertRaises(PermissionError):
                _connect_public(("untrusted.example", 443), 5)
            make_socket.assert_not_called()

    def test_connection_is_pinned_to_the_vetted_address(self):
        with patch("folder_memory.http_client.socket.getaddrinfo", return_value=[self.address("8.8.8.8")]) as resolve, \
                patch("folder_memory.http_client.socket.socket") as make_socket:
            _connect_public(("store.example", 443), 5)
            resolve.assert_called_once_with("store.example", 443, type=socket.SOCK_STREAM)
            make_socket.return_value.connect.assert_called_once_with(("8.8.8.8", 443))

    def test_public_profile_checks_ip_inside_the_subprocess(self):
        args = {"url": "http://127.0.0.1:39527/price", "method": "GET", "body": "", "content_type": ""}
        with self.assertRaises(ValueError):
            bounded_request(args, PUBLIC_LIMITS)

    def test_cross_origin_redirect_is_never_contacted(self):
        args = {"url": URL, "method": "GET", "body": "", "content_type": ""}
        with patch("folder_memory.http_client.urllib.request.build_opener") as build:
            opener = build.return_value
            opener.open.return_value = _Response(302, {"Location": "http://127.0.0.1:43122/"})
            with self.assertRaises(PermissionError):
                _request(args, PUBLIC_LIMITS, time.monotonic() + 2)
            opener.open.assert_called_once()

    def test_incomplete_http_content_length_is_rejected(self):
        args = {"url": URL, "method": "GET", "body": "", "content_type": ""}
        headers = Message()
        headers["Content-Type"] = "text/html; charset=utf-8"
        with patch("folder_memory.http_client.urllib.request.build_opener") as build:
            build.return_value.open.return_value = _Response(200, headers, length=100)
            with self.assertRaisesRegex(OSError, "Incomplete"):
                _request(args, PUBLIC_LIMITS, time.monotonic() + 2)


class _Response:
    def __init__(self, code, headers, length=None):
        self.code, self.headers, self.length = code, headers, length

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def read(self, size):
        return b""


class SchedulesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state_dir = Path(temporary.name)
        self.checked = []

    def setup_checks(self, *values):
        pending = iter(values)

        def check(job):
            self.checked.append(job["id"])
            value = next(pending)
            if isinstance(value, ValueError):
                raise value
            amount, currency, *source = value
            return Price(Decimal(amount), currency, source[0] if source else "test source")

        return Schedules(self.state_dir, check=check)

    def test_direction_baseline_persistence_and_errors(self):
        state = self.setup_checks(("100", "RUB"), ("120", "RUB"), PriceUnavailable("Сайт временно недоступен."),
                                  ("110", "EUR"), ("85", "RUB", "other product"), ("110.25", "RUB"))
        job = state.create("project", url=URL, interval=MINUTE, direction="down", now=100)
        self.assertEqual(state.tick(now=99), [])
        self.assertEqual(state.tick(now=100), [])
        self.assertEqual(state.tick(now=159), [])
        self.assertEqual(state.tick(now=160), [])
        self.assertEqual(state.tick(now=220), [])
        self.assertEqual(state.get("project", job["id"])["last_price"], "120")
        self.assertIn("недоступен", state.get("project", job["id"])["last_error"])
        self.assertEqual(state.tick(now=280), [])
        self.assertEqual(state.tick(now=340), [])
        events = state.tick(now=400)
        self.assertEqual([(e["direction"], e["previous_price"], e["price"], e["currency"]) for e in events],
                         [("down", "120", "110.25", "RUB")])
        reopened = Schedules(self.state_dir)
        self.assertEqual(reopened.events("project"), events)
        self.assertEqual(reopened.events("another project"), [])
        self.assertEqual(reopened.get("project", job["id"])["next_run"], 460)
        self.assertIsNone(reopened.get("another project", job["id"]))
        self.assertIsNone(reopened.get("project", job["id"])["last_error"])

    def test_malformed_entity_does_not_stop_other_due_watches(self):
        bad_url = 'https://store.example/malformed'

        def request(args, _limits):
            if args['url'] == bad_url:
                return html_response('<div id="price">&#' + '9' * 4301 + '; 10 RUB</div>', url=bad_url)
            return html_response(product_page(offer('100')))

        state = Schedules(self.state_dir, check=lambda job: check_price(job, request=request))
        bad = state.create('project', url=bad_url, interval=MINUTE, xpath='//*[@id="price"]',
                           now=100, job_id='000000000001')
        good = state.create('project', url=URL, interval=MINUTE, now=100, job_id='000000000002')
        digit_limit = sys.get_int_max_str_digits()
        try:
            sys.set_int_max_str_digits(4300)
            self.assertEqual(state.tick(now=100), [])
        finally:
            sys.set_int_max_str_digits(digit_limit)
        self.assertIn('HTML', state.get('project', bad['id'])['last_error'])
        self.assertEqual(state.get('project', good['id'])['last_price'], '100')

    def test_calendar_month_preserves_day_when_later_month_allows_it(self):
        state = self.setup_checks(("100", "RUB"), ("100", "RUB"), ("100", "RUB"))
        at = lambda month, day: datetime(2024, month, day, 9, 0, tzinfo=UTC).timestamp()
        job = state.create("project", url=URL, interval={"count": 1, "unit": "month"}, now=at(1, 31))
        state.tick(now=at(1, 31))
        self.assertEqual(state.get("project", job["id"])["next_run"], at(2, 29))
        state.tick(now=at(2, 29))
        self.assertEqual(state.get("project", job["id"])["next_run"], at(3, 31))
        state.tick(now=at(5, 1))
        self.assertEqual(state.get("project", job["id"])["next_run"], at(5, 31))
        self.assertEqual(len(self.checked), 3)

    def test_mutations_are_scoped_revision_checked_and_clear_source_baseline(self):
        state = self.setup_checks(("100", "RUB"), ("140", "RUB"), ("10", "RUB"))
        arguments = {"url": URL, "interval": MINUTE, "job_id": "ab12ab12ab12", "now": 100}
        job = state.create("project", **arguments)
        self.assertEqual(job, state.create("project", **arguments))
        self.assertEqual(len(state.list("project")), 1)
        state.tick(now=100)
        paused = state.edit("project", job["id"], 1, enabled=False, now=105)
        self.assertIsNone(paused["next_run"])
        self.assertEqual(state.tick(now=160), [])
        with self.assertRaises(ScheduleConflict):
            state.edit("project", job["id"], 1, enabled=True)
        with self.assertRaises(ScheduleConflict):
            state.delete("another project", job["id"], 2)
        state.edit("project", job["id"], 2, enabled=True, now=170)
        events = state.tick(now=170)
        self.assertEqual(events[0]["direction"], "up")
        changed = state.edit("project", job["id"], 3, url="https://store.example/second-product", now=171)
        self.assertIsNone(changed["last_price"])
        self.assertEqual(state.tick(now=171), [])
        state.delete("project", job["id"], 4)
        self.assertEqual(state.list("project"), [])
        self.assertEqual(state.events("project"), [])

    def test_concurrent_tick_and_late_read_after_disable(self):
        entered = threading.Event()
        release = threading.Event()

        def check(job):
            self.checked.append(job["id"])
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test worker was not released")
            return Price(Decimal(100), "RUB")

        state = Schedules(self.state_dir, check=check)
        job = state.create("project", url=URL, interval=MINUTE, now=100)
        with ThreadPoolExecutor(max_workers=1) as pool:
            outcome = pool.submit(state.tick, now=100, limit=1)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(Schedules(self.state_dir, check=check).tick(now=101), [])
                state.edit("project", job["id"], 1, enabled=False, now=102)
            finally:
                release.set()
            self.assertEqual(outcome.result(timeout=5), [])
        self.assertEqual(self.checked, [job["id"]])
        self.assertIsNone(state.get("project", job["id"])["last_price"])

    def test_detached_worker_singleton_and_idle_exit(self):
        state = Schedules(self.state_dir)
        job = state.create("project", url=URL, interval=MINUTE, now=time.time() + 3600)
        command = [sys.executable, "-m", "folder_memory.schedule_worker", "--state-dir", str(self.state_dir), "--until-idle"]
        with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as worker:
            try:
                deadline = time.monotonic() + 5
                while not state.worker_status()["running"] and time.monotonic() < deadline:
                    time.sleep(0.025)
                self.assertEqual(state.worker_status()["pid"], worker.pid)
                subprocess.run(command, timeout=3, check=True)
                self.assertEqual(state.worker_status()["pid"], worker.pid)
                state.edit("project", job["id"], 1, enabled=False)
                self.assertEqual(worker.wait(timeout=7), 0)
                self.assertFalse(state.worker_status()["running"])
                self.assertIsNone(state.get("project", job["id"])["last_checked"])
            finally:
                if worker.poll() is None:
                    worker.terminate()
                    worker.wait(timeout=7)

    def test_retry_starts_replacement_when_fresh_heartbeat_has_no_lock(self):
        state = Schedules(self.state_dir)
        state.worker_lock_path.touch()
        state.heartbeat(os.getpid())
        self.assertFalse(state.worker_status()["running"])

        with patch("folder_memory.schedule_actions.subprocess.Popen") as launch:
            launch.return_value.poll.return_value = 0
            start_worker(state)
        launch.assert_called_once()

    def test_notification_handles_text_as_data_and_os_failure(self):
        event = {"title": 'Track "price"; <script>', "direction": "down", "previous_price": "10", "price": "5", "currency": "EUR"}
        with patch("folder_memory.schedule_worker.sys.platform", "darwin"), \
                patch("folder_memory.schedule_worker.subprocess.run", return_value=SimpleNamespace(returncode=1)) as run:
            self.assertFalse(notify_desktop(event))
            command = run.call_args.args[0]
            self.assertNotIn(event["title"], command[2])
            self.assertIn(event["title"], command[-1])
            self.assertEqual(run.call_args.kwargs["timeout"], 3)

    def test_live_http_fixture_tracks_public_html_through_same_origin_redirect(self):
        amount = "35.00"
        requests = []

        class Shop(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Host"), self.headers.get("Cookie")))
                if self.path == "/go":
                    self.send_response(302)
                    self.send_header("Location", "/product")
                    self.end_headers()
                    return
                body = product_page(offer(amount), url=origin + "/product").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with ThreadingHTTPServer(("127.0.0.1", 0), Shop) as server:
            origin = f"http://store.example:{server.server_port}"
            serve = threading.Thread(target=server.serve_forever)
            serve.start()
            try:
                def request(args, limits):
                    return _request(args, limits, time.monotonic() + 2)

                def local_fixture(address, timeout, source_address=None):
                    return socket.create_connection(server.server_address, timeout, source_address)

                state = Schedules(self.state_dir, check=lambda job: check_price(job, request=request))
                job = state.create("project", url=origin + "/go", interval=MINUTE, direction="down", now=100)
                with patch("folder_memory.http_client._connect_public", side_effect=local_fixture):
                    self.assertEqual(state.tick(now=100), [])
                    self.assertEqual(state.get("project", job["id"])["last_price"], "35.00")
                    amount = "31.99"
                    events = state.tick(now=160)
                    self.assertEqual([(event["previous_price"], event["price"]) for event in events], [("35.00", "31.99")])
                self.assertEqual([path for path, host, cookie in requests], ["/go", "/product", "/go", "/product"])
                self.assertEqual({(host, cookie) for path, host, cookie in requests}, {(f"store.example:{server.server_port}", None)})
            finally:
                server.shutdown()
                serve.join(3)


if __name__ == "__main__":
    unittest.main()
