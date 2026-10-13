"""Static contracts preventing required CI gates from degrading to skipped."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GITHUB_CI = ROOT / ".github" / "workflows" / "ci.yml"
GITEA_CI = ROOT / ".gitea" / "workflows" / "ci.yml"
RELEASE = ROOT / ".gitea" / "workflows" / "release.yml"
PUBLISH_IMAGE = ROOT / ".github" / "workflows" / "publish-image.yml"
DOCKER_NAMES = ROOT / "scripts" / "ci-docker-names.sh"
HUB_LIFECYCLE_SMOKE = ROOT / "scripts" / "hub-btctl-lifecycle-smoke.sh"
FRONTEND_WORKFLOWS = (
    GITHUB_CI,
    GITEA_CI,
    ROOT / ".gitea" / "workflows" / "release.yml",
)
BACKEND_WORKFLOWS = (GITHUB_CI, GITEA_CI, RELEASE)
PY_COMPILE_COMMAND = (
    "git ls-files -z -- '*.py' | xargs -0 -r python3 -m py_compile"
)
BACKEND_DISCOVERY_COMMAND = (
    "python3 -m coverage run --branch --source=. "
    "--omit='.venv/*,tests/*,tools/*' -m unittest discover -v -s tests/python -t ."
)
COVERAGE_COMMAND = (
    "python3 -m coverage report --precision=1 --show-missing "
    "--fail-under=\"$BT_COVERAGE_FAIL_UNDER\""
)


class CIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = GITHUB_CI.read_text()

    def test_suite_database_stays_outside_the_checkout(self):
        # Discovery must import tests.python as a package so its __init__
        # redirects DB_PATH; top-level discovery wrote translations.db here.
        import cache

        database = Path(cache.DB_PATH).resolve()
        self.assertFalse(database.is_relative_to(ROOT), database)

    def test_all_backend_contract_suites_are_required(self):
        for command in (
            "python3 -m tests.python.test_translation",
            "python3 -m tests.python.test_hardening",
            "python3 -m coverage erase",
            BACKEND_DISCOVERY_COMMAND,
            COVERAGE_COMMAND,
            PY_COMPILE_COMMAND,
            "bash -n btctl install_unraid.sh scripts/*.sh",
        ):
            self.assertIn(command, self.workflow)
        self.assertIn("BT_COVERAGE_FAIL_UNDER: \"60\"", self.workflow)

    def test_release_repeats_the_install_and_lifecycle_gates(self):
        workflow = RELEASE.read_text()
        self.assertIn(PY_COMPILE_COMMAND, workflow)
        self.assertIn(BACKEND_DISCOVERY_COMMAND, workflow)

    def test_reader_session_boundary_is_required_everywhere(self):
        for workflow in BACKEND_WORKFLOWS:
            source = workflow.read_text()
            with self.subTest(workflow=workflow):
                self.assertIn(PY_COMPILE_COMMAND, source)
                self.assertIn(BACKEND_DISCOVERY_COMMAND, source)

    def test_node_install_and_audit_include_locked_dev_tree(self):
        self.assertRegex(self.workflow, r"(?m)^\s*- run: npm ci\s*$")
        self.assertNotRegex(self.workflow, r"(?m)^\s*- run: npm install\s*$")
        self.assertRegex(
            self.workflow, r"(?m)^\s*- run: npm audit --audit-level=high\s*$")
        self.assertNotIn("npm audit --omit=dev", self.workflow)

    def test_frontend_browser_gate_is_required(self):
        for workflow in FRONTEND_WORKFLOWS:
            source = workflow.read_text()
            self.assertRegex(
                source,
                r"(?m)^\s*- run: npx playwright install --with-deps --only-shell chromium\s*$",
            )
            self.assertRegex(source, r"(?m)^\s*- run: npm run test:e2e\s*$")
            self.assertNotIn("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", source)

    def test_docker_gate_cannot_report_success_when_docker_is_missing(self):
        workflows = {
            GITHUB_CI: "ubuntu-latest",
            GITEA_CI: "weebdb-docker",
        }
        for path, runner in workflows.items():
            workflow = path.read_text()
            self.assertNotIn("Detect Docker", workflow)
            self.assertNotIn("docker.outputs.available", workflow)
            self.assertNotIn("skipping docker-smoke", workflow)
            self.assertRegex(
                workflow,
                rf"(?m)^  docker-smoke:\n    runs-on: {runner}$",
            )
            self.assertRegex(workflow, r"(?m)^\s*run: docker version\s*$")
            self.assertRegex(workflow, r"(?m)^\s*run: docker build ")

    def test_docker_smoke_exercises_the_built_proxy_path(self):
        self.assertIn("./scripts/container-smoke.sh", self.workflow)
        self.assertIn("sh scripts/ci-docker-names.sh", self.workflow)
        self.assertNotIn("bt-smoke-${{ github.run_id }}", self.workflow)
        self.assertNotIn(
            "bt-audit:${{ github.run_id }}-${{ github.run_attempt }}",
            self.workflow,
        )
        self.assertIn('docker build -t "$SMOKE_IMAGE" .', self.workflow)
        self.assertIn('./scripts/container-smoke.sh "$SMOKE_IMAGE" "$SMOKE_PREFIX"', self.workflow)
        self.assertIn(
            './scripts/btctl-lifecycle-smoke.sh "$SMOKE_IMAGE" "$SMOKE_PREFIX"',
            self.workflow,
        )
        self.assertIn(
            './scripts/btctl-bootstrap-smoke.sh "$SMOKE_PREFIX"',
            self.workflow,
        )
        self.assertIn(
            './scripts/ca-container-smoke.sh "$SMOKE_IMAGE" "$SMOKE_PREFIX-ca"',
            self.workflow,
        )
        self.assertIn(
            './scripts/hub-container-smoke.sh "$SMOKE_IMAGE" "$SMOKE_PREFIX-hub"',
            self.workflow,
        )
        managed_hub_smoke = (
            './scripts/hub-btctl-lifecycle-smoke.sh '
            '"$SMOKE_IMAGE" "$SMOKE_PREFIX-hl"'
        )
        for workflow in (GITHUB_CI, GITEA_CI, RELEASE):
            with self.subTest(workflow=workflow):
                self.assertIn(managed_hub_smoke, workflow.read_text())
        self.assertNotIn("docker build -t bt-audit:ci", self.workflow)

    def test_docker_names_are_isolated_across_repositories(self):
        def derive(repository, run_id="4242", run_attempt="1"):
            with tempfile.TemporaryDirectory() as temp_dir:
                env_file = Path(temp_dir) / "github-env"
                env = os.environ.copy()
                env.update({
                    "GITHUB_REPOSITORY": repository,
                    "GITHUB_RUN_ID": run_id,
                    "GITHUB_RUN_ATTEMPT": run_attempt,
                    "GITHUB_ENV": str(env_file),
                })
                subprocess.run(
                    ["sh", str(DOCKER_NAMES)],
                    cwd=ROOT,
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return dict(
                    line.split("=", 1)
                    for line in env_file.read_text().splitlines()
                )

        first = derive("felix/CWA-translate-plugin")
        second = derive("another-owner/another-repository")
        repeated = derive("felix/CWA-translate-plugin")
        next_run = derive("felix/CWA-translate-plugin", run_id="4243")
        next_attempt = derive("felix/CWA-translate-plugin", run_attempt="2")
        long_identifiers = derive(
            "felix/CWA-translate-plugin",
            run_id="9" * 200,
            run_attempt="8" * 200,
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first["SMOKE_PREFIX"], second["SMOKE_PREFIX"])
        self.assertNotEqual(first["SMOKE_IMAGE"], second["SMOKE_IMAGE"])
        self.assertNotEqual(first["SMOKE_PREFIX"], next_run["SMOKE_PREFIX"])
        self.assertNotEqual(
            first["SMOKE_PREFIX"], next_attempt["SMOKE_PREFIX"])
        self.assertRegex(
            first["SMOKE_PREFIX"], r"^bt-ci-[0-9a-f]{20}$")
        self.assertRegex(
            first["SMOKE_IMAGE"], r"^bt-audit:[0-9a-f]{20}$")
        self.assertRegex(
            long_identifiers["SMOKE_PREFIX"], r"^bt-ci-[0-9a-f]{20}$")
        self.assertLessEqual(len(long_identifiers["SMOKE_PREFIX"]), 49)

    def test_derived_ci_prefix_fits_the_managed_hub_smoke_contract(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "github-env"
            env = os.environ.copy()
            env.update({
                "GITHUB_REPOSITORY": "felix/CWA-translate-plugin",
                "GITHUB_RUN_ID": "4242",
                "GITHUB_RUN_ATTEMPT": "1",
                "GITHUB_ENV": str(env_file),
            })
            subprocess.run(
                ["sh", str(DOCKER_NAMES)],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            generated = dict(
                line.split("=", 1) for line in env_file.read_text().splitlines()
            )
            derived = generated["SMOKE_PREFIX"] + "-hl"
            env["BT_SMOKE_VALIDATE_PREFIX_ONLY"] = "1"
            validated = subprocess.run(
                [str(HUB_LIFECYCLE_SMOKE), generated["SMOKE_IMAGE"], derived],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(validated.returncode, 0, validated.stderr)
        self.assertLessEqual(len(derived), 49)

    def test_package_lock_root_metadata_matches_package_manifest(self):
        package = json.loads((ROOT / "package.json").read_text())
        lock = json.loads((ROOT / "package-lock.json").read_text())
        root = lock["packages"][""]
        for key in ("name", "version", "license"):
            self.assertEqual(lock.get(key, root.get(key)), package[key])
            self.assertEqual(root[key], package[key])
        self.assertIs(package["private"], True)
        self.assertNotIn("main", package)
        self.assertEqual(
            package["scripts"]["test"],
            "node tests/frontend/test_frontend.js",
        )

    def test_required_steps_have_no_continue_on_error(self):
        self.assertNotIn("continue-on-error", self.workflow)

    def test_ghcr_publication_is_manual_exact_and_fail_closed(self):
        workflow = PUBLISH_IMAGE.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertNotRegex(workflow, r"(?m)^\s+(?:push|pull_request):")
        for token in (
            "release_tag:",
            "release_sha:",
            "packages: write",
            "GITHUB_TOKEN",
            "scripts/release_preflight.py",
            "linux/amd64",
            "aquasec/trivy:0.69.3@sha256:7228e304ae0f610a1fad937baa463598cadac0c2ac4027cc68f3a8b997115689",
            "--severity HIGH,CRITICAL",
            "--sbom=true",
            "--provenance=mode=max",
            "ca-container-smoke.sh",
            "docker manifest inspect",
            "path: trusted",
            "path: candidate",
            "trusted/scripts/release_preflight.py",
            "--metadata-file",
            "containerimage.digest",
            "attestation-manifest",
            "docker logout ghcr.io",
            "GHCR_DIGEST=",
        ):
            self.assertIn(token, workflow)
        self.assertIn('"$IMAGE:$VERSION"', workflow)
        self.assertNotIn('"$IMAGE:latest"', workflow)
        self.assertNotIn("RepoDigests", workflow)
        self.assertGreaterEqual(workflow.count("--severity HIGH,CRITICAL"), 2)
        self.assertGreaterEqual(workflow.count("hub-container-smoke.sh"), 2)
        self.assertIn('manifest_output="$(docker manifest inspect', workflow)
        self.assertRegex(workflow, r"(?m)^  validate:\n    runs-on: ubuntu-latest$")
        self.assertRegex(
            workflow,
            r"(?m)^  publish:\n    needs: validate\n    runs-on: ubuntu-latest$",
        )
        self.assertEqual(workflow.count("packages: write"), 1)
        self.assertGreaterEqual(workflow.count("persist-credentials: false"), 3)
        self.assertIn("ref: ${{ needs.validate.outputs.release_sha }}", workflow)
        self.assertLess(
            workflow.index("trusted/scripts/release_preflight.py"),
            workflow.index("packages: write"),
        )
        self.assertLess(
            workflow.index("Smoke the local candidate"),
            workflow.index("Log in only for the immutable push"),
        )
        self.assertLess(
            workflow.index("Remove registry credentials before verification"),
            workflow.index("Validate the published index and attestations anonymously"),
        )
        for explicit_absence in (
            "manifest unknown",
            "name unknown",
            "no such manifest",
        ):
            self.assertIn(explicit_absence, workflow)
        self.assertIn("could not prove the immutable version tag is absent", workflow)
        for forbidden in ("CR_PAT", "GHCR_PAT", "DOCKERHUB", "continue-on-error"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
