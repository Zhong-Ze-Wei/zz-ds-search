"""zz-ds-search 测试：核心命令、凭据、重试、去重与软预算。"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("zz_ds", ROOT / "scripts" / "zz_ds.py")
zz = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zz)

KEY = "test-secret-not-a-real-key"
RESULT = {"results": [{"title": "Official docs", "url": "https://api-docs.deepseek.com/",
                       "snippet": "Search API", "summary": "API evidence", "content": "",
                       "date": None, "authority": -1, "rerank_score": 1.5}]}


def fake_response(body):
    result = Mock()
    result.status = 200
    result.read.return_value = json.dumps(body).encode()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def http_error(status, retry_after=None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return urllib.error.HTTPError(zz.API_BASE, status, "error", headers,
                                  io.BytesIO(KEY.encode()))


class IsolationMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "XDG_CONFIG_HOME": os.path.join(self.tmp.name, "config"),
            "XDG_STATE_HOME": os.path.join(self.tmp.name, "state"),
            "DEEPSEEK_API_KEY": "",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()


class InputTests(unittest.TestCase):
    def test_operators_preserved_and_length_limit(self):
        query = 'model "search" site:a.com OR site:b.com lang:zh,en after:2026'
        self.assertEqual(zz.search_payload(query)["query"], query)
        self.assertEqual(len(zz.search_payload("x" * 128)["query"]), 128)
        for bad in ("x" * 129, " ", "", None):
            with self.assertRaises(zz.SearchError):
                zz.search_payload(bad)

    def test_count_and_optional_fields(self):
        for bad in (0, 51, True):
            with self.assertRaises(zz.SearchError):
                zz.search_payload("q", bad)
        self.assertEqual(zz.search_payload("q", 50)["count"], 50)
        with self.assertRaises(zz.SearchError):
            zz.search_payload("q", ori_question="x" * 129)
        self.assertNotIn("localization", zz.search_payload("q"))
        self.assertEqual(zz.search_payload("q", localization="::1")["localization"], "::1")
        with self.assertRaises(zz.SearchError):
            zz.search_payload("q", localization="China")

    def test_fetch_rejects_unsafe_urls(self):
        for url in ("file:///etc/passwd", "https://u:p@example.com", "https://@example.com",
                    "https:///oops", "https://example.com:bad", "https://example.com/\n",
                    "x" * 8193):
            with self.subTest(url=str(url)[:40]), self.assertRaises(zz.SearchError):
                zz.fetch_payload(url)
        self.assertEqual(zz.fetch_payload("https://e.com/a?b=1")["url"], "https://e.com/a?b=1")


class NetworkTests(unittest.TestCase):
    def test_search_body_has_no_model(self):
        opener = Mock()
        opener.open.return_value = fake_response(RESULT)
        data, attempts = zz.request_api("search", zz.search_payload("t"), KEY, opener=opener)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertNotIn("model", json.loads(request.data))
        self.assertEqual(data, RESULT)
        self.assertEqual(attempts, 1)

    def test_fetch_endpoint_and_markdown(self):
        opener = Mock()
        opener.open.return_value = fake_response({"title": "T", "content": "# T\n\nbody"})
        data, _ = zz.request_api("fetch", zz.fetch_payload("https://e.com"), KEY, opener=opener)
        self.assertEqual(data["content"], "# T\n\nbody")
        self.assertTrue(opener.open.call_args.args[0].full_url.endswith("/fetch"))

    def test_empty_search_is_not_error(self):
        zz.validate_response("search", {"results": []})
        data = zz.envelope("search", {"query": "x"}, {"results": []}, 1)
        self.assertTrue(data["ok"])
        self.assertTrue(data["empty"])

    def test_null_date_preserved(self):
        self.assertIsNone(zz.envelope("search", {}, RESULT, 1)["results"][0]["date"])

    def test_permanent_errors_do_not_retry_or_leak(self):
        for code in (400, 401, 402, 403, 404, 413, 302):
            opener, sleep = Mock(), Mock()
            opener.open.side_effect = http_error(code)
            with self.subTest(code=code), self.assertRaises(zz.SearchError) as caught:
                zz.request_api("search", {}, KEY, opener=opener, sleep=sleep)
            self.assertEqual(caught.exception.status, code)
            self.assertNotIn(KEY, str(caught.exception))
            self.assertEqual(opener.open.call_count, 1)
            sleep.assert_not_called()

    def test_retryable_errors_bounded(self):
        for code in (408, 429, 500, 504):
            opener, sleep = Mock(), Mock()
            opener.open.side_effect = [http_error(code), http_error(code), fake_response(RESULT)]
            _, attempts = zz.request_api("search", {}, KEY, opener=opener, sleep=sleep)
            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)

    def test_retry_after_too_long_stops(self):
        opener, sleep = Mock(), Mock()
        opener.open.side_effect = http_error(429, "30")
        with self.assertRaises(zz.SearchError) as caught:
            zz.request_api("search", {}, KEY, opener=opener, sleep=sleep)
        self.assertEqual(caught.exception.code, "retry_later")
        sleep.assert_not_called()

    def test_network_failure_does_not_leak_key(self):
        opener, sleep = Mock(), Mock()
        opener.open.side_effect = urllib.error.URLError("boom " + KEY)
        with self.assertRaises(zz.SearchError) as caught:
            zz.request_api("search", {}, KEY, opener=opener, sleep=sleep)
        self.assertEqual(opener.open.call_count, 3)
        self.assertNotIn(KEY, str(caught.exception))

    def test_malformed_response_is_loud(self):
        for operation, data in (("search", {}), ("search", {"results": [{}]}),
                                ("search", {"results": None}), ("fetch", {"title": "x"})):
            with self.assertRaises(zz.SearchError):
                zz.validate_response(operation, data)

    def test_redirect_never_forwards_bearer(self):
        self.assertIsNone(zz.NoRedirect().redirect_request(None, None, 302, "", {},
                                                          "https://evil.example"))

    def test_doctor_rejects_empty_as_success(self):
        with patch.object(zz, "request_api", return_value=({"results": []}, 1)):
            result = zz.cmd_doctor(KEY, "test")
        self.assertFalse(result["search_verified"])
        self.assertTrue(result["http_200"])


class CredentialTests(IsolationMixin, unittest.TestCase):
    def test_private_atomic_save_and_env_priority(self):
        path = zz.save_key(KEY)
        self.assertEqual(zz.load_key()[0], KEY)
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-key"}):
            self.assertEqual(zz.load_key(), ("env-key", "environment"))

    def test_legacy_key_migrated(self):
        legacy = zz.legacy_config_path()
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({"api_key": KEY}))
        os.chmod(legacy, 0o600)
        key, source = zz.load_key()
        self.assertEqual(key, KEY)
        self.assertEqual(source, "migrated_from_qiaomu")
        self.assertTrue(zz.config_path().exists())

    def test_symlinked_config_refused(self):
        target = Path(self.tmp.name) / "keep.txt"
        target.write_text("keep")
        path = zz.config_path()
        path.parent.mkdir(parents=True)
        path.symlink_to(target)
        with self.assertRaises(zz.SearchError):
            zz.save_key(KEY)
        with self.assertRaises(zz.SearchError):
            zz.load_key()
        self.assertEqual(target.read_text(), "keep")

    @unittest.skipUnless(os.name == "posix", "POSIX permission contract")
    def test_insecure_permissions_refused(self):
        path = zz.save_key(KEY)
        path.chmod(0o644)
        with self.assertRaises(zz.SearchError):
            zz.load_key()

    def test_missing_key_is_actionable_and_offline(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), patch.object(zz, "request_api") as request:
            code = zz.main(["search", "test"])
        self.assertEqual(code, 2)
        self.assertIn(zz.KEYS_URL, errors.getvalue())
        request.assert_not_called()

    def test_status_lists_nothing_sensitive(self):
        zz.save_key(KEY)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(zz.main(["status"]), 0)
        self.assertNotIn(KEY, out.getvalue())
        self.assertFalse(json.loads(out.getvalue())["search_verified"])

    def test_setup_refuses_non_interactive(self):
        with patch.object(zz.sys.stdin, "isatty", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(zz.SearchError):
                zz.cmd_setup(no_browser=True)
        self.assertFalse(zz.config_path().exists())

    def test_setup_saves_but_reports_failure(self):
        output = io.StringIO()
        with patch.object(zz.sys.stdin, "isatty", return_value=True), \
                patch.object(zz.getpass, "getpass", return_value=KEY), \
                patch.object(zz, "cmd_doctor", side_effect=zz.error_for_status(402)), \
                contextlib.redirect_stdout(output):
            with self.assertRaises(zz.SearchError):
                zz.cmd_setup(no_browser=True)
        self.assertEqual(zz.load_key()[0], KEY)
        self.assertIn("live check failed", output.getvalue())
        self.assertNotIn(KEY, output.getvalue())

    def test_guide_only_saves_nothing(self):
        with patch.object(zz.webbrowser, "open", return_value=True) as browser, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(zz.cmd_setup(guide_only=True), 0)
        browser.assert_called_once_with(zz.KEYS_URL)
        self.assertFalse(zz.config_path().exists())


class UsageTests(IsolationMixin, unittest.TestCase):
    def test_counter_accumulates_without_blocking(self):
        first = zz.record_usage("search", 1, ["a"])
        self.assertEqual(first["used"], 1)
        self.assertIsNone(first["advisory"])
        for _ in range(10):
            last = zz.record_usage("search", 1)
        self.assertEqual(last["used"], 11)
        self.assertFalse(last["enforced"])
        self.assertIsNotNone(last["advisory"])
        self.assertGreater(last["used"], last["soft_limit"])

    def test_reset_clears_counter(self):
        zz.record_usage("fetch", 3)
        zz.reset_usage()
        self.assertEqual(zz.record_usage("fetch", 1)["used"], 1)

    def test_budget_overrides_respected(self):
        path = zz.state_path()
        zz._atomic_write_json(path, {"operations": {}, "budget_overrides": {"search": 100}}, ".u-")
        self.assertEqual(zz.record_usage("search", 1)["soft_limit"], 100)

    def test_corrupt_state_is_tolerated(self):
        path = zz.state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        self.assertEqual(zz.record_usage("search", 1)["used"], 1)


if __name__ == "__main__":
    unittest.main()
