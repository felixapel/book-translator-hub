"""Unraid adapter using local Docker only; no SSH, registry, or reader fork."""

from __future__ import annotations

import copy
import hashlib
import os
import stat
import string
import tempfile
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol
from xml.sax.saxutils import escape

from btctl_compose import (
    ComposeInstaller,
    InstallError,
    _container_networks,
    _cleaned_attempt_matches,
    _completed_uninstall_for_reinstall,
    _has_exact_reader_version,
    _install_attempt_payload,
    _labels,
    _bounded_error,
    _managed_credential_present,
    _probe_runtime_dependencies,
    _remove_new_session_key,
    _validate_data_destination,
    _verify_identity_edge_artifact,
    _verify_private_network,
)
from btctl_auth import render_authentik_edge
from btctl_core import (
    ConfigError,
    DeploymentPlan,
    DeploymentState,
    DockerProbeBase,
    InstallAttemptStore,
    InstallConfig,
    OperationLock,
    StateStore,
    _fsync_directory,
    ensure_directory_durable,
)


TEMPLATE_ROOT = Path(__file__).parent / "deploy" / "unraid"


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    role: str
    name: str
    image: str
    env_file: Path
    labels: dict[str, str]
    primary_network: str
    network_alias: str
    data_dir: Path | None
    publish_port: int | None


class UnraidDocker(DockerProbeBase, Protocol):
    def build_image(self, repository: Path, image: str, labels: dict[str, str]) -> None: ...
    def remove_data_credential(self, image: str, path: Path, filename: str) -> None: ...
    def create_network(self, name: str, labels: dict[str, str], *, internal: bool) -> None: ...
    def create_container(self, spec: ContainerSpec) -> None: ...
    def connect_network(self, network: str, container: str) -> None: ...
    def start_container(self, name: str) -> None: ...
    def remove_container(self, name: str) -> None: ...
    def remove_network(self, name: str) -> None: ...


def _xml(value: object) -> str:
    return escape(str(value), {'"': "&quot;", "'": "&apos;"})


def render_templates(config: InstallConfig, plan: DeploymentPlan) -> dict[str, str]:
    """Render informational DockerMan templates without embedding secrets."""
    api_source = (TEMPLATE_ROOT / "my-cwa-translate-api.xml.tmpl").read_text(
        encoding="utf-8"
    )
    proxy_source = (TEMPLATE_ROOT / "my-cwa-translate-proxy.xml.tmpl").read_text(
        encoding="utf-8"
    )
    state_dir = Path(config.state_dir)
    common = {
        "IMAGE": _xml(config.image),
        "PRIVATE_NETWORK": _xml(plan.resources["private_network"]["name"]),
    }
    api = string.Template(api_source).substitute(
        **common,
        NAME=_xml(plan.resources["api"]["name"]),
        ENV_FILE=_xml(state_dir / "api.env"),
        DATA_DIR=_xml(config.data_dir),
    )
    if config.proxy_port is None:
        port_config = ""
    else:
        port = _xml(config.proxy_port)
        port_config = (
            f'  <Config Name="Web port" Target="8080" Default="{port}" '
            f'Mode="tcp" Description="Only browser-facing translator port." '
            f'Type="Port" Display="always" Required="true" Mask="false">'
            f"{port}</Config>\n"
        )
    proxy = string.Template(proxy_source).substitute(
        **common,
        NAME=_xml(plan.resources["proxy"]["name"]),
        ENV_FILE=_xml(state_dir / "proxy.env"),
        PUBLIC_ORIGIN=_xml(config.public_origin),
        PORT_CONFIG=port_config,
    )
    try:
        ET.fromstring(api)
        ET.fromstring(proxy)
    except ET.ParseError as exc:
        raise InstallError("generated Unraid template is not valid XML") from exc
    return {"api": api, "proxy": proxy}


def _write_private(path: Path, text: str, *, private_parent: bool = True) -> None:
    if path.parent.is_symlink() or path.is_symlink():
        raise InstallError("managed file destination must not be a symbolic link")
    try:
        ensure_directory_durable(
            path.parent,
            mode=0o700 if private_parent else 0o755,
            enforce_existing_mode=private_parent,
        )
    except ConfigError as exc:
        raise InstallError("managed file directory is unsafe") from exc
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _environment_text(values: dict[str, str]) -> str:
    lines = []
    for name, value in sorted(values.items()):
        if "\n" in value or "\r" in value or "\0" in value:
            raise InstallError("runtime environment contains an unsafe value")
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def prepare_data_directory(path: Path) -> None:
    if path.is_symlink():
        raise InstallError("BT_DATA_DIR must not be a symbolic link")
    try:
        ensure_directory_durable(path, enforce_existing_mode=False)
    except ConfigError as exc:
        raise InstallError("BT_DATA_DIR could not be created durably") from exc

    entries: list[tuple[Path, os.stat_result, int]] = []
    try:
        root_metadata = path.lstat()
        candidates = [path, *sorted(path.rglob("*"))]
        for candidate in candidates:
            metadata = candidate.lstat()
            if metadata.st_dev != root_metadata.st_dev:
                raise InstallError(
                    "BT_DATA_DIR must contain only regular files and directories"
                )
            if stat.S_ISDIR(metadata.st_mode):
                mode = 0o700
            elif stat.S_ISREG(metadata.st_mode):
                mode = 0o600
            else:
                raise InstallError(
                    "BT_DATA_DIR must contain only regular files and directories"
                )
            entries.append((candidate, metadata, mode))
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError("BT_DATA_DIR could not be inspected safely") from exc

    effective_uid = os.geteuid()
    if effective_uid != 0:
        if effective_uid != 101 or any(
            (metadata.st_uid, metadata.st_gid) != (101, 102)
            for _, metadata, _ in entries
        ):
            raise InstallError(
                "BT_DATA_DIR tree must be owned by uid 101 gid 102; "
                "run btctl as root on Unraid"
            )

    try:
        for candidate, _, mode in entries:
            if effective_uid == 0:
                os.chown(candidate, 101, 102, follow_symlinks=False)
            os.chmod(candidate, mode, follow_symlinks=False)
        _fsync_directory(path)
        _fsync_directory(path.parent)
    except (ConfigError, OSError) as exc:
        raise InstallError("BT_DATA_DIR ownership could not be prepared") from exc


def _unraid_labels(config: InstallConfig, role: str, install_id: str) -> dict[str, str]:
    labels = _labels(config, role, install_id)
    labels.update(
        {
            "net.unraid.docker.managed": "dockerman",
            "net.unraid.docker.webui": config.public_origin,
        }
    )
    return labels


class UnraidInstaller:
    def __init__(
        self,
        docker: UnraidDocker,
        *,
        health_timeout_seconds: int = 90,
        prepare_data: Callable[[Path], None] = prepare_data_directory,
    ):
        self.docker = docker
        self.health_timeout_seconds = health_timeout_seconds
        self.prepare_data = prepare_data

    def _preflight(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        *,
        allow_existing_data: bool = False,
        allow_rolled_back_state: bool = False,
    ) -> DeploymentState | None:
        if config.install_profile != "unraid":
            raise InstallError("Unraid installer requires the unraid profile")
        self.docker.require_available()
        previous_state = _completed_uninstall_for_reinstall(
            config,
            plan,
            allow_rolled_back=allow_rolled_back_state,
        )
        reader = self.docker.inspect_container(config.reader_container)
        if reader is None or reader.get("State", {}).get("Status") != "running":
            raise InstallError("configured reader container is missing or stopped")
        if not _has_exact_reader_version(
            reader, config.reader_version, config.reader_image_id
        ):
            raise InstallError("configured reader version lacks exact runtime evidence")
        if config.reader_network not in _container_networks(reader):
            raise InstallError("configured reader is not on BT_READER_NETWORK")
        if self.docker.inspect_network(config.reader_network) is None:
            raise InstallError("BT_READER_NETWORK does not exist")
        if config.edge_network and self.docker.inspect_network(config.edge_network) is None:
            raise InstallError("BT_EDGE_NETWORK does not exist")
        for role in ("api", "proxy"):
            name = str(plan.resources[role]["name"])
            if self.docker.inspect_container(name) is not None:
                raise InstallError(f"container {name} already exists")
        private_name = str(plan.resources["private_network"]["name"])
        if self.docker.inspect_network(private_name) is not None:
            raise InstallError(f"network {private_name} already exists")
        for role in ("api", "proxy"):
            target = Path(plan.resources[f"{role}_template"]["path"])
            if target.exists() or target.is_symlink():
                raise InstallError(f"Unraid {role} template already exists")
        cleaned_retry = False
        try:
            StateStore(Path(config.state_dir))._validate_destination()
            attempt_store = InstallAttemptStore(Path(config.state_dir))
            if attempt_store.path.exists():
                attempt = attempt_store.load()
                if not _cleaned_attempt_matches(attempt, plan):
                    raise InstallError(
                        "unfinished install attempt exists; inspect recovery evidence"
                    )
                cleaned_retry = True
        except ConfigError as exc:
            raise InstallError(f"state directory is unsafe: {exc}") from exc
        _validate_data_destination(
            Path(config.data_dir),
            allow_nonempty=(
                allow_existing_data or previous_state is not None or cleaned_retry
            ),
        )
        return previous_state

    def install(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        repository: Path,
        *,
        _operation_locked: bool = False,
        _allow_existing_data: bool = False,
        _allow_rolled_back_state: bool = False,
    ) -> DeploymentState:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir)):
                return self.install(
                    config,
                    plan,
                    repository,
                    _operation_locked=True,
                    _allow_existing_data=_allow_existing_data,
                    _allow_rolled_back_state=_allow_rolled_back_state,
                )
        previous_state = self._preflight(
            config,
            plan,
            allow_existing_data=_allow_existing_data,
            allow_rolled_back_state=_allow_rolled_back_state,
        )
        install_id = str(uuid.uuid4())
        state_dir = Path(config.state_dir)
        attempt_store = InstallAttemptStore(state_dir)
        retry_evidence: dict[str, object] | None = None
        if attempt_store.path.exists():
            try:
                retry_evidence = attempt_store.load()
            except ConfigError as exc:
                raise InstallError(
                    "cleaned retry evidence could not be reloaded"
                ) from exc
        attempt = _install_attempt_payload(plan, install_id)
        try:
            attempt_store.save(attempt)
        except ConfigError as exc:
            raise InstallError("install attempt could not be committed") from exc
        api_env = state_dir / "api.env"
        proxy_env = state_dir / "proxy.env"
        session_key_preexisting = False
        try:
            image_labels = {
                "io.book-translator.version": config.identity.version,
                "io.book-translator.revision": config.identity.sha,
                "io.book-translator.source": "local-checkout",
                "io.cwa-translate.version": config.identity.version,
                "io.cwa-translate.revision": config.identity.sha,
                "io.cwa-translate.source": "local-checkout",
            }
            if config.reader_type != "cwa":
                image_labels = {
                    key: value
                    for key, value in image_labels.items()
                    if not key.startswith("io.cwa-translate.")
                }
            self.docker.build_image(Path(repository), config.image, image_labels)
            verifier = ComposeInstaller(self.docker)
            image_id = verifier._verify_image(
                config, self.docker.inspect_image(config.image)
            )
            self.prepare_data(Path(config.data_dir))
            if config.uses_reader_session:
                session_key_preexisting = _managed_credential_present(
                    Path(config.data_dir) / "reader_session_key"
                )

            _write_private(
                api_env,
                _environment_text(
                    {
                        **config.api_environment(),
                        "BT_ROLE": "api",
                        **(
                            {"BT_READER_CONNECTOR_ID": install_id}
                            if config.uses_reader_session
                            else {}
                        ),
                    }
                ),
            )
            _write_private(
                proxy_env,
                _environment_text(
                    {
                        **config.proxy_environment(),
                        "BT_ROLE": "proxy",
                        "BT_API_UPSTREAM": "http://translator-api:8390",
                    }
                ),
            )
            templates = render_templates(config, plan)
            for role, source in templates.items():
                _write_private(state_dir / f"{role}.template.xml", source)
            if config.auth_profile == "authentik-forwarded":
                artifact = render_authentik_edge(config, plan)
                artifact_path = Path(
                    str(plan.resources["identity_edge_config"]["path"])
                )
                if artifact_path.name != artifact.filename:
                    raise InstallError(
                        "identity-edge artifact name does not match the plan"
                    )
                _write_private(artifact_path, artifact.content)
        except BaseException as exc:
            cleanup_errors: list[str] = []
            try:
                if retry_evidence is None:
                    attempt_store.remove()
                else:
                    attempt_store.save(retry_evidence)
            except BaseException as cleanup_exc:
                cleanup_errors.append(_bounded_error("journal", cleanup_exc))
            if cleanup_errors:
                attempt["status"] = "cleanup-failed"
                attempt["cleanup_errors"] = cleanup_errors
                try:
                    attempt_store.save(attempt)
                except BaseException:
                    pass
                raise InstallError(
                    f"{exc}; cleanup failed: {'; '.join(cleanup_errors)}"
                ) from exc
            raise

        private_name = str(plan.resources["private_network"]["name"])
        network_attempted = False
        attempted_roles: list[tuple[str, str]] = []
        copied_templates: list[Path] = []
        state_committed = False
        try:
            attempt["status"] = "starting"
            attempt_store.save(attempt)
            network_attempted = True
            self.docker.create_network(
                private_name,
                _labels(config, "private-network", install_id),
                internal=True,
            )
            api_name = str(plan.resources["api"]["name"])
            attempted_roles.append(("api", api_name))
            self.docker.create_container(
                ContainerSpec(
                    role="api",
                    name=api_name,
                    image=config.image,
                    env_file=api_env,
                    labels=_unraid_labels(config, "api", install_id),
                    primary_network=private_name,
                    network_alias="translator-api",
                    data_dir=Path(config.data_dir),
                    publish_port=None,
                )
            )
            api_external = (
                config.reader_network
                if config.uses_reader_session
                else config.edge_network
            )
            self.docker.connect_network(api_external, api_name)
            self.docker.start_container(api_name)
            self.docker.wait_healthy([api_name], self.health_timeout_seconds)

            proxy_name = str(plan.resources["proxy"]["name"])
            attempted_roles.append(("proxy", proxy_name))
            self.docker.create_container(
                ContainerSpec(
                    role="proxy",
                    name=proxy_name,
                    image=config.image,
                    env_file=proxy_env,
                    labels=_unraid_labels(config, "proxy", install_id),
                    primary_network=private_name,
                    network_alias="translator-proxy",
                    data_dir=None,
                    publish_port=config.proxy_port,
                )
            )
            self.docker.connect_network(config.reader_network, proxy_name)
            if config.edge_network:
                self.docker.connect_network(config.edge_network, proxy_name)
            self.docker.start_container(proxy_name)
            self.docker.wait_healthy([proxy_name], self.health_timeout_seconds)
            _probe_runtime_dependencies(self.docker, config, plan)

            resources = copy.deepcopy(plan.resources)
            for role in ("api", "proxy"):
                container_id, _ = verifier._verify_container(
                    config, plan, install_id, role, image_id
                )
                resources[role]["id"] = container_id
            private = self.docker.inspect_network(private_name)
            resources["private_network"]["id"] = _verify_private_network(
                config, install_id, private
            )
            _verify_identity_edge_artifact(config, plan, resources)

            for role, source in templates.items():
                target = Path(plan.resources[f"{role}_template"]["path"])
                _write_private(target, source, private_parent=False)
                copied_templates.append(target)
                resources[f"{role}_template"]["sha256"] = hashlib.sha256(
                    source.encode("utf-8")
                ).hexdigest()
            state = replace(
                DeploymentState.new(install_id=install_id, plan=plan),
                resources=resources,
            )
            state_store = StateStore(state_dir)
            if previous_state is not None:
                state_store.archive(previous_state)
            state_store.save(state)
            state_committed = True
            attempt_store.remove()
            return state
        except BaseException as exc:
            if state_committed:
                raise InstallError(
                    "runtime state committed but install-attempt cleanup failed; "
                    "run doctor before further lifecycle operations"
                ) from exc
            cleanup_errors: list[str] = []
            for target in reversed(copied_templates):
                try:
                    target.unlink()
                except OSError as cleanup_exc:
                    cleanup_errors.append(
                        _bounded_error(f"template {target.name}", cleanup_exc)
                    )
            for role, name in reversed(attempted_roles):
                try:
                    container = self.docker.inspect_container(name)
                    labels = (
                        container.get("Config", {}).get("Labels", {})
                        if container
                        else {}
                    )
                    expected = _unraid_labels(config, role, install_id)
                    if (
                        container is not None
                        and container.get("Config", {}).get("Image") == config.image
                        and all(labels.get(key) == value for key, value in expected.items())
                    ):
                        self.docker.remove_container(name)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(
                        _bounded_error(f"{role} container", cleanup_exc)
                    )
            if network_attempted:
                try:
                    network = self.docker.inspect_network(private_name)
                    if network is not None:
                        _verify_private_network(config, install_id, network)
                        self.docker.remove_network(private_name)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(
                        _bounded_error("private network", cleanup_exc)
                    )
            if not cleanup_errors:
                try:
                    _remove_new_session_key(
                        self.docker,
                        config,
                        preexisting=session_key_preexisting,
                    )
                except BaseException as cleanup_exc:
                    cleanup_errors.append(
                        _bounded_error("reader session credential", cleanup_exc)
                    )
            if cleanup_errors:
                attempt["status"] = "cleanup-failed"
                attempt["cleanup_errors"] = cleanup_errors
                try:
                    attempt_store.save(attempt)
                except BaseException as journal_exc:
                    cleanup_errors.append(_bounded_error("journal", journal_exc))
                raise InstallError(
                    f"{exc}; cleanup failed: {'; '.join(cleanup_errors)}"
                ) from exc
            attempt["status"] = "cleaned"
            attempt["cleanup_errors"] = []
            try:
                attempt_store.save(attempt)
            except BaseException as cleanup_exc:
                cleanup_errors.append(_bounded_error("journal", cleanup_exc))
                attempt["status"] = "cleanup-failed"
                attempt["cleanup_errors"] = cleanup_errors
                try:
                    attempt_store.save(attempt)
                except BaseException:
                    pass
                raise InstallError(
                    f"{exc}; cleanup failed: {'; '.join(cleanup_errors)}"
                ) from exc
            raise


class UnraidAdopter:
    """Recover Unraid state only from complete matching btctl evidence."""

    def __init__(self, docker: UnraidDocker):
        self.docker = docker

    def adopt(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        *,
        _operation_locked: bool = False,
    ) -> DeploymentState:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir)):
                return self.adopt(config, plan, _operation_locked=True)
        if config.install_profile != "unraid":
            raise InstallError("Unraid adoption requires the unraid profile")
        self.docker.require_available()
        store = StateStore(Path(config.state_dir))
        if store.path.exists():
            raise InstallError("deployment state already exists; use doctor")
        reader = self.docker.inspect_container(config.reader_container)
        if (
            reader is None
            or reader.get("State", {}).get("Status") != "running"
            or not _has_exact_reader_version(
                reader, config.reader_version, config.reader_image_id
            )
            or config.reader_network not in _container_networks(reader)
        ):
            raise InstallError("configured reader evidence does not match")
        containers = {
            role: self.docker.inspect_container(str(plan.resources[role]["name"]))
            for role in ("api", "proxy")
        }
        for role, container in containers.items():
            labels = container.get("Config", {}).get("Labels", {}) if container else {}
            neutral = "io.book-translator."
            legacy = "io.cwa-translate." if config.reader_type == "cwa" else neutral
            if (
                not container
                or labels.get(neutral + "managed-by") != "btctl"
                or labels.get(neutral + "role") != role
                or labels.get(neutral + "version") != config.identity.version
                or labels.get(neutral + "revision") != config.identity.sha
                or not labels.get(neutral + "install-id")
                or labels.get(legacy + "install-id")
                != labels.get(neutral + "install-id")
            ):
                raise InstallError(f"{role} ownership labels are missing or incompatible")
        install_id = containers["api"]["Config"]["Labels"][
            "io.book-translator.install-id"
        ]
        if containers["proxy"]["Config"]["Labels"].get(
            "io.book-translator.install-id"
        ) != install_id:
            raise InstallError("split runtime install-id labels do not match")
        verifier = ComposeInstaller(self.docker)
        image_id = verifier._verify_image(config, self.docker.inspect_image(config.image))
        resources = copy.deepcopy(plan.resources)
        for role in ("api", "proxy"):
            if containers[role].get("State", {}).get("Health", {}).get("Status") != "healthy":
                raise InstallError(f"{role} container is not healthy")
            container_id, _ = verifier._verify_container(
                config, plan, install_id, role, image_id
            )
            resources[role]["id"] = container_id
        private = self.docker.inspect_network(
            str(plan.resources["private_network"]["name"])
        )
        resources["private_network"]["id"] = _verify_private_network(
            config, install_id, private
        )
        _verify_identity_edge_artifact(config, plan, resources)
        for role in ("api", "proxy"):
            path = Path(plan.resources[f"{role}_template"]["path"])
            if not path.is_file() or path.is_symlink():
                raise InstallError(f"{role} Unraid template is missing")
            expected = render_templates(config, plan)[role].encode("utf-8")
            if path.read_bytes() != expected:
                raise InstallError(f"{role} Unraid template does not match the plan")
            resources[f"{role}_template"]["sha256"] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
        state = replace(
            DeploymentState.new(install_id=install_id, plan=plan),
            status="adopted",
            resources=resources,
        )
        store.save(state)
        return state
