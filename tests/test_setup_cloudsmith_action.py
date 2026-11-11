import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ACTION_DIR = Path(__file__).resolve().parents[1] / ".github" / "actions" / "setup-cloudsmith"
SCRIPT = ACTION_DIR / "configure.py"
INDEX = "https://dl.cloudsmith.io/basic/georgian/georgian-main/python/simple/"
PACKAGES = "next@16.3.1\n@types/node@22.10.1\n"


def run(command, env):
    return subprocess.run(
        [sys.executable, str(SCRIPT), command],
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True, text=True, check=False,
    )


class ConfigureTest(unittest.TestCase):
    def configure(self, npm="georgian-main", python="georgian-main",
                  token="test-token:with/punctuation", temp_suffix="runner temp",
                  bypass="false", reason="", packages=""):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        temp = root / temp_suffix
        env_file = root / "github_env"
        self.summary = root / "step_summary"
        result = run("configure", {
            "NPM_REPOSITORY": npm,
            "PYTHON_REPOSITORY": python,
            "CLOUDSMITH_API_KEY": token,
            "RUNNER_TEMP": str(temp),
            "GITHUB_ENV": str(env_file),
            "COOLDOWN_BYPASS": bypass,
            "COOLDOWN_BYPASS_REASON": reason,
            "COOLDOWN_BYPASS_PACKAGES": packages,
            "GITHUB_STEP_SUMMARY": str(self.summary),
            "GITHUB_TRIGGERING_ACTOR": "octocat",
        })
        return temp, env_file, result

    def test_normal_package_configs_and_protected_files(self):
        for npm, python in (("georgian-main", ""), ("", "georgian-main"), ("georgian-main", "georgian-main")):
            with self.subTest(npm=npm, python=python):
                temp, env_file, result = self.configure(npm=npm, python=python)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout + result.stderr, "")
                exports = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
                config = temp / "cloudsmith"
                expected = {}
                if npm:
                    npmrc = config / "npmrc"
                    expected["NPM_CONFIG_USERCONFIG"] = str(npmrc)
                    self.assertIn("_authToken=${CLOUDSMITH_API_KEY}", npmrc.read_text())
                    self.assertEqual(npmrc.stat().st_mode & 0o777, 0o600)
                if python:
                    netrc = config / "netrc"
                    expected.update({
                        "NETRC": str(netrc),
                        "PIP_INDEX_URL": INDEX,
                        "UV_INDEX_URL": INDEX,
                        "UV_INDEX_USERNAME": "token",
                        "UV_INDEX_PASSWORD": "test-token:with/punctuation",
                        "PIPENV_PYPI_MIRROR": INDEX,
                        "POETRY_HTTP_BASIC_GEORGIAN_MAIN_USERNAME": "token",
                        "POETRY_HTTP_BASIC_GEORGIAN_MAIN_PASSWORD": "test-token:with/punctuation",
                    })
                    self.assertIn("password test-token:with/punctuation", netrc.read_text())
                    self.assertEqual(netrc.stat().st_mode & 0o777, 0o600)
                self.assertEqual(exports, expected)

    def test_requires_a_repository(self):
        _, env_file, result = self.configure(npm="", python="")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(env_file.exists())

    def test_rejects_repository_environment_injection(self):
        for separator in ("\n", "\r", "\r\n"):
            for field in ("npm", "python"):
                with self.subTest(separator=repr(separator), field=field):
                    temp, env_file, result = self.configure(**{field: f"georgian-main{separator}INJECTED=true"})
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(env_file.exists())
                    self.assertFalse((temp / "cloudsmith").exists())

    def test_rejects_token_and_path_injection_without_disclosing_token(self):
        for separator in ("\n", "\r", "\r\n", " "):
            for field in ({"token": f"private-token{separator}INJECTED=true"},
                          {"temp_suffix": f"runner{separator}INJECTED=true"}):
                if separator == " " and "temp_suffix" in field:
                    continue  # spaces are valid in paths
                with self.subTest(separator=repr(separator), field=next(iter(field))):
                    temp, env_file, result = self.configure(**field)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(env_file.exists())
                    self.assertFalse((temp / "cloudsmith").exists())
                    self.assertNotIn("private-token", result.stdout + result.stderr)


    def test_bypass_uses_only_the_bypass_repository_and_warns(self):
        reason = "next 16.3.1 fixes CVE-2026-0001, released today (100% needed)"
        _, env_file, result = self.configure(npm="georgian-ci-bypass", python="georgian-ci-bypass",
                                             bypass="true", reason=reason, packages=PACKAGES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning title=Cloudsmith cooldown bypass::", result.stdout)
        self.assertIn("Reason: next 16.3.1 fixes CVE-2026-0001, released today (100%25 needed)", result.stdout)
        summary = self.summary.read_text()
        self.assertIn("Started by: octocat", summary)
        self.assertIn("  - `next@16.3.1`\n  - `@types/node@22.10.1`\n", summary)
        self.assertIn("next@16.3.1, @types/node@22.10.1. Reason:", result.stdout)
        self.assertIn(f"Reason: {reason}", summary)
        self.assertIn("PIP_INDEX_URL=https://dl.cloudsmith.io/basic/georgian/georgian-ci-bypass/python/simple/",
                      env_file.read_text())
        for npm, python in (("georgian-main", ""), ("", "georgian-main"), ("georgian-ci-bypass", "georgian-rnd")):
            with self.subTest(npm=npm, python=python):
                temp, env_file, result = self.configure(npm=npm, python=python, bypass="true", reason=reason,
                                                        packages=PACKAGES)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("georgian-ci-bypass", result.stderr)
                self.assertFalse(env_file.exists())
                self.assertFalse((temp / "cloudsmith").exists())

    def test_bypass_requires_a_single_line_reason(self):
        for reason in ("", "   ", "first line\nINJECTED=1", "first line\r", "x" * 301):
            with self.subTest(reason=reason[:20]):
                temp, env_file, result = self.configure(npm="georgian-ci-bypass", python="",
                                                        bypass="true", reason=reason, packages=PACKAGES)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cooldown-bypass-reason", result.stderr)
                self.assertNotIn("::warning", result.stdout)
                self.assertFalse(env_file.exists())
                self.assertFalse(self.summary.exists())
                self.assertFalse((temp / "cloudsmith").exists())

    def test_bypass_requires_exact_package_versions(self):
        for packages in ("", "next", "next@", "next@16 extra", "next@16.3.1\r", "`next`@1",
                         "next@1\nINJECTED=1 x", "\n".join(f"p{i}@1" for i in range(21))):
            with self.subTest(packages=packages[:20]):
                temp, env_file, result = self.configure(npm="georgian-ci-bypass", python="", bypass="true",
                                                        reason="needed", packages=packages)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cooldown-bypass-packages", result.stderr)
                self.assertFalse(env_file.exists())
                self.assertFalse(self.summary.exists())

    def test_reason_is_ignored_without_bypass(self):
        _, _, result = self.configure(reason="not used")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(self.summary.exists())


class RequestTest(unittest.TestCase):
    def request(self, reason, packages):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        summary = root / "step_summary"
        result = run("request", {"COOLDOWN_BYPASS_REASON": reason, "COOLDOWN_BYPASS_PACKAGES": packages,
                                 "GITHUB_STEP_SUMMARY": str(summary), "GITHUB_TRIGGERING_ACTOR": "octocat"})
        return result, summary

    def test_shows_reason_and_versions_before_approval(self):
        result, summary = self.request("next 16.3.1 fixes CVE-2026-0001 (100% needed)", PACKAGES)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("::warning title=Cloudsmith cooldown bypass requested::", result.stdout)
        self.assertIn("next@16.3.1, @types/node@22.10.1. Reason: next 16.3.1 fixes CVE-2026-0001 (100%25 needed)",
                      result.stdout)
        text = summary.read_text()
        self.assertIn("### Cloudsmith cooldown bypass requested", text)
        self.assertIn("  - `next@16.3.1`\n", text)
        self.assertIn("If this summary is missing, reject the deployment.", text)

    def test_rejects_bad_requests_without_output(self):
        for reason, packages in (("", PACKAGES), ("why", ""), ("why\nINJECTED=1", PACKAGES), ("why", "a b@1")):
            with self.subTest(reason=reason, packages=packages[:15]):
                result, summary = self.request(reason, packages)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertFalse(summary.exists())


class IdentityTest(unittest.TestCase):
    def slug(self, repository, bypass=None):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "github_output"
            env = {"GITHUB_REPOSITORY": repository, "GITHUB_OUTPUT": str(output)}
            if bypass is not None:
                env["COOLDOWN_BYPASS"] = bypass
            result = run("identity", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            return output.read_text()

    def test_selects_pull_or_bypass_identity(self):
        self.assertEqual(self.slug("georgian-io/Example_Repo", "false"), "service-slug=ci-pull-example-repo\n")
        self.assertEqual(self.slug("georgian-io/Example_Repo", "true"), "service-slug=ci-bypass-example-repo\n")
        candidate = "ci-bypass-" + "a" * 80
        digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
        self.assertEqual(self.slug("georgian-io/" + "a" * 80, "true"), f"service-slug={candidate[:55]}-{digest}\n")

    def test_rejects_other_bypass_values(self):
        for value in ("", "yes", "TRUE", "true\nINJECTED=1"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "github_output"
                result = run("identity", {"GITHUB_REPOSITORY": "georgian-io/example-repo",
                                          "GITHUB_OUTPUT": str(output), "COOLDOWN_BYPASS": value})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(output.exists())

    def test_matches_terraform_service_names(self):
        # Must match the service names Georgian creates in Cloudsmith for each repository.
        self.assertEqual(self.slug("georgian-io/example-repo"), "service-slug=ci-pull-example-repo\n")
        self.assertEqual(self.slug("georgian-io/Example_Repo.Name"), "service-slug=ci-pull-example-repo-name\n")
        self.assertEqual(
            self.slug("georgian-io/A-Very_Long.Repository-Name-That-Keeps-Going-And-Going-Past-The-Limit"),
            "service-slug=ci-pull-a-very-long-repository-name-that-keeps-going-an-5d8022cf\n",
        )


class ActionTest(unittest.TestCase):
    def test_request_action_runs_the_request_command(self):
        action = yaml.safe_load((ACTION_DIR.parent / "cloudsmith-bypass-request" / "action.yml").read_text())
        steps = action["runs"]["steps"]
        self.assertEqual([step["run"] for step in steps],
                         ['python3 "$GITHUB_ACTION_PATH/../setup-cloudsmith/configure.py" request'])
        self.assertTrue(all("${{" not in step["run"] for step in steps))

    def test_run_steps_call_the_script(self):
        steps = yaml.safe_load((ACTION_DIR / "action.yml").read_text())["runs"]["steps"]
        runs = [step["run"] for step in steps if "run" in step]
        self.assertEqual(runs, [
            'python3 "$GITHUB_ACTION_PATH/configure.py" identity',
            'python3 "$GITHUB_ACTION_PATH/configure.py" configure',
        ])


if __name__ == "__main__":
    unittest.main()
