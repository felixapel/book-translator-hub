"""Exercise shipped nginx and real APIs with disposable reader/provider fixtures.

Run inside the candidate image; no stock-reader or external-edge certification
is implied. --serve-browser keeps the same boundary up for a host Chromium.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, "/app")

from flask import request
from werkzeug.serving import make_server

from hub_runtime import HubConfig
from proxy.render_config import render_outputs


FIXTURES = Path(__file__).resolve().parents[1] / "python"
USER_AGENT = "Edge-Trust-Smoke/1.0 (exact context)"
EDGE = "127.0.0.2"
DIRECT = "127.0.0.3"
PROVIDER_CALLS: list[dict] = []
READER_PROBES: dict[str, list[dict]] = {"cwa": [], "kavita": []}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_fixture(name: str):
    spec = importlib.util.spec_from_file_location(name, FIXTURES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EchoHandler(BaseHTTPRequestHandler):
    def respond(self):
        body = json.dumps({"headers": list(self.headers.raw_items())}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = respond

    def log_message(self, *_args):
        pass


class ProviderHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        PROVIDER_CALLS.append(payload)
        user = payload["messages"][-1]["content"]
        if "cwa-translate-segments/v1" in payload["messages"][0]["content"]:
            envelope = json.loads(user)
            content = json.dumps({
                "protocol": "cwa-translate-segments/v1",
                "translations": [
                    {"id": segment["id"], "text": "translated:" + segment["text"]}
                    for segment in envelope["segments"]
                ],
            })
        else:
            content = "translated:" + user
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def call(port, path, *, peer=EDGE, forwarded=None, headers=(), method="GET", payload=None,
         user_agent=USER_AGENT):
    """Preserve raw field order and duplicates, binding the actual TCP source."""
    connection = http.client.HTTPConnection(
        "127.0.0.1", port, timeout=5, source_address=(peer, 0)
    )
    body = b"" if payload is None else json.dumps(payload).encode()
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", "forged.example.test")
        connection.putheader("User-Agent", user_agent)
        if forwarded is not None:
            connection.putheader("X-Forwarded-For", forwarded)
        for name, value in headers:
            connection.putheader(name, value)
        connection.putheader("Content-Length", str(len(body)))
        if payload is not None:
            connection.putheader("Content-Type", "application/json")
        connection.endheaders(body)
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


def json_call(*args, expected=200, **kwargs):
    if kwargs.get("method") in {"POST", "DELETE"} and args[0] in {8080, 8081, 8087}:
        # Exercise authentication after the production same-origin write gate.
        origin_port = 8080 if args[0] == 8087 else args[0]
        kwargs["headers"] = tuple(kwargs.get("headers", ())) + (
            ("Origin", f"http://127.0.0.1:{origin_port}"),
        )
    status, headers, body = call(*args, **kwargs)
    require(status == expected, f"{kwargs.get('method', 'GET')} {args[1]}: {status}, expected {expected}")
    return headers, json.loads(body)


def cookie_from(headers, name):
    for field, value in headers:
        if field.lower() == "set-cookie":
            parsed = SimpleCookie()
            parsed.load(value)
            if name in parsed:
                return f"{name}={parsed[name].coded_value}"
    raise AssertionError(f"missing {name} cookie")


def cookie_only(cookie):
    return (("Cookie", cookie),)


def start_thread_server(stack, server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stack.callback(server.server_close)
    stack.callback(server.shutdown)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_listener(port, process):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        require(process.poll() is None, f"process exited before listener {port}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError(f"listener {port} did not become ready")


def prepare(stack, *, browser=False):
    nginx = Path("/tmp/nginx")
    nginx.mkdir(mode=0o700)
    for directory in ("client_temp", "proxy_temp", "fastcgi_temp", "uwsgi_temp", "scgi_temp"):
        (nginx / directory).mkdir(mode=0o700)
    data = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="edge-trust-")))

    cwa = load_fixture("test_cwa_strong_fixture").create_cwa_strong_app()
    kavita_module = load_fixture("test_kavita_auth_fixture")
    kavita = kavita_module.app
    for name, app, port, probe_path in (
        ("cwa", cwa, 8083, "/ajax/emailstat"),
        ("kavita", kavita, 5000, "/api/Account"),
    ):
        def record_probe(name=name, path=probe_path):
            if request.path == path:
                READER_PROBES[name].append({
                    "address": request.remote_addr,
                    "xff": request.headers.get("X-Forwarded-For"),
                    "ua": request.headers.get("User-Agent"),
                    "cookie": request.headers.get("Cookie", ""),
                    "authorization": request.headers.get("Authorization", ""),
                })
        app.before_request(record_probe)
        start_thread_server(stack, make_server("127.0.0.1", port, app, threaded=True))

    # Minimal browser DOM alongside the existing strong-session fixture. The
    # packaged loader, overlay, API/session routes and provider are unmocked.
    @cwa.get("/read/<int:book_id>")
    def reader(book_id):
        return '<!doctype html><html><head><title>Native edge reader</title></head><body><div id="viewer"><iframe title="Book chapter" src="/chapter/1"></iframe></div></body></html>'

    @cwa.get("/chapter/<int:chapter_id>")
    def chapter(chapter_id):
        return f'<!doctype html><html lang="en"><head><title>Chapter</title></head><body><p id="native-paragraph">Native edge browser paragraph {chapter_id}.</p></body></html>'

    @cwa.get("/favicon.ico")
    def favicon():
        return "", 204

    start_thread_server(stack, ThreadingHTTPServer(("127.0.0.1", 8000), ProviderHandler))
    start_thread_server(stack, ThreadingHTTPServer(("127.0.0.1", 8086), EchoHandler))
    values = {
        "BT_ENABLE_CWA": "true", "BT_ENABLE_KAVITA": "true",
        "BT_CWA_PUBLIC_ORIGIN": os.environ.get(
            "BT_EDGE_SMOKE_PUBLIC_ORIGIN", "http://127.0.0.1:8080"
        ),
        "BT_CWA_READER_UPSTREAM": "http://127.0.0.1:8083",
        "BT_CWA_READER_VERSION": "4.0.6", "BT_CWA_AUTH_PROFILE": "reader-session",
        "BT_CWA_READER_CONNECTOR_ID": "01234567-89ab-4cde-8123-0123456789ab",
        "BT_CWA_PUBLISHED_PORT": "8385",
        "BT_KAVITA_PUBLIC_ORIGIN": "http://127.0.0.1:8081",
        "BT_KAVITA_READER_UPSTREAM": "http://127.0.0.1:5000",
        "BT_KAVITA_READER_VERSION": "0.9.0.2", "BT_KAVITA_AUTH_PROFILE": "reader-session",
        "BT_KAVITA_READER_CONNECTOR_ID": "11234567-89ab-4cde-8123-0123456789ab",
        "BT_KAVITA_PUBLISHED_PORT": "8386",
        "BT_EDGE_TRUSTED_PROXIES": EDGE,
        "BT_CWA_IDENTITY_HEADER": "X-Reader-Login",
        "LLM_PROVIDER": "local", "LLM_MODEL": "edge-smoke",
        "BT_LOCAL_URL": "http://127.0.0.1:8000/v1/chat/completions",
        "BT_AUTH_RATE_LIMIT_PER_MINUTE": "300" if browser else "8",
    }
    runtime = HubConfig.from_environment(values)
    api_processes = []
    for reader in runtime.readers:
        reader_data = data / reader.name
        reader_data.mkdir(mode=0o700)
        key = reader_data / "reader_session_key"
        key.write_bytes(secrets.token_bytes(32))
        key.chmod(0o600)
        environment = {**os.environ, **reader.environment}
        environment.update({
            "DB_PATH": str(reader_data / "translations.db"),
            "BT_CACHE_DIR": str(reader_data), "BT_SESSION_KEY_PATH": str(key),
        })
        log = stack.enter_context((data / f"api-{reader.name}.log").open("w+"))
        process = subprocess.Popen(
            [sys.executable, "/app/server.py"], env=environment,
            stdout=log, stderr=subprocess.STDOUT,
        )
        stack.callback(stop_process, process)
        api_processes.append(process)
        render_outputs(
            Path("/app/proxy/nginx.conf.template"),
            nginx / f"proxy-{reader.name}.conf",
            Path(reader.proxy_environment["BT_BROWSER_CONFIG_PATH"]),
            reader.proxy_environment,
        )
    for namespace, port, trust, api in (
        ("default", 8082, "", "http://127.0.0.1:8086"),
        ("echo", 8085, EDGE, "http://127.0.0.1:8086"),
        ("shared", 8087, "", "http://127.0.0.1:8391"),
    ):
        environment = dict(runtime.readers[0].proxy_environment)
        environment.update({
            "BT_PROXY_NAMESPACE": namespace, "BT_PROXY_PORT": str(port),
            "BT_EDGE_TRUSTED_PROXIES": trust, "BT_API_UPSTREAM": api,
            "BT_BROWSER_CONFIG_PATH": f"/tmp/nginx/browser-config-{namespace}.json",
        })
        if namespace != "shared":
            environment["BT_READER_UPSTREAM"] = "http://127.0.0.1:8086"
        render_outputs(
            Path("/app/proxy/nginx.conf.template"), nginx / f"proxy-{namespace}.conf",
            Path(environment["BT_BROWSER_CONFIG_PATH"]), environment,
        )
    if browser:
        # Controlled immediate edge: discard incoming XFF and emit the actual
        # browser peer. Only this exact bound source is trusted by the listener.
        (nginx / "proxy-browser-edge.conf").write_text('''server {
            listen 8585;
            location / {
                proxy_bind 127.0.0.2;
                proxy_pass http://127.0.0.1:8080;
                proxy_set_header X-Forwarded-For $remote_addr;
                proxy_set_header User-Agent $http_user_agent;
                proxy_set_header Host $http_host;
            }
        }''')
    main = nginx / "main.conf"
    main.write_text(Path("/app/proxy/nginx-main.conf").read_text().replace(
        "worker_processes auto;", "worker_processes 1;"
    ))
    version = subprocess.run(["nginx", "-V"], capture_output=True, text=True, check=True)
    require("--with-http_realip_module" in version.stderr, "shipped nginx lacks HTTP RealIP")
    subprocess.run(["nginx", "-t", "-e", "/dev/stderr", "-c", str(main)], check=True)
    log = stack.enter_context((data / "nginx.log").open("w+"))
    process = subprocess.Popen(
        ["nginx", "-e", "/dev/stderr", "-c", str(main), "-g", "daemon off;"], stdout=log, stderr=subprocess.STDOUT
    )
    stack.callback(stop_process, process)
    for reader, api in zip(runtime.readers, api_processes):
        wait_listener(reader.api_port, api)
        wait_listener(reader.proxy_port, process)
    return runtime, kavita_module.NATIVE_TOKEN


def test_headers(identity_header):
    cases = (
        ("absent", (), EDGE),
        ("empty", (("X-Forwarded-For", ""),), EDGE),
        ("invalid", (("X-Forwarded-For", "invalid"),), EDGE),
        ("invalid-final", (("X-Forwarded-For", "203.0.113.7, invalid"),), EDGE),
        ("ipv4", (("X-Forwarded-For", "203.0.113.7"),), "203.0.113.7"),
        ("forged-leading", (("X-Forwarded-For", "198.51.100.99, 203.0.113.7"),), "203.0.113.7"),
        ("duplicate-fields", (("X-Forwarded-For", "198.51.100.99"), ("x-forwarded-for", "203.0.113.7")), "203.0.113.7"),
        ("invalid-final-field", (("X-Forwarded-For", "203.0.113.7"), ("X-Forwarded-For", "invalid")), EDGE),
        ("ipv4-port", (("X-Forwarded-For", "203.0.113.7:443"),), "203.0.113.7"),
        ("ipv6", (("X-Forwarded-For", "2001:0db8::7"),), "2001:db8::7"),
        ("ipv6-port", (("X-Forwarded-For", "[2001:db8::7]:443"),), "2001:db8::7"),
        ("nginx-trailing-commas", (("X-Forwarded-For", "198.51.100.99,  203.0.113.7,,"),), "203.0.113.7"),
    )
    cookie = "session=reader-proof; bt-cwa-session=" + "a" * 43
    credential = "Bearer native-reader-proof"
    spoof = (
        ("X-Real-IP", "198.51.100.99"), ("Forwarded", "for=198.51.100.99;host=evil"),
        ("X-Forwarded-Host", "evil.example.test"), ("X-Forwarded-Proto", "https"),
        ("X-Forwarded-Port", "1"), (identity_header, "attacker"),
        ("X-BT-Subject", "attacker"), ("X-BT-Roles", "admin"),
        ("X-authentik-uid", "attacker"), ("X-authentik-groups", "admin"),
        ("Cookie", cookie), ("Authorization", credential),
    )
    checks = 0
    for port in (8082, 8085):
        for peer in (EDGE, DIRECT):
            for name, xff, selected in cases:
                expected = selected if port == 8085 and peer == EDGE else peer
                for path, method in (
                    ("/reader", "GET"), ("/bt-api/echo", "GET"),
                    ("/bt-api/session", "POST"), ("/bt-api/session", "DELETE"),
                    ("/bt-api/session", "GET"),
                ):
                    _, payload = json_call(port, path, peer=peer, headers=xff + spoof, method=method)
                    outgoing = {}
                    for field, value in payload["headers"]:
                        outgoing.setdefault(field.lower(), []).append(value)
                    label = f"{port}/{peer}/{name}/{method} {path}"
                    require(outgoing.get("x-forwarded-for") == [expected], label + " XFF")
                    require(outgoing.get("x-real-ip") == [expected], label + " X-Real-IP")
                    require(outgoing.get("user-agent") == [USER_AGENT], label + " User-Agent")
                    require(outgoing.get("host") == ["127.0.0.1:8080"], label + " authority")
                    require(outgoing.get("x-forwarded-host") == ["127.0.0.1:8080"], label + " forwarded authority")
                    require(outgoing.get("x-forwarded-proto") == ["http"], label + " scheme")
                    for field in ("forwarded", "x-forwarded-port", identity_header.lower(), "x-bt-subject", "x-bt-roles", "x-authentik-uid", "x-authentik-groups"):
                        require(field not in outgoing, label + " leaked " + field)
                    reader_route = path == "/reader"
                    exchange = path == "/bt-api/session" and method == "POST"
                    expected_cookie = cookie if reader_route or exchange else (
                        "bt-cwa-session=" + "a" * 43
                        if path != "/bt-api/session" or method == "DELETE" else None
                    )
                    require(outgoing.get("cookie") == ([expected_cookie] if expected_cookie else None), label + " cookie boundary")
                    require(outgoing.get("authorization") == ([credential] if reader_route or exchange else None), label + " bearer boundary")
                    checks += 1
    print(f"PASS: {checks} raw-header routing cases through shipped nginx", flush=True)


def test_sessions(runtime, native_token):
    cwa, kavita = runtime.readers
    # Empty trust still shares the edge key; configured trust separates A and B.
    for port, address in ((8087, "203.0.113.90"), (8080, "203.0.113.10")):
        for _ in range(8):
            json_call(port, "/bt-api/translate/batch", forwarded=address, method="POST", payload={}, expected=401)
        _, limited = json_call(port, "/bt-api/translate/batch", forwarded=address, method="POST", payload={}, expected=429)
        require(limited.get("scope") == "auth_admission", "wrong admission scope")
        require(not PROVIDER_CALLS, "anonymous rejection reached provider")
    _, shared = json_call(8087, "/bt-api/session", forwarded="203.0.113.11", method="POST", expected=429)
    require(shared.get("scope") == "auth_admission", "empty trust unexpectedly split admission")

    address = "203.0.113.11"
    headers, _ = json_call(cwa.proxy_port, "/fixture/login?user=3", forwarded=address)
    native_cookie = cookie_from(headers, "session")
    headers, _ = json_call(cwa.proxy_port, "/bt-api/session", forwarded=address, headers=cookie_only(native_cookie), method="POST")
    cwa_cookie = cookie_from(headers, "bt-cwa-session")
    probe = READER_PROBES["cwa"][-1]
    require(probe["address"] == probe["xff"] == address and probe["ua"] == USER_AGENT, "CWA strong-session probe binding differs")
    require(probe["cookie"] == native_cookie and not probe["authorization"], "CWA probe leaked credentials")

    def translate(reader, client, cookie, book):
        _, policy = json_call(reader.proxy_port, "/bt-api/provider-policy", forwarded=client, headers=cookie_only(cookie))
        payload = {
            "paragraphs": ["native edge translation paragraph"],
            "source_lang": "English", "target_lang": "Spanish",
            "book_id": book, "chapter_id": "chapter-1", "provider_policy": policy,
        }
        _, result = json_call(reader.proxy_port, "/bt-api/translate/batch", forwarded=client, headers=cookie_only(cookie), method="POST", payload=payload)
        require(result["translations"] == ["translated:native edge translation paragraph"], "real provider result missing")
        return payload

    payload = translate(cwa, address, cwa_cookie, "edge-cwa")
    calls = len(PROVIDER_CALLS)
    json_call(cwa.proxy_port, "/bt-api/translate/batch", forwarded="203.0.113.12", headers=cookie_only(cwa_cookie), method="POST", payload=payload, expected=401)
    json_call(cwa.proxy_port, "/bt-api/translate/batch", forwarded=address,
              headers=cookie_only(cwa_cookie), method="POST", payload=payload,
              user_agent="Different-Edge-Browser/1.0", expected=401)
    require(len(PROVIDER_CALLS) == calls, "rejected CWA binding reached provider")
    translate(cwa, address, cwa_cookie, "edge-cwa-restored")
    require(len(PROVIDER_CALLS) > calls, "restored CWA context did not translate")
    json_call(cwa.proxy_port, "/bt-api/session", forwarded="203.0.113.12", headers=cookie_only(native_cookie), method="POST", expected=401)

    other = "203.0.113.20"
    json_call(kavita.proxy_port, "/bt-api/session", forwarded=other, headers=cookie_only(native_cookie), method="POST", expected=401)
    headers, _ = json_call(kavita.proxy_port, "/bt-api/session", forwarded=other, headers=(("Authorization", "Bearer " + native_token),), method="POST")
    kavita_cookie = cookie_from(headers, "bt-kavita-session")
    probe = READER_PROBES["kavita"][-1]
    require(probe["xff"] == other and probe["ua"] == USER_AGENT, "Kavita probe context differs")
    require(probe["authorization"] == "Bearer " + native_token and not probe["cookie"], "Kavita probe leaked cookies")
    payload = translate(kavita, other, kavita_cookie, "edge-kavita")
    calls = len(PROVIDER_CALLS)
    json_call(kavita.proxy_port, "/bt-api/translate/batch", forwarded="203.0.113.21", headers=cookie_only(kavita_cookie), method="POST", payload=payload, expected=401)
    require(len(PROVIDER_CALLS) == calls, "rejected Kavita binding reached provider")
    for target, cookie, client in (
        (kavita, cwa_cookie, address), (cwa, kavita_cookie, other),
    ):
        json_call(target.proxy_port, "/bt-api/metrics", forwarded=client, headers=cookie_only(cookie), expected=401)
        renamed = target.environment["BT_SESSION_COOKIE_NAME"] + "=" + cookie.split("=", 1)[1]
        json_call(target.proxy_port, "/bt-api/metrics", forwarded=client, headers=cookie_only(renamed), expected=401)
    json_call(cwa.proxy_port, "/bt-api/session", forwarded=other, headers=(("Authorization", "Bearer " + native_token),), method="POST", expected=401)
    print("PASS: real API admission isolation, CWA strong sessions, IP/UA rejection, translation and cross-reader credential isolation", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve-browser", action="store_true")
    args = parser.parse_args()
    with contextlib.ExitStack() as stack:
        runtime, token = prepare(stack, browser=args.serve_browser)
        if args.serve_browser:
            print("READY: native browser edge on 8585", flush=True)
            while True:
                time.sleep(1)
        else:
            test_headers(runtime.readers[0].proxy_environment["BT_CWA_IDENTITY_HEADER"])
            test_sessions(runtime, token)


if __name__ == "__main__":
    main()
