import json
import os
import sqlite3
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path
from unittest import mock

from btctl_core import (
    DeploymentPlan,
    InstallAttemptStore,
    InstallConfig,
    ReleaseIdentity,
    StateStore,
)
from btctl_lifecycle import RuntimeUninstaller
from btctl_docker import DockerCLI, DockerCommandError
from btctl_unraid import (
    ContainerSpec,
    InstallError,
    UnraidAdopter,
    UnraidInstaller,
    prepare_data_directory,
    render_templates,
)


def values(root: Path, *, forwarded=False, reader="cwa", reader_version="0.9.0.2"):
    result = {
        "BT_INSTALL_PROFILE": "unraid",
        "BT_INSTALL_NAME": "cwa-translate-test",
        "BT_INGRESS_MODE": "published",
        "BT_PROXY_PORT": "8385",
        "BT_AUTH_PROFILE": "cwa-session",
        "BT_PUBLIC_ORIGIN": "https://books.example.test",
        "CWA_UPSTREAM": "http://calibre-web-automated:8083",
        "BT_CWA_CONTAINER": "calibre-web-automated",
        "BT_CWA_NETWORK": "cwa_default",
        "BT_CWA_VERSION": "4.0.6",
        "BT_STATE_DIR": str(root / "state"),
        "BT_DATA_DIR": str(root / "data"),
        "BT_BACKUP_DIR": str(root / "backups"),
        "BT_UNRAID_TEMPLATE_DIR": str(root / "templates-user"),
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "gpt-4.1-mini",
        "BT_LOCAL_URL": "",
        "LLM_API_KEY": "do-not-copy-to-xml",
    }
    if forwarded:
        result.update({
            "BT_INGRESS_MODE": "docker-edge",
            "BT_PROXY_PORT": "",
            "BT_EDGE_NETWORK": "authentik_backend",
            "BT_AUTH_PROFILE": "authentik-forwarded",
            "BT_IDENTITY_PROXY_IP": "172.30.50.9/32",
            "BT_AUTHENTIK_VERSION": "2026.5.4",
            "BT_AUTHENTIK_OUTPOST_URL": "http://authentik-outpost:9000",
            "BT_REVERSE_PROXY": "caddy",
        })
    if reader == "kavita":
        for name in (
            "CWA_UPSTREAM",
            "BT_CWA_CONTAINER",
            "BT_CWA_NETWORK",
            "BT_CWA_VERSION",
        ):
            result.pop(name)
        result.update(
            {
                "BT_INSTALL_NAME": "kavita-translate-test",
                "BT_AUTH_PROFILE": "reader-session",
                "BT_READER_TYPE": "kavita",
                "BT_READER_UPSTREAM": "http://kavita:5000",
                "BT_READER_CONTAINER": "kavita",
                "BT_READER_NETWORK": "kavita_default",
                "BT_READER_VERSION": reader_version,
                "BT_CWA_IDENTITY_HEADER": "",
            }
        )
    return result


class FakeDocker:
    def __init__(
        self,
        *,
        fail_proxy_health=False,
        fail_network_create_after_effect=False,
        fail_create_role_after_effect=None,
    ):
        self.calls = []
        self.fail_proxy_health = fail_proxy_health
        self.fail_network_create_after_effect = fail_network_create_after_effect
        self.fail_create_role_after_effect = fail_create_role_after_effect
        self.images = {}
        self.networks = {
            "cwa_default": {"Id": "cwa-network"},
            "kavita_default": {"Id": "kavita-network"},
            "authentik_backend": {"Id": "edge-network"},
        }
        self.containers = {
            "calibre-web-automated": {
                "Id": "cwa-id",
                "State": {"Status": "running"},
                "Config": {"Image": "crocodilestick/calibre-web-automated:v4.0.6"},
                "NetworkSettings": {"Networks": {"cwa_default": {}}},
            },
            "kavita": {
                "Id": "kavita-id",
                "State": {"Status": "running"},
                "Config": {"Image": "jvmilazz0/kavita:0.9.0.2"},
                "NetworkSettings": {"Networks": {"kavita_default": {}}},
            },
        }

    def require_available(self):
        self.calls.append(("require_available",))

    def inspect_container(self, name):
        self.calls.append(("inspect_container", name))
        return self.containers.get(name)

    def inspect_network(self, name):
        self.calls.append(("inspect_network", name))
        return self.networks.get(name)

    def inspect_image(self, name):
        self.calls.append(("inspect_image", name))
        return self.images.get(name)

    def build_image(self, repository, image, labels):
        self.calls.append(("build_image", str(repository), image, dict(labels)))
        self.images[image] = {"Id": "sha256:image-id", "Config": {"Labels": labels}}

    def create_network(self, name, labels, *, internal):
        self.calls.append(("create_network", name, dict(labels), internal))
        self.networks[name] = {
            "Id": "private-id",
            "Labels": dict(labels),
            "Internal": internal,
        }
        if self.fail_network_create_after_effect:
            raise InstallError("network create response was lost")

    def create_container(self, spec):
        self.calls.append(("create_container", spec))
        bindings = {}
        if spec.publish_port is not None:
            bindings = {"8080/tcp": [{"HostPort": str(spec.publish_port)}]}
        self.containers[spec.name] = {
            "Id": f"{spec.name}-id",
            "Image": "sha256:image-id",
            "State": {"Status": "created", "Health": {"Status": "starting"}},
            "Config": {
                "Image": spec.image,
                "Labels": dict(spec.labels),
                "Env": spec.env_file.read_text(encoding="utf-8").splitlines(),
                "User": "101:102",
            },
            "HostConfig": {
                "PortBindings": bindings,
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapDrop": ["ALL"],
                "CapAdd": None,
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": {
                    "/tmp": "rw,noexec,nosuid,size=64m,uid=101,gid=102,mode=700"
                },
                "PidsLimit": 256 if spec.role == "api" else 64,
                "Memory": (
                    1024 * 1024 * 1024 if spec.role == "api" else 128 * 1024 * 1024
                ),
                "NanoCpus": 2_000_000_000 if spec.role == "api" else 500_000_000,
                "RestartPolicy": {
                    "Name": "unless-stopped",
                    "MaximumRetryCount": 0,
                },
            },
            "Mounts": (
                [
                    {
                        "Type": "bind",
                        "Source": str(spec.data_dir),
                        "Destination": "/app/data",
                        "RW": True,
                    }
                ]
                if spec.data_dir is not None
                else []
            ),
            "NetworkSettings": {
                "Networks": {
                    spec.primary_network: {
                        "Aliases": [spec.network_alias] if spec.network_alias else []
                    }
                }
            },
        }
        if (
            spec.role == "api"
            and spec.data_dir is not None
            and "BT_AUTH_MODE=reader_session"
            in spec.env_file.read_text(encoding="utf-8").splitlines()
        ):
            session_key = Path(spec.data_dir) / "reader_session_key"
            session_key.write_bytes(b"s" * 32)
            session_key.chmod(0o600)
        if self.fail_create_role_after_effect == spec.role:
            raise InstallError(f"{spec.role} create response was lost")

    def connect_network(self, network, container):
        self.calls.append(("connect_network", network, container))
        self.containers[container]["NetworkSettings"]["Networks"][network] = {}

    def start_container(self, name):
        self.calls.append(("start_container", name))
        self.containers[name]["State"] = {
            "Status": "running",
            "Health": {"Status": "healthy"},
        }
        if name.endswith("-api"):
            mounts = self.containers[name].get("Mounts", [])
            if mounts:
                database = Path(mounts[0]["Source"]) / "translations.db"
                if not database.exists():
                    with closing(sqlite3.connect(database)) as connection:
                        connection.execute("PRAGMA user_version = 2")

    def stop_container(self, name):
        self.calls.append(("stop_container", name))
        self.containers[name]["State"]["Status"] = "exited"

    def wait_healthy(self, names, timeout_seconds):
        self.calls.append(("wait_healthy", tuple(names), timeout_seconds))
        if self.fail_proxy_health and any(name.endswith("-proxy") for name in names):
            raise InstallError("proxy health failed")

    def probe_http(self, container, url):
        self.calls.append(("probe_http", container, url))

    def probe_auth(self, container, url):
        self.calls.append(("probe_auth", container, url))

    def probe_sqlite(self, container, database_path):
        self.calls.append(("probe_sqlite", container, database_path))

    def remove_container(self, name):
        self.calls.append(("remove_container", name))
        self.containers.pop(name, None)

    def remove_network(self, name):
        self.calls.append(("remove_network", name))
        self.networks.pop(name, None)

    def remove_data_credential(self, image, path, filename):
        self.calls.append(("remove_data_credential", image, str(path), filename))
        (Path(path) / filename).unlink()


class UnraidTemplateTests(unittest.TestCase):
    def test_templates_are_parseable_immutable_and_never_publish_api_or_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = ReleaseIdentity.from_checkout(
                version="2.2.0", sha="b" * 40, clean=True
            )
            config = InstallConfig.from_mapping(values(root), identity)
            plan = DeploymentPlan.from_config(config)

            templates = render_templates(config, plan)

            self.assertEqual(set(templates), {"api", "proxy"})
            api = ET.fromstring(templates["api"])
            proxy = ET.fromstring(templates["proxy"])
            self.assertEqual(api.findtext("Repository"), identity.image)
            self.assertEqual(proxy.findtext("Repository"), identity.image)
            self.assertFalse(
                [item for item in api.findall("Config") if item.get("Type") == "Port"]
            )
            self.assertEqual(
                len([item for item in proxy.findall("Config") if item.get("Type") == "Port"]),
                1,
            )
            encoded = json.dumps(templates)
            self.assertNotIn("latest", encoded)
            self.assertNotIn("do-not-copy-to-xml", encoded)
            self.assertIn("managed by btctl", encoded)


class DockerCLIContractTests(unittest.TestCase):
    def test_compose_raw_env_requires_version_230_before_validation(self):
        old = mock.Mock(returncode=0, stdout="2.29.9\n", stderr="")
        with mock.patch("subprocess.run", return_value=old) as run:
            with self.assertRaisesRegex(DockerCommandError, "2.30.0"):
                DockerCLI().compose_validate(Path("/private/compose.json"), "project")
        self.assertEqual(run.call_count, 1)

    def test_supported_compose_version_runs_validation_without_shell(self):
        version = mock.Mock(returncode=0, stdout="v2.30.0\n", stderr="")
        validated = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", side_effect=[version, validated]) as run:
            DockerCLI().compose_validate(Path("/private/compose.json"), "project")
        self.assertEqual(run.call_count, 2)
        self.assertIsInstance(run.call_args.args[0], list)

    def test_inspect_distinguishes_verified_absence_from_docker_failure(self):
        missing = mock.Mock(
            returncode=1,
            stdout="",
            stderr="Error: No such container: cwa-translate-api\n",
        )
        unavailable = mock.Mock(
            returncode=1,
            stdout="",
            stderr="permission denied while trying to connect to the Docker daemon\n",
        )

        with mock.patch("subprocess.run", return_value=missing):
            self.assertIsNone(DockerCLI().inspect_container("cwa-translate-api"))
        with mock.patch("subprocess.run", return_value=unavailable):
            with self.assertRaisesRegex(DockerCommandError, "inspect failed"):
                DockerCLI().inspect_container("cwa-translate-api")

    def test_raw_create_passes_private_env_file_without_secret_or_shell(self):
        spec = ContainerSpec(
            role="api",
            name="cwa-translate-api",
            image="local/cwa-translate:2.2.0-abcdef012345",
            env_file=Path("/private/state/api.env"),
            labels={"io.cwa-translate.role": "api"},
            primary_network="cwa-translate-private",
            network_alias="translator-api",
            data_dir=Path("/mnt/user/appdata/cwa-translate/data"),
            publish_port=None,
        )
        completed = mock.Mock(returncode=0, stdout="container-id\n", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            DockerCLI().create_container(spec)

        arguments = run.call_args.args[0]
        self.assertIsInstance(arguments, list)
        self.assertEqual(arguments[0], "docker")
        self.assertIn("--env-file", arguments)
        self.assertIn("--user", arguments)
        self.assertIn("101:102", arguments)
        self.assertIn("/private/state/api.env", arguments)
        self.assertNotIn("--publish", arguments)
        self.assertNotIn("LLM_API_KEY", " ".join(arguments))
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_compose_data_preparation_preserves_private_operator_group_access(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run, mock.patch(
            "btctl_docker.os.getgid", return_value=4242, create=True
        ):
            DockerCLI().prepare_data_directory(
                "local/cwa-translate:2.2.0-abcdef012345",
                Path("/srv/cwa-translate/data"),
            )

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:3], ["docker", "run", "--rm"])
        self.assertIn("--user", arguments)
        self.assertIn("0:0", arguments)
        self.assertIn("type=bind,src=/srv/cwa-translate/data,dst=/data", arguments)
        self.assertIn("local/cwa-translate:2.2.0-abcdef012345", arguments)
        self.assertNotIn("shell", run.call_args.kwargs)
        script = arguments[-1]
        self.assertIn("find /data -xdev ! -type d ! -type f", script)
        self.assertIn("find /data -xdev -type d -exec chown 101:4242", script)
        self.assertIn("find /data -xdev -type f -exec chown 101:4242", script)
        self.assertIn("find /data -xdev -type d -exec chmod 2750", script)
        self.assertIn("find /data -xdev -type f -exec chmod 0640", script)

    def test_hub_data_preparation_preserves_private_keys_and_reader_directories(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            DockerCLI().prepare_hub_data_directory(
                "local/book-translator-hub:2.3.0-abcdef012345",
                Path("/srv/book-translator-hub/data"),
                ("cwa", "kavita"),
            )

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:3], ["docker", "run", "--rm"])
        self.assertIn("type=bind,src=/srv/book-translator-hub/data,dst=/data", arguments)
        self.assertEqual(arguments[-2:], ["cwa", "kavita"])
        script = arguments[arguments.index("-c") + 1]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for reader in ("cwa", "kavita"):
                reader_dir = root / reader
                reader_dir.mkdir()
                key = reader_dir / "reader_session_key"
                key.write_bytes(b"s" * 32)
                key.chmod(0o640)
                (reader_dir / "translations.db").write_bytes(b"sqlite")
            with mock.patch(
                "sys.argv",
                [
                    "prepare-hub-data",
                    str(root),
                    str(os.geteuid()),
                    str(os.getgid()),
                    "cwa",
                    "kavita",
                ],
            ):
                exec(compile(script, "<prepare-hub-data>", "exec"), {})

            self.assertEqual(root.stat().st_mode & 0o7777, 0o2750)
            for reader in ("cwa", "kavita"):
                reader_dir = root / reader
                self.assertEqual(reader_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(
                    (reader_dir / "reader_session_key").stat().st_mode & 0o777,
                    0o600,
                )
                self.assertEqual(
                    (reader_dir / "translations.db").stat().st_mode & 0o777,
                    0o600,
                )

    def test_hub_credential_removal_is_exact_and_idempotent(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            DockerCLI().remove_hub_data_credentials(
                "local/book-translator-hub:2.3.0-abcdef012345",
                Path("/srv/book-translator-hub/data"),
                ("cwa", "kavita"),
            )

        arguments = run.call_args.args[0]
        script = arguments[arguments.index("-c") + 1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for reader in ("cwa", "kavita"):
                reader_dir = root / reader
                reader_dir.mkdir()
                reader_dir.chmod(0o700)
                (reader_dir / "keep.marker").write_text(reader, encoding="utf-8")
                key = reader_dir / "reader_session_key"
                key.write_bytes(b"s" * 32)
                key.chmod(0o600)
            for _attempt in range(2):
                with mock.patch(
                    "sys.argv",
                    ["remove-hub-keys", str(root), "cwa", "kavita"],
                ):
                    exec(compile(script, "<remove-hub-keys>", "exec"), {})

            for reader in ("cwa", "kavita"):
                self.assertFalse((root / reader / "reader_session_key").exists())
                self.assertTrue((root / reader / "keep.marker").is_file())

    def test_hub_credential_inspection_is_read_only_and_fixed_shape(self):
        completed = mock.Mock(
            returncode=0,
            stdout='{"cwa":true,"kavita":false}\n',
            stderr="",
        )

        with mock.patch("subprocess.run", return_value=completed) as run:
            result = DockerCLI().inspect_hub_data_credentials(
                "local/book-translator-hub:2.3.0-abcdef012345",
                Path("/srv/book-translator-hub/data"),
                ("cwa", "kavita"),
            )

        self.assertEqual(result, {"cwa": True, "kavita": False})
        arguments = run.call_args.args[0]
        self.assertIn("--network", arguments)
        self.assertIn("none", arguments)
        self.assertIn("--read-only", arguments)
        self.assertIn(
            "type=bind,src=/srv/book-translator-hub/data,dst=/data,readonly",
            arguments,
        )
        self.assertEqual(arguments[arguments.index("--user") + 1], "101:102")
        self.assertEqual(arguments[-3:], ["101", "cwa", "kavita"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_hub_data_verification_uses_networkless_read_only_sandbox(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            DockerCLI().verify_hub_data_directory(
                "local/book-translator-hub:2.3.0-abcdef012345",
                Path("/srv/book-translator-hub/data"),
                ("cwa", "kavita"),
            )

        arguments = run.call_args.args[0]
        self.assertIn("--network", arguments)
        self.assertIn("none", arguments)
        self.assertIn("--read-only", arguments)
        self.assertIn(
            "type=bind,src=/srv/book-translator-hub/data,dst=/data,readonly",
            arguments,
        )
        self.assertEqual(arguments[arguments.index("--user") + 1], "101:102")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_legacy_data_preparation_preserves_owner_and_grants_operator_checkpoint_access(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run, mock.patch(
            "btctl_docker.os.getgid", return_value=4242, create=True
        ):
            DockerCLI().prepare_migration_source(
                "sha256:legacy-image-id",
                Path("/srv/cwa-translate/legacy-data"),
            )

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:3], ["docker", "run", "--rm"])
        self.assertIn("sha256:legacy-image-id", arguments)
        self.assertIn(
            "type=bind,src=/srv/cwa-translate/legacy-data,dst=/data",
            arguments,
        )
        script = arguments[-1]
        self.assertIn("find /data -xdev -type d -exec chgrp 4242", script)
        self.assertIn("find /data -xdev -type f -exec chgrp 4242", script)
        self.assertIn("find /data -xdev -type d -exec chmod 2770", script)
        self.assertIn("find /data -xdev -type f -exec chmod 0660", script)
        self.assertNotIn("chown", script)

    def test_runtime_probes_use_exact_container_exec_without_a_host_shell(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            docker = DockerCLI()
            docker.probe_http(
                "cwa-translate-proxy",
                "http://calibre-web-automated:8083/",
            )
            http_arguments = run.call_args.args[0]
            docker.probe_auth(
                "cwa-translate-api",
                "http://calibre-web-automated:8083/ajax/emailstat",
            )
            auth_arguments = run.call_args.args[0]
            docker.probe_reader_auth(
                "book-translator-hub",
                "cwa",
                "http://calibre-web-automated:8083/ajax/emailstat",
            )
            reader_auth_arguments = run.call_args.args[0]
            docker.probe_sqlite(
                "cwa-translate-api",
                "/app/data/translations.db",
            )
            sqlite_arguments = run.call_args.args[0]

        self.assertEqual(http_arguments[:3], ["docker", "exec", "cwa-translate-proxy"])
        self.assertIn("http://calibre-web-automated:8083/", http_arguments)
        self.assertEqual(auth_arguments[:3], ["docker", "exec", "cwa-translate-api"])
        self.assertIn("code in (401,403)", auth_arguments[-2])
        self.assertEqual(
            reader_auth_arguments[:3], ["docker", "exec", "book-translator-hub"]
        )
        self.assertEqual(reader_auth_arguments[-2:], ["cwa", "http://calibre-web-automated:8083/ajax/emailstat"])
        self.assertIn("kind=='text/html'", reader_auth_arguments[-3])
        self.assertEqual(sqlite_arguments[:3], ["docker", "exec", "cwa-translate-api"])
        self.assertIn("/app/data/translations.db", sqlite_arguments)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_provider_probe_requires_every_configured_backend(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            DockerCLI().probe_providers("cwa-translate-api")

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:3], ["docker", "exec", "cwa-translate-api"])
        script = arguments[-1]
        self.assertIn("all(item.get('status')=='ok'", script)
        self.assertIn("check_backend_health", script)

    def test_hub_provider_probe_uses_fixed_secret_free_command(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            DockerCLI().probe_hub_providers("book-translator-hub")

        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments,
            [
                "docker",
                "exec",
                "book-translator-hub",
                "python",
                "/app/hub_runtime.py",
                "--healthcheck-providers",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 240)

    def test_image_version_probe_uses_immutable_networkless_sandbox(self):
        completed = mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch("subprocess.run", return_value=completed) as run:
            DockerCLI().probe_image_version("sha256:legacy", "2.1.4")

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:3], ["docker", "run", "--rm"])
        self.assertIn("--network", arguments)
        self.assertIn("none", arguments)
        self.assertIn("--read-only", arguments)
        self.assertIn("ALL", arguments)
        self.assertIn("no-new-privileges:true", arguments)
        self.assertIn("sha256:legacy", arguments)
        self.assertEqual(arguments[-1], "2.1.4")

    def test_image_build_uses_an_immutable_git_archive_as_stdin(self):
        archive = mock.Mock(returncode=0, stdout=b"tar-bytes", stderr=b"")
        built = mock.Mock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch("subprocess.run", side_effect=[archive, built]) as run:
            DockerCLI().build_image(
                Path("/checkout"),
                "local/cwa-translate:2.2.0-abcdef012345",
                {"io.book-translator.revision": "a" * 40},
            )

        archive_arguments = run.call_args_list[0].args[0]
        build_arguments = run.call_args_list[1].args[0]
        self.assertEqual(archive_arguments[0], "git")
        self.assertIn("core.fsmonitor=false", archive_arguments)
        self.assertIn("core.untrackedCache=false", archive_arguments)
        self.assertEqual(
            archive_arguments[-5:],
            ["-C", "/checkout", "archive", "--format=tar", "a" * 40],
        )
        self.assertEqual(
            run.call_args_list[0].kwargs["env"]["GIT_NO_REPLACE_OBJECTS"],
            "1",
        )
        self.assertEqual(build_arguments[0:2], ["docker", "build"])
        self.assertEqual(build_arguments[-1], "-")
        self.assertEqual(run.call_args_list[1].kwargs["input"], b"tar-bytes")
        self.assertNotIn("/checkout", build_arguments)

    def test_image_build_accepts_the_legacy_cwa_revision_label(self):
        archive = mock.Mock(returncode=0, stdout=b"tar-bytes", stderr=b"")
        built = mock.Mock(returncode=0, stdout=b"", stderr=b"")

        with mock.patch("subprocess.run", side_effect=[archive, built]) as run:
            DockerCLI().build_image(
                Path("/checkout"),
                "local/cwa-translate:2.2.0-abcdef012345",
                {"io.cwa-translate.revision": "b" * 40},
            )

        self.assertEqual(run.call_args_list[0].args[0][-1], "b" * 40)

    def test_image_build_rejects_conflicting_revision_labels(self):
        with mock.patch("subprocess.run") as run:
            with self.assertRaisesRegex(
                DockerCommandError,
                "image build requires one exact source revision",
            ):
                DockerCLI().build_image(
                    Path("/checkout"),
                    "local/book-translator:2.3.0-abcdef012345",
                    {
                        "io.book-translator.revision": "a" * 40,
                        "io.cwa-translate.revision": "b" * 40,
                    },
                )

        run.assert_not_called()

    def test_image_build_rejects_any_malformed_revision_label(self):
        label_sets = (
            {},
            {"io.book-translator.revision": "A" * 40},
            {"io.cwa-translate.revision": "a" * 39},
            {
                "io.book-translator.revision": "a" * 40,
                "io.cwa-translate.revision": "invalid",
            },
        )

        for labels in label_sets:
            with self.subTest(labels=labels), mock.patch("subprocess.run") as run:
                with self.assertRaisesRegex(
                    DockerCommandError,
                    "image build requires one exact source revision",
                ):
                    DockerCLI().build_image(
                        Path("/checkout"),
                        "local/book-translator:2.3.0-abcdef012345",
                        labels,
                    )
                run.assert_not_called()


class UnraidDataPreparationTests(unittest.TestCase):
    def test_root_hardens_the_complete_existing_tree_for_the_runtime_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            nested = data / "cache"
            nested.mkdir(parents=True)
            database = data / "translations.db"
            database.write_text("database", encoding="utf-8")
            cached = nested / "entry.json"
            cached.write_text("cache", encoding="utf-8")

            with (
                mock.patch("btctl_unraid.os.geteuid", return_value=0),
                mock.patch("btctl_unraid.os.chown") as chown,
            ):
                prepare_data_directory(data)

            self.assertEqual(
                {call.args for call in chown.call_args_list},
                {
                    (data, 101, 102),
                    (nested, 101, 102),
                    (database, 101, 102),
                    (cached, 101, 102),
                },
            )
            self.assertEqual(data.stat().st_mode & 0o777, 0o700)
            self.assertEqual(nested.stat().st_mode & 0o777, 0o700)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cached.stat().st_mode & 0o777, 0o600)

    def test_invalid_descendant_stops_before_any_ownership_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "escape").symlink_to(root / "outside")

            with (
                mock.patch("btctl_unraid.os.geteuid", return_value=0),
                mock.patch("btctl_unraid.os.chown") as chown,
                self.assertRaisesRegex(InstallError, "only regular files and directories"),
            ):
                prepare_data_directory(data)

            chown.assert_not_called()


class UnraidInstallTests(unittest.TestCase):
    def setUp(self):
        self.identity = ReleaseIdentity.from_checkout(
            version="2.2.0", sha="c" * 40, clean=True
        )

    def test_install_uses_two_raw_containers_and_commits_verified_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker()

            state = UnraidInstaller(docker, prepare_data=lambda path: path.mkdir()).install(
                config, plan, root
            )

            specs = [call[1] for call in docker.calls if call[0] == "create_container"]
            self.assertEqual({spec.role for spec in specs}, {"api", "proxy"})
            api = next(spec for spec in specs if spec.role == "api")
            proxy = next(spec for spec in specs if spec.role == "proxy")
            self.assertIsNone(api.publish_port)
            self.assertEqual(proxy.publish_port, 8385)
            self.assertEqual(api.image, proxy.image)
            self.assertEqual(api.image, self.identity.image)
            self.assertEqual(os.stat(root / "state" / "api.env").st_mode & 0o777, 0o600)
            self.assertIn(
                "BT_API_UPSTREAM=http://translator-api:8390\n",
                (root / "state" / "proxy.env").read_text(encoding="utf-8"),
            )
            self.assertNotIn("do-not-copy-to-xml", (root / "state" / "state.json").read_text())
            self.assertEqual(StateStore(root / "state").load(), state)
            self.assertTrue((root / "templates-user" / "my-cwa-translate-api.xml").is_file())
            self.assertTrue((root / "templates-user" / "my-cwa-translate-proxy.xml").is_file())

    def test_latest_kavita_accepts_only_the_configured_runtime_image_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime_image_id = "sha256:" + "1" * 64
            config = InstallConfig.from_mapping(
                {
                    **values(root, reader="kavita"),
                    "BT_READER_IMAGE_ID": runtime_image_id,
                },
                self.identity,
            )
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker()
            docker.containers["kavita"]["Config"]["Image"] = (
                "jvmilazz0/kavita:latest"
            )
            docker.containers["kavita"]["Image"] = runtime_image_id

            self.assertIsNone(UnraidInstaller(docker)._preflight(config, plan))

            docker.containers["kavita"]["Image"] = "sha256:" + "2" * 64
            with self.assertRaisesRegex(InstallError, "reader version"):
                UnraidInstaller(docker)._preflight(config, plan)

    def test_kavita_0_9_1_4_unraid_plan_keeps_its_exact_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            config = InstallConfig.from_mapping(
                values(
                    Path(directory), reader="kavita", reader_version="0.9.1.4"
                ),
                self.identity,
            )
            plan = DeploymentPlan.from_config(config)

        self.assertEqual(plan.reader_contract_version, "kavita-0.9.1.4-epub-v1")
        self.assertEqual(config.api_environment()["BT_PUBLIC_ORIGIN"], config.public_origin)

    def test_failure_removes_only_created_roles_and_private_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker(fail_proxy_health=True)

            with self.assertRaisesRegex(InstallError, "health"):
                UnraidInstaller(docker, prepare_data=lambda path: path.mkdir()).install(
                    config, plan, root
                )

            removed = [
                call[1]
                for call in docker.calls
                if call[0] in {"remove_container", "remove_network"}
            ]
            self.assertEqual(
                removed,
                [
                    "cwa-translate-test-proxy",
                    "cwa-translate-test-api",
                    "cwa-translate-test-private",
                ],
            )
            self.assertIn(
                "remove_data_credential", [call[0] for call in docker.calls]
            )
            self.assertIn("calibre-web-automated", docker.containers)
            self.assertFalse((root / "state" / "state.json").exists())

    def test_failed_reader_session_start_removes_new_key_and_can_retry(self):
        for reader in ("cwa", "kavita"):
            with (
                self.subTest(reader=reader),
                tempfile.TemporaryDirectory() as directory,
            ):
                root = Path(directory)
                config_values = values(root, reader=reader)
                config_values["BT_AUTH_PROFILE"] = "reader-session"
                config = InstallConfig.from_mapping(config_values, self.identity)
                plan = DeploymentPlan.from_config(config)
                docker = FakeDocker(fail_proxy_health=True)
                installer = UnraidInstaller(
                    docker, prepare_data=lambda path: path.mkdir(exist_ok=True)
                )

                with self.assertRaisesRegex(InstallError, "health"):
                    installer.install(config, plan, root)

                session_key = Path(config.data_dir) / "reader_session_key"
                self.assertFalse(session_key.exists())
                database = Path(config.data_dir) / "translations.db"
                self.assertTrue(database.exists())
                attempt_store = InstallAttemptStore(Path(config.state_dir))
                self.assertEqual(attempt_store.load()["status"], "cleaned")
                docker.fail_proxy_health = False
                state = installer.install(config, plan, root)
                self.assertEqual(state.status, "installed")
                self.assertFalse(attempt_store.path.exists())
                self.assertTrue(database.exists())

    def test_failed_session_key_cleanup_retains_recovery_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(
                values(root, reader="kavita"), self.identity
            )
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker(fail_proxy_health=True)

            def fail_credential_cleanup(image, path, filename):
                docker.calls.append(
                    ("remove_data_credential", image, str(path), filename)
                )
                raise InstallError("credential removal failed")

            docker.remove_data_credential = fail_credential_cleanup
            with self.assertRaisesRegex(
                InstallError, "health.*reader session credential.*removal failed"
            ):
                UnraidInstaller(
                    docker, prepare_data=lambda path: path.mkdir(exist_ok=True)
                ).install(config, plan, root)

            self.assertTrue((Path(config.data_dir) / "reader_session_key").exists())
            journal = InstallAttemptStore(Path(config.state_dir)).load()
            self.assertEqual(journal["status"], "cleanup-failed")
            self.assertTrue(
                any(
                    "reader session credential" in error
                    for error in journal["cleanup_errors"]
                )
            )

    def test_cleaned_retry_evidence_survives_a_pre_runtime_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(
                values(root, reader="kavita"), self.identity
            )
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker(fail_proxy_health=True)
            installer = UnraidInstaller(
                docker, prepare_data=lambda path: path.mkdir(exist_ok=True)
            )
            with self.assertRaisesRegex(InstallError, "health"):
                installer.install(config, plan, root)

            attempt_store = InstallAttemptStore(Path(config.state_dir))
            cleaned = attempt_store.load()
            original_build = docker.build_image

            def fail_build(repository, image, labels):
                raise InstallError("build failed before runtime")

            docker.fail_proxy_health = False
            docker.build_image = fail_build
            with self.assertRaisesRegex(InstallError, "build failed"):
                installer.install(config, plan, root)

            self.assertEqual(attempt_store.load(), cleaned)
            docker.build_image = original_build
            self.assertEqual(installer.install(config, plan, root).status, "installed")

    def test_cleaned_retry_requires_the_exact_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(
                values(root, reader="kavita"), self.identity
            )
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker(fail_proxy_health=True)
            installer = UnraidInstaller(
                docker, prepare_data=lambda path: path.mkdir(exist_ok=True)
            )
            with self.assertRaisesRegex(InstallError, "health"):
                installer.install(config, plan, root)

            attempt_store = InstallAttemptStore(Path(config.state_dir))
            mismatched = attempt_store.load()
            mismatched["config_fingerprint"] = "0" * 64
            attempt_store.save(mismatched)
            build_count = sum(call[0] == "build_image" for call in docker.calls)

            docker.fail_proxy_health = False
            with self.assertRaisesRegex(InstallError, "unfinished install attempt"):
                installer.install(config, plan, root)

            self.assertEqual(
                sum(call[0] == "build_image" for call in docker.calls), build_count
            )

    def test_ambiguous_network_create_cleans_only_exact_labeled_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker(fail_network_create_after_effect=True)

            with self.assertRaisesRegex(InstallError, "response was lost"):
                UnraidInstaller(
                    docker, prepare_data=lambda path: path.mkdir()
                ).install(config, plan, root)

            self.assertNotIn(plan.resources["private_network"]["name"], docker.networks)
            self.assertFalse((root / "state" / "state.json").exists())

    def test_ambiguous_container_create_cleans_exact_attempted_runtime(self):
        for failed_role in ("api", "proxy"):
            with self.subTest(failed_role=failed_role), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = InstallConfig.from_mapping(values(root), self.identity)
                plan = DeploymentPlan.from_config(config)
                docker = FakeDocker(fail_create_role_after_effect=failed_role)

                with self.assertRaisesRegex(InstallError, "response was lost"):
                    UnraidInstaller(
                        docker, prepare_data=lambda path: path.mkdir()
                    ).install(config, plan, root)

                for role in ("api", "proxy"):
                    self.assertNotIn(plan.resources[role]["name"], docker.containers)
                self.assertNotIn(
                    plan.resources["private_network"]["name"], docker.networks
                )
                self.assertFalse((root / "state" / "state.json").exists())

    def test_forwarded_install_writes_a_private_caddy_identity_edge_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(
                values(root, forwarded=True), self.identity
            )
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker()

            state = UnraidInstaller(
                docker, prepare_data=lambda path: path.mkdir()
            ).install(config, plan, root)

            artifact = root / "state" / "authentik-edge.caddy"
            self.assertTrue(artifact.is_file())
            self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
            self.assertIn("request_header -Cookie", artifact.read_text())
            self.assertIn("sha256", state.resources["identity_edge_config"])

    def test_reinstall_after_managed_uninstall_archives_old_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker()
            installer = UnraidInstaller(
                docker, prepare_data=lambda path: path.mkdir(exist_ok=True)
            )
            original = installer.install(config, plan, root)
            RuntimeUninstaller(docker).uninstall(config, plan)

            replacement = installer.install(config, plan, root)

            self.assertNotEqual(replacement.install_id, original.install_id)
            history = (
                root
                / "state"
                / "history"
                / f"{original.install_id}-uninstalled.json"
            )
            self.assertTrue(history.is_file())
            self.assertIn('"status": "uninstalled"', history.read_text())

    def test_template_collision_stops_before_image_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            template_dir = root / "templates-user"
            template_dir.mkdir()
            (template_dir / "my-cwa-translate-api.xml").write_text("preserve")
            docker = FakeDocker()

            with self.assertRaisesRegex(InstallError, "template"):
                UnraidInstaller(docker, prepare_data=lambda path: path.mkdir()).install(
                    config, plan, root
                )

            self.assertNotIn("build_image", [call[0] for call in docker.calls])
            self.assertEqual(
                (template_dir / "my-cwa-translate-api.xml").read_text(), "preserve"
            )

    def test_fresh_install_rejects_a_nonempty_data_directory_before_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            marker = data / "belongs-to-another-app"
            marker.write_text("preserve", encoding="utf-8")
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker()

            with self.assertRaisesRegex(InstallError, "empty for a fresh install"):
                UnraidInstaller(
                    docker, prepare_data=lambda path: self.fail("must not prepare data")
                ).install(config, plan, root)

            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            self.assertNotIn("build_image", [call[0] for call in docker.calls])

    def test_cleanup_errors_are_aggregated_and_leave_recovery_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker(fail_proxy_health=True)
            original_remove = docker.remove_container

            def remove_container(name):
                if name.endswith("-proxy"):
                    raise InstallError("proxy removal failed")
                original_remove(name)

            docker.remove_container = remove_container
            with self.assertRaisesRegex(
                InstallError, "proxy health failed.*cleanup.*proxy removal failed"
            ):
                UnraidInstaller(
                    docker, prepare_data=lambda path: path.mkdir()
                ).install(config, plan, root)

            journal = InstallAttemptStore(Path(config.state_dir)).load()
            self.assertEqual(journal["status"], "cleanup-failed")
            self.assertTrue(
                any("proxy" in error for error in journal["cleanup_errors"])
            )


class UnraidAdoptTests(unittest.TestCase):
    def test_adopt_recovers_btctl_runtime_without_docker_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = ReleaseIdentity.from_checkout(
                version="2.2.0", sha="d" * 40, clean=True
            )
            config = InstallConfig.from_mapping(values(root), identity)
            plan = DeploymentPlan.from_config(config)
            docker = FakeDocker()
            UnraidInstaller(docker, prepare_data=lambda path: path.mkdir()).install(
                config, plan, root
            )
            (root / "state" / "state.json").unlink()
            before = len(docker.calls)

            state = UnraidAdopter(docker).adopt(config, plan)

            self.assertEqual(state.status, "adopted")
            for resource in (
                "api",
                "proxy",
                "private_network",
                "api_template",
                "proxy_template",
            ):
                self.assertEqual(state.resources[resource]["ownership"], "owned")
            self.assertFalse(
                {"build_image", "create_network", "create_container", "start_container"}
                & {call[0] for call in docker.calls[before:]}
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
