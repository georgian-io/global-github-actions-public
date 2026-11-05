"""Select the job's Cloudsmith identity and configure package clients safely.

`identity` prints the service slug for GITHUB_REPOSITORY to GITHUB_OUTPUT.
`configure` writes npm, pip, uv, Poetry and Pipenv configuration and the
matching GitHub environment records. Neither prints rejected values.

With COOLDOWN_BYPASS=true both use the repository's ci-bypass- identity, which
Cloudsmith trusts only in the approved cloudsmith-bypass environment, and
`configure` requires georgian-ci-bypass, a one-line reason and the exact
package versions. `request` validates the same reason and versions and shows
them on the run in an earlier job, so the reviewer sees them before approving.
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
BYPASS_REPOSITORY = "georgian-ci-bypass"
MAX_REASON = 300
MAX_PACKAGES = 20
# name@version, with an optional npm scope. No whitespace, quotes or backticks,
# so a value can't add Markdown, annotations or environment records.
PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]{0,63}/)?[A-Za-z0-9][A-Za-z0-9._-]{0,99}@[A-Za-z0-9][A-Za-z0-9.+!_-]{0,63}")


def cooldown_bypass(env):
    value = env.get("COOLDOWN_BYPASS", "false")
    if value not in ("true", "false"):
        raise ValueError("cooldown-bypass must be true or false.")
    return value == "true"


def bypass_request(env):
    """The reason and package versions of a bypass, validated. Never echoes a rejected value."""
    reason = env.get("COOLDOWN_BYPASS_REASON", "")
    # A reviewer may approve their own bypass run, so the reason is the
    # written record of why it was needed.
    if not reason.strip() or "\r" in reason or "\n" in reason or len(reason) > MAX_REASON:
        raise ValueError(f"Set cooldown-bypass-reason to one line of at most {MAX_REASON} characters.")
    text = env.get("COOLDOWN_BYPASS_PACKAGES", "")
    packages = [line.strip() for line in text.split("\n") if line.strip()]
    if "\r" in text or not 1 <= len(packages) <= MAX_PACKAGES or not all(PACKAGE.fullmatch(p) for p in packages):
        raise ValueError(f"Set cooldown-bypass-packages to 1 to {MAX_PACKAGES} lines of name@version.")
    return reason, packages


def write_summary(env, text):
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(single_line(summary), "a", encoding="utf-8") as output:
            output.write(text)


def bypass_record(env, title, lead, reason, packages, closing):
    """A warning annotation plus a job summary, both visible on the run page."""
    escaped = reason.replace("%", "%25")
    print(f"::warning title={title}::{lead} {', '.join(packages)}. Reason: {escaped}")
    actor = env.get("GITHUB_TRIGGERING_ACTOR") or env.get("GITHUB_ACTOR") or "unknown"
    listed = "".join(f"  - `{package}`\n" for package in packages)
    write_summary(env, f"### {title}\n\n- Started by: {actor}\n- Packages:\n{listed}- Reason: {reason}\n\n{closing}\n")


def request(env):
    """Run in a job before the gated one: the reviewer reads this while approving."""
    reason, packages = bypass_request(env)
    bypass_record(
        env, "Cloudsmith cooldown bypass requested", "A later job in this run waits for approval to install", reason, packages,
        "Approve the `cloudsmith-bypass` deployment only if these versions and this reason match the change. "
        "If this summary is missing, reject the deployment.",
    )


def service_slug(repository, prefix="ci-pull"):
    """Name of the per-repository Cloudsmith service: lowercase, hyphens, at most 64 characters."""
    candidate = re.sub(r"[^a-z0-9]+", "-", f"{prefix}-{repository}".lower()).strip("-")
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
    # Cloudsmith trusts the ci-bypass- identity only for a token with the
    # cloudsmith-bypass environment claim, so a job outside that approved
    # environment fails at authentication.
    slug = service_slug(repository, "ci-bypass" if cooldown_bypass(env) else "ci-pull")
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

    bypass = cooldown_bypass(env)
    if bypass:
        # The bypass identity can read only georgian-ci-bypass. Naming it keeps
        # the bypass visible in the workflow diff.
        if any(repository and repository != BYPASS_REPOSITORY for repository in (npm, python)):
            raise ValueError(f"With cooldown-bypass, set each repository input to {BYPASS_REPOSITORY}.")
        reason, packages = bypass_request(env)

    token = env["CLOUDSMITH_API_KEY"]
    if not TOKEN.fullmatch(token):
        raise ValueError("Invalid Cloudsmith credential format.")
    config_dir = Path(single_line(env["RUNNER_TEMP"])) / "cloudsmith"
    environment_file = single_line(env["GITHUB_ENV"])

    if bypass:
        bypass_record(
            env, "Cloudsmith cooldown bypass", f"This approved job installs from {BYPASS_REPOSITORY}, skipping the cooldown, for",
            reason, packages, "Approver: see this run's deployment review for the `cloudsmith-bypass` environment.",
        )

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


COMMANDS = {"identity": identity, "configure": configure, "request": request}

if __name__ == "__main__":
    try:
        COMMANDS[sys.argv[1]](os.environ)
    except ValueError as error:
        # Messages name the input that failed, never its value.
        print(f"Cloudsmith setup failed: {error}", file=sys.stderr)
        sys.exit(1)
    except (KeyError, IndexError):
        print("Cloudsmith setup failed: invalid or missing input.", file=sys.stderr)
        sys.exit(1)
