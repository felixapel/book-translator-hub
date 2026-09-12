"""Universal one-container deployment model and schema-3 ownership state."""

from __future__ import annotations

import hashlib
import json
import os
import copy
import fcntl
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from btctl_compose import (
    InstallError,
    _bounded_error,
    _cleaned_attempt_matches,
    _container_networks,
    _has_exact_reader_version,
    _install_attempt_payload,
)
from btctl_core import (
    ConfigError,
    InstallAttemptStore,
    OperationLock,
    ReleaseIdentity,
    StateStore,
    _absolute_dir,
    _require_disjoint_directories,
    ensure_directory_durable,
    paths_overlap,
    read_private_text,
    redact_mapping,
)
from btctl_lifecycle import (
    _make_tree_durable,
    _secure_copy_atomic,
    _sqlite_integrity,
    _tree_manifest,
)
from hub_runtime import HubConfig, HubConfigError, _PROCESS_KEYS


HUB_STATE_SCHEMA_VERSION = 3
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_ACTIVE_STATES = {"installed", "adopted"}


def _effective_uid() -> int:
    """Return the host operator UID; isolated for deterministic policy tests."""
    return os.geteuid()


def is_hub_configuration(values: Mapping[str, str]) -> bool:
    return values.get("BT_TOPOLOGY") == "hub"


def _choice(values: Mapping[str, str], name: str, choices: set[str]) -> str:
    value = values.get(name, "")
    if value not in choices:
        raise ConfigError(f"{name} must be one of {sorted(choices)}")
    return value


def _clean_name(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "")
    if not isinstance(value, str) or not _NAME_RE.fullmatch(value):
        raise ConfigError(f"{name} must be one bounded Docker-safe name")
    return value


def _paths_overlap(first: Path, second: Path) -> bool:
    """Shared path-containment check; canonical logic lives in btctl_core."""
    return paths_overlap(first, second)


def _runtime_environment(values: Mapping[str, str], runtime: HubConfig) -> dict[str, str]:
    environment = {
        name: value
        for name, value in values.items()
        if isinstance(value, str)
        and (
            name in _PROCESS_KEYS
            or name.startswith("BT_CWA_LLM_")
            or name.startswith("BT_KAVITA_LLM_")
            or name in {"BT_CWA_LOCAL_URL", "BT_KAVITA_LOCAL_URL"}
            or name.startswith("BT_CWA_MAX_")
            or name.startswith("BT_KAVITA_MAX_")
        )
    }
    environment["BT_ROLE"] = "hub"
    environment["BT_MAX_CONCURRENT"] = values.get("BT_MAX_CONCURRENT", "2")
    environment["BT_MAX_UPSTREAM_INFLIGHT"] = values.get(
        "BT_MAX_UPSTREAM_INFLIGHT", "2"
    )
    for reader in ("cwa", "kavita"):
        environment[f"BT_ENABLE_{reader.upper()}"] = "false"
    for reader in runtime.readers:
        prefix = f"BT_{reader.name.upper()}_"
        environment[f"BT_ENABLE_{reader.name.upper()}"] = "true"
        environment.update(
            {
                prefix + "PUBLIC_ORIGIN": reader.public_origin,
                prefix + "READER_UPSTREAM": reader.upstream,
                prefix + "READER_VERSION": reader.version,
                prefix + "AUTH_PROFILE": reader.auth_profile,
                prefix + "READER_CONNECTOR_ID": reader.environment[
                    "BT_READER_CONNECTOR_ID"
                ],
                prefix + "PUBLISHED_PORT": str(reader.published_port),
            }
        )
    return environment


@dataclass(frozen=True, slots=True)
class HubInstallConfig:
    identity: ReleaseIdentity
    install_profile: str
    install_name: str
    state_dir: str
    data_dir: str
    backup_dir: str
    runtime: HubConfig
    reader_containers: dict[str, str]
    reader_networks: dict[str, str]
    reader_image_ids: dict[str, str]
    environment: dict[str, str] = field(repr=False)

    @property
    def image(self) -> str:
        return f"local/book-translator-hub:{self.identity.version}-{self.identity.sha[:12]}"

    @classmethod
    def from_mapping(
        cls, values: Mapping[str, str], identity: ReleaseIdentity
    ) -> "HubInstallConfig":
        if values.get("BT_TOPOLOGY") != "hub":
            raise ConfigError("BT_TOPOLOGY must be hub")
        profile = _choice(
            values, "BT_INSTALL_PROFILE", {"compose-existing", "unraid"}
        )
        install_name = _clean_name(values, "BT_INSTALL_NAME")
        state_dir = _absolute_dir(values.get("BT_STATE_DIR", ""), "BT_STATE_DIR")
        data_dir = _absolute_dir(values.get("BT_DATA_DIR", ""), "BT_DATA_DIR")
        backup_dir = _absolute_dir(values.get("BT_BACKUP_DIR", ""), "BT_BACKUP_DIR")
        _require_disjoint_directories(
            {
                "BT_STATE_DIR": state_dir,
                "BT_DATA_DIR": data_dir,
                "BT_BACKUP_DIR": backup_dir,
            }
        )
        try:
            runtime = HubConfig.from_environment(values)
        except HubConfigError as exc:
            raise ConfigError(str(exc)) from exc
        containers: dict[str, str] = {}
        networks: dict[str, str] = {}
        image_ids: dict[str, str] = {}
        for reader in runtime.readers:
            prefix = f"BT_{reader.name.upper()}_"
            container = _clean_name(values, prefix + "READER_CONTAINER")
            network = _clean_name(values, prefix + "READER_NETWORK")
            if (urlsplit(reader.upstream).hostname or "").casefold() != container.casefold():
                raise ConfigError(
                    f"{prefix}READER_UPSTREAM host must match its declared reader container"
                )
            image_id = values.get(prefix + "READER_IMAGE_ID", "")
            if image_id and not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
                raise ConfigError(f"{prefix}READER_IMAGE_ID must be an exact image ID")
            containers[reader.name] = container
            networks[reader.name] = network
            image_ids[reader.name] = image_id
        return cls(
            identity=identity,
            install_profile=profile,
            install_name=install_name,
            state_dir=state_dir,
            data_dir=data_dir,
            backup_dir=backup_dir,
            runtime=runtime,
            reader_containers=containers,
            reader_networks=networks,
            reader_image_ids=image_ids,
            environment=_runtime_environment(values, runtime),
        )


@dataclass(frozen=True, slots=True)
class HubPlan:
    version: str
    revision: str
    image: str
    config_fingerprint: str
    install_profile: str
    state_dir: str
    data_dir: str
    backup_dir: str
    readers: dict[str, dict[str, object]]
    resources: dict[str, dict[str, object]]
    environment: dict[str, str]

    @classmethod
    def from_config(cls, config: HubInstallConfig) -> "HubPlan":
        readers = {
            reader.name: {
                "container": config.reader_containers[reader.name],
                "network": config.reader_networks[reader.name],
                "version": reader.version,
                "contract": reader.environment["BT_READER_CONTRACT_VERSION"],
                "published_port": reader.published_port,
            }
            for reader in config.runtime.readers
        }
        public = {
            "install_profile": config.install_profile,
            "install_name": config.install_name,
            "state_dir": config.state_dir,
            "data_dir": config.data_dir,
            "backup_dir": config.backup_dir,
            "readers": readers,
            "environment": redact_mapping(config.environment),
        }
        fingerprint = hashlib.sha256(
            json.dumps(public, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        ports = [reader.published_port for reader in config.runtime.readers]
        resources = {
            "hub": {
                "name": config.install_name,
                "ownership": "owned",
                "role": "hub",
                "published_ports": ports,
            },
            "data": {
                "path": config.data_dir,
                "ownership": "external",
                "retention": "always-preserve",
            },
        }
        for reader in config.runtime.readers:
            resources[f"reader_{reader.name}"] = {
                "name": config.reader_containers[reader.name],
                "network": config.reader_networks[reader.name],
                "ownership": "external",
            }
            resources[f"session_key_{reader.name}"] = {
                "path": str(Path(config.data_dir) / reader.name / "reader_session_key"),
                "ownership": "owned-credential",
                "retention": "remove-on-uninstall",
            }
        return cls(
            version=config.identity.version,
            revision=config.identity.sha,
            image=config.image,
            config_fingerprint=fingerprint,
            install_profile=config.install_profile,
            state_dir=config.state_dir,
            data_dir=config.data_dir,
            backup_dir=config.backup_dir,
            readers=readers,
            resources=resources,
            environment=redact_mapping(config.environment),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": HUB_STATE_SCHEMA_VERSION,
            "topology": "hub",
            "version": self.version,
            "revision": self.revision,
            "image": self.image,
            "config_fingerprint": self.config_fingerprint,
            "install_profile": self.install_profile,
            "state_dir": self.state_dir,
            "data_dir": self.data_dir,
            "backup_dir": self.backup_dir,
            "readers": self.readers,
            "resources": self.resources,
            "environment": self.environment,
        }


@dataclass(frozen=True, slots=True)
class HubState:
    schema_version: int
    topology: str
    install_id: str
    status: str
    version: str
    revision: str
    image: str
    config_fingerprint: str
    install_profile: str
    readers: dict[str, dict[str, object]]
    resources: dict[str, dict[str, object]]

    @classmethod
    def new(cls, *, install_id: str, plan: HubPlan) -> "HubState":
        normalized = str(uuid.UUID(install_id))
        if normalized != install_id:
            raise ConfigError("install_id must be a canonical UUID")
        return cls(
            schema_version=HUB_STATE_SCHEMA_VERSION,
            topology="hub",
            install_id=normalized,
            status="installed",
            version=plan.version,
            revision=plan.revision,
            image=plan.image,
            config_fingerprint=plan.config_fingerprint,
            install_profile=plan.install_profile,
            readers=plan.readers,
            resources=plan.resources,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "topology": self.topology,
            "install_id": self.install_id,
            "status": self.status,
            "version": self.version,
            "revision": self.revision,
            "image": self.image,
            "config_fingerprint": self.config_fingerprint,
            "install_profile": self.install_profile,
            "readers": self.readers,
            "resources": self.resources,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "HubState":
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "topology", "install_id", "status", "version",
            "revision", "image", "config_fingerprint", "install_profile",
            "readers", "resources",
        }:
            raise ConfigError("hub state fields do not match schema 3")
        state = cls(**payload)
        if state.schema_version != HUB_STATE_SCHEMA_VERSION or state.topology != "hub":
            raise ConfigError("unsupported hub state schema")
        try:
            if str(uuid.UUID(state.install_id)) != state.install_id:
                raise ValueError
        except (ValueError, AttributeError) as exc:
            raise ConfigError("hub state install_id is invalid") from exc
        if state.status not in _ACTIVE_STATES | {"uninstalling", "uninstalled", "rolled_back"}:
            raise ConfigError("hub state status is invalid")
        if not isinstance(state.readers, dict) or not state.readers:
            raise ConfigError("hub state readers are incomplete")
        if not isinstance(state.resources, dict):
            raise ConfigError("hub state resources are incomplete")
        return state


class HubStateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "state.json"

    def save(self, state: HubState) -> None:
        if self.state_dir.is_symlink() or self.path.is_symlink():
            raise ConfigError("hub state destination must not be a symbolic link")
        ensure_directory_durable(self.state_dir)
        descriptor, temporary = tempfile.mkstemp(prefix=".state.json.", dir=self.state_dir)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(state.to_dict(), handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def load(self) -> HubState:
        try:
            source = read_private_text(
                self.state_dir, self.path.name, label="hub deployment state"
            )
            return HubState.from_dict(json.loads(source))
        except json.JSONDecodeError as exc:
            raise ConfigError("hub state is invalid JSON") from exc

    def archive(self, state: HubState) -> Path:
        if state.status not in {"uninstalled", "rolled_back"}:
            raise ConfigError("only completed hub state can be archived")
        history = self.state_dir / "history"
        ensure_directory_durable(history)
        target = history / f"{state.install_id}-{state.status}.json"
        payload = state.to_dict()
        if target.exists():
            try:
                existing = json.loads(
                    read_private_text(history, target.name, label="hub state history")
                )
            except json.JSONDecodeError as exc:
                raise ConfigError("hub state history is invalid") from exc
            if existing != payload:
                raise ConfigError("hub state history conflicts with prior evidence")
            return target
        _write_private_json(target, payload)
        return target


def _labels(config: HubInstallConfig, install_id: str) -> dict[str, str]:
    return {
        "io.book-translator.managed-by": "btctl",
        "io.book-translator.install-id": install_id,
        "io.book-translator.role": "hub",
        "io.book-translator.topology": "hub",
        "io.book-translator.version": config.identity.version,
        "io.book-translator.revision": config.identity.sha,
    }


def render_hub_compose(
    config: HubInstallConfig, plan: HubPlan, install_id: str
) -> dict[str, object]:
    def literal(value: str) -> str:
        return value.replace("$", "$$")

    network_keys: dict[str, str] = {}
    for reader in config.runtime.readers:
        network = config.reader_networks[reader.name]
        network_keys.setdefault(network, f"reader_{reader.name}")
    networks = {
        key: {"name": network, "external": True}
        for network, key in network_keys.items()
    }
    service = {
        "image": config.image,
        "pull_policy": "never",
        "container_name": config.install_name,
        "env_file": [{
            "path": literal(str(Path(config.state_dir) / "hub.env")),
            "required": True,
            "format": "raw",
        }],
        "labels": _labels(config, install_id),
        "volumes": [
            {"type": "bind", "source": literal(config.data_dir), "target": "/app/data"}
        ],
        "tmpfs": ["/tmp:rw,noexec,nosuid,size=134217728,uid=101,gid=102,mode=700"],
        "pids_limit": 384,
        "mem_limit": "2g",
        "cpus": 2.5,
        "extra_hosts": ["host.docker.internal:host-gateway"],
        "ports": [
            {
                "target": reader.proxy_port,
                "published": reader.published_port,
                "protocol": "tcp",
            }
            for reader in config.runtime.readers
        ],
        "networks": {
            network_keys[config.reader_networks[reader.name]]: {}
            for reader in config.runtime.readers
        },
        "user": "101:102",
        "privileged": False,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "restart": "unless-stopped",
    }
    return {"name": config.install_name, "services": {"hub": service}, "networks": networks}


def _write_private_json(path: Path, payload: object) -> None:
    if path.is_symlink():
        raise InstallError("hub deployment artifact must not be a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _write_private_environment(path: Path, values: Mapping[str, str]) -> None:
    if any("\n" in key or "\n" in value for key, value in values.items()):
        raise InstallError("hub environment contains an unsupported newline")
    if path.is_symlink():
        raise InstallError("hub environment must not be a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key, value in sorted(values.items()):
                handle.write(f"{key}={value}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


@dataclass(frozen=True, slots=True)
class HubContainerSpec:
    name: str
    image: str
    env_file: Path
    labels: dict[str, str]
    primary_network: str
    data_dir: Path
    ports: tuple[tuple[int, int], ...]


@dataclass(frozen=True, slots=True)
class HubDoctorReport:
    ok: bool
    checks: list[dict[str, str]]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": self.checks}


class HubInstaller:
    """Install one owned hub while treating both stock readers as external."""

    def __init__(self, docker, *, health_timeout_seconds: int = 90):
        self.docker = docker
        self.health_timeout_seconds = health_timeout_seconds

    def _verify_readers(self, config: HubInstallConfig) -> None:
        for reader in config.runtime.readers:
            container = self.docker.inspect_container(
                config.reader_containers[reader.name]
            )
            if (
                container is None
                or container.get("State", {}).get("Status") != "running"
                or config.reader_networks[reader.name]
                not in _container_networks(container)
                or not _has_exact_reader_version(
                    container,
                    reader.version,
                    config.reader_image_ids[reader.name],
                )
            ):
                raise InstallError(
                    f"external {reader.name} reader identity does not match"
                )

    def _verify_hub(
        self,
        config: HubInstallConfig,
        install_id: str,
        *,
        require_healthy: bool,
        expected_image_id: str = "",
    ) -> dict:
        container = self.docker.inspect_container(config.install_name)
        if container is None:
            raise InstallError("hub container is missing")
        labels = container.get("Config", {}).get("Labels", {})
        if any(labels.get(key) != value for key, value in _labels(config, install_id).items()):
            raise InstallError("hub container ownership labels do not match")
        if expected_image_id and container.get("Image") != expected_image_id:
            raise InstallError("hub container image identity does not match")
        live_environment = {}
        for entry in container.get("Config", {}).get("Env", []):
            if isinstance(entry, str) and "=" in entry:
                key, value = entry.split("=", 1)
                live_environment[key] = value
        if any(
            live_environment.get(key) != value
            for key, value in config.environment.items()
        ):
            raise InstallError("hub container environment does not match")
        if require_healthy and (
            container.get("State", {}).get("Status") != "running"
            or container.get("State", {}).get("Health", {}).get("Status") != "healthy"
        ):
            raise InstallError("hub container is not healthy")
        if _container_networks(container) != set(config.reader_networks.values()):
            raise InstallError("hub reader networks do not match")
        host = container.get("HostConfig", {})
        security = host.get("SecurityOpt", [])
        if (
            container.get("Config", {}).get("User") != "101:102"
            or host.get("ReadonlyRootfs") is not True
            or host.get("Privileged") is not False
            or set(host.get("CapDrop", [])) != {"ALL"}
            or "no-new-privileges:true" not in security
            or host.get("PidsLimit") != 384
            or host.get("Memory") != 2 * 1024 * 1024 * 1024
            or host.get("NanoCpus") != 2_500_000_000
        ):
            raise InstallError("hub container sandbox does not match")
        tmpfs = host.get("Tmpfs")
        tmpfs_options: set[str] = set()
        if isinstance(tmpfs, dict) and set(tmpfs) == {"/tmp"}:
            value = tmpfs.get("/tmp")
            if isinstance(value, str):
                tmpfs_options = set(value.split(","))
        size_options = {
            item for item in tmpfs_options if item.startswith("size=")
        }
        if (
            tmpfs_options - size_options
            != {"rw", "noexec", "nosuid", "uid=101", "gid=102", "mode=700"}
            or size_options != {"size=134217728"}
        ):
            raise InstallError("hub container sandbox does not match")
        expected_bindings = {
            f"{reader.proxy_port}/tcp": str(reader.published_port)
            for reader in config.runtime.readers
        }
        bindings = host.get("PortBindings", {})
        if set(bindings) != set(expected_bindings):
            raise InstallError("hub published ports do not match")
        for key, port in expected_bindings.items():
            entries = bindings.get(key)
            if (
                not isinstance(entries, list)
                or not entries
                or any(entry.get("HostPort") != port for entry in entries)
            ):
                raise InstallError("hub published ports do not match")
        mounts = container.get("Mounts", [])
        persistent = [
            mount for mount in mounts
            if isinstance(mount, dict) and mount.get("Type") in {"bind", "volume"}
        ]
        if (
            len(persistent) != 1
            or persistent[0].get("Type") != "bind"
            or persistent[0].get("Destination") != "/app/data"
            or persistent[0].get("RW") is not True
            or Path(str(persistent[0].get("Source", ""))).resolve()
            != Path(config.data_dir).resolve()
        ):
            raise InstallError("hub data bind does not match")
        return container

    def _verify_image(self, config: HubInstallConfig) -> str:
        image = self.docker.inspect_image(config.image)
        identifier = image.get("Id") if image else None
        labels = image.get("Config", {}).get("Labels", {}) if image else {}
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", identifier)
            or labels.get("io.book-translator.version") != config.identity.version
            or labels.get("io.book-translator.revision") != config.identity.sha
            or labels.get("io.book-translator.topology") != "hub"
        ):
            raise InstallError("built hub image identity does not match")
        return identifier

    def _probe(self, config: HubInstallConfig) -> None:
        for reader in config.runtime.readers:
            self.docker.probe_http(
                config.install_name,
                f"http://127.0.0.1:{reader.proxy_port}/bt-api/ping",
            )
            self.docker.probe_http(
                config.install_name,
                f"{reader.upstream}/",
            )
            self.docker.probe_reader_auth(
                config.install_name,
                reader.name,
                reader.environment["BT_READER_AUTH_URL"],
            )
            self.docker.probe_sqlite(
                config.install_name,
                f"/app/data/{reader.name}/translations.db",
            )

    def _preflight(
        self, config: HubInstallConfig, *, allow_existing_data: bool
    ) -> HubState | None:
        self.docker.require_available()
        self._verify_readers(config)
        existing = self.docker.inspect_container(config.install_name)
        if existing is not None:
            raise InstallError("hub container name is already in use")
        store = HubStateStore(Path(config.state_dir))
        prior: HubState | None = None
        if store.path.exists():
            prior = store.load()
            if prior.status not in {"uninstalled", "rolled_back"}:
                raise InstallError("active hub deployment state already exists")
            if (
                prior.install_profile != config.install_profile
                or prior.resources.get("hub", {}).get("name") != config.install_name
                or prior.resources.get("data", {}).get("path") != config.data_dir
            ):
                raise InstallError("completed hub state belongs to different resources")
        data = Path(config.data_dir)
        if data.is_symlink() or (data.exists() and not data.is_dir()):
            raise InstallError("BT_DATA_DIR must be one real directory")
        if (
            data.exists()
            and any(data.iterdir())
            and not allow_existing_data
            and prior is None
        ):
            raise InstallError("BT_DATA_DIR must be empty for a fresh hub install")
        return prior

    def _remove_new_session_keys(
        self,
        config: HubInstallConfig,
        preexisting: Mapping[str, bool],
    ) -> None:
        """Remove only credentials created by this uncommitted attempt."""
        readers = tuple(reader.name for reader in config.runtime.readers)
        current = self.docker.inspect_hub_data_credentials(
            config.image, Path(config.data_dir), readers
        )
        created = tuple(
            reader
            for reader in readers
            if current[reader] and not preexisting.get(reader, False)
        )
        if not created:
            return
        self.docker.remove_hub_data_credentials(
            config.image, Path(config.data_dir), created
        )
        remaining = self.docker.inspect_hub_data_credentials(
            config.image, Path(config.data_dir), created
        )
        if any(remaining.values()):
            raise InstallError(
                "reader session credential cleanup did not complete"
            )

    def install(
        self,
        config: HubInstallConfig,
        plan: HubPlan,
        repository: Path,
        *,
        allow_existing_data: bool = False,
        _operation_locked: bool = False,
    ) -> HubState:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir)):
                return self.install(
                    config,
                    plan,
                    repository,
                    allow_existing_data=allow_existing_data,
                    _operation_locked=True,
                )
        attempt_store = InstallAttemptStore(
            Path(config.state_dir),
            additional_allowed_files=frozenset(
                {
                    "deployment.hub.compose.json",
                    "hub.env",
                    "topology-migration.json",
                }
            ),
        )
        retry_evidence: dict[str, object] | None = None
        if attempt_store.path.exists():
            try:
                retry_evidence = attempt_store.load()
            except ConfigError as exc:
                raise InstallError(
                    "cleaned hub retry evidence could not be reloaded"
                ) from exc
            if not _cleaned_attempt_matches(retry_evidence, plan):
                raise InstallError(
                    "hub install attempt is not exact completed cleanup evidence"
                )
        prior_state = self._preflight(
            config,
            allow_existing_data=(
                allow_existing_data or retry_evidence is not None
            ),
        )
        install_id = str(uuid.uuid4())
        state_dir = Path(config.state_dir)
        ensure_directory_durable(state_dir)
        attempt = _install_attempt_payload(plan, install_id)
        try:
            attempt_store.save(attempt)
        except ConfigError as exc:
            raise InstallError("hub install attempt could not be committed") from exc
        image_id = ""
        data_dir = Path(config.data_dir)
        started = False
        state_committed = False
        session_keys_preexisting: dict[str, bool] = {}
        compose_path = state_dir / "deployment.hub.compose.json"
        environment_path = state_dir / "hub.env"
        try:
            attempt["status"] = "starting"
            attempt_store.save(attempt)
            image_labels = {
                "io.book-translator.version": config.identity.version,
                "io.book-translator.revision": config.identity.sha,
                "io.book-translator.source": "local-checkout",
                "io.book-translator.topology": "hub",
            }
            self.docker.build_image(Path(repository), config.image, image_labels)
            image_id = self._verify_image(config)
            ensure_directory_durable(data_dir, enforce_existing_mode=False)
            reader_names = tuple(
                reader.name for reader in config.runtime.readers
            )
            self.docker.prepare_hub_data_directory(
                config.image,
                data_dir,
                reader_names,
            )
            session_keys_preexisting = (
                self.docker.inspect_hub_data_credentials(
                    config.image, data_dir, reader_names
                )
            )
            if config.install_profile == "compose-existing":
                _write_private_environment(
                    environment_path,
                    config.environment,
                )
                _write_private_json(
                    compose_path, render_hub_compose(config, plan, install_id)
                )
                self.docker.compose_validate(compose_path, config.install_name)
                started = True
                self.docker.compose_up(compose_path, config.install_name)
            else:
                _write_private_environment(environment_path, config.environment)
                first = config.runtime.readers[0]
                started = True
                self.docker.create_hub_container(
                    HubContainerSpec(
                        name=config.install_name,
                        image=config.image,
                        env_file=environment_path,
                        labels=_labels(config, install_id),
                        primary_network=config.reader_networks[first.name],
                        data_dir=data_dir,
                        ports=tuple(
                            (reader.published_port, reader.proxy_port)
                            for reader in config.runtime.readers
                        ),
                    )
                )
                for reader in config.runtime.readers[1:]:
                    network = config.reader_networks[reader.name]
                    if network != config.reader_networks[first.name]:
                        self.docker.connect_network(network, config.install_name)
                self.docker.start_container(config.install_name)
            self.docker.wait_healthy(
                [config.install_name], self.health_timeout_seconds
            )
            self._probe(config)
            container = self._verify_hub(
                config,
                install_id,
                require_healthy=True,
                expected_image_id=image_id,
            )
            resources = copy.deepcopy(plan.resources)
            resources["hub"]["id"] = container.get("Id", "")
            resources["hub"]["image_id"] = image_id
            state = replace(
                HubState.new(install_id=install_id, plan=plan),
                resources=resources,
            )
            state_store = HubStateStore(state_dir)
            if prior_state is not None:
                state_store.archive(prior_state)
            state_store.save(state)
            state_committed = True
            attempt_store.remove()
            return state
        except BaseException as exc:
            if state_committed:
                raise InstallError(
                    "hub runtime state committed but install-attempt cleanup failed; "
                    "run doctor before further lifecycle operations"
                ) from exc
            cleanup_errors: list[str] = []
            if started:
                try:
                    if config.install_profile == "compose-existing" and compose_path.exists():
                        self.docker.compose_down(compose_path, config.install_name)
                    elif self.docker.inspect_container(config.install_name) is not None:
                        self.docker.remove_container(config.install_name)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(_bounded_error("compose" if config.install_profile == "compose-existing" else "hub container", cleanup_exc))
            if started and not cleanup_errors:
                try:
                    self._remove_new_session_keys(
                        config, session_keys_preexisting
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


class HubDoctor:
    def __init__(self, docker):
        self.docker = docker

    def run(
        self,
        config: HubInstallConfig,
        plan: HubPlan,
        *,
        deep: bool = False,
    ) -> HubDoctorReport:
        checks: list[dict[str, str]] = []

        def check(name: str, operation) -> bool:
            try:
                operation()
            except Exception as exc:
                checks.append({"name": name, "status": "failed", "detail": str(exc)})
                return False
            checks.append({"name": name, "status": "ok", "detail": "verified"})
            return True

        holder: dict[str, HubState] = {}

        def state_check() -> None:
            state = HubStateStore(Path(config.state_dir)).load()
            if state.status not in _ACTIVE_STATES:
                raise InstallError("hub deployment is not active")
            if any(
                getattr(state, key) != getattr(plan, key)
                for key in (
                    "version", "revision", "image", "config_fingerprint", "install_profile"
                )
            ):
                raise InstallError("hub state does not match this plan")
            holder["state"] = state

        if not check("state", state_check):
            return HubDoctorReport(False, checks)
        installer = HubInstaller(self.docker)
        check("docker", self.docker.require_available)
        check("external-readers", lambda: installer._verify_readers(config))
        check(
            "runtime",
            lambda: installer._verify_hub(
                config,
                holder["state"].install_id,
                require_healthy=True,
                expected_image_id=str(
                    holder["state"].resources.get("hub", {}).get("image_id", "")
                ),
            ),
        )
        check("runtime-dependencies", lambda: installer._probe(config))

        def verify_artifacts() -> None:
            state_dir = Path(config.state_dir)
            environment_path = state_dir / "hub.env"
            expected_environment = (
                config.environment
            )
            expected_text = "".join(
                f"{key}={value}\n"
                for key, value in sorted(expected_environment.items())
            )
            try:
                actual_environment = read_private_text(
                    state_dir, environment_path.name,
                    label="hub environment artifact",
                )
            except ConfigError as exc:
                raise InstallError(
                    "hub environment artifact is not trustworthy"
                ) from exc
            if actual_environment != expected_text:
                raise InstallError("hub environment artifact has drifted")
            if config.install_profile == "compose-existing":
                compose_path = state_dir / "deployment.hub.compose.json"
                try:
                    actual_compose = json.loads(read_private_text(
                        state_dir, compose_path.name,
                        label="hub Compose artifact",
                    ))
                except (ConfigError, json.JSONDecodeError) as exc:
                    raise InstallError(
                        "hub Compose artifact is not trustworthy"
                    ) from exc
                if actual_compose != render_hub_compose(
                    config, plan, holder["state"].install_id
                ):
                    raise InstallError("hub Compose artifact has drifted")

        check("artifacts", verify_artifacts)

        check(
            "data-isolation",
            lambda: self.docker.verify_hub_data_directory(
                config.image,
                Path(config.data_dir),
                tuple(reader.name for reader in config.runtime.readers),
            ),
        )
        if deep and all(item["status"] == "ok" for item in checks):
            def verify_providers() -> None:
                try:
                    self.docker.probe_hub_providers(config.install_name)
                except Exception as exc:
                    raise InstallError("provider deep probe failed") from exc

            check("providers", verify_providers)
        return HubDoctorReport(all(item["status"] == "ok" for item in checks), checks)


class HubUninstaller:
    def __init__(self, docker):
        self.docker = docker

    def uninstall(
        self,
        config: HubInstallConfig,
        plan: HubPlan,
        *,
        _operation_locked: bool = False,
    ) -> HubState:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir), create=False):
                return self.uninstall(config, plan, _operation_locked=True)
        store = HubStateStore(Path(config.state_dir))
        state = store.load()
        if state.status not in _ACTIVE_STATES | {"uninstalling", "uninstalled"}:
            raise InstallError("hub deployment is not active")
        if state.config_fingerprint != plan.config_fingerprint:
            raise InstallError("hub state does not match this plan")
        attempt_store = InstallAttemptStore(
            Path(config.state_dir),
            additional_allowed_files=frozenset(
                {
                    "deployment.hub.compose.json",
                    "hub.env",
                    "topology-migration.json",
                }
            ),
        )
        committed_attempt = False
        if attempt_store.path.exists():
            try:
                attempt = attempt_store.load()
            except ConfigError as exc:
                raise InstallError(
                    "hub install attempt could not be reconciled"
                ) from exc
            committed_attempt = (
                attempt.get("status") == "starting"
                and attempt.get("install_id") == state.install_id
                and attempt.get("version") == state.version
                and attempt.get("revision") == state.revision
                and attempt.get("image") == state.image
                and attempt.get("config_fingerprint")
                == state.config_fingerprint
                and attempt.get("install_profile") == state.install_profile
                and attempt.get("resources") == plan.resources
                and attempt.get("cleanup_errors") == []
            )
            if not committed_attempt:
                raise InstallError(
                    "hub install attempt does not match committed state"
                )
        if state.status == "uninstalled":
            if committed_attempt:
                try:
                    attempt_store.remove()
                except ConfigError as exc:
                    raise InstallError(
                        "hub uninstall completed but install-attempt reconciliation failed"
                    ) from exc
            return state
        expected_image_id = str(state.resources.get("hub", {}).get("image_id", ""))
        if self.docker.inspect_container(config.install_name) is not None:
            HubInstaller(self.docker)._verify_hub(
                config,
                state.install_id,
                require_healthy=state.status in _ACTIVE_STATES,
                expected_image_id=expected_image_id,
            )
        current = state
        if state.status in _ACTIVE_STATES:
            current = replace(state, status="uninstalling")
            store.save(current)
        compose_path = Path(config.state_dir) / "deployment.hub.compose.json"
        if config.install_profile == "compose-existing" and compose_path.exists():
            self.docker.compose_down(compose_path, config.install_name)
        elif self.docker.inspect_container(config.install_name) is not None:
            self.docker.remove_container(config.install_name)
        if self.docker.inspect_container(config.install_name) is not None:
            raise InstallError("hub container removal did not complete")
        resources = copy.deepcopy(current.resources)
        resources["hub"]["removed"] = True
        readers = tuple(reader.name for reader in config.runtime.readers)
        pending_credentials = []
        for reader in readers:
            resource = resources.get(f"session_key_{reader}")
            expected = str(Path(config.data_dir) / reader / "reader_session_key")
            if (
                not isinstance(resource, dict)
                or resource.get("path") != expected
                or resource.get("ownership") != "owned-credential"
                or resource.get("retention") != "remove-on-uninstall"
            ):
                raise InstallError("hub session credential inventory does not match")
            if resource.get("removed") is not True:
                pending_credentials.append(reader)
        if pending_credentials:
            self.docker.remove_hub_data_credentials(
                config.image,
                Path(config.data_dir),
                tuple(pending_credentials),
            )
            for reader in pending_credentials:
                resources[f"session_key_{reader}"]["removed"] = True
        completed = replace(current, status="uninstalled", resources=resources)
        store.save(completed)
        if committed_attempt:
            try:
                attempt_store.remove()
            except ConfigError as exc:
                raise InstallError(
                    "hub uninstall completed but install-attempt reconciliation failed"
                ) from exc
        return completed


class _TopologyLocks:
    """Acquire every distinct lifecycle parent in deterministic path order."""

    def __init__(self, state_dirs: list[Path]):
        self.targets = sorted({path.parent.resolve() for path in state_dirs}, key=str)
        self.descriptors: list[int] = []

    def __enter__(self):
        try:
            for target in self.targets:
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
                if target.is_symlink() or not target.is_dir():
                    raise ConfigError("topology lock target is unsafe")
                descriptor = os.open(
                    target,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                )
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    os.close(descriptor)
                    raise ConfigError(
                        "another btctl lifecycle operation is already in progress"
                    ) from exc
                self.descriptors.append(descriptor)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, _type, _value, _traceback):
        for descriptor in reversed(self.descriptors):
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        self.descriptors.clear()


class HubTopologyMigration:
    """Cut over exact active split states into one reversible schema-3 hub."""

    JOURNAL_SCHEMA = 1

    def __init__(self, docker, *, health_timeout_seconds: int = 90):
        self.docker = docker
        self.health_timeout_seconds = health_timeout_seconds

    @staticmethod
    def _state_directory(path: Path) -> Path:
        candidate = Path(path)
        if candidate.name == "state.json":
            candidate = candidate.parent
        if not candidate.is_absolute() or candidate == Path("/") or ".." in candidate.parts:
            raise ConfigError("--from-state must be an absolute state directory or state.json")
        return candidate

    def _sources(
        self, config: HubInstallConfig, source_paths: list[Path]
    ) -> dict[str, dict[str, object]]:
        expected = {reader.name for reader in config.runtime.readers}
        if len(source_paths) != len(expected):
            raise InstallError("migration requires one --from-state per enabled reader")
        sources: dict[str, dict[str, object]] = {}
        for raw in source_paths:
            state_dir = self._state_directory(raw)
            state = StateStore(state_dir).load()
            if state.status not in _ACTIVE_STATES:
                raise InstallError("migration source state is not active")
            reader = state.reader_type
            if reader not in expected or reader in sources:
                raise InstallError("migration source readers do not match the hub")
            resources = state.resources
            api = resources.get("api", {})
            proxy = resources.get("proxy", {})
            data = resources.get("data", {})
            if not all(
                isinstance(item, dict) for item in (api, proxy, data)
            ):
                raise InstallError("migration source inventory is incomplete")
            source = {
                "reader": reader,
                "state_dir": str(state_dir),
                "install_id": state.install_id,
                "api_name": api.get("name"),
                "api_id": api.get("id"),
                "proxy_name": proxy.get("name"),
                "proxy_id": proxy.get("id"),
                "data_dir": data.get("path"),
            }
            if any(not isinstance(source[name], str) or not source[name] for name in (
                "api_name", "api_id", "proxy_name", "proxy_id", "data_dir"
            )):
                raise InstallError("migration source identity is incomplete")
            sources[reader] = source
        if set(sources) != expected:
            raise InstallError("migration source readers do not match the hub")
        return sources

    def _verify_source_containers(
        self, sources: Mapping[str, dict[str, object]]
    ) -> dict[str, dict[str, str]]:
        statuses: dict[str, dict[str, str]] = {}
        for reader, source in sources.items():
            statuses[reader] = {}
            for role in ("api", "proxy"):
                name = str(source[f"{role}_name"])
                container = self.docker.inspect_container(name)
                if container is None or container.get("Id") != source[f"{role}_id"]:
                    raise InstallError("migration source container identity drifted")
                status = container.get("State", {}).get("Status")
                if status not in {"running", "exited", "created"}:
                    raise InstallError("migration source container is in an unsafe state")
                statuses[reader][role] = status
        return statuses

    @staticmethod
    def _validate_source_paths(
        config: HubInstallConfig, sources: Mapping[str, dict[str, object]]
    ) -> None:
        hub_paths = {
            "hub state": Path(config.state_dir),
            "hub data": Path(config.data_dir),
            "hub backup": Path(config.backup_dir),
        }
        source_paths: dict[str, Path] = {}
        for reader, source in sources.items():
            data_dir = Path(str(source.get("data_dir", "")))
            state_dir = Path(str(source.get("state_dir", "")))
            if (
                not data_dir.is_absolute()
                or data_dir == Path("/")
                or ".." in data_dir.parts
                or data_dir.is_symlink()
                or not data_dir.is_dir()
            ):
                raise InstallError("migration source data path is unsafe")
            database = data_dir / "translations.db"
            if database.is_symlink() or not database.is_file():
                raise InstallError("migration source translations.db is missing")
            if _paths_overlap(data_dir, state_dir):
                raise InstallError("migration source state and data paths overlap")
            for label, hub_path in hub_paths.items():
                if _paths_overlap(data_dir, hub_path) or _paths_overlap(
                    state_dir, hub_path
                ):
                    raise InstallError(f"migration source paths overlap {label}")
            source_paths[reader] = data_dir
        ordered = sorted(source_paths.items())
        for index, (reader, path) in enumerate(ordered):
            for other_reader, other_path in ordered[index + 1:]:
                if _paths_overlap(path, other_path):
                    raise InstallError(
                        f"migration source data paths overlap for {reader} and {other_reader}"
                    )

    @staticmethod
    def _validate_target_root(
        config: HubInstallConfig,
        journal: Mapping[str, object] | None,
    ) -> None:
        root = Path(config.data_dir)
        if root.is_symlink() or not root.is_dir():
            raise InstallError("hub migration data root is unsafe")
        entries = list(root.iterdir())
        if journal is None:
            if entries:
                raise InstallError("hub migration data root must be empty")
            return
        copied = journal.get("copied", {})
        if not isinstance(copied, dict):
            raise InstallError("topology migration copy evidence is invalid")
        readers = {reader.name for reader in config.runtime.readers}
        allowed = readers | {f".{reader}.migration.partial" for reader in readers}
        for entry in entries:
            if (
                entry.name not in allowed
                or entry.is_symlink()
                or not entry.is_dir()
            ):
                raise InstallError("hub migration data root has an unexpected entry")

    @staticmethod
    def _sync_directory(path: Path) -> None:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _copy_reader_data(
        self,
        *,
        reader: str,
        source_data: Path,
        data_root: Path,
        evidence: object,
    ) -> dict[str, object]:
        target = data_root / reader
        work = data_root / f".{reader}.migration.partial"
        _sqlite_integrity(source_data, checkpoint=True)
        source_manifest, source_files = _tree_manifest(source_data)
        expected = {"manifest": source_manifest, "files": source_files}
        if evidence:
            if evidence != expected or work.exists() or work.is_symlink():
                raise InstallError("copied migration data has drifted")
            manifest, files = _tree_manifest(target)
            if {"manifest": manifest, "files": files} != expected:
                raise InstallError("copied migration data has drifted")
            return expected
        if target.exists() or target.is_symlink():
            if work.exists() or work.is_symlink():
                raise InstallError("migration copy has conflicting recovery trees")
            manifest, files = _tree_manifest(target)
            if {"manifest": manifest, "files": files} != expected:
                raise InstallError("unjournaled migration copy does not match its source")
            return expected
        if work.exists() or work.is_symlink():
            if work.is_symlink() or not work.is_dir():
                raise InstallError("migration partial copy is unsafe")
            manifest, files = _tree_manifest(work)
            if {"manifest": manifest, "files": files} == expected:
                _make_tree_durable(work)
                os.replace(work, target)
                self._sync_directory(data_root)
                return expected
            shutil.rmtree(work)
            self._sync_directory(data_root)
        _secure_copy_atomic(source_data, target, work)
        manifest, files = _tree_manifest(target)
        if {"manifest": manifest, "files": files} != expected:
            raise InstallError("published migration copy does not match its source")
        return expected

    def _restart_sources(self, journal: Mapping[str, object]) -> None:
        sources = journal.get("sources", {})
        initial = journal.get("initial_statuses", {})
        if not isinstance(sources, dict) or not isinstance(initial, dict):
            raise InstallError("topology migration journal is incomplete")
        names: list[str] = []
        for reader, source in sources.items():
            statuses = initial.get(reader, {})
            if not isinstance(source, dict) or not isinstance(statuses, dict):
                raise InstallError("topology migration journal source is invalid")
            for role in ("api", "proxy"):
                if statuses.get(role) == "running":
                    name = str(source[f"{role}_name"])
                    current = self.docker.inspect_container(name)
                    if current is None or current.get("Id") != source[f"{role}_id"]:
                        raise InstallError("cannot restore a drifted migration source")
                    if current.get("State", {}).get("Status") != "running":
                        self.docker.start_container(name)
                    names.append(name)
        if names:
            self.docker.wait_healthy(names, self.health_timeout_seconds)

    @staticmethod
    def _journal_path(config: HubInstallConfig) -> Path:
        return Path(config.state_dir) / "topology-migration.json"

    def _load_journal(self, config: HubInstallConfig) -> dict[str, object]:
        path = self._journal_path(config)
        try:
            payload = json.loads(
                read_private_text(
                    path.parent, path.name, label="topology migration journal"
                )
            )
        except (ConfigError, json.JSONDecodeError) as exc:
            raise InstallError("topology migration journal is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != self.JOURNAL_SCHEMA:
            raise InstallError("topology migration journal schema is unsupported")
        return payload

    def _save_journal(
        self, config: HubInstallConfig, journal: Mapping[str, object]
    ) -> None:
        payload = dict(journal)
        payload["schema_version"] = self.JOURNAL_SCHEMA
        _write_private_json(self._journal_path(config), payload)

    def migrate(
        self,
        config: HubInstallConfig,
        plan: HubPlan,
        repository: Path,
        source_paths: list[Path],
    ) -> HubState:
        if _effective_uid() != 0:
            raise InstallError(
                "topology migration must run as root to preserve private source credentials"
            )
        normalized = [self._state_directory(path) for path in source_paths]
        lock_paths = [Path(config.state_dir), *normalized]
        with _TopologyLocks(lock_paths):
            ensure_directory_durable(Path(config.state_dir))
            data_root = Path(config.data_dir)
            ensure_directory_durable(data_root, enforce_existing_mode=False)
            journal_path = self._journal_path(config)
            if journal_path.exists():
                journal = self._load_journal(config)
                if journal.get("status") not in {"prepared", "copying", "failed"}:
                    raise InstallError("topology migration is already finalized")
                sources = journal.get("sources")
                if not isinstance(sources, dict):
                    raise InstallError("topology migration journal is incomplete")
                current_sources = self._sources(config, normalized)
                if sources != current_sources:
                    raise InstallError("migration sources do not match the journal")
                self._validate_source_paths(config, sources)
                self._validate_target_root(config, journal)
            else:
                sources = self._sources(config, normalized)
                self._validate_source_paths(config, sources)
                self._validate_target_root(config, None)
                statuses = self._verify_source_containers(sources)
                journal = {
                    "status": "prepared",
                    "migration_id": str(uuid.uuid4()),
                    "hub_fingerprint": plan.config_fingerprint,
                    "sources": sources,
                    "initial_statuses": statuses,
                    "copied": {},
                }
                self._save_journal(config, journal)
            if journal.get("hub_fingerprint") != plan.config_fingerprint:
                raise InstallError("hub plan does not match the migration journal")
            hub_store = HubStateStore(Path(config.state_dir))
            if hub_store.path.exists():
                recovered = hub_store.load()
                if (
                    recovered.status in _ACTIVE_STATES
                    and recovered.config_fingerprint == plan.config_fingerprint
                ):
                    HubInstaller(self.docker)._verify_hub(
                        config,
                        recovered.install_id,
                        require_healthy=True,
                        expected_image_id=str(
                            recovered.resources.get("hub", {}).get("image_id", "")
                        ),
                    )
                    journal["status"] = "committed"
                    journal["hub_install_id"] = recovered.install_id
                    self._save_journal(config, journal)
                    return recovered
            copied = journal.get("copied", {})
            if not isinstance(copied, dict):
                raise InstallError("topology migration copy evidence is invalid")
            statuses = self._verify_source_containers(sources)
            journal["status"] = "copying"
            self._save_journal(config, journal)
            installed_state: HubState | None = None
            try:
                for reader, source in sources.items():
                    for role in ("proxy", "api"):
                        if statuses[reader][role] == "running":
                            self.docker.stop_container(str(source[f"{role}_name"]))
                for reader, source in sources.items():
                    source_data = Path(str(source["data_dir"]))
                    evidence = copied.get(reader)
                    copied[reader] = self._copy_reader_data(
                        reader=reader,
                        source_data=source_data,
                        data_root=data_root,
                        evidence=evidence,
                    )
                    if not evidence:
                        journal["copied"] = copied
                        self._save_journal(config, journal)
                installed_state = HubInstaller(
                    self.docker,
                    health_timeout_seconds=self.health_timeout_seconds,
                ).install(
                    config,
                    plan,
                    repository,
                    allow_existing_data=True,
                    _operation_locked=True,
                )
            except BaseException as exc:
                cleanup_failed = False
                if installed_state is not None:
                    try:
                        HubUninstaller(self.docker).uninstall(
                            config, plan, _operation_locked=True
                        )
                    except BaseException:
                        journal["hub_cleanup"] = "failed"
                        cleanup_failed = True
                else:
                    try:
                        cleanup_failed = (
                            self.docker.inspect_container(config.install_name)
                            is not None
                        )
                    except BaseException:
                        cleanup_failed = True
                    if cleanup_failed:
                        journal["hub_cleanup"] = "failed"
                journal["status"] = "failed"
                journal["error"] = exc.__class__.__name__
                journal_write_failed = False
                try:
                    self._save_journal(config, journal)
                except BaseException:
                    journal_write_failed = True
                if cleanup_failed:
                    raise InstallError(
                        "hub commit failed and cleanup is incomplete; source runtimes remain stopped"
                    ) from exc
                self._restart_sources(journal)
                if journal_write_failed:
                    raise InstallError(
                        "hub migration failed and its recovery journal could not be updated"
                    ) from exc
                raise
            journal["status"] = "committed"
            journal["hub_install_id"] = installed_state.install_id
            try:
                self._save_journal(config, journal)
            except BaseException as exc:
                raise InstallError(
                    "hub is active but its migration journal commit is pending; rerun migrate-topology"
                ) from exc
            return installed_state

    def rollback(
        self, config: HubInstallConfig, plan: HubPlan
    ) -> HubState:
        preview = self._load_journal(config)
        preview_sources = preview.get("sources", {})
        if not isinstance(preview_sources, dict):
            raise InstallError("topology migration journal is incomplete")
        source_state_dirs = [
            Path(str(source.get("state_dir", "")))
            for source in preview_sources.values()
            if isinstance(source, dict)
        ]
        with _TopologyLocks([Path(config.state_dir), *source_state_dirs]):
            journal = self._load_journal(config)
            if journal.get("migration_id") != preview.get("migration_id"):
                raise InstallError("topology migration journal changed during rollback")
            if journal.get("sources") != preview_sources:
                raise InstallError("topology migration sources changed during rollback")
            if journal.get("status") == "rolled_back":
                self._restart_sources(journal)
                return HubStateStore(Path(config.state_dir)).load()
            if journal.get("status") != "committed":
                raise InstallError("only a committed topology migration can roll back")
            state = HubUninstaller(self.docker).uninstall(
                config, plan, _operation_locked=True
            )
            self._restart_sources(journal)
            rolled_back = replace(state, status="rolled_back")
            HubStateStore(Path(config.state_dir)).save(rolled_back)
            journal["status"] = "rolled_back"
            self._save_journal(config, journal)
            return rolled_back
