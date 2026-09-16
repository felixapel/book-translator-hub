import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.check_docs import REPOSITORY, collect_errors


class DocumentationContractTests(unittest.TestCase):
    def copy_repository_fixture(
        self,
        destination: Path,
        source: Path = REPOSITORY,
    ) -> Path:
        fixture = destination / "repository"
        fixture.mkdir(parents=True)
        for relative in (
            "VERSION",
            ".env.example",
            "README.md",
            "CONTRIBUTING.md",
            "SECURITY.md",
            "CODE_OF_CONDUCT.md",
            "AGENTS.md",
            "CLAUDE.md",
        ):
            source_path = source / relative
            destination_path = fixture / relative
            destination_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination_path)
        for root_name in ("docs", ".github", ".gitea"):
            for source_path in sorted((source / root_name).rglob("*.md")):
                relative = source_path.relative_to(source)
                destination_path = fixture / relative
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination_path)
        return fixture

    def test_fixture_copies_only_reviewed_documentation_inputs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = self.copy_repository_fixture(temp / "source")
            sentinel = source / "backups" / "ignored-sentinel.db"
            sentinel.parent.mkdir()
            sentinel.write_text("must not be copied\n", encoding="utf-8")

            fixture = self.copy_repository_fixture(temp / "output", source)

            self.assertFalse((fixture / "backups").exists())
            self.assertFalse(any(fixture.rglob("ignored-sentinel.db")))

    def test_repository_documentation_contract(self):
        self.assertEqual(collect_errors(REPOSITORY), [])

    def test_public_readme_requires_a_published_tag_without_inferring_one(self):
        version = (Path(REPOSITORY) / "VERSION").read_text(encoding="utf-8").strip()
        readme = (Path(REPOSITORY) / "README.md").read_text(encoding="utf-8")
        self.assertIn("git switch --detach vX.Y.Z", readme)
        self.assertNotIn(f"git switch --detach v{version}", readme)

    def test_checker_derives_current_series_and_checks_agent_guidance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.copy_repository_fixture(Path(temp_dir))
            original_version = (fixture / "VERSION").read_text(
                encoding="utf-8"
            ).strip()
            (fixture / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            readme_path = fixture / "README.md"
            readme_path.write_text(
                readme_path.read_text(encoding="utf-8")
                + "\ngit switch --detach v9.8.7\n",
                encoding="utf-8",
            )
            claude_path = fixture / "CLAUDE.md"
            claude_path.write_text(
                claude_path.read_text(encoding="utf-8")
                + "\nDo not use the stale v9.8.6 checkout.\n",
                encoding="utf-8",
            )

            errors = collect_errors(fixture)

            self.assertTrue(any(
                "CLAUDE.md contains stale current-series releases: v9.8.6"
                in error
                for error in errors
            ))

    def test_checker_does_not_infer_a_published_tag_from_a_prerelease(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.copy_repository_fixture(Path(temp_dir))
            original_version = (fixture / "VERSION").read_text(
                encoding="utf-8"
            ).strip()
            candidate = "9.8.7-rc.1"
            (fixture / "VERSION").write_text(candidate + "\n", encoding="utf-8")
            readme_path = fixture / "README.md"
            readme_path.write_text(
                readme_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
            claude_path = fixture / "CLAUDE.md"
            claude_path.write_text(
                claude_path.read_text(encoding="utf-8")
                + f"\nThe current candidate is v{candidate}.\n",
                encoding="utf-8",
            )

            errors = collect_errors(fixture)

            self.assertFalse(any(
                "README.md contains stale current-series releases" in error
                for error in errors
            ))

    def test_checker_validates_support_template_links(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = self.copy_repository_fixture(Path(temp_dir))
            template = (
                fixture
                / ".github"
                / "PULL_REQUEST_TEMPLATE"
                / "pull_request.md"
            )
            template.write_text(
                template.read_text(encoding="utf-8")
                + "\n[Broken contract](missing-validation.md)\n",
                encoding="utf-8",
            )

            errors = collect_errors(fixture)

            self.assertTrue(any(
                "broken relative link in "
                ".github/PULL_REQUEST_TEMPLATE/pull_request.md"
                in error
                for error in errors
            ))

    def test_public_readme_leads_with_the_safe_managed_path(self):
        readme = (Path(REPOSITORY) / "README.md").read_text(encoding="utf-8")
        for contract in (
            "./btctl plan --env",
            "./btctl install --env",
            "./btctl doctor --env",
            "full checkout including `.git`",
            "does not require host Python, host Git or NerdTools",
            "docs/install/btctl.md",
            "docs/reference/compatibility.md",
        ):
            self.assertIn(contract, readme)
        for unsupported_claim in (
            "Zero-touch install",
            "never truncates real translations",
            "does not publish container images",
            "Gemma's pre-training coverage",
        ):
            self.assertNotIn(unsupported_claim, readme)

    def test_managed_guides_cover_install_and_complete_lifecycle(self):
        root = Path(REPOSITORY)
        install = (root / "docs/install/btctl.md").read_text(encoding="utf-8")
        lifecycle = (root / "docs/operations/lifecycle.md").read_text(
            encoding="utf-8"
        )
        for command in ("plan", "install", "doctor"):
            self.assertIn(f"./btctl {command} --env", install)
        for command in ("doctor", "adopt", "upgrade", "rollback", "uninstall"):
            self.assertIn(f"./btctl {command} --env", lifecycle)
        for contract in (
            "host Python, host Git and NerdTools are not required",
            "Docker socket is equivalent to root",
            "warm ordinary build cache",
            "full Git checkout",
        ):
            self.assertIn(contract, install)

    def test_ca_guide_keeps_the_combined_api_private_and_digest_pinned(self):
        source = (
            Path(REPOSITORY) / "docs/install/community-applications.md"
        ).read_text(encoding="utf-8")
        for contract in (
            "Unraid 7.3.2",
            "CWA 4.0.6",
            "linux/amd64",
            "BT_ROLE=all",
            "101:102",
            "0700",
            "8080",
            "8385",
            "8390",
            "not published",
            "sha256",
            "BT_AUTH_MODE=cwa_session",
            "/ajax/emailstat",
        ):
            self.assertIn(contract, source)
        self.assertRegex(source, r"Allow Reverse Proxy\s+Authentication")
        self.assertNotIn(":latest", source)

    def test_architecture_classifies_supported_profiles_consistently(self):
        root = Path(REPOSITORY)
        architecture = (root / "docs/reference/architecture.md").read_text(
            encoding="utf-8"
        )
        configuration = (
            root / "docs/reference/configuration.md"
        ).read_text(encoding="utf-8")
        for contract in (
            "Managed split profile",
            "Community Applications combined profile",
            "BT_ROLE=all",
            "not a supported production installation method",
        ):
            self.assertIn(contract, architecture)
        self.assertIn(
            "`all` only for the certified, digest-pinned Community Applications profile",
            configuration,
        )
        self.assertNotIn("compatibility-only `all`", configuration)

    def test_configuration_documents_public_safety_limits(self):
        source = (
            Path(REPOSITORY) / "docs/reference/configuration.md"
        ).read_text(encoding="utf-8")
        for name, default in (
            ("BT_MAX_BATCH_PARAGRAPHS", "50"),
            ("BT_MAX_PARAGRAPH_CHARS", "8000"),
            ("BT_CACHE_SCOPE_MAX_CHARS", "512"),
            ("BT_MAX_CONTENT_LENGTH", "2097152"),
            ("BT_AUTH_MAX_INFLIGHT_PER_CLIENT", "2"),
        ):
            self.assertIn(f"| `{name}` | `{default}` |", source)
        self.assertIn("BT_CACHE_DIR", source)
        self.assertIn("BT_CACHE_OPERATOR_GROUP_ACCESS", source)
        self.assertIn("lifecycle-internal", source)

    def test_authentik_guide_is_fail_closed_and_edge_owned(self):
        source = (Path(REPOSITORY) / "docs/install/authentik.md").read_text(
            encoding="utf-8"
        )
        for contract in (
            "BT_AUTH_PROFILE=authentik-forwarded",
            "BT_INGRESS_MODE=docker-edge",
            "BT_IDENTITY_PROXY_IP=",
            "BT_AUTHENTIK_OUTPOST_URL=",
            "./btctl auth-snippet --env",
            "./btctl doctor --env",
            "X-authentik-uid",
            "Cookie",
            "CVE-2026-25748",
            "2026.2.5+",
            "2026.5.4+",
        ):
            self.assertIn(contract, source)
        self.assertNotIn("BT_AUTH_MODE=disabled", source)
        self.assertNotIn("BT_ALLOW_INSECURE_AUTH=true", source)

    def test_compatibility_and_troubleshooting_are_actionable(self):
        root = Path(REPOSITORY)
        compatibility = (root / "docs/reference/compatibility.md").read_text(
            encoding="utf-8"
        )
        for contract in (
            "CWA 4.x",
            "CWA 3.1.4",
            "Unraid",
            "Compose",
            "Chromium",
            "Nginx",
            "Traefik",
            "Caddy",
            "OpenAI-compatible",
            "vLLM",
            "Ollama",
            "LM Studio",
            "llama.cpp",
        ):
            self.assertIn(contract, compatibility)

        troubleshooting = (
            root / "docs/operations/troubleshooting.md"
        ).read_text(encoding="utf-8")
        for contract in (
            "./btctl doctor --env",
            "Community Applications install has no `btctl` state",
            "no mapping for 8390",
            "/bt-config.json",
            "authentik-forwarded",
            "BT_IDENTITY_PROXY_IP",
        ):
            self.assertIn(contract, troubleshooting)

    def test_public_support_files_are_actionable(self):
        root = Path(REPOSITORY)
        security = (root / "SECURITY.md").read_text(encoding="utf-8")
        contributing = (root / "CONTRIBUTING.md").read_text(encoding="utf-8")
        bug = (root / ".github/ISSUE_TEMPLATE/bug_report.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("felixguillermoapel@gmail.com", security)
        self.assertIn("GitHub Security Advisories", security)
        for command in ("npm ci", "npm test", "npm run test:e2e", "python3"):
            self.assertIn(command, contributing)
        self.assertIn("./btctl doctor", bug)
        self.assertIn("exact tag or commit", bug)


if __name__ == "__main__":
    unittest.main()
