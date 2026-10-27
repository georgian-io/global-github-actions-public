import contextlib
import hashlib
import importlib.util
import io
import netrc
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/setup-cloudsmith/configure.py"
spec = importlib.util.spec_from_file_location("configure", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


ACTION = SCRIPT.parent / "action.yml"


def identity_script():
    """Return the identity step's bash script from action.yml."""
    lines = ACTION.read_text().splitlines()
    start = lines.index("      id: identity")
    run = next(i for i in range(start, len(lines)) if lines[i] == "      run: |")
    body = []
    for line in lines[run + 1:]:
        if line and not line.startswith("        "):
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def parse_environment(content):
    return dict(line.split("=", 1) for line in content.splitlines())


class SetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(NPM_REPOSITORY="georgian-test", PYTHON_REPOSITORY="georgian-test",
                        CLOUDSMITH_API_KEY="synthetic-test-token", RUNNER_TEMP=str(self.root),
                        GITHUB_ENV=str(self.root / "environment"))

    def test_valid_clients_and_permissions(self):
        module.configure(self.env)
        values = parse_environment(Path(self.env["GITHUB_ENV"]).read_text())
        self.assertEqual(set(values), {"NPM_CONFIG_USERCONFIG", "NETRC", "PIP_INDEX_URL",
                                       "UV_INDEX_URL", "UV_INDEX_USERNAME", "UV_INDEX_PASSWORD"})
        self.assertEqual(values["PIP_INDEX_URL"],
                         "https://dl.cloudsmith.io/basic/georgian/georgian-test/python/simple/")
        self.assertEqual(values["UV_INDEX_PASSWORD"], self.env["CLOUDSMITH_API_KEY"])
        self.assertEqual(netrc.netrc(values["NETRC"]).authenticators("dl.cloudsmith.io"),
                         ("token", "", self.env["CLOUDSMITH_API_KEY"]))
        self.assertIn("${CLOUDSMITH_API_KEY}", Path(values["NPM_CONFIG_USERCONFIG"]).read_text())
        for key in ("NETRC", "NPM_CONFIG_USERCONFIG"):
            self.assertEqual(Path(values[key]).stat().st_mode & 0o777, 0o600)

    def test_rejects_injection_before_writing(self):
        for key in ("NPM_REPOSITORY", "PYTHON_REPOSITORY", "CLOUDSMITH_API_KEY", "RUNNER_TEMP"):
            for suffix in ("\nINJECTED=yes", "\rINJECTED=yes"):
                with self.subTest(key=key, suffix=repr(suffix)):
                    env = dict(self.env)
                    env[key] += suffix
                    with self.assertRaises(ValueError):
                        module.configure(env)
                    self.assertFalse(Path(env["GITHUB_ENV"]).exists())
                    self.assertFalse((self.root / "cloudsmith").exists())

    def test_single_manager(self):
        for manager in ("NPM_REPOSITORY", "PYTHON_REPOSITORY"):
            with self.subTest(manager=manager):
                env = dict(self.env, **{manager: ""})
                Path(env["GITHUB_ENV"]).write_text("")
                module.configure(env)
                values = parse_environment(Path(env["GITHUB_ENV"]).read_text())
                self.assertEqual("NPM_CONFIG_USERCONFIG" in values, manager != "NPM_REPOSITORY")
                self.assertEqual("NETRC" in values, manager != "PYTHON_REPOSITORY")

    def identity(self, repository, bypass):
        output = self.root / "output"
        output.write_text("")
        env = dict(os.environ, GITHUB_REPOSITORY=f"georgian-io/{repository}",
                   COOLDOWN_BYPASS=bypass, GITHUB_OUTPUT=str(output))
        result = subprocess.run(["bash", "-c", identity_script()], env=env,
                                capture_output=True, text=True, check=False)
        slug = output.read_text().partition("service-slug=")[2].strip() or None
        return result.returncode, slug

    def test_identity_selects_pull_or_bypass_service(self):
        self.assertEqual(self.identity("Open_Source.Repo", "false"), (0, "ci-pull-open-source-repo"))
        self.assertEqual(self.identity("Open_Source.Repo", "true"), (0, "ci-bypass-open-source-repo"))
        candidate = "ci-bypass-" + "a" * 80
        digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
        self.assertEqual(self.identity("a" * 80, "true"), (0, f"{candidate[:55]}-{digest}"))
        for value in ("", "yes", "TRUE"):
            with self.subTest(value=value):
                self.assertEqual(self.identity("repo", value), (1, None))

    def test_bypass_uses_only_the_bypass_repository_and_warns(self):
        env = dict(self.env, NPM_REPOSITORY="georgian-ci-bypass",
                   PYTHON_REPOSITORY="georgian-ci-bypass", COOLDOWN_BYPASS="true")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            module.configure(env)
        self.assertIn("::warning title=Cloudsmith cooldown bypass::", stdout.getvalue())
        values = parse_environment(Path(env["GITHUB_ENV"]).read_text())
        self.assertIn("/georgian-ci-bypass/", values["PIP_INDEX_URL"])
        for npm, python, bypass in (("georgian-main", "", "true"), ("", "georgian-test", "true"),
                                    ("georgian-ci-bypass", "georgian-rnd", "true"),
                                    ("georgian-ci-bypass", "", "yes")):
            with self.subTest(npm=npm, python=python, bypass=bypass):
                env = dict(self.env, NPM_REPOSITORY=npm, PYTHON_REPOSITORY=python,
                           COOLDOWN_BYPASS=bypass, GITHUB_ENV=str(self.root / "rejected"))
                with self.assertRaises(ValueError):
                    module.configure(env)
                self.assertFalse((self.root / "rejected").exists())

    def test_missing_repositories(self):
        self.env.update(NPM_REPOSITORY="", PYTHON_REPOSITORY="")
        with self.assertRaises(ValueError):
            module.configure(self.env)


if __name__ == "__main__":
    unittest.main()
