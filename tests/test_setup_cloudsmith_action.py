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


def run(command, env):
    return subprocess.run(
        [sys.executable, str(SCRIPT), command],
        env={"PATH": os.environ.get("PATH", ""), **env},
        capture_output=True, text=True, check=False,
    )


class ConfigureTest(unittest.TestCase):
    def configure(self, npm="georgian-main", python="georgian-main",
                  token="test-token:with/punctuation", temp_suffix="runner temp"):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        temp = root / temp_suffix
        env_file = root / "github_env"
        result = run("configure", {
            "NPM_REPOSITORY": npm,
            "PYTHON_REPOSITORY": python,
            "CLOUDSMITH_API_KEY": token,
            "RUNNER_TEMP": str(temp),
            "GITHUB_ENV": str(env_file),
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


class IdentityTest(unittest.TestCase):
    def slug(self, repository):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "github_output"
            result = run("identity", {"GITHUB_REPOSITORY": repository, "GITHUB_OUTPUT": str(output)})
            self.assertEqual(result.returncode, 0, result.stderr)
            return output.read_text()

    def test_matches_terraform_service_names(self):
        # Must match the service names Georgian creates in Cloudsmith for each repository.
        self.assertEqual(self.slug("georgian-io/example-repo"), "service-slug=ci-pull-example-repo\n")
        self.assertEqual(self.slug("georgian-io/Example_Repo.Name"), "service-slug=ci-pull-example-repo-name\n")
        self.assertEqual(
            self.slug("georgian-io/A-Very_Long.Repository-Name-That-Keeps-Going-And-Going-Past-The-Limit"),
            "service-slug=ci-pull-a-very-long-repository-name-that-keeps-going-an-5d8022cf\n",
        )


class ActionTest(unittest.TestCase):
    def test_run_steps_call_the_script(self):
        steps = yaml.safe_load((ACTION_DIR / "action.yml").read_text())["runs"]["steps"]
        runs = [step["run"] for step in steps if "run" in step]
        self.assertEqual(runs, [
            'python3 "$GITHUB_ACTION_PATH/configure.py" identity',
            'python3 "$GITHUB_ACTION_PATH/configure.py" configure',
        ])


if __name__ == "__main__":
    unittest.main()
