"""Fail-closed contracts for immutable third-party build inputs."""
import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "ci.yml",
    ROOT / ".gitea" / "workflows" / "ci.yml",
    ROOT / ".gitea" / "workflows" / "release.yml",
)
ACTION_WORKFLOWS = WORKFLOWS + (
    ROOT / ".github" / "workflows" / "publish-image.yml",
)

PINNED_ACTIONS = {
    "actions/checkout": (
        "34e114876b0b11c390a56381ad16ebd13914f8d5",
        "v4.3.1",
    ),
    "actions/setup-node": (
        "49933ea5288caeca8642d1e84afbd3f7d6820020",
        "v4.4.0",
    ),
    "docker/setup-buildx-action": (
        "8d2750c68a42422c14e847fe6c8ac0403b4cbd6f",
        "v3.12.0",
    ),
}

BASE_IMAGE = (
    "python:3.11-alpine@"
    "sha256:25976e9d34a0fab1f278cae931f34c8303d97bf0c0d7f85b6b4dcf641d7702a4"
)
OPERATOR_DOCKERFILE = ROOT / "Dockerfile.btctl"
NODE_VERSION = "24.18.0"
APK_PACKAGES = {
    "libgomp": "15.2.0-r5",
    "libxml2": "2.13.9-r2",
    "nginx": "1.30.4-r1",
    "pcre2": "10.48-r0",
}

USES_LINE = re.compile(
    r"(?m)^\s*-?\s*uses:\s*"
    r"(?P<action>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@"
    r"(?P<revision>[0-9a-f]{40})\s+#\s+(?P<version>v\d+(?:\.\d+){0,2})\s*$"
)


class SupplyChainContractTests(unittest.TestCase):
    def test_every_external_action_is_pinned_to_the_reviewed_commit(self):
        for workflow in ACTION_WORKFLOWS:
            source = workflow.read_text()
            uses_lines = [line for line in source.splitlines() if "uses:" in line]
            matches = list(USES_LINE.finditer(source))
            self.assertEqual(
                len(matches),
                len(uses_lines),
                f"{workflow}: every uses line needs a 40-hex commit and version comment",
            )
            for match in matches:
                action = match.group("action")
                self.assertIn(action, PINNED_ACTIONS, f"{workflow}: unreviewed action {action}")
                self.assertEqual(
                    (match.group("revision"), match.group("version")),
                    PINNED_ACTIONS[action],
                    f"{workflow}: unexpected pin for {action}",
                )

    def test_gitea_and_github_ci_only_diverge_on_docker_runner(self):
        def normalize_provider_runner(source: str) -> str:
            source = re.sub(
                r"(?ms)^  # Provider-specific Docker runner:\n"
                r"(?:  #[^\n]*\n)+(?=  docker-smoke:)",
                "",
                source,
            )
            return re.sub(
                r"(?m)(^  docker-smoke:\n)    runs-on: "
                r"(?:ubuntu-latest|weebdb-docker)$",
                r"\1    runs-on: PROVIDER_DOCKER_RUNNER",
                source,
            )

        github_ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        gitea_ci = (ROOT / ".gitea" / "workflows" / "ci.yml").read_text()
        self.assertEqual(
            normalize_provider_runner(gitea_ci),
            normalize_provider_runner(github_ci),
        )

    def test_supply_chain_contract_is_a_required_backend_gate(self):
        for workflow in WORKFLOWS:
            self.assertIn(
                "-m unittest discover -v tests/python",
                workflow.read_text(),
                f"{workflow}: every discovered contract must run in CI",
            )

    def test_container_base_and_operating_system_packages_are_immutable(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertEqual(dockerfile.splitlines()[0], f"FROM {BASE_IMAGE}")
        self.assertEqual(dockerfile.count("apk add"), 1)
        flattened = dockerfile.replace("\\\n", " ")
        apk_add = re.search(r"RUN apk add --no-cache\s+([^\n]+)", flattened)
        self.assertIsNotNone(apk_add)
        installed = dict(token.split("=", 1) for token in apk_add.group(1).split())
        self.assertEqual(installed, APK_PACKAGES)

    def test_operator_image_uses_reviewed_pinned_inputs(self):
        dockerfile = OPERATOR_DOCKERFILE.read_text()
        dispatcher = (ROOT / "btctl").read_text()
        self.assertEqual(
            dockerfile.splitlines()[0], f"FROM {BASE_IMAGE} AS source-exporter"
        )
        flattened = dockerfile.replace("\\\n", " ")
        additions = re.findall(r"RUN apk add --no-cache\s+([^\n]+)", flattened)
        self.assertEqual(len(additions), 3)
        installed = {
            token.split("=", 1)[0]: token.split("=", 1)[1]
            for addition in additions
            for token in addition.split()
        }
        self.assertEqual(installed["git"], "2.54.0-r0")
        self.assertEqual(installed["docker-cli"], "29.5.3-r1")
        self.assertEqual(installed["docker-cli-buildx"], "0.34.1-r1")
        self.assertEqual(installed["bash"], "5.3.9-r1")
        self.assertTrue(all("=" in token for addition in additions for token in addition.split()))
        self.assertIn(BASE_IMAGE, dispatcher)
        for package, version in installed.items():
            if package not in {"docker-cli", "docker-cli-buildx", "bash"}:
                self.assertIn(f"{package}={version}", dispatcher)
        self.assertNotIn("pip install", dockerfile)
        self.assertNotIn("COPY . ", dockerfile)
        self.assertNotIn("COPY *.py", dockerfile)
        for source in (
            "btctl.py",
            "btctl_container.py",
            "btctl_paths.py",
            "VERSION",
            "deploy/unraid/my-cwa-translate-api.xml.tmpl",
            "deploy/unraid/my-cwa-translate-proxy.xml.tmpl",
        ):
            self.assertIn(source, dockerfile)

    def test_embedded_exporter_is_exactly_the_reviewed_socket_free_stage(self):
        dockerfile = OPERATOR_DOCKERFILE.read_text()
        dispatcher = (ROOT / "btctl").read_text()
        function = re.search(
            r"(?ms)^source_exporter_dockerfile\(\) \{\n(?P<body>.*?)^\}",
            dispatcher,
        )
        self.assertIsNotNone(function)
        embedded_lines = []
        for line in function.group("body").splitlines():
            literal = re.fullmatch(r"\s*'(?P<text>.*)'(?: \\)?", line)
            if literal:
                embedded_lines.append(literal.group("text"))
        embedded = "\n".join(embedded_lines) + "\n"

        exporter_stage = dockerfile.split(
            "\nFROM source-exporter AS operator", 1
        )[0]
        reviewed = "\n".join(
            line
            for line in exporter_stage.splitlines()
            if line and not line.startswith("#")
        ) + "\n"

        self.assertEqual(embedded, reviewed)
        for forbidden in ("COPY ", "ADD ", "ENTRYPOINT", "CMD "):
            self.assertNotIn(forbidden, embedded)

    def test_runtime_copy_inputs_are_exact_files_not_open_directories(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("COPY static/ ./static/", dockerfile)
        self.assertNotIn("COPY proxy/ ./proxy/", dockerfile)
        self.assertIn(
            "COPY static/loader.js static/translator.css static/translator.js ./static/",
            dockerfile,
        )
        self.assertIn(
            "COPY proxy/nginx-main.conf proxy/nginx.conf.template "
            "proxy/render_config.py ./proxy/",
            dockerfile,
        )

    def test_docker_context_excludes_local_state_and_backups(self):
        dockerignore = (ROOT / ".dockerignore").read_text().splitlines()
        for path in ("backups/", "data/", "config/translator/"):
            self.assertIn(path, dockerignore)

    def test_python_installs_require_the_reviewed_hashes_and_wheels(self):
        expected_install = (
            "python3 -m pip install --break-system-packages "
            "--require-hashes --only-binary=:all: "
            "-r requirements/requirements.txt"
        )
        expected_tools = (
            "python3 -m pip install --break-system-packages "
            "--require-hashes --only-binary=:all: "
            "-r requirements/requirements-audit.txt"
        )
        for workflow in WORKFLOWS:
            source = workflow.read_text()
            self.assertIn(expected_install, source)
            self.assertIn(expected_tools, source)
            self.assertIn(
                "python3 -m pip_audit -r requirements/requirements.txt "
                "--strict --disable-pip --no-deps",
                source,
            )
            self.assertNotIn("piptools compile", source)
            self.assertNotIn("requirements-pinned.txt", source)

        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertIn(
            "COPY requirements/requirements.txt ./requirements.txt",
            dockerfile,
        )
        self.assertIn(
            "pip install --no-cache-dir --require-hashes "
            "--only-binary=:all: -r requirements.txt",
            dockerfile,
        )
        # Runtime image must not retain packaging tooling after install (Trivy).
        self.assertIn("pip uninstall -y setuptools wheel pip", dockerfile)

    def test_python_lock_files_pin_and_hash_every_dependency(self):
        for name in (
            "requirements.txt",
            "requirements-audit.txt",
            "requirements-compile.txt",
        ):
            lock = (ROOT / "requirements" / name).read_text()
            logical_lock = lock.replace("\\\n", " ")
            requirements = re.findall(
                r"(?m)^([a-z0-9][a-z0-9_.-]*)==([^\s]+)([^\n]*)",
                logical_lock,
            )
            self.assertGreater(len(requirements), 2, f"{name}: lock is unexpectedly small")
            for package, _version, options in requirements:
                self.assertIn(
                    "--hash=sha256:", options, f"{name}: {package} has no sha256 hash"
                )
            self.assertNotRegex(lock, r"(?m)^[a-z0-9_.-]+\s*(?:[<>~!]=?|===)")
            self.assertNotIn("--extra-index-url", lock)
            self.assertNotIn("--trusted-host", lock)
            self.assertNotRegex(lock, r"(?m)^[a-z0-9_.-]+\s*@\s*")

    def test_direct_runtime_imports_are_declared_as_direct_dependencies(self):
        intent = (ROOT / "requirements" / "requirements.in").read_text()
        self.assertIn("requests>=2.31.0,<3.0", intent)
        self.assertIn("urllib3>=2.0,<3.0", intent)

    def test_lock_regeneration_is_pinned_and_uses_public_pypi(self):
        compiler = (ROOT / "scripts" / "compile-requirements.sh").read_text()
        compiler_intent = (
            ROOT / "requirements" / "requirements-compile.in"
        ).read_text()
        compiler_lock = (
            ROOT / "requirements" / "requirements-compile.txt"
        ).read_text()
        self.assertIn('EXPECTED_PYTHON="3.11"', compiler)
        self.assertIn('EXPECTED_PIP_COMPILE="7.5.3"', compiler)
        self.assertIn("PIP_CONFIG_FILE=/dev/null", compiler)
        self.assertIn("PIP_INDEX_URL=https://pypi.org/simple", compiler)
        self.assertIn('cd "$(dirname "$0")/../requirements"', compiler)
        for option in (
            "--generate-hashes",
            "--resolver=backtracking",
            "--no-emit-index-url",
            "--no-emit-trusted-host",
        ):
            self.assertIn(option, compiler)
        self.assertIn("requirements.in", compiler)
        self.assertIn("requirements-audit.in", compiler)
        self.assertIn("requirements-compile.in", compiler)
        reviewed_build = re.findall(
            r"(?m)^build==([^\s]+)$",
            compiler_intent,
        )
        self.assertEqual(len(reviewed_build), 1)
        self.assertIn(f"build=={reviewed_build[0]} \\", compiler_lock)

    def test_local_dependency_audit_uses_the_same_complete_locks(self):
        audit_path = ROOT / "scripts" / "audit-deps.sh"
        audit = audit_path.read_text()
        self.assertIn(
            "pip-audit -r requirements/requirements.txt "
            "--strict --disable-pip --no-deps",
            audit,
        )
        self.assertIn("npm audit --audit-level=high", audit)
        self.assertNotIn("--omit=dev", audit)
        self.assertTrue(audit_path.stat().st_mode & 0o111)

        compiler_path = ROOT / "scripts" / "compile-requirements.sh"
        self.assertTrue(compiler_path.stat().st_mode & 0o111)

    def test_npm_lock_has_integrity_for_every_registry_artifact(self):
        lock = json.loads((ROOT / "package-lock.json").read_text())
        self.assertGreaterEqual(lock["lockfileVersion"], 3)
        for path, package in lock["packages"].items():
            if not path or package.get("link"):
                continue
            if package.get("resolved", "").startswith("https://registry.npmjs.org/"):
                self.assertRegex(
                    package.get("integrity", ""),
                    r"^sha512-[A-Za-z0-9+/]+={0,2}$",
                    f"{path}: registry artifact lacks a sha512 integrity pin",
                )

    def test_frontend_ci_uses_one_exact_supported_node_release(self):
        self.assertEqual((ROOT / ".node-version").read_text().strip(), NODE_VERSION)
        for workflow in WORKFLOWS:
            source = workflow.read_text()
            self.assertIn('node-version-file: ".node-version"', source)
            self.assertNotRegex(source, r"(?m)^\s*node-version:\s*")

if __name__ == "__main__":
    unittest.main(verbosity=2)
