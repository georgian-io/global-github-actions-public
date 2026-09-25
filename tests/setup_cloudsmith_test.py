import importlib.util
import netrc
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/setup-cloudsmith/configure.py"
spec = importlib.util.spec_from_file_location("configure", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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

    def test_missing_repositories(self):
        self.env.update(NPM_REPOSITORY="", PYTHON_REPOSITORY="")
        with self.assertRaises(ValueError):
            module.configure(self.env)


if __name__ == "__main__":
    unittest.main()
