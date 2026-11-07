"""Select the job's Cloudsmith identity and configure package clients safely.

`identity` prints the service slug for GITHUB_REPOSITORY to GITHUB_OUTPUT.
`configure` writes npm, pip, uv, Poetry and Pipenv configuration and the
matching GitHub environment records. Neither prints rejected values.
"""

import hashlib
import os
from pathlib import Path
import re
import sys

NAMESPACE = "georgian"
SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
# The token also enters netrc. Reject whitespace and quoting characters rather
# than allowing them to create additional netrc fields or records.
TOKEN = re.compile(r"[A-Za-z0-9._~+/=:-]+")


def service_slug(repository):
    """Name of the per-repository ci-pull- Cloudsmith service: lowercase, hyphens, at most 64 characters."""
    candidate = re.sub(r"[^a-z0-9]+", "-", f"ci-pull-{repository}".lower()).strip("-")
    if len(candidate) > 64:
        digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
        candidate = f"{candidate[:55].strip('-')}-{digest}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", candidate):
        raise ValueError("Invalid Cloudsmith service slug.")
    return candidate


def single_line(value):
    if "\r" in value or "\n" in value:
        raise ValueError("Configuration values must be single-line.")
    return value


def poetry_source_variable(repository):
    # Poetry reads credentials for a source named "georgian-main" from
    # POETRY_HTTP_BASIC_GEORGIAN_MAIN_USERNAME and _PASSWORD.
    return "POETRY_HTTP_BASIC_" + repository.upper().replace("-", "_")


def identity(env):
    repository = env["GITHUB_REPOSITORY"].split("/", 1)[-1]
    slug = service_slug(repository)
    with open(single_line(env["GITHUB_OUTPUT"]), "a", encoding="utf-8") as output:
        output.write(f"service-slug={slug}\n")


def configure(env):
    npm = env.get("NPM_REPOSITORY", "")
    python = env.get("PYTHON_REPOSITORY", "")
    if not npm and not python:
        raise ValueError("Set npm-repository or python-repository.")
    for repository in (npm, python):
        if repository and not SLUG.fullmatch(repository):
            raise ValueError("Invalid Cloudsmith repository slug.")

    token = env["CLOUDSMITH_API_KEY"]
    if not TOKEN.fullmatch(token):
        raise ValueError("Invalid Cloudsmith credential format.")
    config_dir = Path(single_line(env["RUNNER_TEMP"])) / "cloudsmith"
    environment_file = single_line(env["GITHUB_ENV"])

    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    values = {}

    def write_config(name, content):
        path = config_dir / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
        return str(path)

    if npm:
        registry = f"https://npm.cloudsmith.io/{NAMESPACE}/{npm}/"
        values["NPM_CONFIG_USERCONFIG"] = write_config(
            "npmrc",
            f"registry={registry}\n"
            f"//npm.cloudsmith.io/{NAMESPACE}/{npm}/:_authToken=${{CLOUDSMITH_API_KEY}}\n"
            "always-auth=true\n",
        )
    if python:
        index = f"https://dl.cloudsmith.io/basic/{NAMESPACE}/{python}/python/simple/"
        values["NETRC"] = write_config(
            "netrc", f"machine dl.cloudsmith.io\nlogin token\npassword {token}\n"
        )
        poetry = poetry_source_variable(python)
        values.update({
            "PIP_INDEX_URL": index,
            "UV_INDEX_URL": index,
            "UV_INDEX_USERNAME": "token",
            "UV_INDEX_PASSWORD": token,
            "PIPENV_PYPI_MIRROR": index,
            # Poetry ignores the variables above. It only uses Cloudsmith when
            # pyproject.toml declares this repository as its primary source.
            f"{poetry}_USERNAME": "token",
            f"{poetry}_PASSWORD": token,
        })

    with open(environment_file, "a", encoding="utf-8") as output:
        # Slugs, credentials and paths were validated above. None can contain
        # CR/LF, so each assignment is exactly one environment line.
        output.writelines(f"{key}={value}\n" for key, value in values.items())


COMMANDS = {"identity": identity, "configure": configure}

if __name__ == "__main__":
    try:
        COMMANDS[sys.argv[1]](os.environ)
    except (ValueError, KeyError, IndexError):
        print("Cloudsmith setup failed: invalid or missing input.", file=sys.stderr)
        sys.exit(1)
