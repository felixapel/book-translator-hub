"""Security contracts for the built image and recommended topology."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def run_entrypoint_ui_version(version, assets, missing=None, root=None):
    """Execute the entrypoint's exact UI-version block against temporary assets."""
    if root is None:
        with tempfile.TemporaryDirectory() as raw_dir:
            return run_entrypoint_ui_version(version, assets, missing, Path(raw_dir))
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    start = entrypoint.index('BT_UI_VERSION="$(cat /app/VERSION')
    end = entrypoint.index('\n\n# Reader upstream normalization', start)
    root = Path(root)
    static = root / "static"
    static.mkdir(exist_ok=True)
    (root / "VERSION").write_text(version, encoding="utf-8")
    for name, content in assets.items():
        target = static / name
        if name == missing:
            target.unlink(missing_ok=True)
        else:
            target.write_text(content, encoding="utf-8")
    version_path = str(root / "VERSION").replace("\\", "/")
    static_path = str(static).replace("\\", "/")
    block = entrypoint[start:end].replace("/app/VERSION", version_path).replace(
        "/app/static", static_path
    )
    shell = shutil.which("sh") or r"C:\Program Files\Git\bin\bash.exe"
    shell_options = "-leu" if shell.lower().endswith("bash.exe") else "-eu"
    return subprocess.run(
        [shell, shell_options, "-c", block + '\nprintf "%s" "$BT_UI_VERSION"'],
        text=True, capture_output=True, check=False,
    )


class ContainerContractTests(unittest.TestCase):
    def test_entrypoint_ui_version_tracks_all_packaged_overlay_assets(self):
        assets = {
            "loader.js": "loader baseline\n",
            "translator.js": "translator baseline\n",
            "translator.css": "css baseline\n",
        }
        with tempfile.TemporaryDirectory() as raw_dir:
            first = run_entrypoint_ui_version("2.4.1\n", assets, root=raw_dir)
            second = run_entrypoint_ui_version("2.4.1\n", assets, root=raw_dir)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            self.assertRegex(first.stdout, r"^2\.4\.1-[a-f0-9]{12}$")
            for changed_name in assets:
                changed = dict(assets)
                changed[changed_name] += "changed\n"
                result = run_entrypoint_ui_version("2.4.1\n", changed, root=raw_dir)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(result.stdout, first.stdout, changed_name)
            missing = run_entrypoint_ui_version("2.4.1\n", assets, missing="translator.css", root=raw_dir)
            self.assertNotEqual(missing.returncode, 0)

    def test_image_declares_the_existing_stable_non_root_identity(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn("addgroup -S -g 102 appuser", dockerfile)
        self.assertIn("adduser -S -D -H -u 101 -G appuser appuser", dockerfile)
        self.assertRegex(dockerfile, r"(?m)^USER appuser$")
        self.assertNotIn('VOLUME ["/app/data"]', dockerfile)
        for obsolete in ("gosu", "shadow=", "linux-pam=", "chown -R"):
            self.assertNotIn(obsolete, dockerfile)
        self.assertNotIn("COPY *.py", dockerfile)
        for runtime_module in (
            "auth.py", "cache.py", "server.py", "singleflight.py",
            "translator.py", "work_budget.py", "reader_session.py",
        ):
            self.assertIn(runtime_module, dockerfile)
        for operator_input in (
            "btctl.py",
            "btctl_container.py",
            "btctl_paths.py",
            "docker-cli",
            "git=",
        ):
            self.assertNotIn(operator_input, dockerfile)

    def test_operator_image_is_distinct_from_the_production_runtime(self):
        dockerfile = (ROOT / "Dockerfile.btctl").read_text()
        self.assertIn("FROM source-exporter AS operator", dockerfile)
        self.assertIn("btctl_reconfigure.py", dockerfile)
        self.assertIn("btctl_hub.py", dockerfile)
        self.assertIn('ENTRYPOINT ["python3", "/opt/btctl/btctl.py"]', dockerfile)
        self.assertNotIn("USER appuser", dockerfile)
        self.assertNotIn("docker-entrypoint.sh", dockerfile)

    def test_bootstrap_smoke_shares_only_its_private_tmpdir_with_sibling_docker(self):
        smoke = (ROOT / "scripts" / "btctl-bootstrap-smoke.sh").read_text()
        self.assertIn(
            '--mount "type=bind,src=$TEMPORARY,dst=$TEMPORARY"',
            smoke,
        )
        self.assertIn('--env "TMPDIR=$TEMPORARY"', smoke)
        dispatcher = smoke.split("run_without_host_tooling() {", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertIn("--cap-add DAC_READ_SEARCH", dispatcher)
        self.assertIn("--cap-add DAC_OVERRIDE", dispatcher)

    def test_entrypoint_never_changes_ownership_or_escalates(self):
        entrypoint = (ROOT / "docker-entrypoint.sh").read_text()
        for forbidden in ("gosu", "chown", "appuser gunicorn"):
            self.assertNotIn(forbidden, entrypoint)
        self.assertIn('BT_ROLE="${BT_ROLE:-auto}"', entrypoint)
        self.assertIn('exec gunicorn --bind', entrypoint)
        self.assertIn('exec nginx -c /app/proxy/nginx-main.conf', entrypoint)
        self.assertIn("api|proxy|all|hub", entrypoint)
        self.assertIn("exec python /app/hub_runtime.py", entrypoint)
        self.assertIn("umask 027", entrypoint)
        self.assertIn('stat -c %a /app/data', entrypoint)
        self.assertNotIn("chmod 700 /app/data", entrypoint)

    def test_image_packages_the_hub_without_publishing_an_api_port(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("hub_runtime.py", dockerfile)
        self.assertIn("EXPOSE 8390 8080 8081", dockerfile)
        self.assertIn("hub_runtime.py --healthcheck", dockerfile)

    def test_api_roles_initialize_cache_before_serving(self):
        entrypoint = (ROOT / "docker-entrypoint.sh").read_text()
        self.assertIn("from cache import init_db; init_db()", entrypoint)
        api_branch = entrypoint.split("    api)", 1)[1].split("        ;;", 1)[0]
        self.assertLess(
            api_branch.index("initialize_cache"),
            api_branch.index("exec gunicorn"),
        )
        combined = entrypoint.split("# Legacy one-container compatibility.", 1)[1]
        self.assertLess(
            combined.index("initialize_cache"),
            combined.index("start_api &"),
        )

    def test_non_root_nginx_writes_only_below_tmp(self):
        config = (ROOT / "proxy" / "nginx-main.conf").read_text()
        for directive in (
            "pid /tmp/nginx/nginx.pid;",
            "client_body_temp_path /tmp/nginx/client_temp;",
            "proxy_temp_path /tmp/nginx/proxy_temp;",
            "fastcgi_temp_path /tmp/nginx/fastcgi_temp;",
            "uwsgi_temp_path /tmp/nginx/uwsgi_temp;",
            "scgi_temp_path /tmp/nginx/scgi_temp;",
            "access_log /dev/stdout bt_privacy;",
            "error_log /dev/stderr warn;",
            "include /tmp/nginx/proxy*.conf;",
        ):
            self.assertIn(directive, config)
        self.assertIn("log_format bt_privacy", config)
        for sensitive_value in (
            "$remote_addr",
            "$http_user_agent",
            "$http_cookie",
            "$http_referer",
            "$request_uri",
            "$query_string",
            "$args",
        ):
            self.assertNotIn(sensitive_value, config)
        self.assertNotRegex(config, r"\$request(?:\s|['\"])")
        self.assertNotRegex(config, r"(?m)^\s*user\s+")

    def test_proxy_backend_connections_have_a_bounded_timeout(self):
        template = (ROOT / "proxy" / "nginx.conf.template").read_text()
        self.assertGreaterEqual(template.count("proxy_connect_timeout 2s;"), 2)

    def test_proxy_uses_validated_origin_and_sanitized_forwarding(self):
        template = (ROOT / "proxy" / "nginx.conf.template").read_text()
        entrypoint = (ROOT / "docker-entrypoint.sh").read_text()
        compose = (ROOT / "docker-compose.yml").read_text()
        smoke = (ROOT / "scripts" / "container-smoke.sh").read_text()

        self.assertNotIn("client_max_body_size 0;", template)
        self.assertIn("client_max_body_size ${BT_CWA_MAX_BODY_SIZE};", template)
        self.assertIn("absolute_redirect off;", template)
        self.assertNotIn("$http_x_forwarded_proto", template)
        self.assertEqual(
            template.count("proxy_set_header Host ${BT_PUBLIC_HOST};"), 3
        )
        self.assertEqual(
            template.count("proxy_set_header X-Forwarded-Proto ${BT_PUBLIC_SCHEME};"),
            3,
        )
        self.assertEqual(
            template.count("proxy_set_header X-Forwarded-For $remote_addr;"), 3
        )
        self.assertEqual(
            template.count("proxy_set_header User-Agent $http_user_agent;"), 3
        )
        self.assertNotIn("$proxy_add_x_forwarded_for", template)
        self.assertNotIn("$http_x_forwarded_for", template)
        self.assertIn("proxy/render_config.py", entrypoint)
        self.assertNotIn("envsubst", entrypoint)
        self.assertIn(
            "BT_PUBLIC_ORIGIN=${BT_PUBLIC_ORIGIN:?Set BT_PUBLIC_ORIGIN to the exact browser-facing origin}",
            compose,
        )
        self.assertNotIn("BT_PUBLIC_ORIGIN=${BT_PUBLIC_ORIGIN:-", compose)
        self.assertIn("BT_CWA_MAX_BODY_SIZE=${BT_CWA_MAX_BODY_SIZE:-2g}", compose)
        self.assertIn("BT_CWA_IDENTITY_HEADER=${BT_CWA_IDENTITY_HEADER:-Remote-User}", compose)
        self.assertIn('proxy_set_header ${BT_CWA_IDENTITY_HEADER} "";', template)
        self.assertIn("BT_PUBLIC_ORIGIN=https://books.example.test:8443", smoke)

    def test_compose_recommends_independent_hardened_roles(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertEqual(compose.count("    build: .\n"), 1)
        self.assertEqual(
            compose.count("    image: cwa-ebook-translate-plugin:local\n"),
            2,
        )
        self.assertRegex(compose, r"(?m)^  book-translator-api:$")
        self.assertRegex(compose, r"(?m)^  book-translator-proxy:$")
        self.assertIn("BT_ROLE=api", compose)
        self.assertIn("BT_ROLE=proxy", compose)
        self.assertIn("BT_API_UPSTREAM=http://translator-api:8390", compose)
        self.assertIn("- translator-api", compose)
        self.assertIn("BT_TRUSTED_PROXIES=172.30.39.3/32", compose)
        self.assertIn("- subnet: 172.30.39.0/24", compose)
        self.assertIn("BT_AUTH_MODE=cwa_session", compose)
        api_environment = compose.split("  book-translator-api:", 1)[1].split(
            "  book-translator-proxy:", 1
        )[0]
        self.assertIn(
            "BT_PUBLIC_ORIGIN=${BT_PUBLIC_ORIGIN:?Set BT_PUBLIC_ORIGIN to the exact browser-facing origin}",
            api_environment,
        )
        self.assertIn("BT_CWA_AUTH_URL=http://calibre-web:8083/ajax/emailstat", compose)
        self.assertIn("BT_ALLOW_PRIVATE_LAN=false", compose)
        api_service = compose.split("  book-translator-api:", 1)[1].split(
            "  book-translator-proxy:", 1
        )[0]
        self.assertIn("cwa-net:", api_service)
        self.assertGreaterEqual(compose.count("read_only: true"), 2)
        self.assertGreaterEqual(compose.count("no-new-privileges:true"), 2)
        self.assertGreaterEqual(compose.count("cap_drop:"), 2)
        self.assertGreaterEqual(compose.count("- ALL"), 2)
        self.assertGreaterEqual(compose.count("/tmp:rw,noexec,nosuid"), 2)

    def test_universal_compose_is_one_hardened_service_with_two_reader_edges(self):
        compose = (ROOT / "docker-compose.hub.yml").read_text(encoding="utf-8")
        example = (ROOT / ".env.hub.example").read_text(encoding="utf-8")

        self.assertEqual(compose.count("    build: .\n"), 1)
        self.assertRegex(compose, r"(?m)^  book-translator-hub:$")
        self.assertNotRegex(compose, r"(?m)^  book-translator-(?:api|proxy):$")
        self.assertIn("BT_ROLE: hub", compose)
        self.assertIn('"${BT_CWA_PUBLISHED_PORT:-8385}:8080"', compose)
        self.assertIn('"${BT_KAVITA_PUBLISHED_PORT:-8386}:8081"', compose)
        self.assertIn("read_only: true", compose)
        self.assertIn("no-new-privileges:true", compose)
        self.assertIn("cap_drop:", compose)
        self.assertIn("- ALL", compose)
        self.assertIn("/tmp:rw,noexec,nosuid", compose)
        self.assertIn("BT_ENABLE_CWA=true", example)
        self.assertIn("BT_ENABLE_KAVITA=true", example)
        self.assertNotRegex(example, r"(?m)^LLM_API_KEY=.+$")

    def test_ci_runs_both_roles_with_the_production_sandbox(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        smoke_path = ROOT / "scripts" / "container-smoke.sh"
        smoke = smoke_path.read_text()
        self.assertTrue(smoke_path.stat().st_mode & 0o111)
        self.assertIn('./scripts/container-smoke.sh "$SMOKE_IMAGE" "$SMOKE_PREFIX"', workflow)
        self.assertIn("docker rm -f -v", smoke)
        for container in (
            "$EDGE_CONTAINER",
            "$OUTPOST_CONTAINER",
            "$PROXY_CONTAINER",
            "$API_CONTAINER",
        ):
            self.assertIn(container, smoke)
        for token in (
            "BT_ROLE=api",
            "BT_ROLE=proxy",
            "BT_AUTH_MODE=token",
            "BT_API_TOKEN=${SMOKE_TOKEN}",
            "--read-only",
            "--cap-drop ALL",
            "--security-opt no-new-privileges:true",
            "docker image inspect \"$SMOKE_IMAGE\" --format '{{.Config.User}}'",
            "BT_AUTH_MODE=reader_session",
            "BT_READER_TYPE=kavita",
            'KAVITA_SMOKE_VERSION="${KAVITA_SMOKE_VERSION:-0.9.0.2}"',
            "0.9.0.2|0.9.1.4",
            'KAVITA_SMOKE_CONTRACT="kavita-${KAVITA_SMOKE_VERSION}-epub-v1"',
            "test_kavita_auth_fixture.py",
            "/bt-api/session",
        ):
            self.assertIn(token, smoke)
        self.assertNotIn("gosu", smoke)

    def test_hub_smoke_proves_two_listeners_and_fail_fast_supervision(self):
        path = ROOT / "scripts" / "hub-container-smoke.sh"
        source = path.read_text(encoding="utf-8")
        self.assertTrue(path.stat().st_mode & 0o111)
        for token in (
            "BT_ROLE=hub",
            "BT_ENABLE_CWA=true",
            "BT_ENABLE_KAVITA=true",
            "proxy-cwa.conf",
            "proxy-kavita.conf",
            "127.0.0.1:8391",
            "reader_session_key",
            "State.Status",
        ):
            self.assertIn(token, source)
        self.assertNotRegex(source, r"-p[^\n]*839[12]")

    def test_kavita_container_fixture_is_literal_compilable_python(self):
        fixture = ROOT / "tests" / "python" / "test_kavita_auth_fixture.py"
        source = fixture.read_text(encoding="utf-8")
        compile(source, str(fixture), "exec")
        self.assertIn('KAVITA_FIXTURE_VERSION', source)
        self.assertIn('{"0.9.0.2", "0.9.1.4"}', source)
        self.assertIn('"kavitaVersion": KAVITA_VERSION', source)
        self.assertIn('class="book-content"', source.replace('\\"', '"'))

    def test_ca_profile_certifies_the_combined_role_without_publishing_api(self):
        smoke_path = ROOT / "scripts" / "ca-container-smoke.sh"
        smoke = smoke_path.read_text(encoding="utf-8")
        self.assertTrue(smoke_path.stat().st_mode & 0o111)
        for token in (
            "BT_ROLE=all",
            "BT_AUTH_MODE=cwa_session",
            "test_cwa_strong_fixture.py",
            "BT_LOCAL_URL=",
            "--user 101:102",
            "--read-only",
            "--cap-drop ALL",
            "no-new-privileges:true",
            "type=bind,src=${DATA_DIR},dst=/app/data",
            "chown 101:102 /data",
            "chmod 0700 /data",
            "wrong ownership or mode",
            "provider-policy",
            "provider_policy",
            "docker rm -f",
            "recreate",
            "cached",
        ):
            self.assertIn(token, smoke)
        self.assertRegex(smoke, r"-p\s+127\.0\.0\.1::8080")
        self.assertNotRegex(smoke, r"-p[^\n]*8390")
        self.assertNotIn("type=volume", smoke)
        self.assertNotIn("docker volume", smoke)
        self.assertIn("./scripts/ca-container-smoke.sh", (
            ROOT / ".github" / "workflows" / "ci.yml"
        ).read_text())

    def test_runtime_image_declares_public_oci_identity(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        for token in (
            "ARG BUILD_VERSION=dev",
            "ARG BUILD_REVISION=unknown",
            'org.opencontainers.image.source="https://github.com/felixapel/book-translator-hub"',
            'org.opencontainers.image.licenses="GPL-3.0-only"',
            'org.opencontainers.image.version="$BUILD_VERSION"',
            'org.opencontainers.image.revision="$BUILD_REVISION"',
        ):
            self.assertIn(token, dockerfile)

    def test_lifecycle_smoke_can_remove_non_root_managed_data(self):
        smoke = (ROOT / "scripts" / "btctl-lifecycle-smoke.sh").read_text()
        cleanup = smoke.split("cleanup() {", 1)[1].split("}\ntrap cleanup EXIT", 1)[0]
        self.assertIn("docker run --rm --user 0:0", cleanup)
        self.assertIn("type=bind,src=${ROOT_DIR},dst=/cleanup", cleanup)
        self.assertIn("chmod -R u+rwX,g+rwX /cleanup", cleanup)
        self.assertLess(
            cleanup.index("chmod -R u+rwX,g+rwX /cleanup"),
            cleanup.index('rm -rf -- "$ROOT_DIR"'),
        )

    def test_lifecycle_cwa_fixture_is_literal_compilable_python(self):
        smoke = (ROOT / "scripts" / "btctl-lifecycle-smoke.sh").read_text()
        marker = 'cat >"$CWA_FIXTURE" <<\'PY\'\n'
        self.assertIn(marker, smoke)
        fixture = smoke.split(marker, 1)[1].split("\nPY\n", 1)[0]
        compile(fixture, "cwa-lifecycle-fixture.py", "exec")
        self.assertIn(
            '--mount "type=bind,src=${CWA_FIXTURE},dst=/app/cwa-fixture.py,readonly"',
            smoke,
        )
        self.assertIn(
            '--entrypoint python "$CWA_IMAGE" /app/cwa-fixture.py',
            smoke,
        )

    def test_lifecycle_smoke_proves_kavita_isolated_from_cwa(self):
        smoke = (ROOT / "scripts" / "btctl-lifecycle-smoke.sh").read_text()
        for token in (
            "BT_READER_TYPE=kavita",
            "BT_READER_VERSION=0.9.0.2",
            "test_kavita_auth_fixture.py",
            'assert_doctor "$KAVITA_ENV"',
            'assert_doctor "$FRESH_ENV"',
            'test ! -e "$KAVITA_DATA/reader_session_key"',
            'docker inspect "$CWA_CONTAINER"',
        ):
            self.assertIn(token, smoke)

    def test_image_auth_defaults_fail_closed_and_proxy_forwards_cwa_cookie(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        entrypoint = (ROOT / "docker-entrypoint.sh").read_text()
        proxy = (ROOT / "proxy" / "nginx.conf.template").read_text()
        self.assertNotRegex(dockerfile, r"(?m)^ENV BT_(?:API_TOKEN|AUTH_MODE)=")
        self.assertIn('mode="${BT_AUTH_MODE:-token}"', entrypoint)
        self.assertIn("validate_api_auth", entrypoint)
        self.assertIn("BT_API_TOKEN is required", entrypoint)
        self.assertIn("disabled auth requires BT_ALLOW_INSECURE_AUTH=true", entrypoint)
        self.assertIn("POST $http_cookie;", proxy)
        self.assertNotIn("default $http_cookie;", proxy)
        self.assertIn("POST $http_authorization;", proxy)
        self.assertNotIn("default $http_authorization;", proxy)
        self.assertIn("DELETE $bt${BT_PROXY_NAMESPACE}_session_cookie;", proxy)
        self.assertIn(
            "proxy_set_header Cookie $bt${BT_PROXY_NAMESPACE}_session_route_cookie;",
            proxy,
        )
        self.assertIn('proxy_set_header ${BT_CWA_IDENTITY_HEADER} "";', proxy)
        self.assertIn('proxy_set_header X-BT-Subject "";', proxy)
        self.assertIn('proxy_set_header X-BT-Roles "";', proxy)

    def test_unraid_helpers_preserve_the_non_root_sandbox(self):
        adapter = (ROOT / "btctl_unraid.py").read_text()
        api_template = (
            ROOT / "deploy" / "unraid" / "my-cwa-translate-api.xml.tmpl"
        ).read_text()
        proxy_template = (
            ROOT / "deploy" / "unraid" / "my-cwa-translate-proxy.xml.tmpl"
        ).read_text()
        for token in (
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "uid=101,gid=102",
        ):
            self.assertIn(token, api_template)
            self.assertIn(token, proxy_template)
        self.assertIn('"BT_ROLE": "api"', adapter)
        self.assertIn(
            "os.chown(candidate, 101, 102, follow_symlinks=False)", adapter
        )
        self.assertIn("publish_port=None", adapter)


if __name__ == "__main__":
    unittest.main(verbosity=2)
