"""Fail-closed contracts for operator-facing shell helpers."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _dispatcher_functions(*names):
    """Return the exact bash definitions of btctl helper functions."""
    source = (ROOT / "btctl").read_text(encoding="utf-8")
    blocks = []
    for name in names:
        match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^\}}\n", source)
        if match is None:
            raise AssertionError(f"btctl does not define {name}()")
        blocks.append(match.group(0))
    return "".join(blocks)


def _exclusive_share_root(directory, link="../cache/appdata"):
    storage_root = Path(directory) / "mnt"
    (storage_root / "cache" / "appdata" / "hub").mkdir(parents=True)
    (storage_root / "user").mkdir()
    (storage_root / "user" / "appdata").symlink_to(link)
    return storage_root


class ShellContractTests(unittest.TestCase):
    def test_bootstrap_files_are_forced_to_linux_line_endings(self):
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("btctl text eol=lf", attributes)
        self.assertIn("Dockerfile.btctl text eol=lf", attributes)

    def test_operator_helpers_are_executable(self):
        for name in (
            "btctl",
            "install_unraid.sh",
        ):
            self.assertTrue((ROOT / name).stat().st_mode & 0o111, name)

    def test_bash_helpers_enable_strict_mode(self):
        for name in (
            "btctl",
            "install_unraid.sh",
        ):
            source = (ROOT / name).read_text()
            self.assertIn("set -euo pipefail", source, name)

    def test_btctl_dispatches_to_python_or_a_hardened_local_operator(self):
        source = (ROOT / "btctl").read_text()
        self.assertEqual(source.splitlines()[0], "#!/usr/bin/env bash")
        for contract in (
            "command -v python3",
            "git --version",
            '"$SCRIPT_DIR/btctl.py"',
            "Dockerfile.btctl",
            "--target",
            "source-exporter",
            "--network",
            "none",
            "--read-only",
            "--pids-limit",
            "--cap-drop",
            "ALL",
            "no-new-privileges:true",
            "/var/run/docker.sock",
            "BTCTL_EXPECTED_REVISION",
            "BTCTL_EXPECTED_ENV_SHA256",
            "core.fsmonitor",
            "GIT_NO_REPLACE_OBJECTS=1",
            "source_exporter_dockerfile",
            "DAC_READ_SEARCH",
            "BTCTL_LOCK_DIRECTORY",
            "lock:ro",
            "DOCKER_HOST=unix:///var/run/docker.sock",
            "[ -S /var/run/docker.sock ]",
            "/run/cwa-translate-btctl-locks",
            'type=bind,src=$HOST_LOCK_DIRECTORY,dst=$HOST_LOCK_DIRECTORY,readonly',
            '[ "$second" = "$HOST_LOCK_DIRECTORY" ]',
            'environment_snapshot="$temporary/install.env.snapshot"',
            'cmp --silent -- "$temporary/mount-plan" "$temporary/final-mount-plan"',
            'final_operator_arguments+=(--env "$environment_snapshot")',
        ):
            self.assertIn(contract, source)
        self.assertNotRegex(source, r"(?m)^\s*(?:source|eval)\s")
        self.assertNotIn("image rm --force", source)
        self.assertNotIn(
            'cp -- "$SCRIPT_DIR/Dockerfile.btctl"',
            source,
        )
        self.assertIn('cp -P -- \\\n            "$env_file" "$environment_snapshot"', source)
        self.assertNotIn("--no-preserve=all", source)
        self.assertEqual(
            source.count('sha256sum -- "$environment_snapshot"'),
            2,
        )
        self.assertNotIn("sha256sum --binary", source)
        self.assertIn('chown 0:0 -- "$environment_snapshot"', source)
        self.assertIn('chmod 0600 -- "$environment_snapshot"', source)

    def test_python_without_git_selects_the_container_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            (tools / "dirname").symlink_to("/usr/bin/dirname")
            python = tools / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = -c ]; then exit 0; fi\n"
                "echo unexpected-native-path >&2\n"
                "exit 42\n",
                encoding="utf-8",
            )
            python.chmod(0o755)

            completed = subprocess.run(
                ["/bin/bash", str(ROOT / "btctl"), "--help"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": str(tools),
                    "BTCTL_DOCKER_BIN": "missing-docker-for-contract",
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(completed.returncode, 42)
        self.assertNotIn("unexpected-native-path", completed.stderr)
        self.assertRegex(
            completed.stderr,
            r"containerized Unraid path must run as root|Docker is required",
        )

    def test_native_dispatch_clears_container_only_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            tools = Path(directory)
            (tools / "dirname").symlink_to("/usr/bin/dirname")
            (tools / "git").symlink_to("/usr/bin/git")
            python = tools / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = -c ]; then exit 0; fi\n"
                "[ -z \"${BTCTL_EXPECTED_REVISION:-}\" ] || exit 41\n"
                "[ -z \"${BTCTL_OPERATOR_REVISION:-}\" ] || exit 41\n"
                "[ -z \"${BTCTL_LOCK_DIRECTORY:-}\" ] || exit 41\n"
                "exit 0\n",
                encoding="utf-8",
            )
            python.chmod(0o755)

            completed = subprocess.run(
                ["/bin/bash", str(ROOT / "btctl"), "--help"],
                cwd=ROOT,
                env={
                    **os.environ,
                    "PATH": str(tools),
                    "BTCTL_EXPECTED_REVISION": "a" * 40,
                    "BTCTL_OPERATOR_REVISION": "b" * 40,
                    "BTCTL_LOCK_DIRECTORY": "/tmp/untrusted-lock",
                },
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_retired_personal_unraid_helpers_are_absent(self):
        for name in ("deploy_unraid.sh", "verify_unraid.sh"):
            self.assertFalse((ROOT / name).exists(), name)

    def test_install_is_a_root_only_btctl_wrapper(self):
        source = (ROOT / "install_unraid.sh").read_text()
        self.assertIn('if [ "$#" -ne 1 ]', source)
        self.assertIn('"${EUID:-$(id -u)}" -ne 0', source)
        self.assertIn('"$SCRIPT_DIR/btctl" --repository "$SCRIPT_DIR" plan', source)
        self.assertIn('install --env "$ENV_FILE" --yes', source)
        self.assertNotRegex(source, r"\bread\s+-r(?:\s+-p)?\s+")
        self.assertNotIn("CWA_PATH", source)
        self.assertNotIn("overlay/read.html", source)
        self.assertNotIn("docker build", source)
        self.assertNotIn("docker pull", source)
        self.assertNotIn("ghcr.io", source)


class DispatcherExclusiveShareTests(unittest.TestCase):
    FUNCTIONS = (
        "fail",
        "unraid_exclusive_share_source",
        "canonical_existing_file",
        "validate_planned_destination",
    )

    def _run(self, storage_root, script, *arguments):
        prelude = (
            "set -euo pipefail\n"
            "EX_USAGE=64\n"
            'UNRAID_STORAGE_ROOT="$1"\n'
            "shift\n"
        )
        return subprocess.run(
            [
                "/bin/bash",
                "-c",
                prelude + _dispatcher_functions(*self.FUNCTIONS) + script,
                "btctl-test",
                str(storage_root),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_exclusive_share_source_maps_only_the_exact_unraid_link(self):
        with tempfile.TemporaryDirectory() as directory:
            storage_root = _exclusive_share_root(directory)
            (storage_root / "user" / "media").mkdir()
            cases = {
                storage_root / "user" / "appdata" / "hub" / "x.env": str(
                    storage_root / "cache" / "appdata" / "hub" / "x.env"
                ),
                storage_root / "user" / "appdata": str(storage_root / "cache" / "appdata"),
                storage_root / "user" / "media" / "x.env": None,
                storage_root / "cache" / "appdata" / "x.env": None,
                storage_root / "user" / ".." / "cache": None,
            }
            for candidate, expected in cases.items():
                with self.subTest(candidate=candidate):
                    completed = self._run(
                        storage_root, 'unraid_exclusive_share_source "$1"', str(candidate)
                    )
                    if expected is None:
                        self.assertNotEqual(completed.returncode, 0)
                        self.assertEqual(completed.stdout, "")
                    else:
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                        self.assertEqual(completed.stdout, expected + "\n")

    def test_share_links_other_than_the_exact_unraid_form_are_not_followed(self):
        def linked_pool(root):
            (root / "real-cache").mkdir()
            (root / "cache" / "appdata").rename(root / "real-cache" / "appdata")
            (root / "cache").rmdir()
            (root / "cache").symlink_to("real-cache")

        def linked_share(root):
            (root / "cache" / "appdata").rename(root / "cache" / "elsewhere")
            (root / "cache" / "appdata").symlink_to("elsewhere")

        cases = (
            ("absolute target", None, None),
            ("other share", "../cache/other", lambda root: (root / "cache" / "other").mkdir()),
            ("reserved root", "../user0/appdata", lambda root: (root / "user0" / "appdata").mkdir(parents=True)),
            ("parent escape", "../../cache/appdata", None),
            ("non-parent prefix", "media/cache/appdata", None),
            ("bare pool name", "cache", None),
            ("trailing slash", "../cache/appdata/", None),
            ("dot component", "../cache/./appdata", None),
            ("nested pool", "../cache/sub/appdata", lambda root: (root / "cache" / "sub" / "appdata").mkdir(parents=True)),
            ("missing pool", "../missing/appdata", None),
            ("linked pool", "../cache/appdata", linked_pool),
            ("linked share directory", "../cache/appdata", linked_share),
        )
        for name, link, prepare in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                target = link or str(Path(directory) / "mnt" / "cache" / "appdata")
                storage_root = _exclusive_share_root(directory, target)
                if prepare is not None:
                    prepare(storage_root)

                completed = self._run(
                    storage_root,
                    'unraid_exclusive_share_source "$1"',
                    str(storage_root / "user" / "appdata" / "hub" / "x.env"),
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, "")

    def test_environment_file_may_live_below_an_exclusive_share(self):
        with tempfile.TemporaryDirectory() as directory:
            storage_root = _exclusive_share_root(directory)
            environment = storage_root / "cache" / "appdata" / "hub" / "install.env"
            environment.write_text("BT_INSTALL_PROFILE=unraid\n", encoding="utf-8")
            (storage_root / "user" / "linked").symlink_to("../cache/appdata")

            accepted = self._run(
                storage_root,
                'canonical_existing_file "$1" "environment file"',
                str(storage_root / "user" / "appdata" / "hub" / "install.env"),
            )
            (storage_root / "cache" / "appdata" / "inner").symlink_to("hub")
            rejected = {
                "non-exclusive share link": self._run(
                    storage_root,
                    'canonical_existing_file "$1" "environment file"',
                    str(storage_root / "user" / "linked" / "hub" / "install.env"),
                ),
                "link below the pool share": self._run(
                    storage_root,
                    'canonical_existing_file "$1" "environment file"',
                    str(storage_root / "user" / "appdata" / "inner" / "install.env"),
                ),
            }

        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(accepted.stdout, f"{environment}\n")
        for name, completed in rejected.items():
            with self.subTest(case=name):
                self.assertEqual(completed.returncode, 64)
                self.assertIn("without symbolic components", completed.stderr)

    def test_planned_destination_must_be_the_exclusive_alias_of_its_source(self):
        with tempfile.TemporaryDirectory() as directory:
            storage_root = _exclusive_share_root(directory)
            pool_hub = storage_root / "cache" / "appdata" / "hub"
            (pool_hub / "data").mkdir()
            (storage_root / "cache" / "appdata" / "other").mkdir()
            alias_hub = storage_root / "user" / "appdata" / "hub"
            cases = (
                ("same path", pool_hub / "data", pool_hub / "data", True),
                ("exclusive alias", pool_hub / "data", alias_hub / "data", True),
                ("alias of another source", pool_hub / "data", storage_root / "user" / "appdata" / "other", False),
                ("unrelated destination", pool_hub / "data", Path(directory) / "etc", False),
                ("parent traversal", pool_hub / "data", alias_hub / "data" / ".." / "data", False),
                ("mount delimiter", pool_hub / "data", alias_hub / "da,ta", False),
                ("tab", pool_hub / "data", alias_hub / "da\tta", False),
                ("newline", pool_hub / "data", alias_hub / "da\nta", False),
            )
            for name, source, destination, valid in cases:
                with self.subTest(case=name):
                    completed = self._run(
                        storage_root,
                        'validate_planned_destination "$1" "$2"',
                        str(source),
                        str(destination),
                    )
                    if valid:
                        self.assertEqual(completed.returncode, 0, completed.stderr)
                    else:
                        self.assertEqual(completed.returncode, 1)
                        self.assertIn("mount planner emitted", completed.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
