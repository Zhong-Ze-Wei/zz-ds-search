"""zz-ds-search 机械结果清理：URL 归一化 + 近似复制检测。

只有机器能确定的事在这里做。相关性、事实冲突、缺口判断一律交回调用模型。
本文件刻意删除了上游 qiaomu-ds-search 的人物简历适配器（BIO_QUERY/GOSSIP/
CLICKBAIT/JOIN/RELEASE 正则）：它们是中文耦合的历史化石，对通用研究无价值。
参考实现：https://github.com/joeseesun/qiaomu-ds-search (MIT)
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, unquote

# 已知的纯跟踪参数。未知参数一律保留——猜测合并会丢证据。
TRACKING = {"fbclid", "gclid", "spm", "ref_src", "ref_url"}
NEGATION = re.compile(r"未|没|不|无|非|否认")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def identity(url: str):
    """返回 (canonical_url, host)；无法安全解析时返回 None。"""
    try:
        p = urlsplit(url)
        if (p.scheme not in ("http", "https") or not p.hostname
                or p.username is not None or p.password is not None
                or any(c.isspace() or ord(c) < 32 for c in url)):
            return None
        port = p.port
    except (ValueError, TypeError):
        return None
    host = p.hostname.lower()
    netloc = "[" + host + "]" if ":" in host else host
    if port is not None and (p.scheme, port) not in (("http", 80), ("https", 443)):
        netloc += ":" + str(port)
    path = p.path or "/"
    # 只处理已知实体 ID / 镜像，不对移动站做通用猜测。
    baidu = re.match(r"^/item/[^/]+/(\d+)/?$", path)
    if host in ("baike.baidu.com", "wapbaike.baidu.com") and baidu:
        return "https://baike.baidu.com/item/" + baidu.group(1), "baike.baidu.com"
    if host in ("wikiwand.com", "www.wikiwand.com") and path.startswith(("/zh-cn/", "/zh/")):
        return "https://zh.wikipedia.org/wiki/" + unquote(path.split("/", 2)[2]), "zh.wikipedia.org"
    if host == "zh.wikipedia.org" and path.startswith("/wiki/") and not p.query:
        return "https://zh.wikipedia.org" + unquote(path), host
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING]
    # 保留 hash 路由和有意义参数（id/page/version）。
    fragment = p.fragment if p.fragment.startswith(("/", "!/")) else ""
    return urlunsplit((p.scheme, netloc, path,
                       urlencode(sorted(query, key=lambda kv: kv[0])), fragment)), host


def fingerprint(row: dict):
    # 只取前缀，让大结果页的处理时间和内存可预测。
    body = (row.get("content") or "")[:24000]
    text = "".join(c for c in normalized(body) if c.isalnum())
    shingles = {text[i:i + 5] for i in range(max(0, len(text) - 4))}
    return (text, tuple(NUMBER.findall(text)), set(NEGATION.findall(text)), shingles)


def near_copy(a, b) -> bool:
    """保守判定：数字顺序或否定词不同就不算同一份证据。"""
    x, y = a[0], b[0]
    if min(len(x), len(y)) < 120 or min(len(x), len(y)) / max(len(x), len(y)) < 0.9:
        return False
    if a[1:3] != b[1:3]:
        return False
    return len(a[3] & b[3]) / max(1, len(a[3] | b[3])) >= 0.94


def process_results(rows, query, exclude_terms=None):
    """机械去重并保留差异版本。语义判断不做，facts 恒为空。"""
    exclude_terms = exclude_terms or []
    kept, prints, duplicates, excluded = [], [], [], []
    for original in rows:
        row = dict(original)
        ids = identity(row.get("url", ""))
        text = normalized(" ".join(row.get(k) or "" for k in ("title", "summary", "snippet", "content")))
        reason = None
        if not ids:
            reason = "invalid_url"
        elif any(normalized(term) in text for term in exclude_terms):
            reason = "excluded_term"
        if reason:
            excluded.append({"url": row.get("url"), "title": row.get("title"), "reason": reason})
            continue
        row["canonical_url"], row["source_family"] = ids
        fp = fingerprint(row)
        match = next((i for i, k in enumerate(kept) if k["canonical_url"] == ids[0]), None)
        method = "canonical_url"
        if match is None:
            match = next((i for i, other in enumerate(prints) if near_copy(fp, other)), None)
            method = "near_identical_content"
        if match is not None:
            representative = kept[match]
            row["source_family"] = representative["source_family"]
            duplicate = {"url": row["url"], "kept_url": representative["url"], "reason": method,
                         "changed_numbers": fp[1] != prints[match][1]}
            # 差异正文必须可查：重复页面可能含矛盾证据。
            fields = ("title", "snippet", "summary", "content", "date", "matched_queries")
            if any(row.get(k) != representative.get(k) for k in ("content", "summary", "snippet")):
                duplicate["variant"] = {k: row[k] for k in fields if k in row}
            queries = list(dict.fromkeys(
                list(representative.get("matched_queries") or []) + list(row.get("matched_queries") or [])))
            if queries:
                representative["matched_queries"] = queries
            duplicates.append(duplicate)
            representative.setdefault("alternates", []).append(duplicate)
        else:
            kept.append(row)
            prints.append(fp)
    return {"results": kept, "quality": {
        "input_count": len(rows), "kept_count": len(kept),
        "duplicate_count": len(duplicates), "excluded_count": len(excluded),
        "source_family_count": len({r["source_family"] for r in kept}),
        "duplicates": duplicates, "excluded": excluded,
        "source_family_note": "保守 URL/正文分组；不同域名不代表独立信源。",
        "semantic_judgment": "由调用模型完成：相关性、覆盖度、事实冲突。",
    }}
