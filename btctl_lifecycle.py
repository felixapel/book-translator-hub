"""Fail-closed diagnosis, removal, and v2.1.4 migration orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from btctl_auth import render_authentik_edge
from btctl_compose import (
    ComposeAdopter,
    ComposeInstaller,
    InstallError,
    _completed_uninstall_for_reinstall,
    _container_networks,
    _has_exact_reader_version,
    _labels,
    _probe_runtime_dependencies,
    _verify_identity_edge_artifact,
    _verify_private_network,
    render_compose,
    render_schema1_compose,
    render_schema2_compose,
    _compose_api_environment,
    _compose_environment_text,
    _compose_proxy_environment,
)
from btctl_core import (
    ConfigError,
    DeploymentPlan,
    DeploymentState,
    DockerProbeBase,
    InstallConfig,
    OperationLock,
    StateStore,
    ensure_directory_durable,
    paths_overlap,
    read_private_text,
)
from btctl_unraid import (
    UnraidAdopter,
    UnraidInstaller,
    _environment_text,
    render_templates,
)


_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_JOURNAL_SCHEMA = 1
_HISTORICAL_CA_LATEST_IMAGE = (
    "ghcr.io/felixapel/cwa-ebook-translate-plugin:latest"
)


class LifecycleDocker(DockerProbeBase, Protocol):
    def remove_container(self, name: str) -> None: ...
    def remove_network(self, name: str) -> None: ...
    def stop_container(self, name: str) -> None: ...
    def start_container(self, name: str) -> None: ...
    def probe_providers(self, container: str) -> None: ...
    def probe_image_version(self, image_id: str, expected_version: str) -> None: ...
    def prepare_migration_source(self, image_id: str, path: Path) -> None: ...
    def remove_data_credential(self, image: str, path: Path, filename: str) -> None: ...


@dataclass(frozen=True, slots=True)
class DoctorReport:
    ok: bool
    checks: list[dict[str, str]]

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": self.checks}


def _state_matches_plan(
    state: DeploymentState, config: InstallConfig, plan: DeploymentPlan
) -> None:
    expected = {
        "version": plan.version,
        "revision": plan.revision,
        "image": plan.image,
        "config_fingerprint": plan.config_fingerprint,
        "install_profile": plan.install_profile,
        "auth_profile": plan.auth_profile,
        "reader_type": plan.reader_type,
        "reader_contract_version": plan.reader_contract_version,
    }
    if any(getattr(state, name) != value for name, value in expected.items()):
        raise InstallError("deployment state does not match this checkout and configuration")
    legacy_resource_names = {"reader": "cwa", "reader_network": "cwa_network"}
    for name, resource in plan.resources.items():
        if state.schema_version == 1 and name == "session_key":
            continue
        state_name = (
            legacy_resource_names.get(name, name)
            if state.schema_version == 1
            else name
        )
        state_resource = state.resources.get(state_name)
        if not isinstance(state_resource, dict):
            raise InstallError("deployment state resource inventory is incomplete")
        for identity_key in ("name", "path", "role"):
            if identity_key in resource and state_resource.get(identity_key) != resource[identity_key]:
                raise InstallError("deployment state resource identity has drifted")


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _verify_session_key(state: DeploymentState, config: InstallConfig) -> None:
    if state.schema_version == 1 or not config.uses_reader_session:
        return
    resource = state.resources.get("session_key")
    expected_path = Path(config.data_dir) / "reader_session_key"
    if (
        not isinstance(resource, dict)
        or resource.get("path") != str(expected_path)
        or resource.get("ownership") != "owned-credential"
    ):
        raise InstallError("reader session credential inventory does not match")
    if resource.get("removed") is True and not expected_path.exists():
        return
    try:
        metadata = expected_path.lstat()
        data_metadata = Path(config.data_dir).lstat()
    except OSError as exc:
        raise InstallError("reader session credential is missing") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != data_metadata.st_uid
        or metadata.st_uid not in {101, os.geteuid()}
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size != 32
    ):
        raise InstallError("reader session credential ownership has drifted")


class DeploymentDoctor:
    """Collect structural checks without changing Docker or local state."""

    def __init__(self, docker: LifecycleDocker):
        self.docker = docker

    def run(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        *,
        deep: bool = False,
        _operation_locked: bool = False,
    ) -> DoctorReport:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir), create=False):
                return self.run(
                    config,
                    plan,
                    deep=deep,
                    _operation_locked=True,
                )
        checks: list[dict[str, str]] = []

        def check(name: str, operation) -> bool:
            try:
                operation()
            except Exception as exc:
                checks.append({"name": name, "status": "failed", "detail": str(exc)})
                return False
            checks.append({"name": name, "status": "ok", "detail": "verified"})
            return True

        store = StateStore(Path(config.state_dir))
        holder: dict[str, object] = {}

        def load_state() -> None:
            state = store.load()
            if state.status not in {"installed", "adopted"}:
                raise InstallError("deployment is not in an active installed state")
            holder["state"] = state

        if not check("state", load_state):
            return DoctorReport(False, checks)
        state = holder["state"]
        assert isinstance(state, DeploymentState)
        check("configuration", lambda: _state_matches_plan(state, config, plan))
        check("docker", self.docker.require_available)

        def verify_reader() -> None:
            reader = self.docker.inspect_container(config.reader_container)
            if (
                reader is None
                or reader.get("State", {}).get("Status") != "running"
                or not _has_exact_reader_version(
                    reader, config.reader_version, config.reader_image_id
                )
                or config.reader_network not in _container_networks(reader)
            ):
                raise InstallError("external reader runtime evidence does not match")

        check("external-reader", verify_reader)
        verifier = ComposeInstaller(self.docker)
        image_holder: dict[str, str] = {}

        def verify_image() -> None:
            image_holder["id"] = verifier._verify_image(
                config,
                self.docker.inspect_image(config.image),
                allow_legacy_labels=state.schema_version == 1,
            )

        image_ok = check("image", verify_image)
        if image_ok:
            for role in ("api", "proxy"):
                def verify_role(role=role) -> None:
                    container_id, container = verifier._verify_container(
                        config,
                        plan,
                        state.install_id,
                        role,
                        image_holder["id"],
                        allow_legacy_labels=state.schema_version == 1,
                    )
                    if state.resources[role].get("id") != container_id:
                        raise InstallError(f"{role} runtime ID does not match state")
                    if container.get("State", {}).get("Health", {}).get("Status") != "healthy":
                        raise InstallError(f"{role} runtime is not healthy")

                check(f"runtime-{role}", verify_role)

            check(
                "runtime-dependencies",
                lambda: _probe_runtime_dependencies(self.docker, config, plan),
            )

        def verify_private_network() -> None:
            name = str(plan.resources["private_network"]["name"])
            network = self.docker.inspect_network(name)
            _verify_private_network(
                config,
                state.install_id,
                network,
                expected_id=str(state.resources["private_network"].get("id", "")),
                allow_legacy_labels=state.schema_version == 1,
            )

        check("private-network", verify_private_network)

        def verify_data() -> None:
            path = Path(config.data_dir)
            if path.is_symlink() or not path.is_dir():
                raise InstallError("translation data directory is missing or not private")
            metadata = path.lstat()
            expected_mode = (
                0o2750 if config.install_profile == "compose-existing" else 0o700
            )
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise InstallError(
                    "translation data directory mode does not match the install profile"
                )
            if (
                config.install_profile == "compose-existing"
                and metadata.st_gid != os.getgid()
            ):
                raise InstallError(
                    "translation data directory operator group does not match"
                )
            if (
                config.install_profile == "unraid"
                and os.geteuid() == 0
                and (metadata.st_uid, metadata.st_gid) != (101, 102)
            ):
                raise InstallError(
                    "translation data directory runtime ownership does not match"
                )

        check("data-directory", verify_data)
        check("session-key", lambda: _verify_session_key(state, config))

        def verify_artifacts() -> None:
            if config.install_profile == "compose-existing":
                path = Path(config.state_dir) / "deployment.compose.json"
                expected_compose = {
                    1: render_schema1_compose,
                    2: render_schema2_compose,
                }.get(state.schema_version, render_compose)(
                    config, plan, state.install_id
                )
                try:
                    actual_compose = json.loads(
                        read_private_text(
                            Path(config.state_dir), path.name,
                            label="Compose artifact",
                        )
                    )
                except (ConfigError, json.JSONDecodeError) as exc:
                    raise InstallError("Compose artifact is not trustworthy") from exc
                if actual_compose != expected_compose:
                    raise InstallError("Compose artifact does not match the plan")
                if state.schema_version == 3:
                    expected_env = {
                        "api": _compose_environment_text(
                            _compose_api_environment(config, state.install_id)
                        ),
                        "proxy": _compose_environment_text(
                            _compose_proxy_environment(config)
                        ),
                    }
                    for role, expected in expected_env.items():
                        env_path = Path(config.state_dir) / f"{role}.env"
                        try:
                            actual = read_private_text(
                                Path(config.state_dir), env_path.name,
                                label=f"{role} Compose environment",
                            )
                        except ConfigError as exc:
                            raise InstallError(
                                f"{role} Compose environment artifact is not trustworthy"
                            ) from exc
                        if actual != expected:
                            raise InstallError(
                                f"{role} Compose environment artifact has drifted"
                            )
            else:
                expected_templates = render_templates(config, plan)
                state_dir = Path(config.state_dir)
                expected_env = {
                    "api": _environment_text(
                        {
                            **config.api_environment(),
                            "BT_ROLE": "api",
                            **(
                                {"BT_READER_CONNECTOR_ID": state.install_id}
                                if config.uses_reader_session
                                else {}
                            ),
                        }
                    ),
                    "proxy": _environment_text({
                        **config.proxy_environment(),
                        "BT_ROLE": "proxy",
                        "BT_API_UPSTREAM": "http://translator-api:8390",
                    }),
                }
                if state.schema_version == 1:
                    legacy_api = {
                        **config.api_environment(),
                        "BT_ROLE": "api",
                    }
                    for name in (
                        "BT_READER_TYPE",
                        "BT_READER_AUTH_URL",
                        "BT_READER_VERSION",
                        "BT_READER_CONTRACT_VERSION",
                        "BT_PUBLIC_ORIGIN",
                        "BT_SESSION_KEY_PATH",
                    ):
                        legacy_api.pop(name, None)
                    legacy_api["BT_AUTH_MODE"] = "cwa_session"
                    legacy_api["BT_CWA_AUTH_URL"] = (
                        f"{config.reader_upstream}/ajax/emailstat"
                    )
                    expected_env["api"] = _environment_text(legacy_api)
                    legacy_proxy = config.proxy_environment()
                    for name in (
                        "BT_READER_TYPE",
                        "BT_READER_UPSTREAM",
                        "BT_READER_VERSION",
                        "BT_READER_CONTRACT_VERSION",
                    ):
                        legacy_proxy.pop(name, None)
                    legacy_proxy["BT_BROWSER_AUTH_MODE"] = "cwa_session"
                    expected_env["proxy"] = _environment_text({
                        **legacy_proxy,
                        "BT_ROLE": "proxy",
                        "BT_API_UPSTREAM": (
                            f"http://{plan.resources['api']['name']}:8390"
                        ),
                    })
                for role in ("api", "proxy"):
                    env_path = state_dir / f"{role}.env"
                    if (
                        env_path.is_symlink()
                        or _mode(env_path) != 0o600
                        or env_path.read_text(encoding="utf-8") != expected_env[role]
                    ):
                        raise InstallError(f"{role} environment artifact has drifted")
                    template_path = Path(plan.resources[f"{role}_template"]["path"])
                    if (
                        template_path.is_symlink()
                        or template_path.read_text(encoding="utf-8")
                        != expected_templates[role]
                    ):
                        raise InstallError(f"{role} Unraid template has drifted")
            resources = copy.deepcopy(state.resources)
            _verify_identity_edge_artifact(config, plan, resources)
            if config.auth_profile == "authentik-forwarded" and (
                resources["identity_edge_config"].get("sha256")
                != state.resources["identity_edge_config"].get("sha256")
            ):
                raise InstallError("identity-edge artifact digest does not match state")

        check("artifacts", verify_artifacts)
        if deep and all(item["status"] == "ok" for item in checks):
            def verify_providers() -> None:
                try:
                    self.docker.probe_providers(
                        str(plan.resources["api"]["name"])
                    )
                except Exception as exc:
                    raise InstallError("provider deep probe failed") from exc

            check("providers", verify_providers)
        return DoctorReport(
            all(item["status"] == "ok" for item in checks), checks
        )


class RuntimeUninstaller:
    """Remove only state-owned runtime objects while retaining data and evidence."""

    def __init__(self, docker: LifecycleDocker):
        self.docker = docker

    def _verify_owned_runtime(
        self,
        state: DeploymentState,
        config: InstallConfig,
        plan: DeploymentPlan,
        *,
        tolerate_missing: bool,
    ) -> None:
        for role in ("proxy", "api"):
            resource = state.resources[role]
            container = self.docker.inspect_container(str(resource["name"]))
            if container is None and (resource.get("removed") or tolerate_missing):
                continue
            labels = container.get("Config", {}).get("Labels", {}) if container else {}
            prefix = (
                "io.cwa-translate."
                if state.schema_version == 1
                else "io.book-translator."
            )
            if (
                container is None
                or container.get("Id") != resource.get("id")
                or labels.get(prefix + "managed-by") != "btctl"
                or labels.get(prefix + "install-id") != state.install_id
                or labels.get(prefix + "role") != role
                or labels.get(prefix + "revision") != config.identity.sha
            ):
                raise InstallError(f"{role} ownership evidence has drifted")
        resource = state.resources["private_network"]
        network = self.docker.inspect_network(str(resource["name"]))
        if network is None and (resource.get("removed") or tolerate_missing):
            return
        _verify_private_network(
            config,
            state.install_id,
            network,
            expected_id=str(resource.get("id", "")),
            allow_legacy_labels=state.schema_version == 1,
        )

    def uninstall(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        *,
        _operation_locked: bool = False,
        _allow_missing_target_data: bool = False,
    ) -> DeploymentState:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir)):
                return self.uninstall(
                    config,
                    plan,
                    _operation_locked=True,
                    _allow_missing_target_data=_allow_missing_target_data,
                )
        store = StateStore(Path(config.state_dir))
        state = store.load()
        if state.status in {"uninstalled", "rolled_back"}:
            return state
        if state.status not in {"installed", "adopted", "uninstalling"}:
            raise InstallError("deployment state cannot be uninstalled")
        _state_matches_plan(state, config, plan)
        mutable_resources = {
            "proxy": "owned",
            "api": "owned",
            "private_network": "owned",
        }
        if state.schema_version in {2, 3} and config.uses_reader_session:
            mutable_resources["session_key"] = "owned-credential"
        if config.install_profile == "unraid":
            mutable_resources.update(
                {"proxy_template": "owned", "api_template": "owned"}
            )
        if any(
            state.resources.get(name, {}).get("ownership") != ownership
            for name, ownership in mutable_resources.items()
        ):
            raise InstallError(
                "lifecycle resource is not classified as owned; refusing removal"
            )
        self.docker.require_available()
        retry = state.status == "uninstalling"
        self._verify_owned_runtime(
            state, config, plan, tolerate_missing=retry
        )

        if config.install_profile == "unraid":
            expected = render_templates(config, plan)
            for role in ("api", "proxy"):
                resource = state.resources[f"{role}_template"]
                path = Path(str(resource["path"]))
                if (resource.get("removed") or retry) and not path.exists():
                    continue
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or path.read_text(encoding="utf-8") != expected[role]
                ):
                    raise InstallError(f"{role} template ownership evidence has drifted")

        resources = copy.deepcopy(state.resources)
        current = replace(state, status="uninstalling", resources=resources)
        store.save(current)
        for role in ("proxy", "api"):
            resource = resources[role]
            name = str(resource["name"])
            container = self.docker.inspect_container(name)
            if container is not None:
                if container.get("State", {}).get("Status") == "running":
                    self.docker.stop_container(name)
                resource["stopped"] = True
                store.save(current)
                self.docker.remove_container(name)
                if self.docker.inspect_container(name) is not None:
                    raise InstallError(
                        f"{role} container removal did not complete"
                    )
            resource["removed"] = True
            store.save(current)
        if state.schema_version in {2, 3} and config.uses_reader_session:
            credential = resources["session_key"]
            if not credential.get("removed"):
                data_dir = Path(config.data_dir)
                if _allow_missing_target_data and not data_dir.exists():
                    pass
                else:
                    _verify_session_key(current, config)
                    self.docker.remove_data_credential(
                        config.image,
                        data_dir,
                        "reader_session_key",
                    )
                credential["removed"] = True
                store.save(current)
        private = resources["private_network"]
        private_name = str(private["name"])
        if self.docker.inspect_network(private_name) is not None:
            self.docker.remove_network(private_name)
            if self.docker.inspect_network(private_name) is not None:
                raise InstallError("private network removal did not complete")
        private["removed"] = True
        store.save(current)

        if config.install_profile == "unraid":
            for role in ("api", "proxy"):
                resource = resources[f"{role}_template"]
                path = Path(str(resource["path"]))
                if path.exists():
                    path.unlink()
                resource["removed"] = True
                store.save(current)

        completed = replace(current, status="uninstalled")
        store.save(completed)
        return completed


def _fsync_path(path: Path, *, directory: bool) -> None:
    """Flush one already-validated file or directory without following links."""
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(
            metadata.st_mode
        )
        if not expected:
            raise InstallError("migration durability target changed type")
        os.fsync(descriptor)
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError("migration data could not be made durable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


class MigrationJournalStore:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "migration-v214.json"

    def save(self, payload: dict[str, object]) -> None:
        if self.state_dir.is_symlink() or self.path.is_symlink():
            raise InstallError("migration journal destination must not be a symbolic link")
        document = dict(payload)
        document["schema_version"] = _JOURNAL_SCHEMA
        try:
            ensure_directory_durable(self.state_dir)
        except ConfigError as exc:
            raise InstallError("migration journal directory is unsafe") from exc
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".migration-v214.json.", dir=self.state_dir
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, self.path)
            _fsync_path(self.state_dir, directory=True)
        except BaseException:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def load(self) -> dict[str, object]:
        try:
            payload = json.loads(
                read_private_text(
                    self.state_dir,
                    self.path.name,
                    label="migration journal",
                )
            )
        except (ConfigError, json.JSONDecodeError) as exc:
            raise InstallError("migration journal could not be read") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _JOURNAL_SCHEMA:
            raise InstallError("migration journal schema is unsupported")
        return payload


def _tree_manifest(root: Path) -> tuple[str, int]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise InstallError("migration data must not contain symbolic links")
        if path.is_dir():
            continue
        if not path.is_file():
            raise InstallError("migration data must contain only directories and files")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        entries.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": digest.hexdigest(),
        })
    canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest(), len(entries)


def _sqlite_integrity(
    data_dir: Path,
    *,
    checkpoint: bool,
    closed_readonly: bool = False,
) -> None:
    database_path = data_dir / "translations.db"
    if database_path.is_symlink() or not database_path.is_file():
        raise InstallError("legacy translations.db is missing")
    if checkpoint and closed_readonly:
        raise InstallError("a read-only SQLite database cannot be checkpointed")
    if closed_readonly:
        # SQLite's ordinary read-only mode can still need to create WAL shared
        # memory. A stopped managed target is safe to inspect immutably only
        # when no recovery sidecar exists; otherwise ignoring the WAL could
        # silently validate stale base pages.
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{database_path}{suffix}")
            if sidecar.exists() or sidecar.is_symlink():
                raise InstallError(
                    "closed SQLite database has an unresolved recovery sidecar"
                )
    try:
        if closed_readonly:
            database_uri = f"{database_path.absolute().as_uri()}?mode=ro&immutable=1"
            connection = sqlite3.connect(database_uri, uri=True, timeout=10)
        else:
            connection = sqlite3.connect(database_path, timeout=10)
        try:
            if checkpoint:
                checkpoint_result = connection.execute(
                    "PRAGMA wal_checkpoint(TRUNCATE)"
                ).fetchone()
                if (
                    not checkpoint_result
                    or len(checkpoint_result) != 3
                    or any(
                        not isinstance(value, int) or value < -1
                        for value in checkpoint_result
                    )
                    or checkpoint_result[0] != 0
                    or checkpoint_result[1] != checkpoint_result[2]
                ):
                    raise InstallError("SQLite WAL checkpoint did not complete")
            result = connection.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise InstallError("SQLite integrity check failed")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise InstallError("SQLite integrity check failed") from exc


def _secure_copy(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise InstallError("migration target must not already exist")
    _tree_manifest(source)
    shutil.copytree(source, target, symlinks=True)
    for path in target.rglob("*"):
        if path.is_symlink():
            raise InstallError("migration copy unexpectedly contains a symbolic link")
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
    os.chmod(target, 0o700)


def _make_tree_durable(root: Path) -> None:
    """Flush copied bytes and directory entries before publishing the tree."""
    files: list[Path] = []
    directories: list[Path] = []
    try:
        for path in root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode):
                files.append(path)
            elif stat.S_ISDIR(metadata.st_mode):
                directories.append(path)
            else:
                raise InstallError(
                    "migration data must contain only directories and files"
                )
    except InstallError:
        raise
    except OSError as exc:
        raise InstallError("migration copy could not be inspected for durability") from exc
    for path in sorted(files):
        _fsync_path(path, directory=False)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _fsync_path(path, directory=True)
    _fsync_path(root, directory=True)


def _preserve_incomplete(source: Path, destination: Path) -> None:
    """Atomically retain an interrupted local copy without trusting its data."""
    if not source.exists() and not source.is_symlink():
        return
    if source.is_symlink() or not source.is_dir():
        raise InstallError("interrupted migration copy is not a safe directory")
    if destination.exists() or destination.is_symlink():
        raise InstallError("interrupted migration preservation path already exists")
    if source.parent.resolve() != destination.parent.resolve():
        raise InstallError("interrupted migration copy cannot cross filesystems")
    # Reject links, devices, sockets, and FIFOs, but preserve every regular
    # byte. The copy is evidence, not an input to the next migration attempt.
    _tree_manifest(source)
    _make_tree_durable(source)
    os.replace(source, destination)
    _fsync_path(destination.parent, directory=True)


def _secure_copy_atomic(source: Path, target: Path, work: Path) -> None:
    """Copy through a same-filesystem work tree and publish by atomic rename."""
    if target.exists() or target.is_symlink() or work.exists() or work.is_symlink():
        raise InstallError("migration copy destination must not already exist")
    if target.parent.resolve() != work.parent.resolve():
        raise InstallError("migration work tree must share the destination filesystem")
    source_manifest = _tree_manifest(source)
    _secure_copy(source, work)
    work_manifest = _tree_manifest(work)
    if source_manifest != work_manifest:
        raise InstallError("migration work copy does not match its source")
    _make_tree_durable(work)
    os.replace(work, target)
    _fsync_path(target.parent, directory=True)


def _paths_overlap(first: Path, second: Path) -> bool:
    """Shared path-containment check; canonical logic lives in btctl_core."""
    return paths_overlap(first, second)


class LegacyUpgrade:
    """Move an exact stopped v2.1.4 data snapshot into the split v2.2 runtime."""

    def __init__(self, docker: LifecycleDocker, *, health_timeout_seconds: int = 90):
        self.docker = docker
        self.health_timeout_seconds = health_timeout_seconds

    @staticmethod
    def _legacy_values(values: dict[str, str]) -> tuple[str, Path]:
        name = values.get("BT_LEGACY_CONTAINER", "")
        raw_path = values.get("BT_LEGACY_DATA_DIR", "")
        if not _CONTAINER_RE.fullmatch(name):
            raise InstallError("BT_LEGACY_CONTAINER must be one exact container name")
        path = Path(raw_path)
        if not path.is_absolute() or ".." in path.parts or path == Path("/"):
            raise InstallError("BT_LEGACY_DATA_DIR must be an absolute non-root directory")
        return name, path

    def _verify_legacy(
        self, name: str, data_dir: Path, *, expected: dict[str, object] | None = None
    ) -> dict:
        container = self.docker.inspect_container(name)
        if container is None:
            raise InstallError("exact legacy container is missing")
        image_ref = container.get("Config", {}).get("Image", "")
        _image_name, tag_separator, tag = image_ref.rsplit("/", 1)[-1].rpartition(":")
        is_version_tag = bool(tag_separator) and tag in {"2.1.4", "v2.1.4"}
        if not is_version_tag and image_ref != (
            _HISTORICAL_CA_LATEST_IMAGE
        ):
            raise InstallError("legacy container is not an approved v2.1.4 source")
        mounts = [
            mount
            for mount in container.get("Mounts", [])
            if mount.get("Destination") == "/app/data"
        ]
        if (
            len(mounts) != 1
            or mounts[0].get("Type") != "bind"
            or mounts[0].get("RW") is not True
            or Path(str(mounts[0].get("Source", ""))).resolve() != data_dir.resolve()
        ):
            raise InstallError("legacy /app/data bind mount does not match")
        if expected and (
            container.get("Id") != expected.get("legacy_container_id")
            or container.get("Image") != expected.get("legacy_image_id")
            or image_ref != expected.get("legacy_image_ref")
        ):
            raise InstallError("legacy runtime identity no longer matches the journal")
        image_id = str(container.get("Image", ""))
        if not image_id:
            raise InstallError("legacy runtime image identity is incomplete")
        self.docker.probe_image_version(image_id, "2.1.4")
        return container

    @staticmethod
    def _verify_journal_paths(
        journal: dict[str, object],
        config: InstallConfig,
        name: str,
        legacy_data: Path,
    ) -> None:
        if journal.get("legacy_container") != name:
            raise InstallError("migration journal legacy container does not match")
        try:
            recorded_legacy = Path(str(journal["legacy_data_dir"]))
            recorded_target = Path(str(journal["target_data_dir"]))
            snapshot = Path(str(journal["snapshot_path"]))
        except KeyError as exc:
            raise InstallError("migration journal path evidence is incomplete") from exc
        if (
            recorded_legacy.resolve() != legacy_data.resolve()
            or recorded_target.resolve() != Path(config.data_dir).resolve()
            or snapshot.parent.resolve() != Path(config.backup_dir).resolve()
        ):
            raise InstallError("migration journal path evidence does not match")
        attempt = journal.get("attempt", 1)
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise InstallError("migration journal attempt counter is invalid")
        if journal.get("legacy_initial_status") not in {"running", "exited", "created"}:
            raise InstallError("migration journal legacy start state is invalid")
        snapshot_work_value = journal.get("snapshot_work_path")
        if snapshot_work_value is not None:
            snapshot_work = Path(str(snapshot_work_value))
            if (
                snapshot_work.parent.resolve() != snapshot.parent.resolve()
                or snapshot_work.name != f"{snapshot.name}.partial"
            ):
                raise InstallError("migration snapshot work path evidence does not match")
        target_work_value = journal.get("target_work_path")
        if target_work_value is not None:
            target_work = Path(str(target_work_value))
            expected_name = f".{recorded_target.name}.btctl-migration-r{attempt}.partial"
            if (
                target_work.parent.resolve() != recorded_target.parent.resolve()
                or target_work.name != expected_name
            ):
                raise InstallError("migration target work path evidence does not match")

    @staticmethod
    def _verify_recorded_snapshot(journal: dict[str, object]) -> None:
        snapshot = Path(str(journal.get("snapshot_path", "")))
        if snapshot.is_symlink() or not snapshot.is_dir():
            raise InstallError("migration snapshot is missing or unsafe")
        _sqlite_integrity(snapshot, checkpoint=False)
        manifest, files = _tree_manifest(snapshot)
        if (
            manifest != journal.get("snapshot_manifest")
            or files != journal.get("snapshot_files")
        ):
            raise InstallError("migration snapshot integrity has drifted")

    @staticmethod
    def _verify_recorded_target(
        journal: dict[str, object], config: InstallConfig
    ) -> None:
        if journal.get("target_reupgrade_status") != "verified":
            raise InstallError(
                "preserved v2.2 target is unavailable for automatic re-upgrade"
            )
        target = Path(config.data_dir)
        if target.is_symlink() or not target.is_dir():
            raise InstallError("preserved v2.2 data target is missing or unsafe")
        _sqlite_integrity(target, checkpoint=False, closed_readonly=True)
        manifest, files = _tree_manifest(target)
        if (
            manifest != journal.get("target_manifest")
            or files != journal.get("target_files")
        ):
            raise InstallError("preserved v2.2 data target integrity has drifted")

    def _validated_prior_rollback(
        self,
        journal: dict[str, object],
        config: InstallConfig,
        name: str,
        legacy_data: Path,
    ) -> dict[str, object]:
        prior = journal.get("prior_rollback")
        if not isinstance(prior, dict) or prior.get("status") != "rolled_back":
            raise InstallError(
                "interrupted re-upgrade has no valid prior rollback evidence"
            )
        self._verify_journal_paths(prior, config, name, legacy_data)
        self._verify_recorded_snapshot(prior)
        return prior

    @staticmethod
    def _capture_target_status(config: InstallConfig) -> dict[str, object]:
        """Classify preserved v2 data without blocking legacy restoration."""
        target = Path(config.data_dir)
        database = target / "translations.db"
        if (
            target.is_symlink()
            or not target.is_dir()
            or database.is_symlink()
            or not database.is_file()
        ):
            return {
                "target_reupgrade_status": "unavailable",
                "target_reupgrade_reason": "missing-or-unsafe",
            }
        try:
            _sqlite_integrity(target, checkpoint=False, closed_readonly=True)
            manifest, files = _tree_manifest(target)
        except (InstallError, OSError):
            return {
                "target_reupgrade_status": "unavailable",
                "target_reupgrade_reason": "integrity-or-read-error",
            }
        return {
            "target_reupgrade_status": "verified",
            "target_manifest": manifest,
            "target_files": files,
        }

    def _restore_legacy_service(
        self,
        plan: DeploymentPlan,
        journal: dict[str, object],
        name: str,
        legacy_data: Path,
    ) -> None:
        """Restore only the exact journaled legacy runtime and prove it healthy."""
        for role in ("api", "proxy"):
            candidate = self.docker.inspect_container(
                str(plan.resources[role]["name"])
            )
            if (
                candidate
                and candidate.get("State", {}).get("Status")
                not in {"created", "exited", "dead"}
            ):
                raise InstallError(
                    "cannot restore legacy while a v2.2 runtime role is active"
                )
        current = self._verify_legacy(name, legacy_data, expected=journal)
        if current.get("State", {}).get("Status") != "running":
            self.docker.start_container(name)
        self.docker.wait_healthy([name], self.health_timeout_seconds)
        running = self._verify_legacy(name, legacy_data, expected=journal)
        if (
            running.get("State", {}).get("Status") != "running"
            or running.get("State", {}).get("Health", {}).get("Status")
            != "healthy"
        ):
            raise InstallError("legacy container did not become healthy")

    @staticmethod
    def _prepare_failed_upgrade_retry(
        journal: dict[str, object],
        config: InstallConfig,
        *,
        preserve_target: bool = False,
    ) -> None:
        """Preserve interrupted trees and make the exact next attempt writable."""
        snapshot = Path(str(journal.get("snapshot_path", "")))
        snapshot_work_value = journal.get("snapshot_work_path")
        if snapshot_work_value is not None:
            snapshot_work = Path(str(snapshot_work_value))
            _preserve_incomplete(
                snapshot_work,
                snapshot_work.with_name(f"{snapshot_work.name}.preserved"),
            )
        if snapshot.exists() or snapshot.is_symlink():
            if journal.get("snapshot_manifest") and journal.get("snapshot_files"):
                LegacyUpgrade._verify_recorded_snapshot(journal)
            else:
                _preserve_incomplete(
                    snapshot,
                    snapshot.with_name(f"{snapshot.name}.uncommitted-preserved"),
                )

        target_work_value = journal.get("target_work_path")
        if target_work_value is not None:
            target_work = Path(str(target_work_value))
            _preserve_incomplete(
                target_work,
                target_work.with_name(f"{target_work.name}.preserved"),
            )

        if preserve_target:
            return

        target = Path(config.data_dir)
        if target.is_symlink():
            raise InstallError("failed upgrade target is a symbolic link")
        if not target.exists():
            return
        if not target.is_dir():
            raise InstallError("failed upgrade target is not a directory")
        attempt = journal.get("attempt", 1)
        if isinstance(attempt, bool) or not isinstance(attempt, int):
            raise InstallError("migration journal attempt counter is invalid")
        preserved = target.with_name(
            f".{target.name}.btctl-migration-r{attempt}.preserved"
        )
        _preserve_incomplete(target, preserved)

    def _prepare_upgrade_attempt(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        repository: Path,
        journal_store: MigrationJournalStore,
        *,
        name: str,
        legacy_data: Path,
        legacy: dict,
        initial_status: str,
        restart_legacy_on_failure: bool,
        previous_journal: dict[str, object] | None,
        reupgrade: bool,
        reupgrade_evidence: dict[str, object] | None,
        retrying_failed_upgrade: bool,
        retrying_reupgrade: bool,
    ) -> tuple[
        dict[str, object],
        ComposeInstaller | UnraidInstaller,
        Path,
        Path,
        Path,
        Path,
        bool,
    ]:
        """Prepare one numbered attempt after legacy identity is verified."""
        repository_root = Path(repository).resolve()
        configured_backup = Path(config.backup_dir)
        if configured_backup.is_symlink():
            raise InstallError("BT_BACKUP_DIR must not be a symbolic link")
        backup_root = configured_backup.resolve()
        if backup_root == repository_root or repository_root in backup_root.parents:
            raise InstallError("BT_BACKUP_DIR must be outside the Git checkout")
        target = Path(config.data_dir)
        if any(
            _paths_overlap(left, right)
            for left, right in (
                (legacy_data, target),
                (legacy_data, backup_root),
                (target, backup_root),
            )
        ):
            raise InstallError(
                "legacy, target, and backup directories must not overlap"
            )

        if retrying_failed_upgrade or retrying_reupgrade:
            if previous_journal is None:
                raise InstallError("migration retry evidence is incomplete")
            self._prepare_failed_upgrade_retry(
                previous_journal,
                config,
                preserve_target=reupgrade,
            )

        installer = (
            ComposeInstaller(self.docker)
            if config.install_profile == "compose-existing"
            else UnraidInstaller(self.docker)
        )
        installer._preflight(
            config,
            plan,
            allow_existing_data=reupgrade,
            allow_rolled_back_state=reupgrade,
        )
        if not reupgrade:
            if target.exists() and any(target.iterdir()):
                raise InstallError("BT_DATA_DIR must be empty before migration")
            if target.exists():
                target.rmdir()

        legacy_id = str(legacy.get("Id", ""))
        legacy_image_id = str(legacy.get("Image", ""))
        if not legacy_id or not legacy_image_id:
            raise InstallError("legacy container identity is incomplete")
        suffix = hashlib.sha256(legacy_id.encode("utf-8")).hexdigest()[:12]
        if reupgrade or retrying_failed_upgrade:
            raw_attempt = previous_journal.get("attempt", 1) if previous_journal else 1
            if isinstance(raw_attempt, bool) or not isinstance(raw_attempt, int):
                raise InstallError("migration journal attempt counter is invalid")
            attempt = raw_attempt + 1
            snapshot = backup_root / f"pre-v2.2.0-{suffix}-r{attempt}"
        else:
            attempt = 1
            snapshot = backup_root / f"pre-v2.2.0-{suffix}"
        try:
            ensure_directory_durable(backup_root)
        except ConfigError as exc:
            raise InstallError("BT_BACKUP_DIR could not be created durably") from exc
        if snapshot.exists() or snapshot.is_symlink():
            raise InstallError("migration snapshot already exists")
        snapshot_work = snapshot.with_name(f"{snapshot.name}.partial")
        target_work = target.with_name(
            f".{target.name}.btctl-migration-r{attempt}.partial"
        )
        for work in (snapshot_work, target_work):
            if work.exists() or work.is_symlink():
                raise InstallError("migration work path already exists")

        stop_before_snapshot = initial_status == "running"
        journal: dict[str, object] = {
            "status": "prepared",
            "legacy_container": name,
            "legacy_container_id": legacy_id,
            "legacy_image_id": legacy_image_id,
            "legacy_image_ref": legacy["Config"]["Image"],
            "legacy_data_dir": str(legacy_data),
            "legacy_initial_status": (
                "running" if restart_legacy_on_failure else initial_status
            ),
            "snapshot_path": str(snapshot),
            "snapshot_work_path": str(snapshot_work),
            "target_data_dir": str(target),
            "target_work_path": str(target_work),
            "attempt": attempt,
        }
        if previous_journal is not None:
            journal["previous_snapshot_path"] = previous_journal.get("snapshot_path")
            journal["previous_install_id"] = previous_journal.get("install_id")
        if reupgrade:
            if reupgrade_evidence is None:
                raise InstallError("re-upgrade rollback evidence is incomplete")
            journal["prior_rollback"] = copy.deepcopy(reupgrade_evidence)
        journal_store.save(journal)
        return (
            journal,
            installer,
            target,
            snapshot,
            snapshot_work,
            target_work,
            stop_before_snapshot,
        )

    def _recover_active_upgrade(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        store: StateStore,
        journal_store: MigrationJournalStore,
        journal: dict[str, object],
        name: str,
        legacy_data: Path,
    ) -> DeploymentState:
        state = store.load()
        if state.status not in {"installed", "adopted"}:
            raise InstallError("migration recovery requires active v2.2 state")
        _state_matches_plan(state, config, plan)
        recorded_install_id = journal.get("install_id")
        if recorded_install_id and recorded_install_id != state.install_id:
            raise InstallError("migration journal install identity does not match state")
        legacy = self._verify_legacy(name, legacy_data, expected=journal)
        if legacy.get("State", {}).get("Status") == "running":
            raise InstallError("active v2.2 recovery requires the legacy writer stopped")
        self._verify_recorded_snapshot(journal)
        target = Path(config.data_dir)
        if target.is_symlink() or not target.is_dir():
            raise InstallError("migration target is missing or unsafe")
        report = DeploymentDoctor(self.docker).run(
            config,
            plan,
            _operation_locked=True,
        )
        if not report.ok:
            raise InstallError("active v2.2 runtime failed recovery verification")
        if journal.get("status") != "upgraded":
            journal["status"] = "upgraded"
            journal["install_id"] = state.install_id
            journal_store.save(journal)
        return state

    def _recover_journal_only_runtime(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        store: StateStore,
        journal_store: MigrationJournalStore,
        journal: dict[str, object],
        name: str,
        legacy_data: Path,
    ) -> DeploymentState | None:
        """Adopt an exact live cutover before any retry can move its data bind."""
        runtime_present = any(
            self.docker.inspect_container(str(plan.resources[role]["name"]))
            is not None
            for role in ("api", "proxy")
        ) or self.docker.inspect_network(
            str(plan.resources["private_network"]["name"])
        ) is not None
        if not runtime_present:
            return None
        if journal.get("status") != "snapshot-complete":
            raise InstallError(
                "journal-only recovery found unexpected v2.2 runtime resources"
            )
        legacy = self._verify_legacy(name, legacy_data, expected=journal)
        if legacy.get("State", {}).get("Status") == "running":
            raise InstallError(
                "journal-only v2.2 recovery requires the legacy writer stopped"
            )
        self._verify_recorded_snapshot(journal)
        target = Path(config.data_dir)
        if target.is_symlink() or not target.is_dir():
            raise InstallError("migration target is missing or unsafe")
        adopter = (
            ComposeAdopter(self.docker)
            if config.install_profile == "compose-existing"
            else UnraidAdopter(self.docker)
        )
        adopter.adopt(config, plan, _operation_locked=True)
        return self._recover_active_upgrade(
            config,
            plan,
            store,
            journal_store,
            journal,
            name,
            legacy_data,
        )

    def upgrade(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        repository: Path,
        values: dict[str, str],
        *,
        _operation_locked: bool = False,
    ) -> DeploymentState:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir)):
                return self.upgrade(
                    config,
                    plan,
                    repository,
                    values,
                    _operation_locked=True,
                )
        store = StateStore(Path(config.state_dir))
        journal_store = MigrationJournalStore(Path(config.state_dir))
        name, legacy_data = self._legacy_values(values)
        reupgrade = False
        retrying_failed_upgrade = False
        retrying_reupgrade = False
        reupgrade_evidence: dict[str, object] | None = None
        previous_journal: dict[str, object] | None = None
        if journal_store.path.exists() and not store.path.exists():
            previous_journal = journal_store.load()
            self._verify_journal_paths(
                previous_journal,
                config,
                name,
                legacy_data,
            )
            if previous_journal.get("status") not in {
                "prepared",
                "snapshot-complete",
                "upgrade-failed",
            }:
                raise InstallError(
                    "journal-only migration is not eligible for safe upgrade retry"
                )
            recovered = self._recover_journal_only_runtime(
                config,
                plan,
                store,
                journal_store,
                previous_journal,
                name,
                legacy_data,
            )
            if recovered is not None:
                return recovered
            retrying_failed_upgrade = True
        elif store.path.exists() or journal_store.path.exists():
            if not journal_store.path.exists():
                raise InstallError(
                    "upgrade state exists without migration journal evidence"
                )
            current_state = store.load()
            previous_journal = journal_store.load()
            self._verify_journal_paths(
                previous_journal,
                config,
                name,
                legacy_data,
            )
            journal_status = previous_journal.get("status")
            if (
                current_state.status in {"installed", "adopted"}
                and journal_status
                in {"snapshot-complete", "upgrade-journal-failed", "upgraded"}
            ):
                return self._recover_active_upgrade(
                    config,
                    plan,
                    store,
                    journal_store,
                    previous_journal,
                    name,
                    legacy_data,
                )
            if (
                current_state.status == "rolled_back"
                and journal_status
                in {
                    "rolled_back",
                    "prepared",
                    "snapshot-complete",
                    "reupgrade-failed",
                }
            ):
                recovered = _completed_uninstall_for_reinstall(
                    config,
                    plan,
                    allow_rolled_back=True,
                )
                if recovered != current_state:
                    raise InstallError("rolled-back state changed during re-upgrade")
                if journal_status != "rolled_back":
                    reupgrade_evidence = self._validated_prior_rollback(
                        previous_journal,
                        config,
                        name,
                        legacy_data,
                    )
                    retrying_reupgrade = True
                else:
                    reupgrade_evidence = previous_journal
                self._verify_recorded_snapshot(reupgrade_evidence)
                self._verify_recorded_target(reupgrade_evidence, config)
                reupgrade = True
            else:
                raise InstallError(
                    "existing migration state is not eligible for upgrade recovery"
                )
        if legacy_data.is_symlink() or not legacy_data.is_dir():
            raise InstallError("BT_LEGACY_DATA_DIR must be a real directory")
        if legacy_data.resolve() == Path(config.data_dir).resolve():
            raise InstallError("legacy and target data directories must differ")

        legacy = self._verify_legacy(
            name,
            legacy_data,
            expected=(
                previous_journal
                if retrying_failed_upgrade
                else reupgrade_evidence
            ),
        )
        initial_status = legacy.get("State", {}).get("Status")
        if initial_status not in {"running", "exited", "created"}:
            raise InstallError("legacy container is not in a migratable state")
        restart_legacy_on_failure = initial_status == "running" or bool(
            previous_journal
            and previous_journal.get("legacy_initial_status") == "running"
        )
        try:
            (
                journal,
                installer,
                target,
                snapshot,
                snapshot_work,
                target_work,
                stop_before_snapshot,
            ) = self._prepare_upgrade_attempt(
                config,
                plan,
                repository,
                journal_store,
                name=name,
                legacy_data=legacy_data,
                legacy=legacy,
                initial_status=initial_status,
                restart_legacy_on_failure=restart_legacy_on_failure,
                previous_journal=previous_journal,
                reupgrade=reupgrade,
                reupgrade_evidence=reupgrade_evidence,
                retrying_failed_upgrade=retrying_failed_upgrade,
                retrying_reupgrade=retrying_reupgrade,
            )
        except BaseException:
            if (
                restart_legacy_on_failure
                and previous_journal is not None
                and (retrying_failed_upgrade or retrying_reupgrade)
            ):
                try:
                    self._restore_legacy_service(
                        plan,
                        previous_journal,
                        name,
                        legacy_data,
                    )
                except BaseException as restore_exc:
                    raise InstallError(
                        "upgrade retry failed and the legacy service could not be restored"
                    ) from restore_exc
            raise
        target_installed = False
        try:
            if stop_before_snapshot:
                self.docker.stop_container(name)
            stopped_legacy = self._verify_legacy(
                name,
                legacy_data,
                expected=journal,
            )
            if stopped_legacy.get("State", {}).get("Status") == "running":
                raise InstallError("legacy writer did not stop")
            self.docker.prepare_migration_source(
                str(journal["legacy_image_id"]),
                legacy_data,
            )
            _sqlite_integrity(legacy_data, checkpoint=True)
            source_manifest, source_files = _tree_manifest(legacy_data)
            _secure_copy_atomic(legacy_data, snapshot, snapshot_work)
            _sqlite_integrity(snapshot, checkpoint=False)
            snapshot_manifest, snapshot_files = _tree_manifest(snapshot)
            if (source_manifest, source_files) != (snapshot_manifest, snapshot_files):
                raise InstallError("offline snapshot does not match the stopped source")
            if not reupgrade:
                _secure_copy_atomic(snapshot, target, target_work)
            _sqlite_integrity(
                target,
                checkpoint=False,
                closed_readonly=reupgrade,
            )
            target_manifest, target_files = _tree_manifest(target)
            if (
                not reupgrade
                and (snapshot_manifest, snapshot_files)
                != (target_manifest, target_files)
            ):
                raise InstallError("migration target does not match the snapshot")

            journal.update({
                "status": "snapshot-complete",
                "snapshot_manifest": snapshot_manifest,
                "snapshot_files": snapshot_files,
                "target_manifest": target_manifest,
                "target_files": target_files,
            })
            journal_store.save(journal)
            state = installer.install(
                config,
                plan,
                Path(repository),
                _operation_locked=True,
                _allow_existing_data=True,
                _allow_rolled_back_state=reupgrade,
            )
            target_installed = True
            journal.update({"status": "upgraded", "install_id": state.install_id})
            journal_store.save(journal)
            return state
        except BaseException as exc:
            if target_installed:
                journal["status"] = "upgrade-journal-failed"
            else:
                journal["status"] = (
                    "reupgrade-failed" if reupgrade else "upgrade-failed"
                )
            restore_error: BaseException | None = None
            if restart_legacy_on_failure and not target_installed:
                try:
                    self._restore_legacy_service(
                        plan,
                        journal,
                        name,
                        legacy_data,
                    )
                except BaseException as recovery_exc:
                    restore_error = recovery_exc
            try:
                journal_store.save(journal)
            except BaseException:
                pass
            if restore_error is not None:
                raise InstallError(
                    "legacy upgrade failed and the legacy service could not be restored"
                ) from restore_error
            if isinstance(exc, InstallError):
                raise
            if target_installed:
                raise InstallError(
                    "v2.2 started but the migration journal could not be committed; "
                    "legacy remains stopped"
                ) from exc
            raise InstallError("legacy upgrade failed") from exc

    def rollback(
        self,
        config: InstallConfig,
        plan: DeploymentPlan,
        *,
        _operation_locked: bool = False,
    ) -> DeploymentState:
        if not _operation_locked:
            with OperationLock(Path(config.state_dir)):
                return self.rollback(config, plan, _operation_locked=True)
        journal_store = MigrationJournalStore(Path(config.state_dir))
        journal = journal_store.load()
        store = StateStore(Path(config.state_dir))
        journal_status = journal.get("status")
        current_state = store.load()
        legacy_data = Path(str(journal.get("legacy_data_dir", "")))
        name = str(journal.get("legacy_container", ""))
        self._verify_journal_paths(journal, config, name, legacy_data)

        if journal_status == "rolled_back":
            if current_state.status != "rolled_back":
                raise InstallError("rolled-back journal does not match deployment state")
            _state_matches_plan(current_state, config, plan)
            self._restore_legacy_service(
                plan,
                journal,
                name,
                legacy_data,
            )
            return current_state

        if journal_status in {"snapshot-complete", "upgrade-journal-failed"}:
            if current_state.status not in {"installed", "adopted"}:
                raise InstallError("migration recovery state is not active")
            _state_matches_plan(current_state, config, plan)
            recorded_install_id = journal.get("install_id")
            if recorded_install_id and recorded_install_id != current_state.install_id:
                raise InstallError("migration recovery install identity does not match")
        elif journal_status not in {"upgraded", "rollback-failed"}:
            raise InstallError("migration journal is not eligible for rollback")
        elif current_state.status not in {
            "installed",
            "adopted",
            "uninstalling",
            "uninstalled",
            "rolled_back",
        }:
            raise InstallError("deployment state is not eligible for rollback recovery")
        _state_matches_plan(current_state, config, plan)

        legacy = self._verify_legacy(name, legacy_data, expected=journal)
        if current_state.status not in {"uninstalled", "rolled_back"}:
            if legacy.get("State", {}).get("Status") == "running":
                raise InstallError(
                    "rollback requires the legacy writer stopped while v2.2 is active"
                )
            source_manifest, source_files = _tree_manifest(legacy_data)
            if (
                source_manifest != journal.get("snapshot_manifest")
                or source_files != journal.get("snapshot_files")
            ):
                raise InstallError("legacy source changed after the offline snapshot")
        self._verify_recorded_snapshot(journal)

        try:
            state = RuntimeUninstaller(self.docker).uninstall(
                config,
                plan,
                _operation_locked=True,
                _allow_missing_target_data=True,
            )
            self._restore_legacy_service(
                plan,
                journal,
                name,
                legacy_data,
            )
            target_status = self._capture_target_status(config)
            for key in (
                "target_reupgrade_status",
                "target_reupgrade_reason",
                "target_manifest",
                "target_files",
            ):
                journal.pop(key, None)
            journal.update(target_status)
            completed = replace(state, status="rolled_back")
            store.save(completed)
            journal["status"] = "rolled_back"
            journal_store.save(journal)
            return completed
        except BaseException:
            journal["status"] = "rollback-failed"
            journal_store.save(journal)
            raise
