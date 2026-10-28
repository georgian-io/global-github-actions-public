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
        reason = "next 16.3.1 fixes CVE-2026-0001, released today (100% needed)"
        summary = self.root / "summary"
        env = dict(self.env, NPM_REPOSITORY="georgian-ci-bypass",
                   PYTHON_REPOSITORY="georgian-ci-bypass", COOLDOWN_BYPASS="true",
                   COOLDOWN_BYPASS_REASON=reason, GITHUB_STEP_SUMMARY=str(summary),
                   GITHUB_TRIGGERING_ACTOR="octocat")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            module.configure(env)
        self.assertIn("::warning title=Cloudsmith cooldown bypass::", stdout.getvalue())
        self.assertIn("Reason: next 16.3.1 fixes CVE-2026-0001, released today (100%25 needed)",
                      stdout.getvalue())
        self.assertIn("Started by: octocat", summary.read_text())
        self.assertIn(f"Reason: {reason}", summary.read_text())
        values = parse_environment(Path(env["GITHUB_ENV"]).read_text())
        self.assertIn("/georgian-ci-bypass/", values["PIP_INDEX_URL"])
        for npm, python, bypass in (("georgian-main", "", "true"), ("", "georgian-test", "true"),
                                    ("georgian-ci-bypass", "georgian-rnd", "true"),
                                    ("georgian-ci-bypass", "", "yes")):
            with self.subTest(npm=npm, python=python, bypass=bypass):
                env = dict(self.env, NPM_REPOSITORY=npm, PYTHON_REPOSITORY=python,
                           COOLDOWN_BYPASS=bypass, COOLDOWN_BYPASS_REASON=reason,
                           GITHUB_ENV=str(self.root / "rejected"))
                with self.assertRaises(ValueError):
                    module.configure(env)
                self.assertFalse((self.root / "rejected").exists())

    def test_bypass_requires_a_single_line_reason(self):
        for reason in ("", "   ", "first line\nINJECTED=1", "first line\r", "x" * 301):
            with self.subTest(reason=reason[:20]):
                summary = self.root / "summary"
                env = dict(self.env, NPM_REPOSITORY="georgian-ci-bypass", PYTHON_REPOSITORY="",
                           COOLDOWN_BYPASS="true", COOLDOWN_BYPASS_REASON=reason,
                           GITHUB_STEP_SUMMARY=str(summary))
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout), self.assertRaises(ValueError):
                    module.configure(env)
                self.assertNotIn("::warning", stdout.getvalue())
                self.assertFalse(Path(env["GITHUB_ENV"]).exists())
                self.assertFalse(summary.exists())
                self.assertFalse((self.root / "cloudsmith").exists())

    def test_missing_repositories(self):
        self.env.update(NPM_REPOSITORY="", PYTHON_REPOSITORY="")
        with self.assertRaises(ValueError):
            module.configure(self.env)


if __name__ == "__main__":
    unittest.main()
