"""Write package configuration and GitHub environment records safely."""

import os
from pathlib import Path
import re
import sys


def configure(env):
    npm = env.get("NPM_REPOSITORY", "")
    python = env.get("PYTHON_REPOSITORY", "")
    if not npm and not python:
        raise ValueError("Set npm-repository or python-repository.")
    for repository in (npm, python):
        if repository and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", repository):
            raise ValueError("Invalid Cloudsmith repository slug.")
    bypass = env.get("COOLDOWN_BYPASS", "false")
    if bypass not in ("true", "false"):
        raise ValueError("cooldown-bypass must be true or false.")
    # The bypass identity can read only georgian-ci-bypass. Naming it keeps
    # the bypass visible in the workflow diff.
    if bypass == "true" and any(r and r != "georgian-ci-bypass" for r in (npm, python)):
        raise ValueError("With cooldown-bypass, set each repository input to georgian-ci-bypass.")
    # A reviewer may approve their own bypass run, so the reason is the
    # written record of why it was needed.
    reason = env.get("COOLDOWN_BYPASS_REASON", "")
    if bypass == "true" and (not reason.strip() or "\n" in reason or "\r" in reason
                             or len(reason) > 300):
        raise ValueError("With cooldown-bypass, set cooldown-bypass-reason to one line of at most 300 characters.")

    token = env["CLOUDSMITH_API_KEY"]
    # The token also enters netrc. Reject whitespace and quoting characters
    # rather than allowing them to create additional netrc fields or records.
    if not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
        raise ValueError("Invalid Cloudsmith credential format.")
    config_dir = Path(env["RUNNER_TEMP"]) / "cloudsmith"
    if "\r" in str(config_dir) or "\n" in str(config_dir):
        raise ValueError("Invalid runner temporary path.")

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
        registry = f"https://npm.cloudsmith.io/georgian/{npm}/"
        values["NPM_CONFIG_USERCONFIG"] = write_config(
            "npmrc",
            f"registry={registry}\n"
            f"//npm.cloudsmith.io/georgian/{npm}/:_authToken=${{CLOUDSMITH_API_KEY}}\n"
            "always-auth=true\n",
        )
    if python:
        index = f"https://dl.cloudsmith.io/basic/georgian/{python}/python/simple/"
        values["NETRC"] = write_config(
            "netrc", f"machine dl.cloudsmith.io\nlogin token\npassword {token}\n"
        )
        values.update(PIP_INDEX_URL=index, UV_INDEX_URL=index,
                      UV_INDEX_USERNAME="token", UV_INDEX_PASSWORD=token)

    with open(env["GITHUB_ENV"], "a", encoding="utf-8") as output:
        # Repository slugs, credentials and paths were validated above. None
        # can contain CR/LF, so each assignment is exactly one environment line.
        output.writelines(f"{key}={value}\n" for key, value in values.items())
    if bypass == "true":
        print("::warning title=Cloudsmith cooldown bypass::This job installs packages "
              "from georgian-ci-bypass, which skips the package cooldown. "
              f"Reason: {reason.replace('%', '%25')}")
        started_by = env.get("GITHUB_TRIGGERING_ACTOR") or env.get("GITHUB_ACTOR") or "unknown"
        with open(env.get("GITHUB_STEP_SUMMARY") or os.devnull, "a", encoding="utf-8") as summary:
            summary.write("### Cloudsmith cooldown bypass\n\n"
                          f"- Started by: {started_by}\n"
                          f"- Reason: {reason}\n"
                          "- Approver: see this run's deployment review for the "
                          "`cloudsmith-bypass` environment.\n")


if __name__ == "__main__":
    try:
        configure(os.environ)
    except (ValueError, KeyError):
        # Do not echo rejected values, especially the credential.
        print("Cloudsmith configuration failed: invalid or missing input.", file=sys.stderr)
        sys.exit(1)
