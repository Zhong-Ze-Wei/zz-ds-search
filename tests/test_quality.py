"""机械去重与多批次合并的行为测试。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("quality", ROOT / "scripts" / "quality.py")
quality = importlib.util.module_from_spec(spec)
spec.loader.exec_module(quality)


def row(url, content="", title="t", **extra):
    base = {"url": url, "title": title, "snippet": "", "summary": "", "content": content}
    base.update(extra)
    return base


class IdentityTests(unittest.TestCase):
    def test_tracking_params_dropped(self):
        canon, host = quality.identity("https://e.com/a?utm_source=x&id=7#frag")
        self.assertEqual(canon, "https://e.com/a?id=7")
        self.assertEqual(host, "e.com")

    def test_hash_router_preserved(self):
        canon, _ = quality.identity("https://e.com/app#/route/1")
        self.assertEqual(canon, "https://e.com/app#/route/1")

    def test_unknown_params_kept(self):
        canon, _ = quality.identity("https://e.com/p?weird=1")
        self.assertIn("weird=1", canon)

    def test_baidu_baike_entity_merge(self):
        a, _ = quality.identity("https://baike.baidu.com/item/abc/123")
        b, _ = quality.identity("https://wapbaike.baidu.com/item/other/123/")
        self.assertEqual(a, b)

    def test_invalid_urls_rejected(self):
        for url in ("", "javascript:alert(1)", "https://u:p@e.com", "https://e.com/\n"):
            self.assertIsNone(quality.identity(url))


class DedupeTests(unittest.TestCase):
    def test_same_url_merged_and_variant_kept(self):
        body = "A" * 400
        other = body[:-5] + "BBBBB"
        result = quality.process_results([row("https://e.com/x", body),
                                          row("https://e.com/x?utm_source=z", other)], "q")
        self.assertEqual(result["quality"]["kept_count"], 1)
        self.assertEqual(result["quality"]["duplicate_count"], 1)
        duplicate = result["quality"]["duplicates"][0]
        self.assertEqual(duplicate["reason"], "canonical_url")
        self.assertIn("variant", duplicate)

    def test_near_identical_content_merged(self):
        body = "这是一个用于测试近似复制检测的较长正文。" * 40
        result = quality.process_results([row("https://a.com/1", body),
                                          row("https://b.com/2", body)], "q")
        self.assertEqual(result["quality"]["kept_count"], 1)
        self.assertEqual(result["quality"]["duplicates"][0]["reason"], "near_identical_content")

    def test_changed_numbers_not_merged(self):
        body = "这份报告统计了 2023 年的数据。" * 30
        changed = "这份报告统计了 2024 年的数据。" * 30
        result = quality.process_results([row("https://a.com/1", body),
                                          row("https://b.com/2", changed)], "q")
        self.assertEqual(result["quality"]["kept_count"], 2)

    def test_exclude_terms_are_reported(self):
        result = quality.process_results([row("https://e.com/x", "内容", title="广告页")], "q",
                                         exclude_terms=["广告"])
        self.assertEqual(result["quality"]["kept_count"], 0)
        self.assertEqual(result["quality"]["excluded"][0]["reason"], "excluded_term")

    def test_matched_queries_union(self):
        body = "x" * 300
        result = quality.process_results(
            [row("https://e.com/a", body, matched_queries=["q1"]),
             row("https://e.com/a", body, matched_queries=["q2"])], "q")
        self.assertEqual(result["results"][0]["matched_queries"], ["q1", "q2"])

    def test_no_semantic_judgment_fields(self):
        result = quality.process_results([row("https://e.com/a", "x" * 200)], "q")
        self.assertNotIn("facts", result)
        self.assertNotIn("conflicts", result)


class MergeTests(unittest.TestCase):
    def _write(self, directory, name, payload):
        path = Path(directory) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_merge_is_offline_and_keeps_upstream(self):
        import importlib.util as iu
        spec = iu.spec_from_file_location("zz_ds", ROOT / "scripts" / "zz_ds.py")
        zz = iu.module_from_spec(spec)
        spec.loader.exec_module(zz)
        with tempfile.TemporaryDirectory() as tmp:
            a = self._write(tmp, "a.json", {"ok": True, "results": [row("https://e.com/a", "x" * 300)]})
            b = self._write(tmp, "b.json", {"ok": False, "results": [row("https://e.com/b", "y" * 300)]})
            args = zz.build_parser().parse_args(["merge", "q", a, b])
            result = zz.cmd_merge(args)
        self.assertEqual(result["operation"], "merge")
        self.assertEqual(result["network_requests"], 0)
        self.assertFalse(result["upstream_complete"])
        self.assertEqual(result["quality"]["kept_count"], 2)


if __name__ == "__main__":
    unittest.main()
