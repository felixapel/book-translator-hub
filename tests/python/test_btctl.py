import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from btctl import _docker_for, _load_values, _parser

from btctl_core import (
    ConfigError,
    DeploymentPlan,
    DeploymentState,
    InstallConfig,
    InstallAttemptStore,
    OperationLock,
    ReleaseIdentity,
    StateStore,
    compatibility_tier,
    parse_env_text,
    redact_mapping,
    release_identity_from_checkout,
)


ROOT = Path(__file__).resolve().parents[2]
BTCTL = ROOT / "btctl.py"


class ParserTests(unittest.TestCase):
    def test_doctor_deep_is_explicit_and_defaults_off(self):
        parser = _parser()
        ordinary = parser.parse_args(["doctor", "--env", "install.env"])
        deep = parser.parse_args(
            ["doctor", "--env", "install.env", "--deep"]
        )

        self.assertFalse(ordinary.deep)
        self.assertTrue(deep.deep)


class ReleaseIdentityTests(unittest.TestCase):
    def test_clean_checkout_derives_immutable_local_image(self):
        identity = ReleaseIdentity.from_checkout(
            version="2.2.0",
            sha="0123456789abcdef0123456789abcdef01234567",
            clean=True,
        )

        self.assertEqual(identity.image, "local/cwa-translate:2.2.0-0123456789ab")

    def test_dirty_or_non_semver_checkout_is_rejected(self):
        with self.assertRaisesRegex(ConfigError, "clean checkout"):
            ReleaseIdentity.from_checkout(version="2.2.0", sha="a" * 40, clean=False)
        with self.assertRaisesRegex(ConfigError, "VERSION"):
            ReleaseIdentity.from_checkout(version="latest", sha="a" * 40, clean=True)

    def test_replacement_ref_cannot_substitute_another_tree_for_head(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)

            def git(*arguments: str) -> str:
                return subprocess.run(
                    ["git", *arguments],
                    cwd=repository,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()

            git("init", "-b", "main")
            git("config", "user.name", "Replacement Test")
            git("config", "user.email", "replacement@example.invalid")
            (repository / "VERSION").write_text("2.2.0\n", encoding="utf-8")
            (repository / "payload.txt").write_text("canonical\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "canonical")
            canonical = git("rev-parse", "HEAD")

            (repository / "payload.txt").write_text("replacement\n", encoding="utf-8")
            git("add", "payload.txt")
            replacement_tree = git("write-tree")
            replacement = subprocess.run(
                ["git", "commit-tree", replacement_tree],
                cwd=repository,
                check=True,
                input="replacement\n",
                capture_output=True,
                text=True,
            ).stdout.strip()
            git("replace", canonical, replacement)
            git("reset", "--hard", canonical)
            self.assertEqual(git("status", "--porcelain=v1"), "")

            with self.assertRaisesRegex(ConfigError, "clean checkout"):
                release_identity_from_checkout(repository)


class DockerEnvironmentTests(unittest.TestCase):
    def test_unraid_native_path_forces_the_local_socket(self):
        config = mock.Mock(install_profile="unraid")
        with tempfile.TemporaryDirectory() as directory:
            lock_directory = Path(directory) / "lock"
            socket_metadata = mock.Mock(st_mode=stat.S_IFSOCK)
            lock_metadata = mock.Mock(
                st_mode=stat.S_IFDIR | 0o700,
                st_uid=0,
            )

            def metadata(path: Path):
                if path == Path("/var/run/docker.sock"):
                    return socket_metadata
                self.assertEqual(path, lock_directory)
                return lock_metadata

            with (
                mock.patch("btctl.os.geteuid", return_value=0),
                mock.patch("btctl._UNRAID_LOCK_DIRECTORY", lock_directory),
                mock.patch("btctl.Path.lstat", autospec=True, side_effect=metadata),
                mock.patch.dict(
                    os.environ,
                    {
                        "DOCKER_HOST": "tcp://remote.example:2376",
                        "DOCKER_CONTEXT": "remote",
                        "DOCKER_TLS_VERIFY": "1",
                        "DOCKER_CERT_PATH": "/tmp/certs",
                    },
                    clear=False,
                ),
            ):
                _docker_for(config)

                self.assertEqual(
                    os.environ["DOCKER_HOST"], "unix:///var/run/docker.sock"
                )
                self.assertEqual(
                    os.environ["BTCTL_LOCK_DIRECTORY"], str(lock_directory)
                )
                self.assertNotIn("DOCKER_CONTEXT", os.environ)
                self.assertNotIn("DOCKER_TLS_VERIFY", os.environ)
                self.assertNotIn("DOCKER_CERT_PATH", os.environ)


class EnvironmentSnapshotTests(unittest.TestCase):
    def test_container_operator_rejects_environment_snapshot_digest_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "install.env"
            environment.write_text("BT_INSTALL_NAME=first\n", encoding="utf-8")
            environment.chmod(0o600)
            metadata = environment.stat()

            with (
                mock.patch.dict(
                    os.environ,
                    {"BTCTL_EXPECTED_ENV_SHA256": "0" * 64},
                    clear=False,
                ),
                mock.patch(
                    "btctl.os.fstat",
                    return_value=mock.Mock(
                        st_mode=metadata.st_mode,
                        st_nlink=1,
                        st_size=metadata.st_size,
                        st_uid=0,
                    ),
                ),
            ):
                with self.assertRaisesRegex(ConfigError, "snapshot changed"):
                    _load_values(environment)

    def test_container_operator_accepts_the_bound_root_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = Path(directory) / "install.env"
            payload = b"BT_INSTALL_NAME=first\n"
            environment.write_bytes(payload)
            environment.chmod(0o600)
            metadata = environment.stat()

            with (
                mock.patch.dict(
                    os.environ,
                    {"BTCTL_EXPECTED_ENV_SHA256": hashlib.sha256(payload).hexdigest()},
                    clear=False,
                ),
                mock.patch(
                    "btctl.os.fstat",
                    return_value=mock.Mock(
                        st_mode=metadata.st_mode,
                        st_nlink=1,
                        st_size=metadata.st_size,
                        st_uid=0,
                    ),
                ),
            ):
                values = _load_values(environment)

            self.assertEqual(values["BT_INSTALL_NAME"], "first")


class OperationLockTests(unittest.TestCase):
    def test_same_state_directory_allows_only_one_lifecycle_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"

            with OperationLock(state_dir):
                with self.assertRaisesRegex(ConfigError, "already in progress"):
                    with OperationLock(state_dir):
                        self.fail("a second lifecycle operation acquired the lock")

            with OperationLock(state_dir):
                self.assertFalse(state_dir.exists())

    def test_read_only_lock_never_creates_a_missing_state_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "missing-state"

            with OperationLock(state_dir, create=False):
                self.assertFalse(state_dir.exists())

            self.assertFalse(state_dir.exists())
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_container_lock_mount_serializes_without_exposing_state_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock_directory = root / "mounted-lock-parent"
            lock_directory.mkdir()
            first_state = root / "first-parent" / "state"
            second_state = root / "second-parent" / "state"

            with mock.patch.dict(
                os.environ,
                {"BTCTL_LOCK_DIRECTORY": str(lock_directory)},
            ):
                with OperationLock(first_state):
                    with self.assertRaisesRegex(ConfigError, "already in progress"):
                        with OperationLock(second_state):
                            self.fail("the dedicated container lock was bypassed")

            self.assertFalse(first_state.exists())
            self.assertFalse(second_state.exists())


class InstallConfigTests(unittest.TestCase):
    def setUp(self):
        self.identity = ReleaseIdentity.from_checkout(
            version="2.2.0", sha="b" * 40, clean=True
        )
        self.base = {
            "BT_INSTALL_PROFILE": "compose-existing",
            "BT_INGRESS_MODE": "published",
            "BT_AUTH_PROFILE": "cwa-session",
            "BT_PUBLIC_ORIGIN": "https://books.example.test",
            "CWA_UPSTREAM": "http://calibre-web-automated:8083",
            "BT_CWA_CONTAINER": "calibre-web-automated",
            "BT_CWA_NETWORK": "cwa_default",
            "BT_CWA_VERSION": "4.0.6",
            "BT_STATE_DIR": "/srv/cwa-translate/state",
            "BT_DATA_DIR": "/srv/cwa-translate/data",
            "BT_BACKUP_DIR": "/srv/cwa-translate/backups",
            "BT_PROXY_PORT": "8385",
            "LLM_PROVIDER": "local",
            "LLM_MODEL": "gemma4-12b",
            "BT_LOCAL_URL": "http://host.docker.internal:2819/v1/chat/completions",
        }

    def test_cwa_session_profile_derives_safe_runtime_contract(self):
        config = InstallConfig.from_mapping(self.base, self.identity)
        api = config.api_environment()
        proxy = config.proxy_environment()

        self.assertEqual(api["BT_AUTH_MODE"], "reader_session")
        self.assertEqual(api["BT_CACHE_OPERATOR_GROUP_ACCESS"], "true")
        self.assertEqual(proxy["BT_BROWSER_AUTH_MODE"], "reader_session")
        self.assertEqual(proxy["BT_BROWSER_CREDENTIALS"], "same-origin")
        self.assertEqual(
            api["BT_READER_AUTH_URL"],
            "http://calibre-web-automated:8083/ajax/emailstat",
        )
        self.assertEqual(api["BT_TRUSTED_PROXY_HOST"], "translator-proxy")
        self.assertEqual(proxy["BT_CWA_IDENTITY_HEADER"], "Remote-User")
        self.assertEqual(config.image, self.identity.image)
        self.assertNotIn("CWA_UPSTREAM", api)
        self.assertNotIn("LLM_API_KEY", proxy)
        self.assertNotIn("BT_INSTALL_PROFILE", api)
        self.assertNotIn("BT_IMAGE", api)
        self.assertNotIn("BT_ALLOW_INSECURE_AUTH", api)

    def test_adaptive_batch_controls_are_validated_and_reach_split_roles(self):
        values = {
            **self.base,
            "BT_BATCH_SIZE": "10",
            "BT_BATCH_SOURCE_TOKEN_BUDGET": "450",
            "BT_BATCH_MAX_TOKENS": "1200",
            "BT_CLIENT_PREFETCH_GAP_MS": "1000",
            "BT_MAX_BATCH_PARAGRAPHS": "50",
        }

        config = InstallConfig.from_mapping(values, self.identity)

        self.assertEqual(config.api_environment()["BT_BATCH_SIZE"], "10")
        self.assertEqual(
            config.api_environment()["BT_BATCH_SOURCE_TOKEN_BUDGET"], "450"
        )
        self.assertEqual(config.api_environment()["BT_BATCH_MAX_TOKENS"], "1200")
        self.assertEqual(
            config.api_environment()["BT_MAX_BATCH_PARAGRAPHS"], "50"
        )
        self.assertEqual(config.proxy_environment()["BT_BATCH_SIZE"], "10")
        self.assertEqual(
            config.proxy_environment()["BT_CLIENT_PREFETCH_GAP_MS"], "1000"
        )
        self.assertEqual(config.public_contract()["batch_size"], 10)

        invalid = {
            "BT_BATCH_SIZE": ("0", "1 to 50"),
            "BT_BATCH_SOURCE_TOKEN_BUDGET": ("-1", "0 to"),
            "BT_BATCH_MAX_TOKENS": ("0", "1 to"),
            "BT_CLIENT_PREFETCH_GAP_MS": ("10001", "0 to 10000"),
        }
        for name, (value, message) in invalid.items():
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, message):
                InstallConfig.from_mapping(
                    {**values, name: value}, self.identity
                )

    def test_browser_batch_cannot_exceed_api_batch_limit(self):
        with self.assertRaisesRegex(
            ConfigError, "BT_BATCH_SIZE must not exceed BT_MAX_BATCH_PARAGRAPHS"
        ):
            InstallConfig.from_mapping(
                {
                    **self.base,
                    "BT_BATCH_SIZE": "10",
                    "BT_MAX_BATCH_PARAGRAPHS": "5",
                },
                self.identity,
            )

    def test_legacy_and_generic_cwa_reader_inputs_normalize_identically(self):
        legacy = InstallConfig.from_mapping(self.base, self.identity)
        generic_values = {
            **self.base,
            "BT_READER_TYPE": "cwa",
            "BT_READER_UPSTREAM": self.base["CWA_UPSTREAM"],
            "BT_READER_CONTAINER": self.base["BT_CWA_CONTAINER"],
            "BT_READER_NETWORK": self.base["BT_CWA_NETWORK"],
            "BT_READER_VERSION": self.base["BT_CWA_VERSION"],
        }
        generic = InstallConfig.from_mapping(generic_values, self.identity)

        self.assertEqual(generic.reader_type, "cwa")
        self.assertEqual(generic.reader_contract_version, "cwa-epub-v1")
        self.assertEqual(generic.public_contract(), legacy.public_contract())
        self.assertEqual(
            DeploymentPlan.from_config(generic).config_fingerprint,
            DeploymentPlan.from_config(legacy).config_fingerprint,
        )

    def test_conflicting_generic_and_legacy_cwa_inputs_are_rejected(self):
        aliases = {
            "BT_READER_UPSTREAM": "http://other:8083",
            "BT_READER_CONTAINER": "other",
            "BT_READER_NETWORK": "other_default",
            "BT_READER_VERSION": "4.0.7",
        }
        for name, value in aliases.items():
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, name):
                InstallConfig.from_mapping(
                    {**self.base, "BT_READER_TYPE": "cwa", name: value},
                    self.identity,
                )

    def test_exact_certified_kavita_reader_contract(self):
        values = {
            **self.base,
            "BT_INSTALL_NAME": "kavita-translate",
            "BT_AUTH_PROFILE": "reader-session",
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "BT_READER_CONTAINER": "kavita",
            "BT_READER_NETWORK": "kavita_default",
            "BT_READER_VERSION": "0.9.0.2",
            "BT_STATE_DIR": "/srv/kavita-translate/state",
            "BT_DATA_DIR": "/srv/kavita-translate/data",
            "BT_BACKUP_DIR": "/srv/kavita-translate/backups",
        }
        for legacy_name in (
            "CWA_UPSTREAM",
            "BT_CWA_CONTAINER",
            "BT_CWA_NETWORK",
            "BT_CWA_VERSION",
        ):
            values.pop(legacy_name)

        config = InstallConfig.from_mapping(values, self.identity)

        self.assertEqual(config.reader_type, "kavita")
        self.assertEqual(config.reader_version, "0.9.0.2")
        self.assertEqual(config.reader_image_id, "")
        self.assertEqual(config.compatibility_tier, "certified")
        self.assertEqual(config.reader_contract_version, "kavita-0.9.0.2-epub-v1")
        self.assertEqual(
            config.image,
            "local/kavita-translate:2.2.0-bbbbbbbbbbbb",
        )
        self.assertNotEqual(config.image, self.identity.image)
        self.assertEqual(config.api_environment()["BT_AUTH_MODE"], "reader_session")
        self.assertEqual(
            config.api_environment()["BT_READER_AUTH_URL"],
            "http://kavita:5000/api/Account",
        )
        self.assertEqual(config.proxy_environment()["BT_READER_TYPE"], "kavita")
        self.assertEqual(
            config.proxy_environment()["BT_READER_UPSTREAM"],
            "http://kavita:5000",
        )

        for version in ("0.9.0.1", "v0.9.0.2", "0.9.1.0", "latest"):
            with self.subTest(version=version), self.assertRaisesRegex(
                ConfigError, "Kavita reader version is not certified"
            ):
                InstallConfig.from_mapping(
                    {**values, "BT_READER_VERSION": version}, self.identity
                )

        exact_image_id = "sha256:" + "1" * 64
        pinned = InstallConfig.from_mapping(
            {**values, "BT_READER_IMAGE_ID": exact_image_id}, self.identity
        )
        self.assertEqual(pinned.reader_image_id, exact_image_id)
        self.assertEqual(
            DeploymentPlan.from_config(pinned).resources["reader"]["image_id"],
            exact_image_id,
        )
        for image_id in ("latest", "sha256:1234", "sha256:" + "A" * 64):
            with self.subTest(image_id=image_id), self.assertRaisesRegex(
                ConfigError, "BT_READER_IMAGE_ID"
            ):
                InstallConfig.from_mapping(
                    {**values, "BT_READER_IMAGE_ID": image_id}, self.identity
                )

    def test_kavita_0_9_1_4_preserves_the_old_pair_and_projects_exact_cors(self):
        values = {
            **self.base,
            "BT_INSTALL_NAME": "kavita-translate",
            "BT_AUTH_PROFILE": "reader-session",
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "BT_READER_CONTAINER": "kavita",
            "BT_READER_NETWORK": "kavita_default",
            "BT_READER_VERSION": "0.9.1.4",
            "BT_STATE_DIR": "/srv/kavita-translate/state",
            "BT_DATA_DIR": "/srv/kavita-translate/data",
            "BT_BACKUP_DIR": "/srv/kavita-translate/backups",
        }
        for legacy_name in (
            "CWA_UPSTREAM", "BT_CWA_CONTAINER", "BT_CWA_NETWORK", "BT_CWA_VERSION"
        ):
            values.pop(legacy_name)

        config = InstallConfig.from_mapping(values, self.identity)

        self.assertEqual(config.reader_contract_version, "kavita-0.9.1.4-epub-v1")
        self.assertEqual(
            config.api_environment()["BT_ALLOWED_ORIGINS"], "https://books.example.test"
        )
        self.assertEqual(
            config.api_environment()["BT_PUBLIC_ORIGIN"], "https://books.example.test"
        )

    def test_kavita_forbids_cwa_only_inputs_and_non_reader_auth(self):
        base = {
            **self.base,
            "BT_READER_TYPE": "kavita",
            "BT_READER_UPSTREAM": "http://kavita:5000",
            "BT_READER_CONTAINER": "kavita",
            "BT_READER_NETWORK": "kavita_default",
            "BT_READER_VERSION": "0.9.0.2",
        }
        for legacy_name in (
            "CWA_UPSTREAM",
            "BT_CWA_CONTAINER",
            "BT_CWA_NETWORK",
            "BT_CWA_VERSION",
        ):
            base.pop(legacy_name)
        for name, value in (
            ("BT_CWA_IDENTITY_HEADER", "Remote-User"),
            ("CWA_UPSTREAM", "http://calibre-web-automated:8083"),
        ):
            with self.subTest(name=name), self.assertRaisesRegex(ConfigError, name):
                InstallConfig.from_mapping(
                    {**base, "BT_AUTH_PROFILE": "reader-session", name: value},
                    self.identity,
                )
        with self.assertRaisesRegex(ConfigError, "reader-session"):
            InstallConfig.from_mapping(base, self.identity)

    def test_nginx_facing_origins_reject_variable_or_ambiguous_authorities(self):
        malicious = (
            "https://$http_host",
            "https://%24http_host",
            "https://books.example.test;evil",
            "https://books.example.test\\evil",
        )
        for name in ("BT_PUBLIC_ORIGIN", "CWA_UPSTREAM"):
            for origin in malicious:
                with self.subTest(name=name, origin=origin), self.assertRaisesRegex(
                    ConfigError, name
                ):
                    InstallConfig.from_mapping(
                        {**self.base, name: origin},
                        self.identity,
                    )

    def test_cwa_upstream_is_bound_to_the_inspected_container(self):
        for upstream in (
            "http://other-cwa:8083",
            "http://calibre-web-automated:8080",
            "https://calibre-web-automated:8083",
        ):
            with self.subTest(upstream=upstream), self.assertRaisesRegex(
                ConfigError, "CWA_UPSTREAM.*BT_CWA_CONTAINER"
            ):
                InstallConfig.from_mapping(
                    {**self.base, "CWA_UPSTREAM": upstream}, self.identity
                )

    def test_custom_cwa_identity_header_is_validated_and_propagated(self):
        config = InstallConfig.from_mapping(
            {**self.base, "BT_CWA_IDENTITY_HEADER": "X-Forwarded-User"},
            self.identity,
        )

        self.assertEqual(config.cwa_identity_header, "X-Forwarded-User")
        self.assertEqual(
            config.proxy_environment()["BT_CWA_IDENTITY_HEADER"],
            "X-Forwarded-User",
        )
        self.assertEqual(
            config.public_contract()["cwa_identity_header"],
            "X-Forwarded-User",
        )

        for header in (
            "Remote_User",
            "Remote-User;include",
            "Cookie",
            "X-BT-Subject",
            "X-Forwarded-For",
        ):
            with self.subTest(header=header), self.assertRaisesRegex(
                ConfigError, "BT_CWA_IDENTITY_HEADER"
            ):
                InstallConfig.from_mapping(
                    {**self.base, "BT_CWA_IDENTITY_HEADER": header}, self.identity
                )
    def test_forwarded_profile_requires_exact_peer_and_patched_authentik(self):
        values = {
            **self.base,
            "BT_INGRESS_MODE": "docker-edge",
            "BT_EDGE_NETWORK": "authentik_backend",
            "BT_PROXY_PORT": "",
            "BT_AUTH_PROFILE": "authentik-forwarded",
            "BT_IDENTITY_PROXY_IP": "172.30.50.9/32",
            "BT_AUTHENTIK_VERSION": "2026.5.4",
            "BT_AUTHENTIK_OUTPOST_URL": "http://authentik-outpost:9000",
            "BT_REVERSE_PROXY": "nginx",
        }

        config = InstallConfig.from_mapping(values, self.identity)
        api = config.api_environment()
        proxy = config.proxy_environment()

        self.assertEqual(api["BT_AUTH_MODE"], "forwarded")
        self.assertEqual(proxy["BT_BROWSER_AUTH_MODE"], "forwarded")
        self.assertEqual(proxy["BT_BROWSER_CREDENTIALS"], "include")
        self.assertEqual(api["BT_IDENTITY_TRUSTED_PROXIES"], "172.30.50.9/32")
        self.assertEqual(api["BT_TRUSTED_PROXIES"], "172.30.50.9/32")
        self.assertEqual(api["BT_FORWARDED_SUBJECT_HEADER"], "X-authentik-uid")
        self.assertNotIn("BT_CWA_AUTH_URL", api)

    def test_forwarded_profile_rejects_broad_peer_and_affected_versions(self):
        for peer in ("172.30.50.0/24", "10.0.0.0/8", "0.0.0.0/0"):
            with self.subTest(peer=peer), self.assertRaisesRegex(
                ConfigError, "exact /32 or /128"
            ):
                InstallConfig.from_mapping(
                    {
                        **self.base,
                        "BT_INGRESS_MODE": "docker-edge",
                        "BT_EDGE_NETWORK": "authentik_backend",
                        "BT_PROXY_PORT": "",
                        "BT_AUTH_PROFILE": "authentik-forwarded",
                        "BT_IDENTITY_PROXY_IP": peer,
                        "BT_AUTHENTIK_VERSION": "2026.5.4",
                        "BT_AUTHENTIK_OUTPOST_URL": "http://authentik-outpost:9000",
                        "BT_REVERSE_PROXY": "traefik",
                    },
                    self.identity,
                )

        auth_values = {
            **self.base,
            "BT_INGRESS_MODE": "docker-edge",
            "BT_EDGE_NETWORK": "authentik_backend",
            "BT_PROXY_PORT": "",
            "BT_AUTH_PROFILE": "authentik-forwarded",
            "BT_IDENTITY_PROXY_IP": "2001:db8::9/128",
            "BT_AUTHENTIK_OUTPOST_URL": "http://authentik-outpost:9000",
            "BT_REVERSE_PROXY": "caddy",
        }
        for version in (
            "2025.10.3",
            "2025.12.4",
            "2025.12.6",
            "2026.2.4",
            "2026.5.3",
            "2026.8.0",
        ):
            with self.subTest(version=version), self.assertRaisesRegex(
                ConfigError, "supported security floor"
            ):
                InstallConfig.from_mapping(
                    {**auth_values, "BT_AUTHENTIK_VERSION": version},
                    self.identity,
                )

        for version in ("2026.2.5", "2026.2.99", "2026.5.4", "2026.5.99"):
            with self.subTest(version=version):
                config = InstallConfig.from_mapping(
                    {**auth_values, "BT_AUTHENTIK_VERSION": version},
                    self.identity,
                )
                self.assertEqual(config.authentik_version, version)

    def test_cwa_compatibility_is_explicit_and_fail_closed(self):
        self.assertEqual(compatibility_tier("4.0.6"), "tier1")
        self.assertEqual(compatibility_tier("4.9.0"), "tier1")
        self.assertEqual(compatibility_tier("3.1.4"), "legacy")
        for version in ("3.1.3", "3.2.0", "5.0.0", "latest"):
            with self.subTest(version=version), self.assertRaises(ConfigError):
                compatibility_tier(version)

        with self.assertRaisesRegex(ConfigError, "migration-only"):
            InstallConfig.from_mapping(
                {**self.base, "BT_CWA_VERSION": "3.1.4"}, self.identity
            )
        legacy = InstallConfig.from_mapping(
            {**self.base, "BT_CWA_VERSION": "3.1.4"},
            self.identity,
            allow_legacy_cwa=True,
        )
        self.assertEqual(legacy.compatibility_tier, "legacy")

    def test_profile_specific_network_and_path_contract(self):
        with self.assertRaisesRegex(ConfigError, "BT_EDGE_NETWORK"):
            InstallConfig.from_mapping(
                {
                    **self.base,
                    "BT_INGRESS_MODE": "docker-edge",
                    "BT_PROXY_PORT": "",
                    "BT_AUTH_PROFILE": "authentik-forwarded",
                    "BT_IDENTITY_PROXY_IP": "172.30.50.9/32",
                    "BT_AUTHENTIK_VERSION": "2026.5.4",
                    "BT_AUTHENTIK_OUTPOST_URL": "http://authentik-outpost:9000",
                    "BT_REVERSE_PROXY": "nginx",
                },
                self.identity,
            )
        with self.assertRaisesRegex(ConfigError, "absolute"):
            InstallConfig.from_mapping(
                {**self.base, "BT_STATE_DIR": "./state"}, self.identity
            )
        with self.assertRaisesRegex(ConfigError, "unsafe for DockerMan"):
            InstallConfig.from_mapping(
                {
                    **self.base,
                    "BT_INSTALL_PROFILE": "unraid",
                    "BT_STATE_DIR": "/mnt/user/appdata/cwa;--privileged",
                    "BT_UNRAID_TEMPLATE_DIR": "/boot/config/plugins/dockerMan/templates-user",
                },
                self.identity,
            )
        for overrides in (
            {"BT_STATE_DIR": "/srv/cwa-translate/data/state"},
            {"BT_DATA_DIR": "/srv/cwa-translate/state/data"},
            {"BT_BACKUP_DIR": "/srv/cwa-translate/data/backups"},
        ):
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                ConfigError, "overlap"
            ):
                InstallConfig.from_mapping(
                    {**self.base, **overrides}, self.identity
                )
        with self.assertRaisesRegex(ConfigError, "overlap"):
            InstallConfig.from_mapping(
                {
                    **self.base,
                    "BT_INSTALL_PROFILE": "unraid",
                    "BT_UNRAID_TEMPLATE_DIR": "/srv/cwa-translate/data/templates",
                },
                self.identity,
            )

    def test_install_contract_never_accepts_disabled_or_shared_token_auth(self):
        for profile in ("disabled", "token", "forwarded"):
            with self.subTest(profile=profile), self.assertRaisesRegex(
                ConfigError, "BT_AUTH_PROFILE"
            ):
                InstallConfig.from_mapping(
                    {**self.base, "BT_AUTH_PROFILE": profile}, self.identity
                )
        with self.assertRaisesRegex(ConfigError, "BT_INSTALL_PROFILE"):
            InstallConfig.from_mapping(
                {**self.base, "BT_INSTALL_PROFILE": "compose-bundled"}, self.identity
            )

    def test_provider_contract_requires_exactly_one_credential_path(self):
        with self.assertRaisesRegex(ConfigError, "BT_LOCAL_URL"):
            InstallConfig.from_mapping(
                {**self.base, "BT_LOCAL_URL": "", "LLM_API_KEY": "secret"},
                self.identity,
            )
        with self.assertRaisesRegex(ConfigError, "LLM_API_KEY"):
            InstallConfig.from_mapping(
                {
                    **self.base,
                    "LLM_PROVIDER": "openai",
                    "BT_LOCAL_URL": "",
                    "LLM_API_KEY": "",
                },
                self.identity,
            )
        with self.assertRaisesRegex(ConfigError, "LLM_PROVIDER"):
            InstallConfig.from_mapping(
                {**self.base, "LLM_PROVIDER": "typo"}, self.identity
            )
        for endpoint in (
            "http://host.docker.internal:2819/not-chat",
            "http://host.docker.internal:2819/v1/chat/completions/",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(
                ConfigError, "BT_LOCAL_URL"
            ):
                InstallConfig.from_mapping(
                    {**self.base, "BT_LOCAL_URL": endpoint}, self.identity
                )

    def test_managed_provider_contract_supports_fallback_and_custom_endpoint(self):
        gemini_primary = InstallConfig.from_mapping(
            {
                **self.base,
                "LLM_PROVIDER": "gemini",
                "LLM_MODEL": "gemini-3.5-flash-lite",
                "LLM_API_KEY": "gemini-private-key",
                "LLM_FALLBACK_PROVIDER": "local",
                "LLM_FALLBACK_MODEL": "gemma4-12b",
            },
            self.identity,
        )
        primary_env = gemini_primary.api_environment()
        self.assertEqual(primary_env["LLM_FALLBACK_PROVIDER"], "local")
        self.assertEqual(primary_env["LLM_FALLBACK_MODEL"], "gemma4-12b")
        self.assertEqual(primary_env["LLM_FALLBACK_API_KEY"], "")
        self.assertEqual(
            primary_env["BT_LOCAL_URL"],
            "http://host.docker.internal:2819/v1/chat/completions",
        )

        custom = InstallConfig.from_mapping(
            {
                **self.base,
                "LLM_PROVIDER": "openai-compatible",
                "LLM_MODEL": "translation-model",
                "BT_LOCAL_URL": "",
                "LLM_CUSTOM_ENDPOINT": "https://llm.example.test/v1/chat/completions",
                "LLM_CUSTOM_API_KEY": "custom-private-key",
            },
            self.identity,
        )
        custom_env = custom.api_environment()
        self.assertEqual(
            custom_env["LLM_CUSTOM_ENDPOINT"],
            "https://llm.example.test/v1/chat/completions",
        )
        self.assertEqual(custom_env["LLM_CUSTOM_API_KEY"], "custom-private-key")
        self.assertEqual(custom_env["LLM_API_KEY"], "")
        self.assertNotIn("custom-private-key", repr(custom.public_contract()))

    def test_managed_provider_contract_rejects_ambiguous_or_unsafe_fallbacks(self):
        invalid_cases = (
            {
                "LLM_PROVIDER": "gemini",
                "LLM_MODEL": "gemini-3.5-flash-lite",
                "LLM_API_KEY": "primary-key",
                "LLM_FALLBACK_PROVIDER": "gemini",
                "LLM_FALLBACK_MODEL": "gemini-3.5-flash-lite",
                "BT_LOCAL_URL": "",
            },
            {
                "LLM_PROVIDER": "openai-compatible",
                "LLM_MODEL": "translation-model",
                "LLM_CUSTOM_ENDPOINT": "http://llm.example.test/v1/chat/completions",
                "LLM_CUSTOM_API_KEY": "custom-key",
                "BT_LOCAL_URL": "",
            },
            {
                "LLM_PROVIDER": "openai-compatible",
                "LLM_MODEL": "translation-model",
                "LLM_CUSTOM_ENDPOINT": "https://llm.example.test/v1/chat/completions/",
                "LLM_CUSTOM_API_KEY": "custom-key",
                "BT_LOCAL_URL": "",
            },
        )
        for values in invalid_cases:
            with self.subTest(values=values), self.assertRaises(ConfigError):
                InstallConfig.from_mapping({**self.base, **values}, self.identity)

    def test_redaction_never_returns_secret_values(self):
        values = {
            "LLM_API_KEY": "cloud-secret",
            "BT_API_TOKEN": "compat-secret",
            "BT_PUBLIC_ORIGIN": "https://books.example.test",
        }

        redacted = redact_mapping(values)

        self.assertEqual(redacted["LLM_API_KEY"], "<redacted>")
        self.assertEqual(redacted["BT_API_TOKEN"], "<redacted>")
        self.assertEqual(
            redacted["BT_PUBLIC_ORIGIN"], "https://books.example.test"
        )
        self.assertNotIn("cloud-secret", repr(redacted))
        self.assertNotIn("compat-secret", repr(redacted))


class PlanAndStateTests(unittest.TestCase):
    def setUp(self):
        self.identity = ReleaseIdentity.from_checkout(
            version="2.2.0", sha="c" * 40, clean=True
        )
        self.values = {
            "BT_INSTALL_PROFILE": "compose-existing",
            "BT_INGRESS_MODE": "published",
            "BT_AUTH_PROFILE": "cwa-session",
            "BT_PUBLIC_ORIGIN": "https://books.example.test",
            "CWA_UPSTREAM": "http://calibre-web-automated:8083",
            "BT_CWA_CONTAINER": "calibre-web-automated",
            "BT_CWA_NETWORK": "cwa_default",
            "BT_CWA_VERSION": "4.0.6",
            "BT_STATE_DIR": "/srv/cwa-translate/state",
            "BT_DATA_DIR": "/srv/cwa-translate/data",
            "BT_BACKUP_DIR": "/srv/cwa-translate/backups",
            "BT_PROXY_PORT": "8385",
            "LLM_PROVIDER": "openai",
            "LLM_MODEL": "gpt-4.1-mini",
            "BT_LOCAL_URL": "",
            "LLM_API_KEY": "never-print-this",
        }

    def test_plan_is_deterministic_redacted_and_declares_exact_ownership(self):
        config = InstallConfig.from_mapping(self.values, self.identity)

        first = DeploymentPlan.from_config(config).to_dict()
        second = DeploymentPlan.from_config(config).to_dict()

        self.assertEqual(first, second)
        encoded = json.dumps(first, sort_keys=True)
        self.assertNotIn("never-print-this", encoded)
        self.assertEqual(first["image"], self.identity.image)
        self.assertEqual(first["compatibility_tier"], "tier1")
        self.assertEqual(first["schema_version"], 3)
        self.assertEqual(first["reader_type"], "cwa")
        self.assertEqual(first["reader_contract_version"], "cwa-epub-v1")
        self.assertEqual(first["resources"]["reader"]["ownership"], "external")
        self.assertEqual(first["resources"]["api"]["ownership"], "owned")
        self.assertEqual(first["resources"]["proxy"]["ownership"], "owned")
        self.assertEqual(first["resources"]["api"]["published_ports"], [])
        self.assertEqual(first["resources"]["proxy"]["published_ports"], [8385])

    def test_schema_one_state_loads_without_implicit_rewrite(self):
        payload = {
            "schema_version": 1,
            "install_id": "01234567-89ab-4cde-8123-0123456789ab",
            "status": "installed",
            "version": "2.2.0",
            "revision": "c" * 40,
            "image": "local/cwa-translate:2.2.0-cccccccccccc",
            "config_fingerprint": "d" * 64,
            "install_profile": "compose-existing",
            "auth_profile": "cwa-session",
            "resources": {"cwa": {"name": "calibre-web-automated"}},
        }

        state = DeploymentState.from_dict(payload)

        self.assertEqual(state.schema_version, 1)
        self.assertEqual(state.reader_type, "cwa")
        self.assertEqual(state.reader_contract_version, "cwa-epub-v1")
        self.assertEqual(state.to_dict(), payload)

    def test_state_is_private_atomic_schema_versioned_and_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory))
            state = DeploymentState.new(
                install_id="01234567-89ab-4cde-8123-0123456789ab",
                plan=DeploymentPlan.from_config(
                    InstallConfig.from_mapping(self.values, self.identity)
                ),
            )
            store.save(state)

            state_file = Path(directory) / "state.json"
            payload = state_file.read_text(encoding="utf-8")
            self.assertEqual(os.stat(state_file).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(directory).st_mode & 0o777, 0o700)
            self.assertNotIn("never-print-this", payload)
            self.assertEqual(store.load(), state)

    def test_cleaned_install_attempt_cannot_report_cleanup_errors(self):
        plan = DeploymentPlan.from_config(
            InstallConfig.from_mapping(self.values, self.identity)
        )
        payload = {
            "install_id": "01234567-89ab-4cde-8123-0123456789ab",
            "status": "cleaned",
            "version": plan.version,
            "revision": plan.revision,
            "image": plan.image,
            "config_fingerprint": plan.config_fingerprint,
            "install_profile": plan.install_profile,
            "resources": plan.resources,
            "cleanup_errors": ["compose: cleanup failed"],
        }
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "cannot contain cleanup errors"):
                InstallAttemptStore(Path(directory)).save(payload)

    def test_fresh_state_directory_entry_is_durable_before_state_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            store = StateStore(state_dir)
            state = DeploymentState.new(
                install_id="01234567-89ab-4cde-8123-0123456789ab",
                plan=DeploymentPlan.from_config(
                    InstallConfig.from_mapping(self.values, self.identity)
                ),
            )
            events = []
            original_replace = os.replace

            def replace_path(source, target):
                events.append(("replace", Path(target)))
                original_replace(source, target)

            with mock.patch(
                "btctl_core._fsync_directory",
                create=True,
                side_effect=lambda path: events.append(("fsync", Path(path))),
            ), mock.patch("btctl_core.os.replace", side_effect=replace_path):
                store.save(state)

            publish = events.index(("replace", store.path))
            self.assertIn(("fsync", state_dir), events[:publish])
            self.assertIn(("fsync", root), events[:publish])
            self.assertEqual(events[publish + 1], ("fsync", state_dir))

    def test_state_rejects_unknown_schema_and_symlink_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state.json").write_text('{"schema_version": 999}')
            (root / "state.json").chmod(0o600)
            with self.assertRaisesRegex(ConfigError, "schema"):
                StateStore(root).load()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "elsewhere"
            target.write_text("preserve")
            (root / "state.json").symlink_to(target)
            with self.assertRaisesRegex(ConfigError, "symbolic link"):
                StateStore(root).save(
                    DeploymentState.new(
                        install_id="01234567-89ab-4cde-8123-0123456789ab",
                        plan=DeploymentPlan.from_config(
                            InstallConfig.from_mapping(self.values, self.identity)
                        ),
                    )
                )

    def test_state_save_refuses_to_take_over_an_existing_mutable_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir(mode=0o755)
            root.chmod(0o755)
            marker = root / "belongs-to-another-tool"
            marker.write_text("preserve", encoding="utf-8")
            store = StateStore(root)
            state = DeploymentState.new(
                install_id="01234567-89ab-4cde-8123-0123456789ab",
                plan=DeploymentPlan.from_config(
                    InstallConfig.from_mapping(self.values, self.identity)
                ),
            )

            with self.assertRaisesRegex(ConfigError, "owned.*mode 0700"):
                store.save(state)

            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(store.path.exists())

    def test_state_load_rejects_mutable_or_linked_ownership_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root)
            state = DeploymentState.new(
                install_id="01234567-89ab-4cde-8123-0123456789ab",
                plan=DeploymentPlan.from_config(
                    InstallConfig.from_mapping(self.values, self.identity)
                ),
            )
            store.save(state)

            root.chmod(0o777)
            with self.assertRaisesRegex(ConfigError, "mode 0700"):
                store.load()
            root.chmod(0o700)

            store.path.chmod(0o666)
            with self.assertRaisesRegex(ConfigError, "mode 0600"):
                store.load()
            store.path.chmod(0o600)

            second_link = root / "state-copy.json"
            os.link(store.path, second_link)
            with self.assertRaisesRegex(ConfigError, "private regular file"):
                store.load()

    def test_state_archive_rejects_existing_insecure_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = StateStore(root)
            state = replace(
                DeploymentState.new(
                    install_id="01234567-89ab-4cde-8123-0123456789ab",
                    plan=DeploymentPlan.from_config(
                        InstallConfig.from_mapping(self.values, self.identity)
                    ),
                ),
                status="uninstalled",
            )
            store.save(state)
            history = root / "history"
            history.mkdir(mode=0o700)
            evidence = history / f"{state.install_id}-uninstalled.json"
            evidence.write_text(
                json.dumps(state.to_dict(), sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            evidence.chmod(0o644)

            with self.assertRaisesRegex(ConfigError, "private"):
                store.archive(state)


class CLIPlanTests(unittest.TestCase):
    def test_plan_outputs_json_without_mutating_state_or_leaking_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.name", "btctl test"], cwd=repository, check=True)
            subprocess.run(["git", "config", "user.email", "btctl@example.invalid"], cwd=repository, check=True)
            (repository / "VERSION").write_text("2.2.0\n", encoding="utf-8")
            subprocess.run(["git", "add", "VERSION"], cwd=repository, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=repository, check=True)
            state_dir = Path(directory) / "state"
            env_file = Path(directory) / "install.env"
            env_file.write_text(
                "\n".join(
                    (
                        "BT_INSTALL_PROFILE=compose-existing",
                        "BT_INGRESS_MODE=published",
                        "BT_AUTH_PROFILE=cwa-session",
                        "BT_PUBLIC_ORIGIN=https://books.example.test",
                        "CWA_UPSTREAM=http://calibre-web-automated:8083",
                        "BT_CWA_CONTAINER=calibre-web-automated",
                        "BT_CWA_NETWORK=cwa_default",
                        "BT_CWA_VERSION=4.0.6",
                        f"BT_STATE_DIR={state_dir}",
                        f"BT_DATA_DIR={Path(directory) / 'data'}",
                        f"BT_BACKUP_DIR={Path(directory) / 'backups'}",
                        "BT_PROXY_PORT=8385",
                        "LLM_PROVIDER=openai",
                        "LLM_MODEL=gpt-4.1-mini",
                        "BT_LOCAL_URL=",
                        "LLM_API_KEY=top-secret",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)

            result = subprocess.run(
                [sys.executable, str(BTCTL), "--repository", str(repository), "plan", "--env", str(env_file), "--json"],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["image"].split(":")[0], "local/cwa-translate")
            self.assertNotIn("top-secret", result.stdout + result.stderr)
            self.assertFalse(state_dir.exists())

    def test_invalid_config_uses_exit_64_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "bad.env"
            env_file.write_text("BT_AUTH_PROFILE=disabled\n", encoding="utf-8")
            env_file.chmod(0o600)
            result = subprocess.run(
                [sys.executable, str(BTCTL), "plan", "--env", str(env_file)],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 64)
            self.assertNotIn("Traceback", result.stderr)

    def test_container_operator_rejects_checkout_revision_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=repository, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "btctl test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "btctl@example.invalid"],
                cwd=repository,
                check=True,
            )
            (repository / "VERSION").write_text("2.2.0\n", encoding="utf-8")
            subprocess.run(["git", "add", "VERSION"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=repository,
                check=True,
            )
            env_file = Path(directory) / "bad.env"
            env_file.write_text("BT_AUTH_PROFILE=disabled\n", encoding="utf-8")
            env_file.chmod(0o600)
            environment = {
                **os.environ,
                "BTCTL_EXPECTED_REVISION": "0" * 40,
                "BTCTL_OPERATOR_REVISION": "0" * 40,
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(BTCTL),
                    "--repository",
                    str(repository),
                    "plan",
                    "--env",
                    str(env_file),
                ],
                cwd=ROOT,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 64)
            self.assertIn("revision", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    def test_cli_rejects_symlinked_or_group_readable_environment_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(
                ["git", "init", "-q", "-b", "main"], cwd=repository, check=True
            )
            subprocess.run(
                ["git", "config", "user.name", "btctl test"],
                cwd=repository,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "btctl@example.invalid"],
                cwd=repository,
                check=True,
            )
            (repository / "VERSION").write_text("2.2.0\n", encoding="utf-8")
            subprocess.run(["git", "add", "VERSION"], cwd=repository, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "fixture"],
                cwd=repository,
                check=True,
            )
            target = root / "private.env"
            target.write_text("BT_AUTH_PROFILE=cwa-session\n", encoding="utf-8")
            target.chmod(0o600)
            symlink = root / "symlink.env"
            symlink.symlink_to(target)

            for candidate, expected in (
                (symlink, "symbolic link"),
                (target, "private mode"),
            ):
                if candidate == target:
                    target.chmod(0o640)
                result = subprocess.run(
                    [
                        sys.executable,
                        str(BTCTL),
                        "--repository",
                        str(repository),
                        "plan",
                        "--env",
                        str(candidate),
                    ],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 64)
                self.assertIn(expected, result.stderr)

class EnvParserTests(unittest.TestCase):
    def test_parser_accepts_comments_and_rejects_duplicates_or_shell_syntax(self):
        parsed = parse_env_text("# install\nBT_INSTALL_PROFILE=unraid\nLLM_MODEL='gemma 4'\n")
        self.assertEqual(parsed["BT_INSTALL_PROFILE"], "unraid")
        self.assertEqual(parsed["LLM_MODEL"], "gemma 4")

        with self.assertRaisesRegex(ConfigError, "duplicate"):
            parse_env_text("A=1\nA=2\n")
        with self.assertRaisesRegex(ConfigError, "KEY=value"):
            parse_env_text("export A=1\n")
        with self.assertRaisesRegex(ConfigError, "substitution"):
            parse_env_text("A=$(id)\n")


if __name__ == "__main__":
    unittest.main()
