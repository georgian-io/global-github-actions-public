#!/usr/bin/env python3
"""Tests for the Cloudsmith CI lint."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "actions" / "cloudsmith-ci-lint" / "cloudsmith_ci_lint.py"
SPEC = importlib.util.spec_from_file_location("cloudsmith_ci_lint", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def dedent(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


class CloudsmithCiLintTest(unittest.TestCase):
    def lint(
        self, workflow: str, mode: str = "error", files: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root, ".github", "workflows")
            directory.mkdir(parents=True)
            (directory / "ci.yml").write_text(workflow)
            for path, content in (files or {}).items():
                target = Path(root, path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--root", root, "--mode", mode],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_accepts_setup_before_install(self) -> None:
        workflow = dedent("""
            permissions:
              contents: read
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                    with:
                      npm-repository: javascript-all
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0)
        self.assertIn("found no issues", result.stdout)

    def test_rejects_install_without_setup(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)
        self.assertIn("Add the shared setup-cloudsmith action", result.stdout)

    def test_recognizes_supported_install_commands(self) -> None:
        commands = [
            "npm ci",
            "pnpm install --frozen-lockfile",
            "yarn install --immutable",
            "pip install -r requirements.txt",
            "pip3 install -r requirements.txt",
            "python -m pip install -r requirements.txt",
            "npm install -g actionlint",
            "uv sync",
            "uv pip install -r requirements.txt",
            "uv tool install actionlint",
            "poetry install",
            "pipenv install",
        ]

        for command in commands:
            with self.subTest(command=command):
                workflow = dedent(f"""
                    jobs:
                      test:
                        runs-on: ubuntu-latest
                        steps:
                          - run: {command}
                """)

                result = self.lint(workflow)

                self.assertNotEqual(result.returncode, 0, command)
                self.assertIn("CS001", result.stdout, command)

    def test_ignores_non_install_commands(self) -> None:
        commands = [
            "npm publish",
            "npm run test",
            "pip list",
            "echo npm install",
        ]

        for command in commands:
            with self.subTest(command=command):
                workflow = dedent(f"""
                    jobs:
                      test:
                        runs-on: ubuntu-latest
                        steps:
                          - run: {command}
                """)

                result = self.lint(workflow)

                self.assertEqual(result.returncode, 0, command)
                self.assertIn("found no issues", result.stdout, command)

    def test_detects_installs_in_compound_commands(self) -> None:
        for command in ["npm ci && npm test", "echo ready; pip install six", "uv sync || echo failed"]:
            with self.subTest(command=command):
                result = self.lint(dedent(f"""
                    jobs:
                      test:
                        runs-on: ubuntu-latest
                        steps:
                          - run: {command}
                """))

                self.assertNotEqual(result.returncode, 0, command)
                self.assertIn("CS001", result.stdout, command)

    def test_rejects_setup_after_install(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: uv sync
                  - uses: cloudsmith-io/cloudsmith-cli-action@v2
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)

    def test_rejects_missing_oidc_permission(self) -> None:
        workflow = dedent("""
            permissions:
              contents: read
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: cloudsmith-io/cloudsmith-cli-action@v2
                  - run: pip install -r requirements.txt
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS002", result.stdout)
        self.assertIn("permissions.id-token", result.stdout)

    def test_job_permissions_do_not_inherit_omitted_workflow_permission(self) -> None:
        workflow = dedent("""
            permissions:
              contents: read
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                permissions:
                  contents: read
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS002", result.stdout)

    def test_accepts_job_write_all_permission(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                permissions: write-all
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                    with:
                      npm-repository: georgian-test
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("found no issues", result.stdout)

    def test_rejects_job_read_all_permission(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                permissions: read-all
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS002", result.stdout)

    def test_accepts_public_setup_action(self) -> None:
        workflow = dedent("""
            permissions:
              contents: read
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                    with:
                      npm-repository: georgian-test
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0)
        self.assertIn("found no issues", result.stdout)

    def test_reports_setup_and_oidc_findings_together(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)
        self.assertIn("CS002", result.stdout)

    def test_rejects_public_registry_in_install_job(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - run: npm ci --registry https://registry.npmjs.org
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS003", result.stdout)

    def test_allows_reviewed_public_registry_reason(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              release:
                runs-on: ubuntu-latest
                env:
                  CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY: Public npm release.
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - run: npm ci --registry https://registry.npmjs.org
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0)
        self.assertIn("found no issues", result.stdout)

    def test_ignores_public_registry_in_publish_only_job(self) -> None:
        workflow = dedent("""
            jobs:
              release:
                runs-on: ubuntu-latest
                steps:
                  - uses: actions/setup-node@v4
                    with:
                      registry-url: https://registry.npmjs.org
                  - run: npm publish
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0)
        self.assertIn("found no issues", result.stdout)

    def test_ignores_public_registry_after_cloudsmith_install(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              release:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - run: npm ci
                  - uses: actions/setup-node@v4
                    with:
                      registry-url: https://registry.npmjs.org
                  - run: npm publish
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0)
        self.assertIn("found no issues", result.stdout)

    def test_accepts_direct_auth_with_registry_configuration(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                env:
                  NPM_CONFIG_REGISTRY: https://npm.cloudsmith.io/georgian/javascript-all/
                steps:
                  - uses: cloudsmith-io/cloudsmith-cli-action@v2
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0)
        self.assertIn("found no issues", result.stdout)

    def test_resolves_make_install_target(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - name: Install dependencies
                    run: make install-dev
        """)

        result = self.lint(workflow, files={"Makefile": "install-dev:\n\tuv sync --all-extras\n"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)
        self.assertIn("CS002", result.stdout)
        self.assertIn("Makefile", result.stdout)
        self.assertIn("uv sync --all-extras", result.stdout)

    def test_accepts_make_install_after_cloudsmith_setup(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - name: Install dependencies
                    run: make install-dev
        """)

        result = self.lint(workflow, files={"Makefile": "install-dev:\n\tuv sync --all-extras\n"})

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("found no issues", result.stdout)

    def test_reports_public_registry_in_resolved_make_recipe(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - name: Install dependencies
                    run: make install-dev
        """)

        result = self.lint(
            workflow,
            files={"Makefile": "install-dev:\n\tuv sync --index-url https://pypi.org/simple\n"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS003", result.stdout)
        self.assertIn("Makefile", result.stdout)

    def test_resolves_make_working_directory(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - name: Install dependencies
                    working-directory: services/api
                    run: make install-dev
        """)

        result = self.lint(workflow, files={"services/api/Makefile": "install-dev:\n\tuv sync\n"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)
        self.assertIn("services/api/Makefile", result.stdout)

    def test_detects_uv_run_with_external_project_dependencies(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - name: Run generator
                    run: uv run scripts/generate.py
        """)

        result = self.lint(
            workflow,
            files={"pyproject.toml": '[project]\nname = "demo"\ndependencies = [\n  "requests",\n]\n'},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)
        self.assertIn("CS002", result.stdout)
        self.assertIn("project metadata", result.stdout)

    def test_ignores_uv_run_with_no_sync(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: uv run --no-sync scripts/generate.py
        """)

        result = self.lint(
            workflow,
            files={"pyproject.toml": '[project]\nname = "demo"\ndependencies = [\n  "requests",\n]\n'},
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("found no issues", result.stdout)

    def test_ignores_uv_run_without_external_dependencies(self) -> None:
        workflow = dedent("""
            jobs:
              release:
                runs-on: ubuntu-latest
                steps:
                  - run: uv run scripts/create_archive.py
        """)

        result = self.lint(
            workflow,
            files={
                "pyproject.toml": '[project]\nname = "demo"\ndependencies = []\n',
                "uv.lock": 'version = 1\n\n[[package]]\nname = "demo"\n',
            },
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("found no issues", result.stdout)

    def test_detects_uv_run_with_ephemeral_package(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: uv run --no-project --with pytest scripts/test.py
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)
        self.assertIn("CS002", result.stdout)

    def test_reports_unresolved_make_install_wrapper(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - name: Install dependencies
                    run: make install-ci
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS004", result.stdout)
        self.assertNotIn("CS001", result.stdout)

    def test_accepts_direct_auth_with_cloudsmith_source_in_makefile(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: cloudsmith-io/cloudsmith-cli-action@v2
                  - run: make install-dev
        """)

        result = self.lint(
            workflow,
            files={
                "Makefile": "install-dev:\n\tuv sync --index-url https://dl.cloudsmith.io/georgian/python/georgian-test/simple/\n"
            },
        )

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("found no issues", result.stdout)

    def test_rejects_direct_auth_without_registry_configuration(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: cloudsmith-io/cloudsmith-cli-action@v2
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS001", result.stdout)

    def test_warn_mode_reports_without_failure(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - run: poetry install
        """)

        result = self.lint(workflow, mode="warn")

        self.assertEqual(result.returncode, 0)
        self.assertIn("::warning", result.stdout)
        self.assertIn("CS001", result.stdout)

    def test_checks_every_install_in_a_job(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - name: Install npm packages
                    run: npm ci
                  - name: Install from PyPI
                    run: pip install --index-url https://pypi.org/simple six
        """)

        result = self.lint(workflow)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS003", result.stdout)
        self.assertIn("  Step: Install from PyPI\n", result.stdout)
        self.assertNotIn("  Step: Install npm packages\n", result.stdout)

    def test_reports_missing_setup_for_install_before_setup(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - name: Early install
                    run: npm ci
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - name: Later install
                    run: pip install six
        """)

        result = self.lint(workflow)

        self.assertIn("CS001", result.stdout)
        self.assertIn("  Step: Early install\n", result.stdout)
        self.assertNotIn("  Step: Later install\n", result.stdout)

    def test_unresolved_wrapper_does_not_hide_later_installs(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - name: Wrapper
                    run: make install-ci
                  - name: Direct install
                    run: npm ci
        """)

        result = self.lint(workflow)

        self.assertIn("CS004", result.stdout)
        self.assertIn("  Step: Wrapper\n", result.stdout)
        self.assertIn("CS001", result.stdout)
        self.assertIn("  Step: Direct install\n", result.stdout)
        self.assertIn("found 2 issue(s)", result.stdout)

    def test_reports_unresolved_wrapper_after_install(self) -> None:
        workflow = dedent("""
            permissions:
              id-token: write
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                  - run: npm ci
                  - name: Wrapper
                    run: make install-ci
        """)

        result = self.lint(workflow)

        self.assertIn("CS004", result.stdout)
        self.assertIn("  Step: Wrapper\n", result.stdout)
        self.assertIn("found 1 issue(s)", result.stdout)

    def test_does_not_repeat_identical_findings(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                steps:
                  - name: Install twice
                    run: npm ci && npm install
                  - name: Install again
                    run: pip install six
        """)

        result = self.lint(workflow)

        self.assertEqual(result.stdout.count(": CS001:"), 2)
        self.assertEqual(result.stdout.count(": CS002:"), 1)
        self.assertIn("found 3 issue(s)", result.stdout)

    def poetry_workflow(self, command: str = "poetry install", setup: bool = True) -> str:
        setup_step = "      - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main\n" if setup else ""
        return (
            "permissions:\n  id-token: write\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"{setup_step}      - run: {command}\n"
        )

    def poetry_source(self, extra: str) -> str:
        return (
            '[tool.poetry]\nname = "demo"\n\n[[tool.poetry.source]]\nname = "georgian-main"\n'
            'url = "https://dl.cloudsmith.io/basic/georgian/georgian-main/python/simple/"\n' + extra
        )

    def test_rejects_poetry_install_without_cloudsmith_source(self) -> None:
        result = self.lint(self.poetry_workflow(), files={"pyproject.toml": '[tool.poetry]\nname = "demo"\n'})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CS005", result.stdout)
        self.assertNotIn("CS001", result.stdout)

    def test_rejects_poetry_install_without_pyproject(self) -> None:
        result = self.lint(self.poetry_workflow())

        self.assertIn("CS005", result.stdout)

    def test_accepts_poetry_with_primary_cloudsmith_source(self) -> None:
        files = {"pyproject.toml": self.poetry_source('priority = "primary"\n')}

        result = self.lint(self.poetry_workflow(), files=files)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("found no issues", result.stdout)

    def test_accepts_poetry_with_legacy_default_cloudsmith_source(self) -> None:
        files = {"pyproject.toml": self.poetry_source("default = true\n")}

        result = self.lint(self.poetry_workflow(), files=files)

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_rejects_poetry_with_supplemental_cloudsmith_source(self) -> None:
        files = {"pyproject.toml": self.poetry_source('priority = "supplemental"\n')}

        result = self.lint(self.poetry_workflow(), files=files)

        self.assertIn("CS005", result.stdout)

    def test_rejects_poetry_with_primary_non_cloudsmith_source(self) -> None:
        pyproject = '[[tool.poetry.source]]\nname = "mirror"\nurl = "https://mirror.example/simple/"\npriority = "primary"\n'

        result = self.lint(self.poetry_workflow(), files={"pyproject.toml": pyproject})

        self.assertIn("CS005", result.stdout)

    def test_rejects_poetry_sources_that_only_mention_cloudsmith(self) -> None:
        for url in (
            "https://evil.example/cloudsmith.io/georgian/main/python/simple/",
            "https://dl.cloudsmith.io.evil.example/basic/georgian/main/python/simple/",
            "https://evilcloudsmith.io/basic/georgian/main/python/simple/",
            "http://dl.cloudsmith.io/basic/georgian/georgian-main/python/simple/",
            "https://dl.cloudsmith.io/basic/someone-else/main/python/simple/",
        ):
            with self.subTest(url=url):
                pyproject = f'[[tool.poetry.source]]\nname = "mirror"\nurl = "{url}"\npriority = "primary"\n'

                result = self.lint(self.poetry_workflow(), files={"pyproject.toml": pyproject})

                self.assertIn("CS005", result.stdout)

    def test_poetry_still_requires_setup_action(self) -> None:
        files = {"pyproject.toml": self.poetry_source('priority = "primary"\n')}

        result = self.lint(self.poetry_workflow(setup=False), files=files)

        self.assertIn("CS001", result.stdout)
        self.assertNotIn("CS005", result.stdout)

    def test_uses_pyproject_from_working_directory_for_poetry(self) -> None:
        workflow = self.poetry_workflow().replace("- run: poetry install", "- run: poetry install\n        working-directory: app")
        files = {
            "pyproject.toml": '[tool.poetry]\nname = "root"\n',
            "app/pyproject.toml": self.poetry_source('priority = "primary"\n'),
        }

        result = self.lint(workflow, files=files)

        self.assertEqual(result.returncode, 0, result.stdout)

    def test_checks_other_poetry_commands(self) -> None:
        for command in ["poetry add requests", "poetry lock", "poetry update", "poetry sync"]:
            with self.subTest(command=command):
                result = self.lint(self.poetry_workflow(command), files={"pyproject.toml": '[tool.poetry]\nname = "demo"\n'})

                self.assertIn("CS005", result.stdout, command)

    def test_poetry_sources_without_tomllib(self) -> None:
        content = self.poetry_source('priority = "primary"  # Cloudsmith\n') + '\n[tool.other]\nurl = "x"\n'

        with mock.patch.object(MODULE, "tomllib", None):
            sources = MODULE.poetry_sources(content)

        self.assertEqual(sources, [{
            "name": "georgian-main",
            "url": "https://dl.cloudsmith.io/basic/georgian/georgian-main/python/simple/",
            "priority": "primary",
        }])
        self.assertEqual(MODULE.poetry_sources(content), sources)

    def test_exemption_with_reason_skips_job(self) -> None:
        workflow = dedent("""
            jobs:
              test:
                runs-on: ubuntu-latest
                env:
                  CLOUDSMITH_LINT_EXEMPT: Vendored installer, tracked in SEC-123.
                steps:
                  - run: npm ci
        """)

        result = self.lint(workflow)

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("found no issues", result.stdout)

    def test_false_like_exemption_values_do_not_exempt(self) -> None:
        for value in ["false", "False", '"false"', '"0"', "0", '""', "''", "", '"  "']:
            with self.subTest(value=value):
                workflow = dedent(f"""
                    jobs:
                      test:
                        runs-on: ubuntu-latest
                        env:
                          CLOUDSMITH_LINT_EXEMPT: {value}
                        steps:
                          - run: npm ci
                """)

                result = self.lint(workflow)

                self.assertIn("CS001", result.stdout, value)

    def test_false_like_public_registry_reasons_do_not_allow(self) -> None:
        for value in ["false", '"false"', "0", '""']:
            with self.subTest(value=value):
                workflow = dedent(f"""
                    permissions:
                      id-token: write
                    jobs:
                      release:
                        runs-on: ubuntu-latest
                        env:
                          CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY: {value}
                        steps:
                          - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
                          - run: npm ci --registry https://registry.npmjs.org
                """)

                result = self.lint(workflow)

                self.assertIn("CS003", result.stdout, value)

    def test_skips_invalid_yaml_without_traceback(self) -> None:
        for mode, returncode in (("warn", 0), ("error", 1)):
            with self.subTest(mode=mode):
                result = self.lint("jobs: [\n  test: {\n", mode=mode)

                self.assertEqual(result.returncode, returncode)
                self.assertIn("skipped: Cannot parse", result.stdout)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_skips_non_mapping_workflow_without_traceback(self) -> None:
        for workflow in ["just text\n", "jobs: [build]\n", "jobs:\n  build: run npm ci\n"]:
            with self.subTest(workflow=workflow):
                result = self.lint(workflow, mode="warn")

                self.assertEqual(result.returncode, 0)
                self.assertIn("::warning file=.github/workflows/ci.yml", result.stdout)
                self.assertIn("not a", result.stdout)
                self.assertNotIn("Traceback", result.stdout + result.stderr)

    def test_unreadable_workflow_does_not_hide_other_findings(self) -> None:
        result = self.lint(
            "just text\n",
            mode="warn",
            files={".github/workflows/build.yml": "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm ci\n"},
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("skipped", result.stdout)
        self.assertIn("CS001", result.stdout)

if __name__ == "__main__":
    unittest.main()
