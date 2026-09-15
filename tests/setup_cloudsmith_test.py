import importlib.util
import netrc
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / ".github/actions/setup-cloudsmith/configure.py"
spec = importlib.util.spec_from_file_location("configure", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def parse_environment(content):
    lines = iter(content.splitlines())
    result = {}
    for line in lines:
        name, delimiter = line.split("<<", 1)
        value = []
        for line in lines:
            if line == delimiter:
                break
            value.append(line)
        else:
            raise AssertionError("Missing environment delimiter")
        result[name] = "\n".join(value)
    return result


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

    def test_delimiter_collision_and_embedded_assignment(self):
        payload = "value\ncollision\nINJECTED=yes"
        with patch.object(module.secrets, "token_hex", side_effect=["collision", "safe-boundary"]):
            record = module.environment_record("EXPECTED", payload)
        self.assertEqual(parse_environment(record), {"EXPECTED": payload})

    def test_single_manager(self):
        self.env["PYTHON_REPOSITORY"] = ""
        module.configure(self.env)
        self.assertEqual(set(parse_environment(Path(self.env["GITHUB_ENV"]).read_text())),
                         {"NPM_CONFIG_USERCONFIG"})

    def test_missing_repositories(self):
        self.env.update(NPM_REPOSITORY="", PYTHON_REPOSITORY="")
        with self.assertRaises(ValueError):
            module.configure(self.env)


if __name__ == "__main__":
    unittest.main()
