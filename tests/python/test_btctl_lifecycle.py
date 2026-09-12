"""Lifecycle contracts for diagnosis, conservative removal, and v2.1.4 migration."""

from __future__ import annotations

import copy
import os
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

from btctl_compose import (
    ComposeInstaller,
    InstallError,
    _write_private_json,
    render_schema2_compose,
)
from btctl_core import DeploymentPlan, InstallConfig, ReleaseIdentity, StateStore
from btctl_lifecycle import (
    DeploymentDoctor,
    LegacyUpgrade,
    MigrationJournalStore,
    RuntimeUninstaller,
    _preserve_incomplete,
    _secure_copy_atomic,
    _sqlite_integrity,
    _tree_manifest,
)
from tests.python.test_btctl_compose import FakeDocker, values


class LifecycleDocker(FakeDocker):
    def probe_providers(self, container):
        self.calls.append(("probe_providers", container))

    def probe_image_version(self, image_id, expected_version):
        self.calls.append(("probe_image_version", image_id, expected_version))

    def stop_container(self, name):
        self.calls.append(("stop_container", name))
        self.containers[name]["State"]["Status"] = "exited"

    def start_container(self, name):
        self.calls.append(("start_container", name))
        self.containers[name]["State"] = {
            "Status": "running",
            "Health": {"Status": "healthy"},
        }

    def remove_container(self, name):
        self.calls.append(("remove_container", name))
        self.containers.pop(name, None)

    def remove_network(self, name):
        self.calls.append(("remove_network", name))
        self.networks.pop(name, None)

    def prepare_migration_source(self, image_id, path):
        self.calls.append(("prepare_migration_source", image_id, str(path)))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.identity = ReleaseIdentity.from_checkout(
            version="2.2.0", sha="9" * 40, clean=True
        )

    def installed(self, root: Path):
        config = InstallConfig.from_mapping(values(root), self.identity)
        plan = DeploymentPlan.from_config(config)
        docker = LifecycleDocker()
        state = ComposeInstaller(docker).install(config, plan, root)
        docker.networks[plan.resources["private_network"]["name"]]["Internal"] = True
        return config, plan, docker, state

    def migration_fixture(self, root: Path, *, read_write: bool = True):
        config = InstallConfig.from_mapping(values(root), self.identity)
        plan = DeploymentPlan.from_config(config)
        legacy_data = root / "legacy-data"
        legacy_data.mkdir()
        with closing(sqlite3.connect(legacy_data / "translations.db")) as database:
            database.execute(
                "CREATE TABLE translations (key TEXT PRIMARY KEY, value TEXT)"
            )
            database.execute("INSERT INTO translations VALUES ('key', 'value')")
            database.commit()
        docker = LifecycleDocker()
        docker.containers["book-translator-v214-rollback"] = {
            "Id": "legacy-container-id",
            "Image": "sha256:legacy-image-id",
            "State": {"Status": "running", "Health": {"Status": "healthy"}},
            "Config": {"Image": "local/book-translator:2.1.4", "Labels": {}},
            "Mounts": [{
                "Type": "bind",
                "Source": str(legacy_data),
                "Destination": "/app/data",
                "RW": read_write,
            }],
        }
        migration_values = {
            "BT_LEGACY_CONTAINER": "book-translator-v214-rollback",
            "BT_LEGACY_DATA_DIR": str(legacy_data),
        }
        repository = root / "checkout"
        repository.mkdir()
        return config, plan, docker, migration_values, repository

    def interrupted_migration_journal(
        self,
        config: InstallConfig,
        docker: LifecycleDocker,
        migration_values: dict[str, str],
        *,
        status: str = "prepared",
        prior_rollback: dict[str, object] | None = None,
        attempt: int = 1,
    ) -> dict[str, object]:
        legacy = docker.containers["book-translator-v214-rollback"]
        snapshot = Path(config.backup_dir) / f"interrupted-r{attempt}"
        snapshot_work = snapshot.with_name(f"{snapshot.name}.partial")
        target = Path(config.data_dir)
        target_work = target.with_name(
            f".{target.name}.btctl-migration-r{attempt}.partial"
        )
        payload: dict[str, object] = {
            "status": status,
            "legacy_container": "book-translator-v214-rollback",
            "legacy_container_id": legacy["Id"],
            "legacy_image_id": legacy["Image"],
            "legacy_image_ref": legacy["Config"]["Image"],
            "legacy_data_dir": migration_values["BT_LEGACY_DATA_DIR"],
            "legacy_initial_status": "running",
            "snapshot_path": str(snapshot),
            "snapshot_work_path": str(snapshot_work),
            "target_data_dir": str(target),
            "target_work_path": str(target_work),
            "attempt": attempt,
        }
        if prior_rollback is not None:
            payload["prior_rollback"] = copy.deepcopy(prior_rollback)
        if status == "snapshot-complete":
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            _secure_copy_atomic(
                Path(migration_values["BT_LEGACY_DATA_DIR"]),
                snapshot,
                snapshot_work,
            )
            snapshot_manifest, snapshot_files = _tree_manifest(snapshot)
            target_manifest, target_files = _tree_manifest(target)
            payload.update({
                "snapshot_manifest": snapshot_manifest,
                "snapshot_files": snapshot_files,
                "target_manifest": target_manifest,
                "target_files": target_files,
            })
        MigrationJournalStore(Path(config.state_dir)).save(payload)
        return payload

    def test_historical_ca_latest_reference_requires_exact_image_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _config, _plan, docker, migration_values, _repository = (
                self.migration_fixture(root)
            )
            legacy_name = migration_values["BT_LEGACY_CONTAINER"]
            legacy = docker.containers[legacy_name]
            legacy["Config"]["Image"] = (
                "ghcr.io/felixapel/cwa-ebook-translate-plugin:latest"
            )

            verified = LegacyUpgrade(docker)._verify_legacy(
                legacy_name,
                Path(migration_values["BT_LEGACY_DATA_DIR"]),
            )

            self.assertIs(verified, legacy)
            self.assertIn(
                ("probe_image_version", legacy["Image"], "2.1.4"),
                docker.calls,
            )

    def test_legacy_migration_rejects_unrelated_latest_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _config, _plan, docker, migration_values, _repository = (
                self.migration_fixture(root)
            )
            legacy_name = migration_values["BT_LEGACY_CONTAINER"]
            legacy = docker.containers[legacy_name]
            legacy["Config"]["Image"] = "example.invalid/translator:latest"

            with self.assertRaisesRegex(InstallError, "approved v2.1.4 source"):
                LegacyUpgrade(docker)._verify_legacy(
                    legacy_name,
                    Path(migration_values["BT_LEGACY_DATA_DIR"]),
                )

            self.assertNotIn(
                ("probe_image_version", legacy["Image"], "2.1.4"),
                docker.calls,
            )

    def test_legacy_migration_rejects_tagless_version_like_repository(self):
        for image_ref in ("example.invalid/2.1.4", "example.invalid/v2.1.4"):
            with self.subTest(image_ref=image_ref), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _config, _plan, docker, migration_values, _repository = (
                    self.migration_fixture(root)
                )
                legacy_name = migration_values["BT_LEGACY_CONTAINER"]
                legacy = docker.containers[legacy_name]
                legacy["Config"]["Image"] = image_ref

                with self.assertRaisesRegex(InstallError, "approved v2.1.4 source"):
                    LegacyUpgrade(docker)._verify_legacy(
                        legacy_name,
                        Path(migration_values["BT_LEGACY_DATA_DIR"]),
                    )

                self.assertNotIn(
                    ("probe_image_version", legacy["Image"], "2.1.4"),
                    docker.calls,
                )

    def test_historical_ca_latest_reference_rejects_wrong_image_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _config, _plan, docker, migration_values, _repository = (
                self.migration_fixture(root)
            )
            legacy_name = migration_values["BT_LEGACY_CONTAINER"]
            docker.containers[legacy_name]["Config"]["Image"] = (
                "ghcr.io/felixapel/cwa-ebook-translate-plugin:latest"
            )

            with mock.patch.object(
                docker,
                "probe_image_version",
                side_effect=InstallError("legacy image version mismatch"),
            ):
                with self.assertRaisesRegex(InstallError, "version mismatch"):
                    LegacyUpgrade(docker)._verify_legacy(
                        legacy_name,
                        Path(migration_values["BT_LEGACY_DATA_DIR"]),
                    )

    def test_migration_journal_load_rejects_mutable_local_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            store = MigrationJournalStore(state_dir)
            store.save({"status": "snapshot-complete"})

            state_dir.chmod(0o777)
            with self.assertRaisesRegex(InstallError, "could not be read"):
                store.load()
            state_dir.chmod(0o700)

            store.path.chmod(0o666)
            with self.assertRaisesRegex(InstallError, "could not be read"):
                store.load()

    def test_legacy_restore_rejects_paused_v2_role_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, _repository = (
                self.migration_fixture(root)
            )
            legacy_name = migration_values["BT_LEGACY_CONTAINER"]
            legacy = docker.containers[legacy_name]
            legacy["State"]["Status"] = "exited"
            api_name = str(plan.resources["api"]["name"])
            docker.containers[api_name] = {"State": {"Status": "paused"}}
            journal = {
                "legacy_container_id": legacy["Id"],
                "legacy_image_id": legacy["Image"],
                "legacy_image_ref": legacy["Config"]["Image"],
            }

            with self.assertRaisesRegex(
                InstallError, "cannot restore legacy while a v2.2 runtime role"
            ):
                LegacyUpgrade(docker)._restore_legacy_service(
                    plan,
                    journal,
                    legacy_name,
                    Path(migration_values["BT_LEGACY_DATA_DIR"]),
                )

            self.assertNotIn(("start_container", legacy_name), docker.calls)

    def test_migration_journal_commit_fsyncs_parent_after_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "state"
            store = MigrationJournalStore(state_dir)
            events = []
            original_replace = os.replace
            from btctl_core import _fsync_directory as original_fsync_directory

            def replace(source, target):
                events.append(("replace", Path(target)))
                original_replace(source, target)

            def fsync_directory(path):
                events.append(("fsync-directory", Path(path)))
                original_fsync_directory(path)

            with mock.patch(
                "btctl_core._fsync_directory",
                side_effect=fsync_directory,
            ), mock.patch(
                "btctl_lifecycle._fsync_path",
                side_effect=lambda path, *, directory: events.append(
                    ("fsync-path", Path(path), directory)
                ),
            ), mock.patch("btctl_lifecycle.os.replace", side_effect=replace):
                store.save({"status": "prepared"})

            publish = events.index(("replace", store.path))
            self.assertIn(("fsync-directory", state_dir), events[:publish])
            self.assertIn(("fsync-directory", state_dir.parent), events[:publish])
            self.assertEqual(
                events[publish + 1],
                ("fsync-path", state_dir, True),
            )

    def test_atomic_tree_copy_is_durable_before_and_after_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            work = root / ".target.partial"
            source.mkdir()
            (source / "nested").mkdir()
            (source / "nested" / "payload.bin").write_bytes(b"durable payload")
            events = []
            original_replace = os.replace

            def fsync_path(path, *, directory):
                events.append(("fsync", Path(path), directory))

            def replace(source_path, target_path):
                events.append(("replace", Path(source_path), Path(target_path)))
                original_replace(source_path, target_path)

            with mock.patch(
                "btctl_lifecycle._fsync_path",
                create=True,
                side_effect=fsync_path,
            ), mock.patch("btctl_lifecycle.os.replace", side_effect=replace):
                _secure_copy_atomic(source, target, work)

            publish = events.index(("replace", work, target))
            self.assertIn(("fsync", work / "nested" / "payload.bin", False), events[:publish])
            self.assertIn(("fsync", work / "nested", True), events[:publish])
            self.assertIn(("fsync", work, True), events[:publish])
            self.assertEqual(events[publish + 1], ("fsync", target.parent, True))
            self.assertEqual((target / "nested" / "payload.bin").read_bytes(), b"durable payload")

    def test_partial_preservation_fsyncs_tree_before_rename_and_parent_after(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "interrupted.partial"
            destination = root / "interrupted.partial.preserved"
            source.mkdir()
            (source / "payload.bin").write_bytes(b"preserved evidence")
            events = []
            original_replace = os.replace

            def fsync_path(path, *, directory):
                events.append(("fsync", Path(path), directory))

            def replace(source_path, target_path):
                events.append(("replace", Path(source_path), Path(target_path)))
                original_replace(source_path, target_path)

            with mock.patch(
                "btctl_lifecycle._fsync_path",
                side_effect=fsync_path,
            ), mock.patch("btctl_lifecycle.os.replace", side_effect=replace):
                _preserve_incomplete(source, destination)

            publish = events.index(("replace", source, destination))
            self.assertIn(("fsync", source / "payload.bin", False), events[:publish])
            self.assertIn(("fsync", source, True), events[:publish])
            self.assertEqual(events[publish + 1], ("fsync", destination.parent, True))
            self.assertEqual(
                (destination / "payload.bin").read_bytes(),
                b"preserved evidence",
            )

    def test_upgrade_durably_creates_every_backup_directory_before_legacy_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, _plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            backup_root = root / "durable-parent" / "nested" / "backups"
            config = replace(config, backup_dir=str(backup_root))
            plan = DeploymentPlan.from_config(config)
            events = []
            from btctl_core import _fsync_directory as original_fsync_directory

            def fsync_directory(path):
                events.append(("fsync", Path(path)))
                original_fsync_directory(path)

            original_stop = docker.stop_container

            def stop_container(name):
                events.append(("stop", name))
                original_stop(name)

            with mock.patch(
                "btctl_core._fsync_directory", side_effect=fsync_directory
            ), mock.patch.object(
                docker, "stop_container", side_effect=stop_container
            ):
                LegacyUpgrade(docker).upgrade(
                    config, plan, repository, migration_values
                )

            stop = events.index(("stop", migration_values["BT_LEGACY_CONTAINER"]))
            before_stop = events[:stop]
            for created in (
                root / "durable-parent",
                root / "durable-parent" / "nested",
                backup_root,
            ):
                self.assertIn(("fsync", created), before_stop)
                self.assertIn(("fsync", created.parent), before_stop)

    def test_sqlite_checkpoint_busy_result_fails_before_migration_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database_path = data_dir / "translations.db"
            writer = sqlite3.connect(database_path)
            reader = None
            try:
                self.assertEqual(
                    writer.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal"
                )
                writer.execute("CREATE TABLE translations (value TEXT)")
                writer.execute("INSERT INTO translations VALUES ('first')")
                writer.commit()

                reader = sqlite3.connect(database_path)
                reader.execute("BEGIN")
                reader.execute("SELECT * FROM translations").fetchall()
                writer.execute("INSERT INTO translations VALUES ('second')")
                writer.commit()

                with self.assertRaisesRegex(InstallError, "checkpoint"):
                    _sqlite_integrity(data_dir, checkpoint=True)
            finally:
                if reader is not None:
                    reader.close()
                writer.close()

    def test_closed_sqlite_integrity_accepts_read_only_wal_database_without_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database_path = data_dir / "translations.db"
            with closing(sqlite3.connect(database_path)) as database:
                self.assertEqual(
                    database.execute("PRAGMA journal_mode=WAL").fetchone()[0],
                    "wal",
                )
                database.execute("CREATE TABLE translations (value TEXT)")
                database.execute("INSERT INTO translations VALUES ('safe')")
                database.commit()
            for suffix in ("-wal", "-shm", "-journal"):
                self.assertFalse(Path(str(database_path) + suffix).exists())
            os.chmod(database_path, 0o400)

            _sqlite_integrity(
                data_dir,
                checkpoint=False,
                closed_readonly=True,
            )

    def test_closed_sqlite_integrity_rejects_recovery_sidecars(self):
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            database_path = data_dir / "translations.db"
            with closing(sqlite3.connect(database_path)) as database:
                database.execute("CREATE TABLE translations (value TEXT)")
            Path(str(database_path) + "-wal").write_bytes(b"pending")

            with self.assertRaisesRegex(InstallError, "sidecar"):
                _sqlite_integrity(
                    data_dir,
                    checkpoint=False,
                    closed_readonly=True,
                )

    def test_legacy_runtime_proves_version_inside_immutable_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )

            LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertIn(
                ("probe_image_version", "sha256:legacy-image-id", "2.1.4"),
                docker.calls,
            )
            self.assertIn(
                (
                    "prepare_migration_source",
                    "sha256:legacy-image-id",
                    migration_values["BT_LEGACY_DATA_DIR"],
                ),
                docker.calls,
            )

    def test_doctor_proves_runtime_identity_auth_networks_ports_and_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, _ = self.installed(root)

            report = DeploymentDoctor(docker).run(config, plan)

            self.assertTrue(report.ok, report.to_dict())
            self.assertTrue(all(check["status"] == "ok" for check in report.checks))

            api = docker.containers[plan.resources["api"]["name"]]
            api["Config"]["Env"] = [
                item for item in api["Config"]["Env"]
                if not item.startswith("BT_AUTH_MODE=")
            ] + ["BT_AUTH_MODE=disabled", "BT_ALLOW_INSECURE_AUTH=true"]

            drift = DeploymentDoctor(docker).run(config, plan)
            self.assertFalse(drift.ok)
            self.assertIn("runtime", " ".join(str(check) for check in drift.checks))

    def test_legacy_schema_two_compose_remains_doctor_and_uninstall_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, state = self.installed(root)
            legacy = replace(state, schema_version=2)
            StateStore(Path(config.state_dir)).save(legacy)
            _write_private_json(
                Path(config.state_dir) / "deployment.compose.json",
                render_schema2_compose(config, plan, state.install_id),
            )
            for name in ("api.env", "proxy.env"):
                (Path(config.state_dir) / name).unlink()

            report = DeploymentDoctor(docker).run(config, plan)
            self.assertTrue(report.ok, report.to_dict())
            removed = RuntimeUninstaller(docker).uninstall(config, plan)
            self.assertEqual(removed.status, "uninstalled")

    def test_doctor_deep_probes_only_after_structural_success(self):
        with tempfile.TemporaryDirectory() as directory:
            config, plan, docker, _ = self.installed(Path(directory))

            ordinary = DeploymentDoctor(docker).run(config, plan)
            self.assertTrue(ordinary.ok, ordinary.to_dict())
            self.assertFalse(
                [call for call in docker.calls if call[0] == "probe_providers"]
            )

            deep = DeploymentDoctor(docker).run(config, plan, deep=True)
            self.assertTrue(deep.ok, deep.to_dict())
            self.assertEqual(
                [call for call in docker.calls if call[0] == "probe_providers"],
                [("probe_providers", plan.resources["api"]["name"])],
            )

            docker.containers[config.reader_container]["State"]["Status"] = "exited"
            failed = DeploymentDoctor(docker).run(config, plan, deep=True)
            self.assertFalse(failed.ok)
            self.assertEqual(
                [call for call in docker.calls if call[0] == "probe_providers"],
                [("probe_providers", plan.resources["api"]["name"])],
            )

    def test_doctor_deep_sanitizes_provider_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config, plan, docker, _ = self.installed(Path(directory))

            def fail(_container):
                raise RuntimeError("private-key-value model-name endpoint")

            docker.probe_providers = fail
            report = DeploymentDoctor(docker).run(config, plan, deep=True)

            self.assertFalse(report.ok)
            provider = report.checks[-1]
            self.assertEqual(provider["name"], "providers")
            self.assertEqual(provider["detail"], "provider deep probe failed")

    def test_doctor_accepts_exact_compose_operator_group_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, _state = self.installed(root)
            Path(config.data_dir).chmod(0o2750)

            report = DeploymentDoctor(docker).run(config, plan)

            self.assertTrue(report.ok, report.to_dict())

    def test_doctor_rejects_overbroad_compose_data_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, _state = self.installed(root)
            Path(config.data_dir).chmod(0o2770)

            report = DeploymentDoctor(docker).run(config, plan)

            self.assertFalse(report.ok)
            self.assertEqual(
                [
                    item["name"]
                    for item in report.checks
                    if item["status"] == "failed"
                ],
                ["data-directory"],
            )

    def test_uninstall_removes_only_owned_runtime_and_preserves_data_and_cwa(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, _ = self.installed(root)
            docker.containers["unrelated"] = {"Id": "preserve-me"}

            state = RuntimeUninstaller(docker).uninstall(config, plan)

            self.assertEqual(state.status, "uninstalled")
            self.assertNotIn(plan.resources["api"]["name"], docker.containers)
            self.assertNotIn(plan.resources["proxy"]["name"], docker.containers)
            self.assertNotIn(plan.resources["private_network"]["name"], docker.networks)
            self.assertIn("calibre-web-automated", docker.containers)
            self.assertIn("unrelated", docker.containers)
            self.assertTrue(Path(config.data_dir).is_dir())
            self.assertTrue(Path(config.backup_dir).parent.is_dir())
            self.assertEqual(StateStore(Path(config.state_dir)).load(), state)
            for role in ("proxy", "api"):
                name = str(plan.resources[role]["name"])
                stop_index = docker.calls.index(("stop_container", name))
                remove_index = docker.calls.index(("remove_container", name))
                self.assertLess(stop_index, remove_index)
                self.assertTrue(state.resources[role]["stopped"])

            before = len(docker.calls)
            repeated = RuntimeUninstaller(docker).uninstall(config, plan)
            self.assertEqual(repeated.status, "uninstalled")
            self.assertEqual(len(docker.calls), before)

    def test_uninstall_retry_accepts_only_verified_missing_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, state = self.installed(root)
            resources = copy.deepcopy(state.resources)
            proxy_name = str(resources["proxy"]["name"])
            docker.containers.pop(proxy_name)
            resources["proxy"]["removed"] = True
            interrupted = replace(
                state, status="uninstalling", resources=resources
            )
            StateStore(Path(config.state_dir)).save(interrupted)

            completed = RuntimeUninstaller(docker).uninstall(config, plan)

            self.assertEqual(completed.status, "uninstalled")
            self.assertTrue(completed.resources["proxy"]["removed"])
            self.assertNotIn(plan.resources["api"]["name"], docker.containers)

    def test_uninstall_fails_closed_when_removal_does_not_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, _ = self.installed(root)
            with mock.patch.object(
                docker,
                "remove_container",
                lambda name: docker.calls.append(("remove_container", name)),
            ), self.assertRaisesRegex(InstallError, "removal did not complete"):
                RuntimeUninstaller(docker).uninstall(config, plan)

    def test_uninstall_retry_fails_closed_when_docker_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, state = self.installed(root)
            interrupted = replace(state, status="uninstalling")
            StateStore(Path(config.state_dir)).save(interrupted)

            with mock.patch.object(
                docker,
                "require_available",
                side_effect=InstallError("Docker daemon unavailable"),
            ), self.assertRaisesRegex(InstallError, "unavailable"):
                RuntimeUninstaller(docker).uninstall(config, plan)

            self.assertEqual(
                StateStore(Path(config.state_dir)).load().status, "uninstalling"
            )
            self.assertFalse(
                [call for call in docker.calls if call[0] in {"remove_container", "remove_network"}]
            )

    def test_compose_reinstall_archives_completed_uninstall_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, original = self.installed(root)
            RuntimeUninstaller(docker).uninstall(config, plan)

            replacement = ComposeInstaller(docker).install(config, plan, root)

            self.assertNotEqual(replacement.install_id, original.install_id)
            self.assertEqual(StateStore(root / "state").load(), replacement)
            history = (
                root
                / "state"
                / "history"
                / f"{original.install_id}-uninstalled.json"
            )
            self.assertTrue(history.is_file())
            self.assertEqual(history.stat().st_mode & 0o777, 0o600)
            self.assertIn('"status": "uninstalled"', history.read_text())

    def test_reinstall_failure_keeps_uninstalled_state_current(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, _ = self.installed(root)
            uninstalled = RuntimeUninstaller(docker).uninstall(config, plan)
            docker.fail_health = True

            with self.assertRaisesRegex(InstallError, "health"):
                ComposeInstaller(docker).install(config, plan, root)

            self.assertEqual(StateStore(root / "state").load(), uninstalled)
            self.assertFalse((root / "state" / "history").exists())

    def test_uninstall_stops_before_mutation_when_ownership_has_drifted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, _ = self.installed(root)
            docker.containers[plan.resources["proxy"]["name"]]["Id"] = "replacement"
            before = len(docker.calls)

            with self.assertRaisesRegex(InstallError, "ownership"):
                RuntimeUninstaller(docker).uninstall(config, plan)

            self.assertFalse(
                {"remove_container", "remove_network"}
                & {call[0] for call in docker.calls[before:]}
            )

    def test_uninstall_never_mutates_a_resource_not_classified_as_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, state = self.installed(root)
            resources = copy.deepcopy(state.resources)
            resources["proxy"]["ownership"] = "adopted"
            StateStore(root / "state").save(replace(state, resources=resources))
            before = len(docker.calls)

            with self.assertRaisesRegex(InstallError, "not classified as owned"):
                RuntimeUninstaller(docker).uninstall(config, plan)

            self.assertFalse(
                {"remove_container", "remove_network"}
                & {call[0] for call in docker.calls[before:]}
            )

    def test_upgrade_snapshots_offline_then_rollback_restarts_exact_v214(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            legacy_data = root / "legacy-data"
            legacy_data.mkdir()
            database = sqlite3.connect(legacy_data / "translations.db")
            database.execute("CREATE TABLE translations (key TEXT PRIMARY KEY, value TEXT)")
            database.execute("INSERT INTO translations VALUES ('key', 'value')")
            database.commit()
            database.close()
            docker = LifecycleDocker()
            docker.containers["book-translator-v214-rollback"] = {
                "Id": "legacy-container-id",
                "Image": "sha256:legacy-image-id",
                "State": {"Status": "running"},
                "Config": {"Image": "local/book-translator:2.1.4", "Labels": {}},
                "Mounts": [{
                    "Type": "bind",
                    "Source": str(legacy_data),
                    "Destination": "/app/data",
                    "RW": True,
                }],
            }
            migration_values = {
                "BT_LEGACY_CONTAINER": "book-translator-v214-rollback",
                "BT_LEGACY_DATA_DIR": str(legacy_data),
            }
            repository = root / "checkout"
            repository.mkdir()

            state = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(state.status, "installed")
            self.assertEqual(
                docker.containers["book-translator-v214-rollback"]["State"]["Status"],
                "exited",
            )
            journal = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(journal["status"], "upgraded")
            snapshot = Path(journal["snapshot_path"])
            self.assertTrue((snapshot / "translations.db").is_file())
            target_database = Path(config.data_dir) / "translations.db"
            self.assertTrue(target_database.is_file())
            with closing(sqlite3.connect(target_database)) as database:
                database.execute(
                    "CREATE TABLE translations_v2 (key TEXT PRIMARY KEY, value TEXT)"
                )
                database.execute(
                    "INSERT INTO translations_v2 VALUES ('v2-key', 'v2-value')"
                )
                database.commit()

            rolled_back = LegacyUpgrade(docker).rollback(config, plan)

            self.assertEqual(rolled_back.status, "rolled_back")
            self.assertEqual(
                docker.containers["book-translator-v214-rollback"]["State"]["Status"],
                "running",
            )
            self.assertTrue(snapshot.is_dir())
            self.assertTrue(Path(config.data_dir).is_dir())
            self.assertEqual(
                MigrationJournalStore(Path(config.state_dir)).load()["status"],
                "rolled_back",
            )

            with closing(sqlite3.connect(legacy_data / "translations.db")) as database:
                database.execute(
                    "INSERT INTO translations VALUES ('after-rollback', 'legacy-value')"
                )
                database.commit()

            reupgraded = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(reupgraded.status, "installed")
            self.assertNotEqual(reupgraded.install_id, state.install_id)
            latest_journal = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(latest_journal["status"], "upgraded")
            self.assertNotEqual(latest_journal["snapshot_path"], str(snapshot))
            with closing(sqlite3.connect(
                Path(latest_journal["snapshot_path"]) / "translations.db"
            )) as database:
                self.assertEqual(
                    database.execute(
                        "SELECT value FROM translations WHERE key='after-rollback'"
                    ).fetchone(),
                    ("legacy-value",),
                )
            with closing(sqlite3.connect(target_database)) as database:
                self.assertEqual(
                    database.execute(
                        "SELECT value FROM translations_v2 WHERE key='v2-key'"
                    ).fetchone(),
                    ("v2-value",),
                )

    def test_upgrade_retry_recovers_when_final_journal_commit_was_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            legacy_data = root / "legacy-data"
            legacy_data.mkdir()
            with closing(sqlite3.connect(legacy_data / "translations.db")) as database:
                database.execute(
                    "CREATE TABLE translations (key TEXT PRIMARY KEY, value TEXT)"
                )
                database.execute("INSERT INTO translations VALUES ('key', 'value')")
                database.commit()
            docker = LifecycleDocker()
            docker.containers["book-translator-v214-rollback"] = {
                "Id": "legacy-container-id",
                "Image": "sha256:legacy-image-id",
                "State": {"Status": "running"},
                "Config": {"Image": "local/book-translator:2.1.4", "Labels": {}},
                "Mounts": [{
                    "Type": "bind",
                    "Source": str(legacy_data),
                    "Destination": "/app/data",
                    "RW": True,
                }],
            }
            migration_values = {
                "BT_LEGACY_CONTAINER": "book-translator-v214-rollback",
                "BT_LEGACY_DATA_DIR": str(legacy_data),
            }
            repository = root / "checkout"
            repository.mkdir()
            original_save = MigrationJournalStore.save

            def interrupted_save(store, payload):
                if payload.get("status") in {"upgraded", "upgrade-journal-failed"}:
                    raise OSError("simulated interrupted journal commit")
                return original_save(store, payload)

            with mock.patch.object(
                MigrationJournalStore,
                "save",
                autospec=True,
                side_effect=interrupted_save,
            ):
                with self.assertRaisesRegex(InstallError, "journal"):
                    LegacyUpgrade(docker).upgrade(
                        config, plan, repository, migration_values
                    )

            active = StateStore(Path(config.state_dir)).load()
            self.assertEqual(active.status, "installed")
            self.assertEqual(
                MigrationJournalStore(Path(config.state_dir)).load()["status"],
                "snapshot-complete",
            )
            before = len(docker.calls)

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered, active)
            self.assertEqual(
                MigrationJournalStore(Path(config.state_dir)).load()["status"],
                "upgraded",
            )
            mutation_calls = {
                call[0]
                for call in docker.calls[before:]
                if call[0]
                in {"build_image", "compose_up", "compose_down", "stop_container", "start_container"}
            }
            self.assertEqual(mutation_calls, set())

    def test_upgrade_retries_a_journal_only_transient_pre_cutover_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )

            with mock.patch.object(
                docker,
                "stop_container",
                side_effect=InstallError("transient stop failure"),
            ):
                with self.assertRaisesRegex(InstallError, "transient"):
                    LegacyUpgrade(docker).upgrade(
                        config, plan, repository, migration_values
                    )

            self.assertFalse(StateStore(Path(config.state_dir)).path.exists())
            failed = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(failed["status"], "upgrade-failed")

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered.status, "installed")
            completed = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(completed["status"], "upgraded")
            self.assertEqual(completed["attempt"], 2)
            self.assertTrue(str(completed["snapshot_path"]).endswith("-r2"))

    def test_upgrade_recovers_a_durable_prepared_crash_and_preserves_partial_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            original_save = MigrationJournalStore.save
            crashed = False

            def crash_after_prepared(store, payload):
                nonlocal crashed
                result = original_save(store, payload)
                if payload.get("status") == "prepared" and not crashed:
                    crashed = True
                    raise RuntimeError("simulated power loss after prepared journal")
                return result

            with mock.patch.object(
                MigrationJournalStore,
                "save",
                autospec=True,
                side_effect=crash_after_prepared,
            ):
                with self.assertRaisesRegex(RuntimeError, "power loss"):
                    LegacyUpgrade(docker).upgrade(
                        config, plan, repository, migration_values
                    )

            journal_store = MigrationJournalStore(Path(config.state_dir))
            interrupted = journal_store.load()
            self.assertEqual(interrupted["status"], "prepared")
            partial = Path(interrupted["snapshot_work_path"])
            partial.mkdir()
            (partial / "copied-before-crash.bin").write_bytes(b"preserve me")

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered.status, "installed")
            completed = journal_store.load()
            self.assertEqual(completed["attempt"], 2)
            preserved = partial.with_name(f"{partial.name}.preserved")
            self.assertEqual(
                (preserved / "copied-before-crash.bin").read_bytes(),
                b"preserve me",
            )

    def test_prepared_crash_retry_failure_restores_originally_running_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            legacy = docker.containers["book-translator-v214-rollback"]
            snapshot = Path(config.backup_dir) / "interrupted-snapshot"
            target = Path(config.data_dir)
            MigrationJournalStore(Path(config.state_dir)).save({
                "status": "prepared",
                "legacy_container": "book-translator-v214-rollback",
                "legacy_container_id": legacy["Id"],
                "legacy_image_id": legacy["Image"],
                "legacy_image_ref": legacy["Config"]["Image"],
                "legacy_data_dir": migration_values["BT_LEGACY_DATA_DIR"],
                "legacy_initial_status": "running",
                "snapshot_path": str(snapshot),
                "snapshot_work_path": str(snapshot.with_name(f"{snapshot.name}.partial")),
                "target_data_dir": str(target),
                "target_work_path": str(
                    target.with_name(f".{target.name}.btctl-migration-r1.partial")
                ),
                "attempt": 1,
            })
            legacy["State"]["Status"] = "exited"

            with mock.patch(
                "btctl_lifecycle._sqlite_integrity",
                side_effect=InstallError("retry integrity failure"),
            ):
                with self.assertRaisesRegex(InstallError, "retry integrity"):
                    LegacyUpgrade(docker).upgrade(
                        config, plan, repository, migration_values
                    )

            self.assertEqual(legacy["State"]["Status"], "running")
            self.assertIn(
                ("start_container", "book-translator-v214-rollback"),
                docker.calls,
            )

    def test_prepared_retry_preflight_failure_restores_healthy_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            self.interrupted_migration_journal(
                config,
                docker,
                migration_values,
            )
            journal_store = MigrationJournalStore(Path(config.state_dir))
            before_journal = journal_store.load()
            legacy = docker.containers["book-translator-v214-rollback"]
            legacy["State"]["Status"] = "exited"

            with mock.patch.object(
                ComposeInstaller,
                "_preflight",
                side_effect=InstallError("retry preflight failure"),
            ):
                with self.assertRaisesRegex(InstallError, "retry preflight"):
                    LegacyUpgrade(docker).upgrade(
                        config, plan, repository, migration_values
                    )

            self.assertEqual(journal_store.load(), before_journal)
            self.assertEqual(legacy["State"]["Status"], "running")
            self.assertEqual(legacy["State"]["Health"]["Status"], "healthy")
            self.assertIn(
                ("wait_healthy", ("book-translator-v214-rollback",), 90),
                docker.calls,
            )
            self.assertNotIn("build_image", {call[0] for call in docker.calls})

    def test_upgrade_recovers_journal_only_snapshot_complete_cutover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )
            journal_store = MigrationJournalStore(Path(config.state_dir))
            interrupted = journal_store.load()
            interrupted["status"] = "snapshot-complete"
            interrupted.pop("install_id", None)
            journal_store.save(interrupted)
            StateStore(Path(config.state_dir)).path.unlink()
            for role in ("api", "proxy"):
                docker.containers.pop(str(plan.resources[role]["name"]), None)
            docker.networks.pop(str(plan.resources["private_network"]["name"]), None)

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered.status, "installed")
            completed = journal_store.load()
            self.assertEqual(completed["status"], "upgraded")
            self.assertEqual(completed["attempt"], 2)
            preserved_target = Path(config.data_dir).with_name(
                f".{Path(config.data_dir).name}.btctl-migration-r1.preserved"
            )
            self.assertTrue((preserved_target / "translations.db").is_file())

    def test_upgrade_adopts_live_runtime_after_state_commit_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )
            journal_store = MigrationJournalStore(Path(config.state_dir))
            interrupted = journal_store.load()
            interrupted["status"] = "snapshot-complete"
            interrupted.pop("install_id", None)
            journal_store.save(interrupted)
            StateStore(Path(config.state_dir)).path.unlink()
            target = Path(config.data_dir)
            before_manifest = _tree_manifest(target)
            before_calls = len(docker.calls)

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered.status, "adopted")
            self.assertEqual(_tree_manifest(target), before_manifest)
            self.assertEqual(journal_store.load()["status"], "upgraded")
            preserved_target = target.with_name(
                f".{target.name}.btctl-migration-r1.preserved"
            )
            self.assertFalse(preserved_target.exists())
            mutation_calls = {
                call[0]
                for call in docker.calls[before_calls:]
                if call[0]
                in {
                    "build_image",
                    "compose_up",
                    "compose_down",
                    "stop_container",
                    "start_container",
                }
            }
            self.assertEqual(mutation_calls, set())

    def test_upgrade_revalidates_exact_legacy_identity_after_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            legacy_name = migration_values["BT_LEGACY_CONTAINER"]
            original_stop = docker.stop_container

            def replace_during_stop(name):
                original_stop(name)
                docker.containers[name]["Id"] = "replacement-container-id"
                docker.containers[name]["Image"] = "sha256:replacement-image-id"

            with mock.patch.object(
                docker, "stop_container", side_effect=replace_during_stop
            ):
                with self.assertRaises(InstallError):
                    LegacyUpgrade(docker).upgrade(
                        config, plan, repository, migration_values
                    )

            call_names = [call[0] for call in docker.calls]
            self.assertNotIn("prepare_migration_source", call_names)
            self.assertNotIn("build_image", call_names)
            self.assertNotIn("compose_up", call_names)
            self.assertEqual(
                docker.containers[legacy_name]["Id"],
                "replacement-container-id",
            )

    def test_reupgrade_retry_retains_the_last_valid_rollback_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )
            LegacyUpgrade(docker).rollback(config, plan)

            with mock.patch.object(
                docker,
                "stop_container",
                side_effect=InstallError("transient re-upgrade stop failure"),
            ):
                with self.assertRaisesRegex(InstallError, "transient"):
                    LegacyUpgrade(docker).upgrade(
                        config, plan, repository, migration_values
                    )

            failed = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(failed["status"], "reupgrade-failed")
            self.assertEqual(failed["prior_rollback"]["status"], "rolled_back")

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered.status, "installed")
            completed = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(completed["status"], "upgraded")
            self.assertEqual(completed["attempt"], 3)

    def test_reupgrade_recovers_rolled_back_prepared_journal_with_valid_prior_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(config, plan, repository, migration_values)
            LegacyUpgrade(docker).rollback(config, plan)
            journal_store = MigrationJournalStore(Path(config.state_dir))
            prior = journal_store.load()
            interrupted_attempt = int(prior["attempt"]) + 1
            self.interrupted_migration_journal(
                config,
                docker,
                migration_values,
                status="prepared",
                prior_rollback=prior,
                attempt=interrupted_attempt,
            )
            docker.containers["book-translator-v214-rollback"]["State"][
                "Status"
            ] = "exited"

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered.status, "installed")
            completed = journal_store.load()
            self.assertEqual(completed["status"], "upgraded")
            self.assertEqual(completed["attempt"], interrupted_attempt + 1)

    def test_reupgrade_recovers_rolled_back_snapshot_complete_journal_with_valid_prior_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(config, plan, repository, migration_values)
            LegacyUpgrade(docker).rollback(config, plan)
            journal_store = MigrationJournalStore(Path(config.state_dir))
            prior = journal_store.load()
            interrupted_attempt = int(prior["attempt"]) + 1
            self.interrupted_migration_journal(
                config,
                docker,
                migration_values,
                status="snapshot-complete",
                prior_rollback=prior,
                attempt=interrupted_attempt,
            )
            docker.containers["book-translator-v214-rollback"]["State"][
                "Status"
            ] = "exited"

            recovered = LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )

            self.assertEqual(recovered.status, "installed")
            completed = journal_store.load()
            self.assertEqual(completed["status"], "upgraded")
            self.assertEqual(completed["attempt"], interrupted_attempt + 1)

    def test_reupgrade_rejects_invalid_prior_rollback_before_docker_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(config, plan, repository, migration_values)
            LegacyUpgrade(docker).rollback(config, plan)
            journal_store = MigrationJournalStore(Path(config.state_dir))
            prior = journal_store.load()
            prior["status"] = "upgraded"
            self.interrupted_migration_journal(
                config,
                docker,
                migration_values,
                status="prepared",
                prior_rollback=prior,
                attempt=int(prior["attempt"]) + 1,
            )
            before = len(docker.calls)

            with self.assertRaisesRegex(InstallError, "prior rollback"):
                LegacyUpgrade(docker).upgrade(
                    config, plan, repository, migration_values
                )

            self.assertFalse(
                {"stop_container", "start_container", "build_image"}
                & {call[0] for call in docker.calls[before:]}
            )

    def test_active_snapshot_complete_state_can_roll_back_when_doctor_would_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )
            journal_store = MigrationJournalStore(Path(config.state_dir))
            interrupted = journal_store.load()
            interrupted["status"] = "snapshot-complete"
            interrupted.pop("install_id", None)
            journal_store.save(interrupted)
            docker.containers[plan.resources["api"]["name"]]["State"]["Health"][
                "Status"
            ] = "unhealthy"

            rolled_back = LegacyUpgrade(docker).rollback(config, plan)

            self.assertEqual(rolled_back.status, "rolled_back")
            self.assertEqual(journal_store.load()["status"], "rolled_back")

    def test_upgrade_integrity_failure_restarts_legacy_and_never_builds_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = InstallConfig.from_mapping(values(root), self.identity)
            plan = DeploymentPlan.from_config(config)
            legacy_data = root / "legacy-data"
            legacy_data.mkdir()
            (legacy_data / "translations.db").write_bytes(b"not a sqlite database")
            repository = root / "checkout"
            repository.mkdir()
            docker = LifecycleDocker()
            docker.containers["book-translator-v214-rollback"] = {
                "Id": "legacy-container-id",
                "Image": "sha256:legacy-image-id",
                "State": {"Status": "running"},
                "Config": {"Image": "local/book-translator:2.1.4", "Labels": {}},
                "Mounts": [{
                    "Type": "bind",
                    "Source": str(legacy_data),
                    "Destination": "/app/data",
                    "RW": True,
                }],
            }

            with self.assertRaisesRegex(InstallError, "integrity"):
                LegacyUpgrade(docker).upgrade(config, plan, repository, {
                    "BT_LEGACY_CONTAINER": "book-translator-v214-rollback",
                    "BT_LEGACY_DATA_DIR": str(legacy_data),
                })

            self.assertEqual(
                docker.containers["book-translator-v214-rollback"]["State"]["Status"],
                "running",
            )
            self.assertNotIn("build_image", [call[0] for call in docker.calls])
            journal = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(journal["status"], "upgrade-failed")

    def test_upgrade_rejects_a_read_only_legacy_data_bind_before_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root, read_write=False)
            )

            with self.assertRaisesRegex(InstallError, "bind mount"):
                LegacyUpgrade(docker).upgrade(
                    config, plan, repository, migration_values
                )

            self.assertFalse(
                {"stop_container", "build_image"}
                & {call[0] for call in docker.calls}
            )

    def test_rollback_waits_for_legacy_health_and_can_retry_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(
                config, plan, repository, migration_values
            )
            docker.fail_health = True

            with self.assertRaisesRegex(InstallError, "health"):
                LegacyUpgrade(docker, health_timeout_seconds=1).rollback(config, plan)

            self.assertEqual(
                MigrationJournalStore(Path(config.state_dir)).load()["status"],
                "rollback-failed",
            )
            self.assertEqual(
                StateStore(Path(config.state_dir)).load().status, "uninstalled"
            )

            docker.fail_health = False
            completed = LegacyUpgrade(
                docker, health_timeout_seconds=1
            ).rollback(config, plan)

            self.assertEqual(completed.status, "rolled_back")
            self.assertIn(
                ("wait_healthy", ("book-translator-v214-rollback",), 1),
                docker.calls,
            )

    def test_rollback_missing_v2_target_restores_healthy_legacy_and_marks_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(config, plan, repository, migration_values)
            shutil.rmtree(config.data_dir)

            completed = LegacyUpgrade(docker).rollback(config, plan)

            self.assertEqual(completed.status, "rolled_back")
            legacy = docker.containers["book-translator-v214-rollback"]
            self.assertEqual(legacy["State"]["Status"], "running")
            self.assertEqual(legacy["State"]["Health"]["Status"], "healthy")
            journal = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(journal["status"], "rolled_back")
            self.assertEqual(journal["target_reupgrade_status"], "unavailable")
            self.assertEqual(journal["target_reupgrade_reason"], "missing-or-unsafe")
            self.assertNotIn("target_manifest", journal)
            self.assertNotIn("target_files", journal)

    def test_rollback_corrupt_v2_target_restores_healthy_legacy_and_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(config, plan, repository, migration_values)
            target_database = Path(config.data_dir) / "translations.db"
            target_database.write_bytes(b"corrupt v2 target evidence")

            completed = LegacyUpgrade(docker).rollback(config, plan)

            self.assertEqual(completed.status, "rolled_back")
            self.assertEqual(target_database.read_bytes(), b"corrupt v2 target evidence")
            journal = MigrationJournalStore(Path(config.state_dir)).load()
            self.assertEqual(journal["target_reupgrade_status"], "unavailable")
            self.assertEqual(
                journal["target_reupgrade_reason"],
                "integrity-or-read-error",
            )
            legacy = docker.containers["book-translator-v214-rollback"]
            self.assertEqual(legacy["State"]["Status"], "running")
            self.assertEqual(legacy["State"]["Health"]["Status"], "healthy")

    def test_unavailable_target_blocks_reupgrade_before_legacy_stop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(config, plan, repository, migration_values)
            (Path(config.data_dir) / "translations.db").write_bytes(b"corrupt")
            LegacyUpgrade(docker).rollback(config, plan)
            before = len(docker.calls)

            with self.assertRaisesRegex(InstallError, "unavailable"):
                LegacyUpgrade(docker).upgrade(
                    config, plan, repository, migration_values
                )

            self.assertFalse(
                {"stop_container", "build_image"}
                & {call[0] for call in docker.calls[before:]}
            )
            self.assertEqual(
                docker.containers["book-translator-v214-rollback"]["State"][
                    "Status"
                ],
                "running",
            )

    def test_idempotent_rolled_back_call_rechecks_and_restores_legacy_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, plan, docker, migration_values, repository = (
                self.migration_fixture(root)
            )
            LegacyUpgrade(docker).upgrade(config, plan, repository, migration_values)
            rolled_back = LegacyUpgrade(docker).rollback(config, plan)
            legacy = docker.containers["book-translator-v214-rollback"]
            legacy["State"] = {"Status": "exited"}
            before = len(docker.calls)

            recovered = LegacyUpgrade(docker).rollback(config, plan)

            self.assertEqual(recovered, rolled_back)
            self.assertEqual(legacy["State"]["Status"], "running")
            self.assertEqual(legacy["State"]["Health"]["Status"], "healthy")
            self.assertIn(
                ("start_container", "book-translator-v214-rollback"),
                docker.calls[before:],
            )
            self.assertIn(
                ("wait_healthy", ("book-translator-v214-rollback",), 90),
                docker.calls[before:],
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
