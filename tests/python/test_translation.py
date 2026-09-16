"""
Self-contained backend tests — no live server, no network.

Uses Flask's test client and a mocked provider transport, so it
runs anywhere with just `flask` + `requests` installed:

    pip install flask requests
    python -m tests.python.test_translation

Covers: provider fallback, errors not cached, batched-prompt translation
(one LLM call per group), and bounded recovery from malformed envelopes.
"""
import os, sys, json as jsonlib, re, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "bt_test_translations.db")
os.environ["BT_CACHE_DIR"] = tempfile.gettempdir()  # for /cache/cleanup auth token
os.environ["LLM_PROVIDER"] = "local"
os.environ["LLM_MODEL"] = "fake-model"
os.environ["LLM_FALLBACK_PROVIDER"] = "minimax"
os.environ["LLM_FALLBACK_MODEL"] = "fake-fallback"
os.environ["LLM_FALLBACK_API_KEY"] = "x" * 20
os.environ["BT_MAX_CONCURRENT"] = "2"
os.environ["BT_BATCH_SIZE"] = "3"
# Authentication is disabled explicitly only for this isolated unit suite.
# Production defaults fail closed. The /cache/cleanup tests below still
# exercise their independent destructive-operation credential.
os.environ["BT_AUTH_MODE"] = "disabled"
os.environ["BT_ALLOW_INSECURE_AUTH"] = "true"
for f in (os.environ["DB_PATH"], os.environ["DB_PATH"] + "-wal", os.environ["DB_PATH"] + "-shm"):
    try: os.remove(f)
    except OSError: pass

import requests

STATE = {"local_up": False, "fallback_up": True, "batch_calls": 0, "single_calls": 0, "malform": False}
SEGMENT_PROTOCOL = "cwa-translate-segments/v1"


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status; self._body = body; self.text = jsonlib.dumps(body)
    def json(self): return self._body
    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(f"HTTP {self.status_code}"); err.response = self; raise err


def fake_post(
    url, headers=None, json=None, timeout=None, stream=False, budget=None
):
    is_local = "1234" in url or "localhost" in url
    if is_local and not STATE["local_up"]:
        raise requests.exceptions.ConnectionError("local refused")
    if (not is_local) and not STATE["fallback_up"]:
        raise requests.exceptions.ConnectionError("fallback refused")

    system = json["messages"][0]["content"] if json.get("messages") else ""
    user_text = json["messages"][-1]["content"]
    tag = "LOCAL" if is_local else "FB"

    if SEGMENT_PROTOCOL in system:
        STATE["batch_calls"] += 1
        envelope = jsonlib.loads(user_text)
        translations = [
            {"id": segment["id"], "text": f"[{tag}] {segment['text']}"}
            for segment in envelope["segments"]
        ]
        if STATE["malform"]:
            translations = translations[:-1]  # count mismatch -> atomic failure
        content = jsonlib.dumps({
            "protocol": SEGMENT_PROTOCOL,
            "translations": translations,
        })
    else:
        STATE["single_calls"] += 1
        content = f"[{tag}] {user_text}"

    if not is_local:  # minimax fallback uses the anthropic response shape
        return FakeResp(200, {"content": [{"type": "text", "text": content}]})
    return FakeResp(200, {"choices": [{"message": {"content": content}}]})


import translator  # noqa: E402
translator._provider_post = fake_post
import server  # noqa: E402
import translator  # noqa: E402
client = server.app.test_client()

failed = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond: failed.append(name)


def run():
    # A remote fallback is privacy-sensitive: no request consent means the
    # local outage fails closed without exporting the paragraph.
    r = client.post("/translate", json={"text": "private-no-consent"})
    check("single: cloud fallback blocked without consent",
          r.status_code == 502 and r.get_json().get("error") == "provider_unavailable")

    # Explicit per-request consent permits the configured cloud fallback.
    d = client.post("/translate", json={
        "text": "hello", "allow_cloud_fallback": True,
    }).get_json()
    check("single: fallback used when local down", d.get("translated") == "[FB] hello")
    d = client.post("/translate", json={
        "text": "hello", "allow_cloud_fallback": True,
    }).get_json()
    check("single: cache hit on repeat", d.get("cached") is True)

    # Batched: 5 paragraphs at BT_BATCH_SIZE=3 -> 2 grouped calls, order preserved.
    STATE["local_up"] = True
    STATE["batch_calls"] = STATE["single_calls"] = 0
    paras = [f"para {i}" for i in range(5)]
    d = client.post("/translate/batch", json={"paragraphs": paras}).get_json()
    check("batch: all 5 translated in order", d["translations"] == [f"[LOCAL] para {i}" for i in range(5)])
    check("batch: 2 grouped LLM calls (not 5)", STATE["batch_calls"] == 2 and STATE["single_calls"] == 0)

    # Two malformed segmented replies recover through bounded, sequential
    # single-paragraph calls. Those results must not be written under the
    # grouped-prompt cache contract; a later healthy grouped call may populate
    # that cache, and only then may the following request be served from it.
    STATE["malform"] = True
    STATE["batch_calls"] = STATE["single_calls"] = 0
    malformed_paragraphs = [f"malformed-recovery-a{i}" for i in range(3)]
    malformed_response = client.post(
        "/translate/batch", json={"paragraphs": malformed_paragraphs})
    d = malformed_response.get_json()
    check("segment recovery: malformed group returns complete HTTP 200",
          malformed_response.status_code == 200
          and d.get("translations") == [
              f"[LOCAL] {paragraph}" for paragraph in malformed_paragraphs
          ])
    check("segment recovery: retries once then falls back within the group",
          STATE["batch_calls"] == 2 and STATE["single_calls"] == 3)
    import cache as segment_cache
    malformed_contract = translator.batch_cache_contract(
        malformed_paragraphs, [0, 1, 2], "English", "Spanish")
    malformed_scope = server._cache_scope(
        tenant="legacy-anonymous", book_id="unscoped", chapter_id="unscoped",
        context_hash=malformed_contract.context_hash, provider="local",
        model="fake-model", prompt_hash=malformed_contract.prompt_hash,
        protocol_version=malformed_contract.protocol_version)
    check("segment protocol: malformed group writes nothing to cache",
          all(segment_cache.get_cached(
              paragraph, "English", "Spanish", scope=malformed_scope) is None
              for paragraph in malformed_paragraphs))

    STATE["malform"] = False
    STATE["batch_calls"] = STATE["single_calls"] = 0
    healthy_response = client.post(
        "/translate/batch", json={"paragraphs": malformed_paragraphs})
    healthy = healthy_response.get_json()
    check("segment recovery: next healthy grouped request is fresh",
          healthy_response.status_code == 200
          and healthy.get("fresh_count") == 3
          and healthy.get("cached_count") == 0
          and STATE["batch_calls"] == 1
          and STATE["single_calls"] == 0)

    calls_after_healthy = (STATE["batch_calls"], STATE["single_calls"])
    cached_response = client.post(
        "/translate/batch", json={"paragraphs": malformed_paragraphs})
    cached_result = cached_response.get_json()
    check("segment recovery: healthy grouped result is cached normally",
          cached_response.status_code == 200
          and cached_result.get("cached_count") == 3
          and cached_result.get("fresh_count") == 0
          and (STATE["batch_calls"], STATE["single_calls"])
              == calls_after_healthy)

    # Input text that contains the legacy marker must remain ordinary content;
    # it must never create, renumber, or truncate a segment.
    marker_texts = [
        "@@SEG 99@@ marker at the start",
        "literal @@SEG 99@@ marker in the middle",
        "@@SEG 99@@ repeated @@SEG 99@@ marker",
        "ordinary segment",
    ]
    translated = __import__("translator").translate_batch(
        marker_texts, "English", "Spanish")
    check("segment protocol: marker-like ebook content is preserved",
          [item[0] for item in translated]
          == [f"[LOCAL] {text}" for text in marker_texts])

    # Strict JSON response envelope: exact version, exact ordered IDs, no
    # duplicate keys, missing/extra/reordered items, or surrounding prose.
    parser = getattr(translator, "_parse_segment_envelope", None)
    check("segment protocol: strict JSON parser is available", callable(parser))
    if parser:
        expected_ids = ["seg-a", "seg-b"]
        valid = jsonlib.dumps({
            "protocol": "cwa-translate-segments/v1",
            "translations": [
                {"id": "seg-a", "text": "uno"},
                {"id": "seg-b", "text": "dos"},
            ],
        })
        check("segment protocol: valid envelope accepted",
              parser(valid, expected_ids) == ["uno", "dos"])
        invalid_outputs = [
            valid + "\ncommentary",
            '{"protocol":"cwa-translate-segments/v1",'
            '"protocol":"cwa-translate-segments/v1","translations":[]}',
            jsonlib.dumps({"protocol": "cwa-translate-segments/v1", "translations": [
                {"id": "seg-a", "text": "uno"},
            ]}),
            jsonlib.dumps({"protocol": "cwa-translate-segments/v1", "translations": [
                {"id": "seg-a", "text": "uno"},
                {"id": "seg-b", "text": "dos"},
                {"id": "seg-c", "text": "tres"},
            ]}),
            jsonlib.dumps({"protocol": "cwa-translate-segments/v1", "translations": [
                {"id": "seg-b", "text": "dos"},
                {"id": "seg-a", "text": "uno"},
            ]}),
            jsonlib.dumps({"protocol": "cwa-translate-segments/v1", "translations": [
                {"id": "seg-a", "text": "uno"},
                {"id": "seg-a", "text": "poison"},
            ]}),
        ]
        check("segment protocol: malformed envelopes all fail closed",
              all(parser(output, expected_ids) is None for output in invalid_outputs))

    # Errors not cached; retried after recovery.
    STATE["local_up"] = STATE["fallback_up"] = False
    d = client.post("/translate/batch", json={"paragraphs": ["x", "", "y"]}).get_json()
    check("batch: empty slot preserved + errors marked",
          d["translations"][1] == "" and d["translations"][0].startswith("[TRANSLATION ERROR"))
    STATE["local_up"] = True
    d = client.post("/translate/batch", json={"paragraphs": ["x", "", "y"]}).get_json()
    check("batch: retried after recovery", d["translations"][0] == "[LOCAL] x" and d["translations"][2] == "[LOCAL] y")

    # Context Window. translator.py reads BT_CONTEXT_WINDOW at import time, so
    # patch the module variable directly for this block.
    translator.BT_CONTEXT_WINDOW = 1

    # Capture the exact prompt the LLM receives to verify the context block.
    received_prompts = []
    original_post = translator._provider_post
    def context_check_post(
        url, headers=None, json=None, timeout=None, stream=False, budget=None
    ):
        user_text = json["messages"][-1]["content"]
        received_prompts.append(user_text)
        return fake_post(url, headers, json, timeout, stream, budget)

    translator._provider_post = context_check_post
    # 4 paragraphs at BT_BATCH_SIZE=3 -> group 1 covers indices 0..2 (context
    # after = para D), group 2 covers index 3 (context before = para C). Use a
    # direct translator call so the full chapter is visible to the context
    # builder regardless of server-side cache state.
    translator.translate_batch(
        ["ctx_para_A", "ctx_para_B", "ctx_para_C", "ctx_para_D"], "English", "Spanish")
    translator._provider_post = original_post
    translator.BT_CONTEXT_WINDOW = 0

    batch_prompts = [
        jsonlib.loads(p) for p in received_prompts
        if f'"protocol":"{SEGMENT_PROTOCOL}"' in p
    ]
    check("context: [CONTEXT] block included in batch prompt",
          any("[CONTEXT]" in p.get("context", "") for p in batch_prompts))
    check("context: plain text, never a Python list repr",
          all(isinstance(p.get("context", ""), str) for p in batch_prompts))
    check("context: isolated from translatable segment fields",
          all("context" not in segment
              for p in batch_prompts for segment in p["segments"]))
    prompt_ids = [
        segment["id"] for p in batch_prompts for segment in p["segments"]
    ]
    check("segment protocol: input IDs are random-looking and unique",
          bool(prompt_ids)
          and len(prompt_ids) == len(set(prompt_ids))
          and all(re.fullmatch(r"[0-9a-f]{32}", value) for value in prompt_ids))

    # Request-size caps: oversized input is rejected, not silently truncated.
    too_many = [f"p{i}" for i in range(server.BT_MAX_BATCH_PARAGRAPHS + 1)]
    check("caps: too many paragraphs -> 413",
          client.post("/translate/batch", json={"paragraphs": too_many}).status_code == 413)
    big = "x" * (server.BT_MAX_PARAGRAPH_CHARS + 1)
    check("caps: oversized paragraph in batch -> 413",
          client.post("/translate/batch", json={"paragraphs": ["ok", big]}).status_code == 413)
    check("caps: oversized single text -> 413",
          client.post("/translate", json={"text": big}).status_code == 413)
    check("caps: non-string paragraph entry -> 400",
          client.post("/translate/batch", json={"paragraphs": ["ok", 42]}).status_code == 400)

    # /cache/cleanup input validation: negative days would wipe the whole cache.
    # BT_API_TOKEN is not set; the endpoint auto-generates a token. We pre-write
    # a known token to BT_CACHE_DIR/cleanup_token (the path server.py uses) and
    # use that as our X-BT-Token header value.
    from pathlib import Path as _Path
    _cleanup_token_file = _Path(os.environ["BT_CACHE_DIR"]) / "cleanup_token"
    _cleanup_token_file.write_text("test-translation-cleanup-token")
    server._cleanup_token_cache = None  # force re-read
    cleanup_headers = {"X-BT-Token": "test-translation-cleanup-token"}
    try:
        check("cleanup: negative days rejected",
              client.post("/cache/cleanup", json={"days": -1}, headers=cleanup_headers).status_code == 400)
        check("cleanup: non-integer days rejected",
              client.post("/cache/cleanup", json={"days": "abc"}, headers=cleanup_headers).status_code == 400)
        check("cleanup: valid days accepted",
              client.post("/cache/cleanup", json={"days": 3650}, headers=cleanup_headers).status_code == 200)
    finally:
        if _cleanup_token_file.exists():
            _cleanup_token_file.unlink()
        server._cleanup_token_cache = None

    # Cache-key normalization: single + batch endpoints must share entries, and
    # surrounding whitespace must not cause a second paid translation.
    STATE["local_up"] = True
    d1 = client.post("/translate", json={"text": "norm_test_para"}).get_json()
    check("normalization: first translate is fresh", d1.get("cached") is False)
    d2 = client.post("/translate/batch", json={"paragraphs": ["  norm_test_para  "]}).get_json()
    check("normalization: whitespace variant hits cache via batch",
          d2.get("cached_count") == 1 and d2.get("fresh_count") == 0)

    # hit_count: a real cache hit increments /stats total_hits.
    before_hits = client.get("/stats").get_json()["total_hits"]
    client.post("/translate", json={"text": "norm_test_para"})
    after_hits = client.get("/stats").get_json()["total_hits"]
    check("stats: cache hit increments total_hits", after_hits == before_hits + 1)

    # source==target batch short-circuit returns the FULL response contract
    # (backends[]/cached[] must be present so API clients can index safely).
    d = client.post("/translate/batch", json={
        "paragraphs": ["same lang a", "same lang b"],
        "source_lang": "English", "target_lang": "English"}).get_json()
    check("same-lang batch: full contract shape",
          d.get("skipped") == "source==target"
          and d.get("backends") == ["skipped", "skipped"]
          and d.get("cached") == [False, False]
          and d.get("error_codes") == [None, None]
          and d.get("retry_after_seconds") == [None, None]
          and d.get("translations") == ["same lang a", "same lang b"])

    # Loader inherits its version from its own ?v= param (no hardcoded version).
    loader_src = (ROOT / "static/loader.js").read_text(encoding="utf-8")
    check("loader: no hardcoded semver", not re.search(r"VERSION\s*=\s*'\d", loader_src))
    check("loader: derives version from currentScript", "document.currentScript" in loader_src)

    # The cleanup token value must never be written to logs.
    check("token hygiene: token value not logged",
          "cleanup token: %s" not in (ROOT / "server.py").read_text(encoding="utf-8"))

    # Fallback cache scoping: a translation produced by the FALLBACK provider
    # must be cached under the fallback's model key (caching it under the
    # primary model would be cross-provider poisoning via the fallback path),
    # and must still be served from cache after the primary recovers.
    import cache as cache_mod
    STATE["local_up"] = False
    STATE["fallback_up"] = True
    d = client.post("/translate", json={
        "text": "fb_scope_para", "allow_cloud_fallback": True,
    }).get_json()
    check("fb-scope: fallback served the fresh translation",
          d.get("translated") == "[FB] fb_scope_para" and d.get("backend") == "minimax")
    fb_contract = translator.single_cache_contract("English", "Spanish")
    fb_scope = server._cache_scope(
        tenant="legacy-anonymous", book_id="unscoped", chapter_id="unscoped",
        context_hash=fb_contract.context_hash, provider="minimax",
        model="fake-fallback", prompt_hash=fb_contract.prompt_hash,
        protocol_version=fb_contract.protocol_version)
    primary_scope = server._cache_scope(
        tenant="legacy-anonymous", book_id="unscoped", chapter_id="unscoped",
        context_hash=fb_contract.context_hash, provider="local",
        model="fake-model", prompt_hash=fb_contract.prompt_hash,
        protocol_version=fb_contract.protocol_version)
    check("fb-scope: cached under the FALLBACK model key",
          cache_mod.get_cached("fb_scope_para", "English", "Spanish", scope=fb_scope) == "[FB] fb_scope_para")
    check("fb-scope: NOT cached under the primary model key",
          cache_mod.get_cached("fb_scope_para", "English", "Spanish", scope=primary_scope) is None)
    STATE["local_up"] = True
    d = client.post("/translate", json={
        "text": "fb_scope_para", "allow_cloud_fallback": True,
    }).get_json()
    check("fb-scope: cache hit after primary recovers (no re-pay)",
          d.get("cached") is True and d.get("translated") == "[FB] fb_scope_para")
    d = client.post("/translate/batch", json={
        "paragraphs": ["fb_scope_para"], "allow_cloud_fallback": True,
    }).get_json()
    check("fb-scope: batch lookup also finds the fallback-keyed entry",
          d.get("cached_count") == 1 and d["translations"][0] == "[FB] fb_scope_para")

    # Provider attribution: batch results report the provider that ACTUALLY
    # served them (fallback when local is down), not the configured primary.
    STATE["local_up"] = False
    STATE["fallback_up"] = True
    results = translator.translate_batch(
        ["attribution_test_para"], "English", "Spanish",
        allow_cloud_fallback=True,
    )
    check("attribution: fallback provider reported in batch results",
          results[0][0] == "[FB] attribution_test_para" and results[0][1] == "minimax")
    STATE["local_up"] = True

    # CORS grants only exact operator-configured origins. A private address is
    # not implicitly trusted merely because it belongs to an RFC1918 range.
    original_origins = server.ALLOWED_ORIGINS
    try:
        allowlisted = "http://192.168.1.50:8083"
        server.ALLOWED_ORIGINS = frozenset({allowlisted})
        r = client.get("/ping", headers={"Origin": allowlisted})
        check("cors: exact allowlisted origin is allowed",
              r.headers.get("Access-Control-Allow-Origin") == allowlisted)
        check("cors: allowlisted origin exposes Retry-After to JS",
              "Retry-After" in (r.headers.get("Access-Control-Expose-Headers") or ""))
        r = client.get("/ping", headers={"Origin": "http://192.168.1.51:8083"})
        check("cors: unknown private origin is not auto-granted",
              r.headers.get("Access-Control-Allow-Origin") is None)
        r = client.get("/ping", headers={"Origin": "https://evil.example.com"})
        check("cors: unknown public origin rejected",
              r.headers.get("Access-Control-Allow-Origin") is None)
    finally:
        server.ALLOWED_ORIGINS = original_origins

    # Output token cap is proportional to input and clamped to the ceiling, so a
    # rambling model can't burn thousands of tokens on a short paragraph.
    check("output cap: short input stays small (no runaway generation)",
          translator._output_cap("A short sentence.", 4096) < 1000)
    check("output cap: long input clamps to the ceiling",
          translator._output_cap("x" * 100000, 4096) == 4096)
    check("output cap: never below the floor",
          translator._output_cap("", 4096) >= translator.BT_OUTPUT_TOKEN_FLOOR)
    check("output cap: monotonic in input size",
          translator._output_cap("x" * 50, 4096) <= translator._output_cap("x" * 5000, 4096))
    # CJK tokenizes ~2-3x denser than Latin; a flat chars/3.5 estimate would
    # under-budget Chinese/Japanese/Korean sources and truncate translations.
    check("output cap: CJK source gets a bigger budget than same-length Latin",
          translator._output_cap("日本語" * 200, 8192) > translator._output_cap("abc" * 200, 8192))

    check("invalid language rejected",
          client.post("/translate", json={"text": "x", "target_lang": "Klingon"}).status_code == 400)

    # Language catalog: frontend picker and backend validation must offer the
    # exact same set, or the UI could offer a language the API rejects.
    js = (ROOT / "static/translator.js").read_text(encoding="utf-8")
    block = re.search(r"const TOP_LANGUAGES = \[(.*?)\];.*?const MORE_LANGUAGES = \[(.*?)\];", js, re.S)
    js_codes = [c.replace("\\'", "'") for c in
                re.findall(r"code:\s*'((?:[^'\\]|\\.)*)'", block.group(1) + block.group(2))]
    check("languages: no duplicates in frontend catalog", len(js_codes) == len(set(js_codes)))
    check("languages: frontend and backend sets identical", set(js_codes) == server.VALID_LANGUAGES)
    check("languages: catalog covers 100+ languages", len(server.VALID_LANGUAGES) >= 100)
    check("languages: a wider-tier language accepted end-to-end",
          client.post("/translate", json={"text": "", "target_lang": "Quechua"}).status_code == 200)

    # /ping is an instant liveness probe (no LLM) used by the Docker healthcheck.
    pr = client.get("/ping")
    check("ping returns 200 instantly", pr.status_code == 200 and pr.get_json().get("status") == "ok")

    # Rate limiting has two independent budgets. Exercise authentication first
    # without accidentally exhausting the API-work budget, then raise only the
    # auth budget while proving the API budget returns its own admission scope.
    original_auth_limit = server.BT_AUTH_RATE_LIMIT_PER_MINUTE
    try:
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = 1
        first_auth = client.post(
            "/translate",
            json={
                "text": "auth-budget-first",
                "source_lang": "English",
                "target_lang": "English",
            },
        )
        auth_limited = client.post(
            "/translate",
            json={
                "text": "auth-budget-second",
                "source_lang": "English",
                "target_lang": "English",
            },
        )
        check("auth rate limit: first request is admitted", first_auth.status_code == 200)
        check("auth rate limit: exhausted budget returns 429",
              auth_limited.status_code == 429)
        check("auth rate limit: response identifies auth admission",
              auth_limited.get_json().get("scope") == "auth_admission")

        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()
        limit = server.RATE_LIMIT_MAX
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = limit + 1
        for i in range(limit):
            client.post("/translate", json={"text": f"rate{i}"})
        resp = client.post("/translate", json={"text": "limit_test"})
        check("rate limit: returns status 429", resp.status_code == 429)
        check("rate limit: response has Retry-After header", "Retry-After" in resp.headers)
        check("rate limit: response JSON has retry_after", resp.get_json().get("retry_after") is not None)
        check("rate limit: response explicitly marks replay as safe",
              resp.get_json().get("retry_safe") is True)
        check("rate limit: response identifies API admission",
              resp.get_json().get("scope") == "api_admission")
        # CORS preflights must NOT burn rate-limit budget: a 429 on an OPTIONS
        # surfaces as a cryptic CORS error in the browser, and every real request
        # would cost 2x. Even while fully rate-limited, OPTIONS sails through.
        check("rate limit: OPTIONS preflight exempt while rate-limited",
              client.options("/translate/batch",
                             headers={"Origin": "http://192.168.1.2:8083"}).status_code != 429)
    finally:
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = original_auth_limit
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()

    # ── Audit-fix regression tests (2026-07-02) ──────────────────────────

    # B1: db_size_mb reports the real on-disk footprint, not just the main
    # .db file. With WAL mode, the main file is often empty while the data
    # lives in -wal. If the operator only saw the main file they would
    # think the cache was empty (0.0 MB) when it actually had rows.
    import cache as cache_mod
    server._rate_limit_store.clear()
    # Force a translation so a row is written.
    unique = "audit_b1_db_size_text"
    client.post("/translate", json={"text": unique, "source_lang": "English", "target_lang": "Spanish"})
    stats = client.get("/stats").get_json()
    main_size = os.path.getsize(cache_mod.DB_PATH) if os.path.exists(cache_mod.DB_PATH) else 0
    check("audit B1: stats reports non-zero db_size_mb when rows exist",
          stats["db_size_mb"] > 0 or main_size == 0)
    # Also: db_size_mb must be >= the main-file size (WAL can add bytes).
    check("audit B1: db_size_mb >= main .db file size (WAL counted)",
          stats["db_size_mb"] * 1024 * 1024 >= main_size - 1024)

    # B2: source_lang == target_lang must short-circuit and NOT spend an
    # LLM call. The response carries `skipped` so the frontend can tell
    # passthrough from cache hit, and `translated` echoes the input.
    STATE["single_calls"] = 0
    STATE["batch_calls"] = 0
    r = client.post("/translate", json={"text": "hola", "source_lang": "Spanish", "target_lang": "Spanish"})
    check("audit B2: same-lang single endpoint echoes input",
          r.status_code == 200 and r.get_json().get("translated") == "hola")
    check("audit B2: same-lang single endpoint marks skipped",
          r.get_json().get("skipped") == "source==target")
    check("audit B2: same-lang single endpoint spent no LLM call",
          STATE["single_calls"] == 0 and STATE["batch_calls"] == 0)
    rb = client.post("/translate/batch",
                     json={"paragraphs": ["p1", "p2"], "source_lang": "English", "target_lang": "English"})
    check("audit B2: same-lang batch echoes paragraphs",
          rb.status_code == 200 and rb.get_json().get("translations") == ["p1", "p2"])
    check("audit B2: same-lang batch marks skipped",
          rb.get_json().get("skipped") == "source==target")
    check("audit B2: same-lang batch spent no LLM call",
          STATE["single_calls"] == 0 and STATE["batch_calls"] == 0)
    # And: cache must NOT have polluted entries for self-pairs.
    stats_after = client.get("/stats").get_json()
    bad_pairs = [k for k in stats_after["language_pairs"] if k.startswith(("English→English", "Spanish→Spanish"))]
    check("audit B2: cache has no source==target entries", bad_pairs == [])

    # B3: observability bypasses exhausted translation work, but still consumes
    # the authentication-admission budget and passes the auth authority.
    original_b3_auth_limit = server.BT_AUTH_RATE_LIMIT_PER_MINUTE
    try:
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()
        limit = server.RATE_LIMIT_MAX
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = limit + 2
        for i in range(limit):
            client.post("/translate", json={"text": f"b3burn{i}"})
        over = client.post("/translate", json={"text": "blocked"})
        check("audit B3 setup: /translate returns 429 after burst", over.status_code == 429)
        s = client.get("/stats")
        check("audit B3: /stats remains reachable during an API-work storm",
              s.status_code == 200 and "db_size_mb" in s.get_json())
        exhausted = client.get("/stats")
        check("audit B3: /stats still enforces authentication admission",
              exhausted.status_code == 429
              and exhausted.get_json().get("scope") == "auth_admission")
    finally:
        server.BT_AUTH_RATE_LIMIT_PER_MINUTE = original_b3_auth_limit
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()

    # B4: cache key must include the model. Two cache keys computed for the
    # same text+lang with different models must be DISTINCT, otherwise
    # switching LLM_MODEL silently serves stale translations.
    STATE["single_calls"] = 0
    key_a = cache_mod.compute_cache_key("audit_b4_cache_scope_text", "English", "Spanish", model="model-A")
    key_b = cache_mod.compute_cache_key("audit_b4_cache_scope_text", "English", "Spanish", model="model-B")
    check("audit B4: distinct models produce distinct cache keys",
          key_a != key_b)
    key_a_again = cache_mod.compute_cache_key("audit_b4_cache_scope_text", "English", "Spanish", model="model-A")
    check("audit B4: same inputs produce the same cache key (deterministic)",
          key_a == key_a_again)
    # And: put_cache without model must raise (caller contract).
    raised = False
    try:
        cache_mod.put_cache("x", "English", "Spanish", "y", model="")
    except ValueError:
        raised = True
    check("audit B4: put_cache without model raises ValueError", raised)
    # And: end-to-end, switching LLM_MODEL via translator module produces
    # different cache writes (server reads the live module attribute).
    poison = "audit_b4_cache_scope_text_e2e"
    # Write under model-A (default in env).
    client.post("/translate", json={"text": poison, "source_lang": "English", "target_lang": "Spanish"})
    # Read directly via the same API to confirm the row was written.
    stats_after = client.get("/stats").get_json()
    check("audit B4 setup: end-to-end write landed in cache",
          stats_after["total_entries"] >= 2)
    # Direct DB inspection by content-addressed key. Schema v2 deliberately
    # does not persist source_text, so tests must not reintroduce that privacy
    # leak merely to locate a row.
    conn = cache_mod._get_conn()
    poison_contract = translator.single_cache_contract("English", "Spanish")
    poison_scope = server._cache_scope(
        tenant="legacy-anonymous", book_id="unscoped", chapter_id="unscoped",
        context_hash=poison_contract.context_hash, provider="local",
        model="fake-model", prompt_hash=poison_contract.prompt_hash,
        protocol_version=poison_contract.protocol_version)
    poison_key = cache_mod.compute_cache_key(
        poison, "English", "Spanish", scope=poison_scope)
    rows_for_text = conn.execute(
        "SELECT model FROM translations_v2 WHERE cache_key = ?",
        (poison_key,),
    ).fetchall()
    check("audit B4: cache row is tagged with the model that produced it",
          len(rows_for_text) == 1 and rows_for_text[0][0] == "fake-model")


if __name__ == "__main__":
    run()
    print("\nRESULT:", "ALL PASS" if not failed else f"FAILED: {failed}")
    sys.exit(1 if failed else 0)
