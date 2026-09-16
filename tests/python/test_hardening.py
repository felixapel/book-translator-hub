"""
Self-contained tests for the production-hardening batch (2026-07-02).

These tests share state with ``tests.python.test_translation`` (DB_PATH, env vars,
request.post monkeypatch) by importing the same modules — do NOT add
a second init_db or recreate the app.

Covers:
  - MAX_CONTENT_LENGTH global backstop (rejects oversize bodies with 413
    before the per-field check)
  - /cache/cleanup requires API_TOKEN when BT_API_TOKEN is set
  - /metrics returns Prometheus-friendly counters
  - pre-auth admission per observed client and API limits per auth subject

Run with: python3 -m tests.python.test_hardening
"""
import os, sys, json, subprocess, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Same env-var contract as tests.python.test_translation.
os.environ["DB_PATH"] = os.path.join(tempfile.gettempdir(), "bt_test_translations.db")
os.environ["BT_CACHE_DIR"] = tempfile.gettempdir()  # for /cache/cleanup auth token
os.environ["LLM_PROVIDER"] = "local"
os.environ["LLM_MODEL"] = "fake-model"
os.environ["LLM_FALLBACK_PROVIDER"] = "minimax"
os.environ["LLM_FALLBACK_MODEL"] = "fake-fallback"
os.environ["LLM_FALLBACK_API_KEY"] = "x" * 20
os.environ["BT_MAX_CONCURRENT"] = "2"
os.environ["BT_BATCH_SIZE"] = "3"
os.environ["BT_AUTH_MODE"] = "disabled"
os.environ["BT_ALLOW_INSECURE_AUTH"] = "true"
for f in (os.environ["DB_PATH"], os.environ["DB_PATH"] + "-wal", os.environ["DB_PATH"] + "-shm"):
    try:
        os.remove(f)
    except OSError:
        pass

import ipaddress

# Re-use the same fake_post from tests.python.test_translation.
from tests.python import test_translation  # noqa: E402
STATE = test_translation.STATE
fake_post = test_translation.fake_post
import translator  # noqa: E402
translator._provider_post = fake_post

# Import server after fake_post is installed.
import server  # noqa: E402
from work_budget import WorkBudgetExceeded  # noqa: E402
client = server.app.test_client()

failed = []


def check(name, cond):
    print(("PASS" if cond else "FAIL"), "-", name)
    if not cond:
        failed.append(name)


def run():
    # ─────────────────────────────────────────────────────────────────────
    # H1: MAX_CONTENT_LENGTH backstop
    # ─────────────────────────────────────────────────────────────────────
    server.BT_MAX_CONTENT_LENGTH = 1024  # 1 KB
    server.app.config["MAX_CONTENT_LENGTH"] = 1024

    big_paragraph = "x" * 2048  # 2 KB, well over the 1 KB cap
    r = client.post("/translate", json={"text": big_paragraph})
    check("MAX_CONTENT_LENGTH: oversize body rejected with 413",
          r.status_code == 413)
    try:
        body = r.get_json()
        check("MAX_CONTENT_LENGTH: 413 body is JSON",
              isinstance(body, dict) and "error" in body)
    except Exception:
        check("MAX_CONTENT_LENGTH: 413 body is JSON", False)

    r = client.post("/translate/batch", json={"paragraphs": [big_paragraph]})
    check("MAX_CONTENT_LENGTH: /translate/batch also trips 413",
          r.status_code == 413)

    # Restore the production default so the rest of the suite is unaffected.
    server.BT_MAX_CONTENT_LENGTH = 2 * 1024 * 1024
    server.app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

    # ─────────────────────────────────────────────────────────────────────
    # H2: /cache/cleanup always requires auth (fail-safe)
    # ─────────────────────────────────────────────────────────────────────
    # Three sources of truth for the token, in order:
    #   1. BT_API_TOKEN (the explicit operator/token-mode credential)
    #   2. Auto-generated /app/data/cleanup_token (fail-safe when #1 is unset)
    # The endpoint is NEVER open, even when the operator forgets to set
    # BT_API_TOKEN — that's the whole point of the auto-gen path.

    # Path 1: BT_API_TOKEN is set — that token is the only one accepted.
    original_token = server.API_TOKEN
    original_cached = server._cleanup_token_cache
    server.API_TOKEN = "test-secret-token-please-rotate"
    server._cleanup_token_cache = None
    try:
        r = client.post("/cache/cleanup", json={"days": 1})
        check("/cache/cleanup: unauthenticated rejected with 401",
              r.status_code == 401)

        r = client.post("/cache/cleanup",
                        json={"days": 30},
                        headers={"X-BT-Token": "test-secret-token-please-rotate"})
        check("/cache/cleanup: authenticated call accepted (BT_API_TOKEN)",
              r.status_code == 200 and "deleted" in r.get_json())

        r = client.post("/cache/cleanup",
                        json={"days": 30},
                        headers={"X-BT-Token": "wrong"})
        check("/cache/cleanup: wrong token rejected with 401",
              r.status_code == 401)
    finally:
        server.API_TOKEN = original_token
        server._cleanup_token_cache = original_cached

    # Path 2: BT_API_TOKEN unset. The endpoint must STILL require auth
    # via the auto-generated /app/data/cleanup_token (or in-memory fallback
    # if the file system refuses writes). In the test environment, the
    # data dir is /tmp/bt_test_translations.db's dir, which is writable.
    # We monkeypatch the token to a known value so we can assert behaviour.
    server.API_TOKEN = ""
    server._cleanup_token_cache = None
    server._CLEANUP_TOKEN_PATH = Path(tempfile.gettempdir()) / "bt_test_cleanup_token"
    if server._CLEANUP_TOKEN_PATH.exists():
        server._CLEANUP_TOKEN_PATH.unlink()
    # Set a known token by writing the file directly. This simulates a
    # previous process run that already created the file.
    server._CLEANUP_TOKEN_PATH.write_text("test-autogen-token-abc")

    r = client.post("/cache/cleanup", json={"days": 1})
    check("/cache/cleanup: no BT_API_TOKEN, no header -> 401",
          r.status_code == 401)

    r = client.post("/cache/cleanup",
                    json={"days": 1},
                    headers={"X-BT-Token": "test-autogen-token-abc"})
    check("/cache/cleanup: no BT_API_TOKEN, file token works -> 200",
          r.status_code == 200)

    r = client.post("/cache/cleanup",
                    json={"days": 1},
                    headers={"X-BT-Token": "wrong"})
    check("/cache/cleanup: no BT_API_TOKEN, wrong file token -> 401",
          r.status_code == 401)
    server._CLEANUP_TOKEN_PATH.unlink()
    server._cleanup_token_cache = None

    # ─────────────────────────────────────────────────────────────────────
    # H3: /metrics Prometheus-friendly counters
    # ─────────────────────────────────────────────────────────────────────
    for i in range(5):
        client.post("/translate", json={"text": f"h3_metric_{i}"})

    r = client.get("/metrics")
    check("/metrics: returns 200", r.status_code == 200)
    body = r.get_json()
    check("/metrics: all counters present",
          all(k in body for k in [
              "total_requests", "average_latency_ms", "cache_hit_rate_pct",
              "cache_hits", "cache_misses", "errors",
              "translation_latency_ms_buckets", "singleflight"]))
    check("/metrics: total_requests is non-negative int",
          isinstance(body["total_requests"], int) and body["total_requests"] >= 0)
    check("/metrics: cache_hit_rate_pct is a percentage in [0, 100]",
          0.0 <= body["cache_hit_rate_pct"] <= 100.0)
    check("/metrics: total = hits + misses (invariants)",
          body["total_requests"] == body["cache_hits"] + body["cache_misses"])
    check("/metrics: singleflight state is bounded and observable",
          all(isinstance(body["singleflight"].get(k), int)
              and body["singleflight"][k] >= 0
              for k in ["leaders", "shared_results", "followers_waiting",
                        "wait_timeouts", "capacity_rejections",
                        "active_entries", "retained_entries"]))

    # ─────────────────────────────────────────────────────────────────────
    # H4: authentication admission is per observed client, while successful
    # API work is keyed by the authenticated subject.
    # ─────────────────────────────────────────────────────────────────────
    server._rate_limit_store.clear()
    server._auth_rate_limit_store.clear()
    server.BT_TRUST_PROXY = False
    client.post("/translate", json={"text": "h4_test_a"},
                headers={"X-Forwarded-For": "1.2.3.4"})
    auth_keys = list(server._auth_rate_limit_store.keys())
    api_keys = list(server._rate_limit_store.keys())
    check("auth admission: without BT_TRUST_PROXY, X-Forwarded-For is ignored",
          auth_keys and auth_keys[0] != "1.2.3.4")
    check("API limit: successful work uses the authenticated subject",
          api_keys == [f"authenticated:legacy-anonymous:{auth_keys[0]}"])
    server._rate_limit_store.clear()
    server._auth_rate_limit_store.clear()

    server.BT_TRUST_PROXY = True
    client.post("/translate", json={"text": "h4_test_b"},
                headers={"X-Forwarded-For": "5.6.7.8, 10.0.0.1"})
    keys = list(server._auth_rate_limit_store.keys())
    # LAST hop, not first: standard proxies APPEND the address they saw, so
    # the final entry is the only one a client cannot forge. Keying on the
    # first hop let clients bypass the limiter with a made-up header.
    check("auth admission: with BT_TRUST_PROXY, X-Forwarded-For LAST hop is key",
          keys and keys[0] == "10.0.0.1")
    server.BT_TRUST_PROXY = False
    server._rate_limit_store.clear()
    server._auth_rate_limit_store.clear()

    # ─────────────────────────────────────────────────────────────────────
    # H5: BT_TRUSTED_PROXIES allowlist path (preferred over BT_TRUST_PROXY)
    # ─────────────────────────────────────────────────────────────────────
    # When BT_TRUSTED_PROXIES is set, the rate-limit key uses X-Forwarded-For
    # only if the *peer* (the actual socket source) is in the allowlist.
    # This is the production-safe path; BT_TRUST_PROXY=true (tested in H4)
    # is for dev/local only.
    server._rate_limit_store.clear()
    server.BT_TRUST_PROXY = False
    original_trusted = set(server.BT_TRUSTED_PROXIES)
    original_nets = list(server._TRUSTED_PROXY_NETS)
    original_limit = server.RATE_LIMIT_MAX
    original_authenticator = server.AUTHENTICATOR
    original_public_origin = server.BT_PUBLIC_ORIGIN
    try:
        # Werkzeug's test client connects from 127.0.0.1. Allowlist it.
        server.BT_TRUSTED_PROXIES = {"127.0.0.1/32"}
        server._TRUSTED_PROXY_NETS = [
            ipaddress.ip_network("127.0.0.1/32", strict=False)
        ]
        client.post("/translate", json={"text": "h5_test_a"},
                    headers={"X-Forwarded-For": "9.9.9.9"})
        keys = list(server._auth_rate_limit_store.keys())
        check("BT_TRUSTED_PROXIES: peer in allowlist honors XFF",
              keys and keys[0] == "9.9.9.9")
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()

        # Anti-spoof within the trusted path: a client that FORGES an XFF
        # entry gets it appended-to by the trusted proxy ("forged, real").
        # The limiter must key on the appended (real) address — otherwise a
        # client rotates forged first hops and gets unlimited fresh buckets.
        client.post("/translate", json={"text": "h5_test_spoof"},
                    headers={"X-Forwarded-For": "6.6.6.6, 192.168.1.42"})
        keys = list(server._auth_rate_limit_store.keys())
        check("BT_TRUSTED_PROXIES: forged first hop ignored (keys on last hop)",
              keys and keys[0] == "192.168.1.42")
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()

        # Now switch to an allowlist that does NOT match the peer. The
        # client is still 127.0.0.1, but the allowlist is 10.0.0.0/8. The
        # XFF must be ignored — the key falls back to the peer.
        server.BT_TRUSTED_PROXIES = {"10.0.0.0/8"}
        server._TRUSTED_PROXY_NETS = [
            ipaddress.ip_network("10.0.0.0/8", strict=False)
        ]
        client.post("/translate", json={"text": "h5_test_b"},
                    headers={"X-Forwarded-For": "9.9.9.9"})
        keys = list(server._auth_rate_limit_store.keys())
        check("BT_TRUSTED_PROXIES: peer NOT in allowlist ignores XFF (anti-spoof)",
              keys and keys[0] != "9.9.9.9")

        # Route-level isolation contract: API work is isolated by opaque
        # authenticated subject, even when both subjects share one proxy.
        server.BT_TRUSTED_PROXIES = {"127.0.0.1/32"}
        server._TRUSTED_PROXY_NETS = [
            ipaddress.ip_network("127.0.0.1/32", strict=False)
        ]
        server.RATE_LIMIT_MAX = 1
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()
        server.AUTHENTICATOR = server.RequestAuthenticator(
            mode="forwarded",
            identity_trusted_proxies=("127.0.0.1/32",),
            forwarded_subject_header="X-BT-Subject",
            forwarded_roles_header="",
        )
        server.BT_PUBLIC_ORIGIN = "https://books.example.test"
        payload = {
            "text": "proxy bucket probe",
            "source_lang": "English",
            "target_lang": "English",
        }
        a_first = client.post("/translate", json=payload,
                              headers={"X-Forwarded-For": "192.0.2.10",
                                       "X-BT-Subject": "subject-a",
                                       "Origin": server.BT_PUBLIC_ORIGIN})
        a_second = client.post("/translate", json=payload,
                               headers={"X-Forwarded-For": "192.0.2.10",
                                        "X-BT-Subject": "subject-a",
                                        "Origin": server.BT_PUBLIC_ORIGIN})
        b_first = client.post("/translate", json=payload,
                              headers={"X-Forwarded-For": "192.0.2.11",
                                       "X-BT-Subject": "subject-b",
                                       "Origin": server.BT_PUBLIC_ORIGIN})
        check("authenticated subject A does not consume subject B's API budget",
              a_first.status_code != 429
              and a_second.status_code == 429
              and b_first.status_code == a_first.status_code)
        server.AUTHENTICATOR = original_authenticator
        server.RATE_LIMIT_MAX = original_limit
    finally:
        server.BT_TRUSTED_PROXIES = original_trusted
        server._TRUSTED_PROXY_NETS = original_nets
        server.RATE_LIMIT_MAX = original_limit
        server.AUTHENTICATOR = original_authenticator
        server.BT_PUBLIC_ORIGIN = original_public_origin
        server._rate_limit_store.clear()
        server._auth_rate_limit_store.clear()

    # The entrypoint must export the proxy trust boundary before Gunicorn
    # imports server.py. Import-time configuration set later is ineffective.
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text()
    trust_export = entrypoint.find('export BT_TRUSTED_PROXIES=')
    gunicorn_start = entrypoint.find('exec gunicorn --bind')
    check("entrypoint: trusted proxy config is exported before Gunicorn",
          trust_export >= 0 and gunicorn_start >= 0 and trust_export < gunicorn_start)

    # A typo in the trust allowlist must fail startup, never degrade silently
    # to trusting or grouping the wrong clients.
    invalid_env = os.environ.copy()
    invalid_env["BT_TRUSTED_PROXIES"] = "definitely-not-a-cidr"
    invalid_env["DB_PATH"] = os.path.join(tempfile.gettempdir(), "bt_invalid_proxy.db")
    invalid_proxy = subprocess.run(
        [sys.executable, "-c", "import server"],
        cwd=ROOT,
        env=invalid_env,
        capture_output=True,
        text=True,
        check=False,
    )
    check("BT_TRUSTED_PROXIES: invalid CIDR fails startup",
          invalid_proxy.returncode != 0
          and "definitely-not-a-cidr" in (invalid_proxy.stdout + invalid_proxy.stderr))

    # ─────────────────────────────────────────────────────────────────────
    # H6: /translate/batch per-paragraph attribution
    # ─────────────────────────────────────────────────────────────────────
    # The batch endpoint should expose which provider served each paragraph
    # (and whether it was a cache hit) so the frontend can render per-para
    # attribution and operators can verify the right backend was used.
    server._rate_limit_store.clear()
    # Reset to "local up, fallback up" so the attribution tests are stable.
    STATE["local_up"] = True
    STATE["fallback_up"] = True
    STATE["malform"] = False

    paras_h6 = [
        "h6_brand_new_para_1",  # fresh: backends[i] should be "local", cached[i]=False
        "h6_brand_new_para_2",  # fresh
    ]
    r1 = client.post("/translate/batch", json={"paragraphs": paras_h6}).get_json()
    check("H6: batch returns per-paragraph backends array",
          "backends" in r1 and isinstance(r1["backends"], list)
          and len(r1["backends"]) == len(paras_h6))
    check("H6: batch returns per-paragraph cached array",
          "cached" in r1 and isinstance(r1["cached"], list)
          and len(r1["cached"]) == len(paras_h6))
    check("H6: fresh paragraphs have backends != 'cache'",
          all(b != "cache" for b in r1.get("backends", [])))
    check("H6: fresh paragraphs have cached[i] == False",
          all(c is False for c in r1.get("cached", [])))

    # Repeat: this time both should be cache hits.
    r2 = client.post("/translate/batch", json={"paragraphs": paras_h6}).get_json()
    check("H6: cache-hit backends[i] == 'cache'",
          all(b == "cache" for b in r2.get("backends", [])))
    check("H6: cache-hit cached[i] == True",
          all(c is True for c in r2.get("cached", [])))
    check("H6: cache-hit doesn't increment fresh_count",
          r2.get("fresh_count") == 0 and r2.get("cached_count") == len(paras_h6))

    # Mixed group: one member exists under a different group context. The whole
    # new group must refresh atomically; otherwise removing the cached member
    # would change the prompt seen by its sibling.
    mixed = ["h6_mixed_brand_new", paras_h6[0]]
    r3 = client.post("/translate/batch", json={"paragraphs": mixed}).get_json()
    check("H6: partial group cache refreshes the full group",
          r3.get("fresh_count") == 2 and r3.get("cached_count") == 0)
    check("H6: refreshed group reports fresh backends",
          all(backend != "cache" for backend in r3["backends"]))
    check("H6: refreshed group reports no cache hits",
          all(value is False for value in r3["cached"]))

    # Backend attribution: with local down, fresh paragraphs should report
    # the fallback provider.
    STATE["local_up"] = False
    STATE["fallback_up"] = True
    r4 = client.post("/translate/batch",
                     json={
                         "paragraphs": ["h6_fallback_only_para"],
                         "allow_cloud_fallback": True,
                     }).get_json()
    check("H6: fallback provider reported in per-para backends",
          r4.get("backends") and r4["backends"][0] == "minimax")
    STATE["local_up"] = True

    # Empty paragraph: backends[i] should be "" (no backend, no cache).
    r5 = client.post("/translate/batch", json={"paragraphs": [""]}).get_json()
    check("H6: empty paragraph has backends[i] == '' (no backend)",
          r5.get("backends") and r5["backends"][0] == "")
    check("H6: empty paragraph has cached[i] == False",
          r5.get("cached") and r5["cached"][0] is False)

    # Backward compat: the original aggregate fields are still there.
    check("H6: backward compat: cached_count still present",
          "cached_count" in r1 and "fresh_count" in r1)

    # ─────────────────────────────────────────────────────────────────────
    # H7: malformed JSON shapes fail closed with a stable JSON 400
    # ────────────────────────────────────────────────────────────────────
    malformed_cases = [
        ("/translate", ["text"], "single: top-level array"),
        ("/translate/batch", ["paragraphs"], "batch: top-level array"),
        ("/translate", {"text": "hello", "source_lang": []},
         "single: source_lang must be a string"),
        ("/translate", {"text": "hello", "target_lang": 7},
         "single: target_lang must be a string"),
        ("/translate/batch", {"paragraphs": ["hello"], "source_lang": {}},
         "batch: source_lang must be a string"),
    ]
    for endpoint, payload, label in malformed_cases:
        response = client.post(endpoint, json=payload)
        body = response.get_json(silent=True)
        check(f"JSON schema: {label} returns 400 JSON",
              response.status_code == 400
              and isinstance(body, dict)
              and isinstance(body.get("error"), str))

    # ─────────────────────────────────────────────────────────────────────
    # H8: work-budget exhaustion has a stable, retry-safe HTTP contract
    # ────────────────────────────────────────────────────────────────────
    original_translate_text = server.translate_text
    original_translate_batch = server.translate_batch
    try:
        def exhaust_single(*args, **kwargs):
            raise WorkBudgetExceeded("attempts")

        server.translate_text = exhaust_single
        server._rate_limit_store.clear()
        response = client.post(
            "/translate", json={"text": "h8 unique single budget miss"})
        body = response.get_json(silent=True)
        check("work budget: single exhaustion returns stable 503 JSON",
              response.status_code == 503
              and body.get("error") == "work_budget_exhausted"
              and body.get("reason") == "attempts")

        def exhaust_batch(*args, **kwargs):
            raise WorkBudgetExceeded("deadline")

        server.translate_batch = exhaust_batch
        server._rate_limit_store.clear()
        response = client.post("/translate/batch", json={
            "paragraphs": ["h8 unique batch budget miss"],
        })
        body = response.get_json(silent=True)
        check("work budget: batch exhaustion returns stable 503 JSON",
              response.status_code == 503
              and body.get("error") == "work_budget_exhausted"
              and body.get("reason") == "deadline")

        def queue_full(*args, **kwargs):
            raise WorkBudgetExceeded("queue")

        server.translate_text = queue_full
        server._rate_limit_store.clear()
        response = client.post(
            "/translate", json={"text": "h8 unique queue miss"})
        check("work budget: transient queue rejection exposes Retry-After",
              response.status_code == 503
              and response.headers.get("Retry-After") is not None)
    finally:
        server.translate_text = original_translate_text
        server.translate_batch = original_translate_batch
        server._rate_limit_store.clear()


if __name__ == "__main__":
    run()
    print("\nRESULT:", "ALL PASS" if not failed else f"FAILED: {failed}")
    sys.exit(1 if failed else 0)
