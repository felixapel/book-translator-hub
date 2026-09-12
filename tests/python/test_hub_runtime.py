"""Universal one-container runtime contracts for CWA and Kavita."""

from __future__ import annotations

import http.client
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub_runtime import (
    HubConfig,
    HubConfigError,
    _validate_provider_environment,
    healthcheck,
    healthcheck_providers,
    prepare_runtime,
    process_specs,
    supervise,
)


ROOT = Path(__file__).resolve().parents[2]


def dual_reader_environment() -> dict[str, str]:
    return {
        "BT_ENABLE_CWA": "true",
        "BT_ENABLE_KAVITA": "true",
        "BT_CWA_PUBLIC_ORIGIN": "https://books.example.test",
        "BT_CWA_READER_UPSTREAM": "http://calibre-web:8083",
        "BT_CWA_READER_VERSION": "4.0.6",
        "BT_CWA_AUTH_PROFILE": "reader-session",
        "BT_CWA_READER_CONNECTOR_ID": "01234567-89ab-4cde-8123-0123456789ab",
        "BT_CWA_PUBLISHED_PORT": "8385",
        "BT_KAVITA_PUBLIC_ORIGIN": "https://kavita.example.test",
        "BT_KAVITA_READER_UPSTREAM": "http://kavita:5000",
        "BT_KAVITA_READER_VERSION": "0.9.0.2",
        "BT_KAVITA_AUTH_PROFILE": "reader-session",
        "BT_KAVITA_READER_CONNECTOR_ID": "11234567-89ab-4cde-8123-0123456789ab",
        "BT_KAVITA_PUBLISHED_PORT": "8386",
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "gemini-3.5-flash-lite",
        "LLM_API_KEY": "shared-secret",
        "BT_KAVITA_LLM_PROVIDER": "local",
        "BT_KAVITA_LLM_MODEL": "gemma4-12b",
        "BT_KAVITA_LLM_API_KEY": "",
        "BT_KAVITA_LOCAL_URL": "http://host.docker.internal:8000/v1/chat/completions",
        "BT_MAX_CONCURRENT": "2",
        "BT_MAX_UPSTREAM_INFLIGHT": "2",
        "BT_BATCH_SIZE": "10",
        "BT_BATCH_SOURCE_TOKEN_BUDGET": "450",
        "BT_CLIENT_PREFETCH_GAP_MS": "1000",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }


class HubConfigTests(unittest.TestCase):
    def test_at_least_one_reader_must_be_enabled(self):
        with self.assertRaisesRegex(HubConfigError, "at least one"):
            HubConfig.from_environment({
                "BT_ENABLE_CWA": "false",
                "BT_ENABLE_KAVITA": "false",
            })

    def test_dual_reader_contract_has_distinct_loopback_and_persistent_state(self):
        config = HubConfig.from_environment(dual_reader_environment())

        self.assertEqual(tuple(reader.name for reader in config.readers), ("cwa", "kavita"))
        cwa, kavita = config.readers
        self.assertEqual(cwa.api_port, 8391)
        self.assertEqual(kavita.api_port, 8392)
        self.assertEqual(cwa.proxy_port, 8080)
        self.assertEqual(kavita.proxy_port, 8081)
        self.assertEqual(cwa.environment["DB_PATH"], "/app/data/cwa/translations.db")
        self.assertEqual(kavita.environment["DB_PATH"], "/app/data/kavita/translations.db")
        self.assertEqual(cwa.environment["BT_SESSION_COOKIE_NAME"], "__Host-bt-cwa-session")
        self.assertEqual(
            kavita.environment["BT_SESSION_COOKIE_NAME"],
            "__Host-bt-kavita-session",
        )

    def test_shared_provider_is_inherited_and_present_empty_override_clears_secret(self):
        config = HubConfig.from_environment(dual_reader_environment())
        cwa, kavita = config.readers

        self.assertEqual(cwa.environment["LLM_PROVIDER"], "gemini")
        self.assertEqual(cwa.environment["LLM_API_KEY"], "shared-secret")
        self.assertEqual(kavita.environment["LLM_PROVIDER"], "local")
        self.assertEqual(kavita.environment["LLM_API_KEY"], "")
        self.assertEqual(kavita.environment["LLM_MODEL"], "gemma4-12b")
        self.assertNotIn("BT_KAVITA_LLM_API_KEY", cwa.environment)
        self.assertNotIn("BT_CWA_LLM_API_KEY", kavita.environment)

    def test_reader_session_security_controls_reach_every_enabled_api(self):
        env = dual_reader_environment()
        env.update(
            {
                "BT_SESSION_TTL_SECONDS": "60",
                "BT_SESSION_MAX_ENTRIES": "2500",
                "BT_READER_AUTH_TIMEOUT_SECONDS": "4",
                "BT_READER_AUTH_MAX_RESPONSE_BYTES": "131072",
            }
        )

        config = HubConfig.from_environment(env)

        for reader in config.readers:
            self.assertEqual(reader.environment["BT_SESSION_TTL_SECONDS"], "60")
            self.assertEqual(reader.environment["BT_SESSION_MAX_ENTRIES"], "2500")
            self.assertEqual(
                reader.environment["BT_READER_AUTH_TIMEOUT_SECONDS"], "4"
            )
            self.assertEqual(
                reader.environment["BT_READER_AUTH_MAX_RESPONSE_BYTES"], "131072"
            )

    def test_batch_controls_reach_api_and_browser_without_reader_secrets(self):
        config = HubConfig.from_environment(dual_reader_environment())

        for reader in config.readers:
            self.assertEqual(reader.environment["BT_BATCH_SIZE"], "10")
            self.assertEqual(
                reader.environment["BT_BATCH_SOURCE_TOKEN_BUDGET"], "450"
            )
            self.assertEqual(reader.proxy_environment["BT_BATCH_SIZE"], "10")
            self.assertEqual(
                reader.proxy_environment["BT_CLIENT_PREFETCH_GAP_MS"], "1000"
            )
            self.assertNotIn("LLM_API_KEY", reader.proxy_environment)

    def test_browser_batch_cannot_exceed_api_batch_limit(self):
        env = dual_reader_environment()
        env["BT_MAX_BATCH_PARAGRAPHS"] = "5"

        with self.assertRaisesRegex(
            HubConfigError,
            "BT_BATCH_SIZE must not exceed BT_MAX_BATCH_PARAGRAPHS",
        ):
            HubConfig.from_environment(env)

    def test_remote_provider_without_key_fails_during_plan_not_after_start(self):
        env = dual_reader_environment()
        env["LLM_API_KEY"] = ""

        with self.assertRaisesRegex(HubConfigError, "remote provider requires"):
            HubConfig.from_environment(env)

    def test_total_concurrency_is_split_without_oversubscription(self):
        config = HubConfig.from_environment(dual_reader_environment())

        self.assertEqual(
            sum(int(reader.environment["BT_MAX_CONCURRENT"]) for reader in config.readers),
            2,
        )
        self.assertEqual(
            sum(
                int(reader.environment["BT_MAX_UPSTREAM_INFLIGHT"])
                for reader in config.readers
            ),
            2,
        )

    def test_published_ports_must_not_collide(self):
        env = dual_reader_environment()
        env["BT_KAVITA_PUBLISHED_PORT"] = env["BT_CWA_PUBLISHED_PORT"]

        with self.assertRaisesRegex(HubConfigError, "published ports"):
            HubConfig.from_environment(env)

    def test_single_reader_receives_the_whole_concurrency_budget(self):
        env = dual_reader_environment()
        env["BT_ENABLE_KAVITA"] = "false"

        config = HubConfig.from_environment(env)

        self.assertEqual(len(config.readers), 1)
        self.assertEqual(config.readers[0].environment["BT_MAX_CONCURRENT"], "2")
        self.assertEqual(
            config.readers[0].environment["BT_MAX_UPSTREAM_INFLIGHT"], "2"
        )

    def test_hub_rejects_forwarded_auth_that_requires_a_separate_identity_edge(self):
        env = dual_reader_environment()
        env["BT_ENABLE_KAVITA"] = "false"
        env["BT_CWA_AUTH_PROFILE"] = "authentik-forwarded"

        with self.assertRaisesRegex(HubConfigError, "AUTH_PROFILE is unsupported"):
            HubConfig.from_environment(env)

    def test_process_specs_bind_apis_to_loopback_and_start_one_nginx(self):
        specs = process_specs(HubConfig.from_environment(dual_reader_environment()))

        self.assertEqual(tuple(spec.name for spec in specs), ("api-cwa", "api-kavita", "nginx"))
        self.assertIn("127.0.0.1:8391", specs[0].argv)
        self.assertIn("127.0.0.1:8392", specs[1].argv)
        self.assertEqual(specs[0].environment["BT_READER_TYPE"], "cwa")
        self.assertEqual(specs[1].environment["BT_READER_TYPE"], "kavita")
        self.assertNotIn("BT_KAVITA_LLM_API_KEY", specs[0].environment)

    def test_healthcheck_maps_http_framing_fault_to_unhealthy(self):
        config = HubConfig.from_environment(dual_reader_environment())
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=http.client.BadStatusLine("bad"),
        ):
            self.assertEqual(healthcheck(config), 1)

    def test_local_provider_url_rejects_userinfo(self):
        environment = {
            "LLM_PROVIDER": "local",
            "LLM_MODEL": "gemma4-12b",
            "BT_LOCAL_URL": (
                "http://user:pass@host.docker.internal:8000/v1/chat/completions"
            ),
        }
        with self.assertRaises(HubConfigError):
            _validate_provider_environment(environment, "cwa")

    def test_custom_provider_endpoint_rejects_userinfo(self):
        environment = {
            "LLM_PROVIDER": "openai-compatible",
            "LLM_MODEL": "custom-model",
            "LLM_CUSTOM_ENDPOINT": (
                "https://user@api.example.test/v1/chat/completions"
            ),
            "LLM_CUSTOM_API_KEY": "secret",
        }
        with self.assertRaises(HubConfigError):
            _validate_provider_environment(environment, "cwa")

    def test_blank_custom_api_key_is_stripped_before_requirement_check(self):
        environment = {
            "LLM_PROVIDER": "openai-compatible",
            "LLM_MODEL": "custom-model",
            "LLM_CUSTOM_ENDPOINT": "https://api.example.test/v1/chat/completions",
            "LLM_CUSTOM_API_KEY": "   ",
        }
        with self.assertRaisesRegex(HubConfigError, "dedicated key"):
            _validate_provider_environment(environment, "cwa")

    def test_provider_healthcheck_uses_isolated_reader_env_and_fixed_argv(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((tuple(argv), kwargs))
            return subprocess.CompletedProcess(argv, 0)

        result = healthcheck_providers(
            HubConfig.from_environment(dual_reader_environment()),
            runner=runner,
        )

        self.assertEqual(result, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertNotIn("shared-secret", " ".join(calls[0][0]))
        # Provider probes run concurrently, so completion order is not
        # deterministic: match invocations by their isolated reader env.
        by_provider = {call[1]["env"]["LLM_PROVIDER"]: call for call in calls}
        self.assertEqual(set(by_provider), {"gemini", "local"})
        for call in calls:
            self.assertLessEqual(call[1]["timeout"], 120)

    def test_prepare_runtime_renders_both_readers_and_preflights_before_start(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append((tuple(argv), kwargs))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            runtime = root / "nginx"
            data.mkdir(mode=0o700)

            prepare_runtime(
                HubConfig.from_environment(dual_reader_environment()),
                data_root=data,
                runtime_dir=runtime,
                template_path=ROOT / "proxy" / "nginx.conf.template",
                runner=runner,
            )

            self.assertTrue((runtime / "proxy-cwa.conf").is_file())
            self.assertTrue((runtime / "proxy-kavita.conf").is_file())
            self.assertTrue((runtime / "browser-config-cwa.json").is_file())
            self.assertTrue((runtime / "browser-config-kavita.json").is_file())
            self.assertEqual(stat.S_IMODE((data / "cwa").stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE((data / "kavita").stat().st_mode), 0o700)
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1][0][:2], ("nginx", "-t"))

    def test_prepare_runtime_accepts_private_operator_group_data_root(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            runtime = Path(directory) / "nginx"
            data.mkdir()
            data.chmod(0o2750)

            prepare_runtime(
                HubConfig.from_environment(dual_reader_environment()),
                data_root=data,
                runtime_dir=runtime,
                template_path=ROOT / "proxy" / "nginx.conf.template",
                runner=lambda *_args, **_kwargs: None,
            )

            self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o2750)

    def test_supervisor_terminates_every_child_when_one_exits(self):
        processes = []

        class FakeProcess:
            def __init__(self, name):
                self.name = name
                self.terminated = False
                self.waited = False

            def poll(self):
                return 7 if self.name == "api-cwa" else None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout=None):
                self.waited = True
                return 7 if self.name == "api-cwa" else 0

            def kill(self):
                self.terminated = True

        def popen(argv, *, env):
            name = ("api-cwa", "api-kavita", "nginx")[len(processes)]
            process = FakeProcess(name)
            processes.append(process)
            return process

        result = supervise(
            HubConfig.from_environment(dual_reader_environment()),
            popen_factory=popen,
            sleep=lambda _: None,
            install_signal_handlers=False,
        )

        self.assertEqual(result, 7)
        self.assertEqual(len(processes), 3)
        self.assertTrue(all(process.terminated for process in processes))
        self.assertTrue(all(process.waited for process in processes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
