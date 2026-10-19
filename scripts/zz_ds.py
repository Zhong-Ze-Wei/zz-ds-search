#!/usr/bin/env python3
"""zz-ds-search CLI: DeepSeek official Search / Fetch plus a direct-fetch fallback."""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import random
import re
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from quality import process_results

VERSION = "1.0.0"
API_BASE = "https://api.deepseek.com/v1/tools"
KEYS_URL = "https://platform.deepseek.com/api_keys"
DOCS_URL = "https://api-docs.deepseek.com/guides/search/"
APP_DIR = "zz-ds-search"
LEGACY_DIR = "qiaomu-ds-search"
MAX_QUERY_CHARS = 128
MAX_BYTES = 16 * 1024 * 1024
SOFT_BUDGET = {"search": 8, "fetch": 5, "open": 5}


class SearchError(Exception):
    def __init__(self, message, code="request_failed", status=None):
        super().__init__(message)
        self.code = code
        self.status = status


def _atomic_write_json(path, data, prefix):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=prefix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
        if os.name == "posix":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


# ---------------------------------------------------------------- credentials

def _config_base():
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    if not base.is_absolute():
        raise SearchError("XDG_CONFIG_HOME must be an absolute path.", "invalid_config")
    return base


def config_path():
    return _config_base() / APP_DIR / "credentials.json"


def state_path():
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    if not base.is_absolute():
        raise SearchError("XDG_STATE_HOME must be an absolute path.", "invalid_config")
    return base / APP_DIR / "usage.json"


def legacy_config_path():
    return _config_base() / LEGACY_DIR / "credentials.json"


def validate_key(key):
    key = (key or "").strip()
    if not key or any(c.isspace() for c in key) or not key.isascii():
        raise SearchError("API Key is empty or contains illegal whitespace/characters.",
                          "invalid_key")
    return key


def _reject_symlink(path, label):
    if path.is_symlink() or path.parent.is_symlink():
        raise SearchError(label + " or its directory must not be a symlink.", "unsafe_config")


def _read_private_json(path, label):
    _reject_symlink(path, label)
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SearchError(label + " permissions are too open; run chmod 600 on it.",
                          "unsafe_config")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SearchError(label + " is corrupt; run setup again.", "invalid_config") from None


def load_key():
    """Environment wins, then the private config file; legacy keys migrate."""
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return validate_key(env), "environment"
    path = config_path()
    if not path.exists():
        legacy = legacy_config_path()
        if legacy.exists() and not legacy.is_symlink():
            data = _read_private_json(legacy, "Legacy credential file")
            if isinstance(data, dict) and data.get("api_key"):
                save_key(data.get("api_key"))
                return validate_key(data.get("api_key")), "migrated_from_qiaomu"
        raise SearchError("No API key configured yet. Run setup to create one: " + KEYS_URL,
                          "missing_api_key")
    data = _read_private_json(path, "Credential file")
    if not isinstance(data, dict):
        raise SearchError("Credential file is corrupt; run setup again.", "invalid_config")
    return validate_key(data.get("api_key", "")), "config_file"


def save_key(key):
    key = validate_key(key)
    path = config_path()
    _reject_symlink(path, "Credential file")
    _atomic_write_json(path, {"api_key": key}, ".credentials-")
    return path


# ------------------------------------------------------- usage counter (soft)

def _load_usage():
    path = state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_usage(operation, requests=1, queries=None):
    data = _load_usage()
    tally = data.get("operations")
    if not isinstance(tally, dict):
        tally = {}
        data["operations"] = tally
    entry = tally.get(operation)
    if not isinstance(entry, dict):
        entry = {}
        tally[operation] = entry
    entry["requests"] = int(entry.get("requests", 0)) + requests
    entry["last_at"] = datetime.now(timezone.utc).isoformat()
    if queries:
        seen = entry.get("queries")
        if not isinstance(seen, list):
            seen = []
            entry["queries"] = seen
        for query in queries:
            if query not in seen:
                seen.append(query)
    soft = dict(SOFT_BUDGET)
    overrides = data.get("budget_overrides")
    if isinstance(overrides, dict):
        soft.update(overrides)
    data["updated_at"] = entry["last_at"]
    try:
        _atomic_write_json(state_path(), data, ".usage-")
    except OSError:
        pass
    used = entry["requests"]
    limit = soft.get(operation)
    advisory = None
    if isinstance(limit, int) and used > limit:
        advisory = ("This task has used " + str(used) + " " + operation + " request(s), above "
                    "the suggested " + str(limit) + ". Continue only if more evidence is "
                    "genuinely needed; run reset when the task is done.")
    return {"operation": operation, "used": used, "soft_limit": limit,
            "remaining": None if limit is None else max(0, limit - used),
            "enforced": False, "advisory": advisory}


def reset_usage():
    data = _load_usage()
    data["operations"] = {}
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_write_json(state_path(), data, ".usage-")
    return {"ok": True, "reset": True}


# ------------------------------------------------------------------ payloads

def validate_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise SearchError(field + " must not be empty.", "invalid_input")
    if len(value) > MAX_QUERY_CHARS:
        raise SearchError(field + " has " + str(len(value)) + " characters; the API limit is "
                          + str(MAX_QUERY_CHARS) + ". Split it into shorter queries instead of "
                          "truncating it.", "invalid_input")
    return value


def search_payload(query, count=5, need_content=False, ori_question=None, localization=None):
    data = {"query": validate_text(query, "query"), "count": count, "need_content": need_content}
    if type(count) is not int or not 1 <= count <= 50:
        raise SearchError("count must be an integer in 1..50.", "invalid_input")
    if ori_question is not None:
        data["ori_question"] = validate_text(ori_question, "ori_question")
    if localization is not None:
        try:
            ipaddress.ip_address(localization)
        except ValueError:
            raise SearchError("localization must be an IP address, not a country name.",
                              "invalid_input") from None
        data["localization"] = localization
    return data


def fetch_payload(url):
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = (isinstance(url, str) and len(url) <= 8192
                 and parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not any(c.isspace() or ord(c) < 32 for c in url))
        parsed.port
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise SearchError("URL must be an http(s) address of at most 8192 characters, with a "
                          "host and no embedded credentials.", "invalid_input")
    return {"url": url}


# ------------------------------------------------------------------- network

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def error_for_status(status):
    messages = {
        400: "Invalid request parameters; check query / count / URL.",
        401: "API key invalid or revoked; run setup again.",
        402: "DeepSeek account balance is insufficient; check it on the official site.",
        403: "Fetch was refused by a safety policy; choose another public source.",
        404: "No usable body was returned; the page may be unavailable or gated.",
        408: "Request timed out.",
        413: "Request body too large; shorten the input.",
        429: "Search / Fetch rate limit reached; retry later. Processes share the quota.",
    }
    return SearchError(messages.get(status, "Service unavailable or unexpected HTTP status."),
                       "http_error", status)


def request_api(operation, payload, key, timeout=30, retries=2, opener=None, sleep=time.sleep):
    if operation not in ("search", "fetch"):
        raise SearchError("Unsupported operation.", "invalid_input")
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(
        API_BASE + "/" + operation,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + validate_key(key),
                 "Content-Type": "application/json",
                 "User-Agent": "zz-ds-search/" + VERSION},
        method="POST")
    for attempt in range(retries + 1):
        delay = min(2 ** attempt + random.random(), 10)
        try:
            with opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    raise error_for_status(response.status)
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise SearchError("Response exceeds 16 MiB; reduce count or disable "
                                  "need_content.", "response_too_large")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise SearchError("API returned invalid JSON.", "invalid_response") from None
            validate_response(operation, result)
            return result, attempt + 1
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            exc.close()
            if status not in (408, 429) and not 500 <= status <= 599:
                raise error_for_status(status) from None
            if attempt == retries:
                raise error_for_status(status) from None
            try:
                wait = float(retry_after)
                if wait > 10:
                    raise SearchError("Service asked to wait more than 10s; stopping.",
                                      "retry_later", status)
                delay = max(delay, wait)
            except ValueError:
                pass
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == retries:
                raise SearchError("Network or TLS failure/timeout; check connectivity.",
                                  "network_error") from None
        sleep(delay)


def validate_response(operation, result):
    valid = isinstance(result, dict)
    if operation == "search":
        valid = valid and isinstance(result.get("results"), list)
        if valid:
            valid = all(isinstance(r, dict) and isinstance(r.get("url"), str)
                        for r in result["results"])
    else:
        valid = (valid and isinstance(result.get("title"), str)
                 and isinstance(result.get("content"), str))
    if not valid:
        raise SearchError("Response does not match the official Search / Fetch contract.",
                          "invalid_response")


def envelope(operation, payload, response, attempts, route="deepseek_api"):
    if operation == "search":
        empty = not response.get("results")
    else:
        empty = not (response.get("content") or "").strip()
    return {"ok": True, "provider": "deepseek", "operation": operation, "route": route,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "request": payload, "attempts": attempts, "empty": empty, **response}


# ----------------------------------------------------------------- commands

def cmd_search(args, key):
    payload = search_payload(args.query, args.count, args.need_content,
                             args.ori_question, args.localization)
    response, attempts = request_api("search", payload, key, args.timeout, args.retries)
    if not args.raw:
        response = {**response,
                    **process_results(response["results"], args.query,
                                      exclude_terms=args.exclude_term)}
    result = envelope("search", payload, response, attempts)
    result["usage"] = record_usage("search", 1, [args.query])
    return result


def cmd_research(args, key):
    plan = []
    for value in [args.query] + list(args.expansions or []):
        value = validate_text(value, "query").strip()
        if value not in plan:
            plan.append(value)
    limit = args.max_searches
    if type(limit) is not int or not 1 <= limit <= 8:
        raise SearchError("--max-searches must be an integer in 1..8.", "invalid_input")
    rows, searches, errors = [], [], []
    for index, current in enumerate(plan[:limit]):
        current_payload = search_payload(current, args.count, True)
        purpose = "original" if index == 0 else "agent_expansion"
        try:
            response, attempts = request_api("search", current_payload, key,
                                             args.timeout, args.retries)
        except SearchError as exc:
            if not searches:
                raise
            errors.append({"query": current, "purpose": purpose, "code": exc.code,
                           "message": str(exc), "http_status": exc.status})
            break
        searches.append({"query": current, "purpose": purpose, "attempts": attempts,
                         "result_count": len(response["results"])})
        rows.extend({**r, "matched_queries": [current]} for r in response["results"])
    reviewed = process_results(rows, args.query, exclude_terms=args.exclude_term)
    result = {**envelope("search", {"query": args.query}, reviewed,
                         sum(s["attempts"] for s in searches)),
              "operation": "research", "ok": not errors, "complete": not errors,
              "searches": searches, "errors": errors,
              "planning": {"owner": "calling_agent", "automatic_query_expansion": False,
                           "planned_queries": plan,
                           "executed_queries": [s["query"] for s in searches],
                           "remaining_queries": plan[len(searches):],
                           "stop_reason": "request_failed" if errors else "plan_completed"}}
    result["usage"] = record_usage("search", len(searches) + len(errors), plan)
    return result


def cmd_fetch(args, key):
    payload = fetch_payload(args.url)
    response, attempts = request_api("fetch", payload, key, args.timeout, args.retries)
    result = envelope("fetch", payload, response, attempts)
    result["content_bytes"] = len((response.get("content") or "").encode("utf-8"))
    result["usage"] = record_usage("fetch", 1)
    return result


def _decode_entities(text):
    """Decode named and numeric HTML entities without importing html.parser."""
    named = {"nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"',
             "apos": "'", "hellip": "...", "mdash": "-", "ndash": "-",
             "lsquo": "'", "rsquo": "'", "ldquo": '"', "rdquo": '"'}

    def replace(match):
        body = match.group(1)
        if body.startswith("#x") or body.startswith("#X"):
            try:
                return chr(int(body[2:], 16))
            except (ValueError, OverflowError):
                return match.group(0)
        if body.startswith("#"):
            try:
                return chr(int(body[1:]))
            except (ValueError, OverflowError):
                return match.group(0)
        return named.get(body.lower(), match.group(0))

    text = re.sub(r"&(#?[xX]?[0-9a-zA-Z]+);", replace, text)
    return text


def _html_to_text(html):
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|table|blockquote)>", "\n\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = _decode_entities(html)
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in html.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def cmd_open(args):
    fetch_payload(args.url)
    request = urllib.request.Request(args.url, headers={
        "User-Agent": "zz-ds-search/" + VERSION + " (+direct-fetch)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.1"})
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            content_type = (response.headers.get("Content-Type") or "").lower()
            if "text/html" not in content_type and "text/plain" not in content_type:
                raise SearchError("Direct fetch does not support content type '"
                                  + (content_type or "unknown")
                                  + "'; use fetch for Markdown instead.",
                                  "unsupported_content")
            raw = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise error_for_status(status) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SearchError("Direct fetch failed: network or TLS problem.",
                          "network_error") from None
    if len(raw) > MAX_BYTES:
        raise SearchError("Page exceeds 16 MiB; use fetch or narrow the target.",
                          "response_too_large")
    charset = "utf-8"
    match = re.search(r"charset=([\w-]+)", content_type)
    if match:
        charset = match.group(1)
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    title, body = "", text
    if "text/html" in content_type:
        tm = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        title = _decode_entities(re.sub(r"\s+", " ", tm.group(1))).strip() if tm else ""
        body = _html_to_text(text)
    result = {"ok": True, "provider": "direct", "operation": "open", "route": "direct_fetch",
              "retrieved_at": datetime.now(timezone.utc).isoformat(),
              "request": {"url": args.url}, "attempts": 1, "empty": not body.strip(),
              "title": title, "content": body,
              "content_bytes": len(body.encode("utf-8")),
              "evidence_note": ("Direct fetch, not DeepSeek Fetch. Treat as supporting "
                                "evidence and confirm wording against the original page.")}
    result["usage"] = record_usage("open", 1)
    return result


def read_result_file(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise SearchError("Result file exceeds 16 MiB; narrow the range.", "invalid_input")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise SearchError("Result file is not valid JSON.", "invalid_input") from None


def cmd_merge(args):
    if len(args.files) > 8:
        raise SearchError("merge reads at most 8 result files.", "invalid_input")
    rows, upstream = [], []
    for path in args.files:
        document = read_result_file(path)
        validate_response("search", document)
        upstream.append({"file": str(path),
                         "complete": document.get("upstream_complete",
                                                  document.get("complete",
                                                               document.get("ok", True))),
                         "searches": document.get("searches", []),
                         "errors": document.get("errors", [])})
        for item in document["results"]:
            base = {k: v for k, v in item.items() if k != "alternates"}
            rows.append(base)
            for alternate in item.get("alternates") or []:
                if not isinstance(alternate, dict) or not isinstance(alternate.get("url"), str):
                    raise SearchError("Invalid duplicate-source record.", "invalid_input")
                variant = alternate.get("variant") or {}
                if not isinstance(variant, dict):
                    raise SearchError("Invalid source-variant record.", "invalid_input")
                evidence = {k: variant[k] for k in
                            ("title", "snippet", "summary", "content", "date",
                             "matched_queries") if k in variant}
                rows.append({**base, **evidence, "url": alternate["url"]})
    if len(rows) > 400:
        raise SearchError("merge keeps at most 400 source rows.", "invalid_input")
    reviewed = process_results(rows, args.query, exclude_terms=args.exclude_term)
    return {**envelope("search", {"query": args.query}, reviewed, 0),
            "operation": "merge", "network_requests": 0, "upstream": upstream,
            "upstream_complete": all(u["complete"] for u in upstream)}


def cmd_doctor(key, source):
    payload = search_payload("DeepSeek site:api-docs.deepseek.com", count=1)
    response, attempts = request_api("search", payload, key, retries=0)
    nonempty = bool(response["results"])
    return {"ok": nonempty, "configured": True, "credential_source": source,
            "search_verified": nonempty, "http_200": True,
            "result_count": len(response["results"]), "attempts": attempts,
            "message": "Live search verified." if nonempty
                       else "API responded but returned no results; search is not verified."}


def cmd_setup(no_browser=False, guide_only=False):
    print("1. Open the official API keys page: " + KEYS_URL)
    print("2. Sign in, go to API keys, create a key (name it zz-ds-search).")
    print("3. Paste the key only into the hidden prompt below, never into chat or Git.")
    print("4. A live Search runs after saving (may cost money; see " + DOCS_URL + ").")
    print("On 402 the account balance is low; check it on the platform yourself.")
    if not no_browser:
        try:
            opened = webbrowser.open(KEYS_URL)
        except webbrowser.Error:
            opened = False
        if not opened:
            print("Could not open a browser; visit the link above manually.")
    if guide_only:
        return 0
    if not sys.stdin.isatty():
        raise SearchError("Run setup in the user's own visible terminal; keys must not "
                          "arrive via chat, arguments or a pipe.", "interactive_required")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            key = validate_key(getpass.getpass("DeepSeek API Key (hidden input): "))
        except getpass.GetPassWarning:
            raise SearchError("This terminal cannot hide input; use a local terminal.",
                              "interactive_required") from None
    path = save_key(key)
    print("Saved to " + str(path) + " (dir 700 / file 600).")
    if os.environ.get("DEEPSEEK_API_KEY", "").strip():
        print("Note: DEEPSEEK_API_KEY is set and takes precedence over this file.")
    try:
        checked = cmd_doctor(key, "newly_saved_config")
    except SearchError:
        print("Credentials saved, but the live check failed. Fix the error and run doctor; "
              "no need to re-enter the key.")
        raise
    print(json.dumps(checked, ensure_ascii=False, indent=2))
    return 0 if checked["ok"] else 4


def build_parser():
    root = argparse.ArgumentParser(description="DeepSeek official search and page fetch.")
    sub = root.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="guide API key creation, hidden input, save, verify")
    setup.add_argument("--no-browser", action="store_true")
    setup.add_argument("--guide-only", action="store_true")
    sub.add_parser("status", help="check local config only; no network, no key echo")
    sub.add_parser("doctor", help="one live search to verify the key (may cost money)")
    sub.add_parser("reset", help="clear the usage counter for the current task")

    search = sub.add_parser("search", help="search the public web")
    search.add_argument("query")
    search.add_argument("--count", type=int, default=5)
    search.add_argument("--need-content", action="store_true")
    search.add_argument("--ori-question")
    search.add_argument("--localization", help="optional IP; never collected automatically")
    search.add_argument("--raw", action="store_true", help="skip dedupe, return raw API rows")
    search.add_argument("--exclude-term", action="append", default=[])

    research = sub.add_parser("research", help="run the query plan supplied by the model")
    research.add_argument("query")
    research.add_argument("--query", dest="expansions", action="append", default=[],
                          help="model-supplied complementary query; repeatable")
    research.add_argument("--count", type=int, default=5)
    research.add_argument("--max-searches", type=int, default=8)
    research.add_argument("--exclude-term", action="append", default=[])

    fetch = sub.add_parser("fetch", help="fetch a URL as Markdown through DeepSeek")
    fetch.add_argument("url")
    opened = sub.add_parser("open", help="direct-fetch a public page (not via DeepSeek)")
    opened.add_argument("url")

    merge = sub.add_parser("merge", help="merge saved result files offline; no network")
    merge.add_argument("query")
    merge.add_argument("files", nargs="+")
    merge.add_argument("--exclude-term", action="append", default=[])

    for cmd in (search, research, fetch, opened):
        cmd.add_argument("--timeout", type=int, default=30, metavar="1..120")
        cmd.add_argument("--retries", type=int, default=2, metavar="0..2")
    return root


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "setup":
            return cmd_setup(args.no_browser, args.guide_only)
        if args.command == "merge":
            result = cmd_merge(args)
        elif args.command == "reset":
            result = reset_usage()
        else:
            key, source = load_key()
            if args.command == "status":
                result = {"ok": True, "configured": True, "credential_source": source,
                          "search_verified": False,
                          "message": "Credentials found; run doctor to verify live search."}
            elif args.command == "doctor":
                result = cmd_doctor(key, source)
            elif args.command == "search":
                result = cmd_search(args, key)
            elif args.command == "research":
                result = cmd_research(args, key)
            elif args.command == "fetch":
                result = cmd_fetch(args, key)
            elif args.command == "open":
                result = cmd_open(args)
            else:
                raise SearchError("Unknown command.", "invalid_input")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 4
    except SearchError as exc:
        print(json.dumps({"ok": False,
                          "error": {"code": exc.code, "message": str(exc),
                                    "http_status": exc.status}},
                         ensure_ascii=False), file=sys.stderr)
        return 2 if exc.code == "missing_api_key" else 1
    except (KeyboardInterrupt, EOFError):
        print("Cancelled.", file=sys.stderr)
        return 130
    except OSError:
        print("local io error", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
