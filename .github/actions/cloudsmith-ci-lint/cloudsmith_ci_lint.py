#!/usr/bin/env python3
"""Report GitHub Actions dependency installs that do not go through Cloudsmith.

The lint reads .github/workflows, follows Makefile targets and uv project
metadata that workflow steps call, and reports CS001-CS005 findings.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None


POETRY_COMMAND = re.compile(r"\Apoetry\s+(?:install|add|lock|update|sync)(?:\s|\Z)", re.ASCII)

INSTALL_COMMANDS = [
    re.compile(r"\Anpm\s+(?:ci|install|i)(?:\s|\Z)", re.ASCII),
    re.compile(r"\Apnpm\s+(?:install|i)(?:\s|\Z)", re.ASCII),
    re.compile(r"\Ayarn\s+install(?:\s|\Z)", re.ASCII),
    re.compile(r"\Apip3?\s+install(?:\s|\Z)", re.ASCII),
    re.compile(r"\Apython3?\s+-m\s+pip\s+install(?:\s|\Z)", re.ASCII),
    re.compile(r"\Auv\s+sync(?:\s|\Z)", re.ASCII),
    re.compile(r"\Auv\s+pip\s+install(?:\s|\Z)", re.ASCII),
    re.compile(r"\Auv\s+tool\s+install(?:\s|\Z)", re.ASCII),
    POETRY_COMMAND,
    re.compile(r"\Apipenv\s+install(?:\s|\Z)", re.ASCII),
]

MAKEFILE_NAMES = ["Makefile", "makefile", "GNUmakefile"]
SOURCE_CONFIG_FILES = [".npmrc", ".pypirc", "pip.conf", "pip.ini", "uv.toml", "pyproject.toml", "poetry.toml"]
MAX_MAKE_DEPTH = 5

PUBLIC_REGISTRIES = [
    "registry.npmjs.org",
    "registry.npmjs.com",
    "pypi.org/simple",
    "files.pythonhosted.org",
]

CLOUDSMITH_HOSTS = [
    "npm.cloudsmith.io",
    "dl.cloudsmith.io",
]

CLOUDSMITH_AUTH_ACTIONS = [
    re.compile(r"\Acloudsmith-io/cloudsmith-cli-action@"),
]

CLOUDSMITH_SETUP_ACTIONS = [
    re.compile(r"\A\./\.github/actions/setup-cloudsmith(?:@|\Z)"),
    re.compile(r"\Ageorgian-io/terraform-infra/\.github/actions/setup-cloudsmith@"),
    re.compile(r"\Ageorgian-io/global-github-actions-public/\.github/actions/setup-cloudsmith@"),
]

ENV_PREFIX = re.compile(r"\A(?:sudo\s+)?(?:env\s+)?(?:[A-Z_][A-Z0-9_]*=\S+\s+)*", re.ASCII | re.IGNORECASE)
MAKE_COMMAND = re.compile(r"\A(?:make|gmake)(?:\s|\Z)", re.ASCII)
UV_RUN = re.compile(r"\Auv\s+run(?:\s|\Z)", re.ASCII)
UV_RUN_PLAYWRIGHT = re.compile(r"\Auv\s+run\s+playwright\s+install\b", re.ASCII)
UV_EPHEMERAL = re.compile(r"\s--(?:with|with-editable|from)(?:=|\s)", re.ASCII)
UV_NO_SYNC = re.compile(r"\s--no-sync(?:\s|\Z)", re.ASCII)
UV_NO_PROJECT = re.compile(r"\s--no-project(?:\s|\Z)", re.ASCII)
UV_EXTRAS = re.compile(r"\s--(?:extra|all-extras)(?:[=\s]|\Z)", re.ASCII)
INSTALL_LIKE_TARGET = re.compile(r"\A(?:install|setup|bootstrap|deps?|dependencies)(?:[-_].*)?\Z", re.IGNORECASE)

EXEMPT_VARIABLE = "CLOUDSMITH_LINT_EXEMPT"
ALLOW_PUBLIC_VARIABLE = "CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY"
FALSE_VALUES = {"", "false", "0"}

FINDINGS = {
    "CS001": (
        "This install job does not configure Cloudsmith before dependency installation.",
        "Add the shared setup-cloudsmith action before the install step.",
    ),
    "CS002": (
        "This Cloudsmith install job cannot request a GitHub OIDC token.",
        "Set permissions.id-token to write for the workflow or job.",
    ),
    "CS003": (
        "This install job contains an explicit public package registry.",
        "Use Cloudsmith, or set CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY with a review reason.",
    ),
    "CS004": (
        "This dependency-install wrapper could not be resolved statically.",
        "Use an explicit package-manager command or add a bounded resolver for this wrapper.",
    ),
    "CS005": (
        "This Poetry install downloads from PyPI because pyproject.toml has no primary Cloudsmith source.",
        "Declare the Cloudsmith repository as a primary [[tool.poetry.source]] in pyproject.toml.",
    ),
}


class LintError(Exception):
    """A workflow could not be read or does not have the expected shape."""


@dataclass(frozen=True)
class Finding:
    code: str
    file: str
    job: str
    step: str
    trace: str

    @property
    def message(self) -> str:
        return FINDINGS[self.code][0]

    @property
    def fix(self) -> str:
        return FINDINGS[self.code][1]


@dataclass
class InstallObservation:
    index: int
    command: str
    trace: str
    source_text: str
    directory: Path
    unknown: bool = False


@dataclass
class MakeCommand:
    command: str
    trace: str
    source_text: str


@dataclass
class MakeRule:
    prerequisites: list[str] = field(default_factory=list)
    recipes: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class MakeInvocation:
    unknown: bool
    targets: list[str]
    commands: list[MakeCommand] = field(default_factory=list)


def text(value: Any) -> str:
    return "" if value is None else str(value)


def mapping(value: Any) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}


def read_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def scalar_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(scalar_text(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(scalar_text(item) for item in value)
    return ""


def shell_segments(script: Any) -> list[str]:
    segments = []
    for line in text(script).split("\n"):
        if line.lstrip().startswith("#"):
            continue
        for part in re.split(r"&&|\|\||;", line):
            part = part.strip()
            if part:
                segments.append(ENV_PREFIX.sub("", part, count=1))
    return segments


def direct_install_command(segment: str) -> bool:
    return any(pattern.search(segment) for pattern in INSTALL_COMMANDS)


def normalize(path: Path) -> Path:
    return Path(os.path.abspath(path))


def path_within(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def effective_working_directory(root: Path, workflow: dict, job: dict, step: dict) -> Path | None:
    candidates = [
        step.get("working-directory"),
        mapping(mapping(job.get("defaults")).get("run")).get("working-directory"),
        mapping(mapping(workflow.get("defaults")).get("run")).get("working-directory"),
    ]
    value = next((text(item).strip() for item in candidates if text(item).strip()), "")
    if not value:
        return root
    if "$" in value or value.startswith("~"):
        return None

    path = normalize(root / value)
    return path if path_within(root, path) else None


def ancestor_directories(root: Path, directory: Path) -> list[Path]:
    directories = []
    current = directory
    while path_within(root, current):
        directories.append(current)
        if current == root:
            break
        current = current.parent
    return directories


def nearest_file(root: Path, directory: Path, filename: str) -> Path | None:
    candidates = (ancestor / filename for ancestor in ancestor_directories(root, directory))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def source_configuration_text(root: Path, directory: Path) -> str:
    texts = []
    for ancestor in ancestor_directories(root, directory):
        for name in SOURCE_CONFIG_FILES:
            path = ancestor / name
            content = read_file(path) if path.is_file() else None
            if content is not None:
                texts.append(content)
    return "\n".join(texts)


def logical_makefile_lines(content: str) -> list[tuple[str, int]]:
    logical: list[tuple[str, int]] = []
    pending: tuple[str, int] | None = None

    for number, line in enumerate(content.splitlines(), start=1):
        continuation = line.rstrip().endswith("\\")
        fragment = line.rstrip()[:-1] if continuation else line

        if pending:
            pending = (f"{pending[0]} {fragment.strip()}", pending[1])
            if not continuation:
                logical.append(pending)
                pending = None
        elif continuation:
            pending = (fragment, number)
        else:
            logical.append((line, number))

    if pending:
        logical.append(pending)
    return logical


def parse_makefile(content: str) -> dict[str, MakeRule]:
    rules: dict[str, MakeRule] = {}
    current_targets: list[str] = []

    for line, number in logical_makefile_lines(content):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if line.startswith("\t"):
            for target in current_targets:
                rules[target].recipes.append((stripped, number))
            continue

        if stripped.startswith("include ") or ":" not in line:
            continue

        target_text, prerequisite_text = line.split(":", 1)
        targets = target_text.split()
        if "=" in target_text or not targets:
            continue

        current_targets = targets
        for target in targets:
            rule = rules.setdefault(target, MakeRule())
            rule.prerequisites.extend(item for item in prerequisite_text.split() if item != "|")

    return rules


def makefile_for(directory: Path, name: str | None = None) -> Path | None:
    names = [name] if name is not None else MAKEFILE_NAMES
    candidates = (normalize(directory / item) for item in names)
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def resolve_make_target(
    path: Path, target: str, depth: int = 0, visited: tuple[str, ...] = ()
) -> tuple[list[MakeCommand], bool]:
    """Return the recipe commands a target runs, and whether it could not be resolved."""
    content = read_file(path)
    if content is None or depth > MAX_MAKE_DEPTH or target in visited:
        return [], True

    rule = parse_makefile(content).get(target)
    if rule is None:
        return [], True

    commands = []
    for prerequisite in rule.prerequisites:
        if "$" in prerequisite or "%" in prerequisite:
            continue
        commands.extend(resolve_make_target(path, prerequisite, depth + 1, (*visited, target))[0])
    for command, number in rule.recipes:
        commands.append(MakeCommand(command.lstrip("@").strip(), f"{path}:{number}", content))
    return commands, False


def parse_make_invocation(segment: str, directory: Path) -> MakeInvocation:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return MakeInvocation(unknown=True, targets=[])

    make_directory = directory
    makefile_name = None
    targets: list[str] = []
    arguments = iter(tokens[1:])
    for token in arguments:
        if token in ("-C", "--directory"):
            value = next(arguments, "")
            if not value or "$" in value:
                return MakeInvocation(unknown=True, targets=targets)
            make_directory = directory / value
        elif token.startswith("-C") and len(token) > 2:
            make_directory = directory / token[2:]
        elif token in ("-f", "--file"):
            makefile_name = next(arguments, "")
        elif token.startswith("-f") and len(token) > 2:
            makefile_name = token[2:]
        elif not token.startswith("-"):
            targets.append(token)

    targets = targets or ["all"]
    make_directory = normalize(make_directory)
    makefile = makefile_for(make_directory, makefile_name) if path_within(directory, make_directory) else None
    if makefile is None:
        return MakeInvocation(unknown=True, targets=targets)

    invocation = MakeInvocation(unknown=False, targets=targets)
    for target in targets:
        commands, unknown = resolve_make_target(makefile, target)
        invocation.commands.extend(commands)
        invocation.unknown = invocation.unknown or (unknown and not commands)
    return invocation


def install_like_target(target: str) -> bool:
    return INSTALL_LIKE_TARGET.search(target) is not None


def toml_section(content: str, name: str) -> str:
    active = False
    lines = []
    for line in content.splitlines(keepends=True):
        match = re.search(r"^\s*\[([^\]]+)\]\s*$", line, re.ASCII)
        if match:
            active = match.group(1) == name
            continue
        if active:
            lines.append(line)
    return "".join(lines)


def has_quoted_line(content: str) -> bool:
    return any(re.match(r"\s*[\"']", line, re.ASCII) for line in content.splitlines())


def nonempty_toml_array(content: str, key: str) -> bool:
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*\[(.*?)\]", content, re.ASCII | re.MULTILINE | re.DOTALL)
    return bool(match) and has_quoted_line(match.group(1))


def nonempty_toml_array_value(content: str) -> bool:
    pattern = r"^\s*[^#\s][^=\n]+\s*=\s*\[(.*?)\]"
    return any(
        has_quoted_line(match.group(1))
        for match in re.finditer(pattern, content, re.ASCII | re.MULTILINE | re.DOTALL)
    )


def poetry_dependencies(content: str) -> bool:
    for line in toml_section(content, "tool.poetry.dependencies").splitlines():
        match = re.match(r"\s*([A-Za-z0-9_.-]+)\s*=", line, re.ASCII)
        if match and match.group(1).lower() != "python":
            return True
    return False


def project_has_external_dependencies(root: Path, directory: Path, command: str) -> bool:
    pyproject = nearest_file(root, directory, "pyproject.toml")
    content = read_file(pyproject) if pyproject else None
    if content is None:
        return False

    if nonempty_toml_array(toml_section(content, "project"), "dependencies"):
        return True
    if UV_EXTRAS.search(command) and nonempty_toml_array_value(toml_section(content, "project.optional-dependencies")):
        return True
    if poetry_dependencies(content):
        return True

    lockfile = nearest_file(root, directory, "uv.lock")
    lock_content = read_file(lockfile) if lockfile else None
    if lock_content is None:
        return False

    packages = re.findall(r"^\[\[package\]\]\s*\nname\s*=\s*[\"']([^\"']+)[\"']", lock_content, re.ASCII | re.MULTILINE)
    return len(packages) > 1


def uv_run_install(command: str, root: Path, directory: Path) -> bool:
    if not UV_RUN.search(command) or UV_RUN_PLAYWRIGHT.search(command):
        return False
    if UV_EPHEMERAL.search(command):
        return True
    if UV_NO_SYNC.search(command) or UV_NO_PROJECT.search(command):
        return False
    return project_has_external_dependencies(root, directory, command)


def poetry_sources(content: str) -> list[dict[str, Any]]:
    if tomllib is None:
        return scanned_poetry_sources(content)
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return []
    sources = mapping(mapping(data.get("tool")).get("poetry")).get("source")
    return [source for source in sources if isinstance(source, dict)] if isinstance(sources, list) else []


def scanned_poetry_sources(content: str) -> list[dict[str, Any]]:
    """Read [[tool.poetry.source]] tables without tomllib, for Python 3.10."""
    sources: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            current = {} if stripped.replace(" ", "") == "[[tool.poetry.source]]" else None
            if current is not None:
                sources.append(current)
            continue
        match = re.match(r"([A-Za-z0-9_-]+)\s*=\s*(\"[^\"]*\"|'[^']*'|true|false)", stripped)
        if current is not None and match:
            key, raw = match.groups()
            current[key] = raw == "true" if raw in ("true", "false") else raw[1:-1]
    return sources


def cloudsmith_python_index(url: str) -> bool:
    # Parse the URL rather than search it: "https://evil.example/cloudsmith.io/"
    # must not count as Cloudsmith.
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return False
    return (
        parts.scheme == "https"
        and (host == "cloudsmith.io" or host.endswith(".cloudsmith.io"))
        and "/georgian/" in parts.path
    )


def poetry_uses_cloudsmith(root: Path, directory: Path) -> bool:
    pyproject = nearest_file(root, directory, "pyproject.toml")
    content = read_file(pyproject) if pyproject else None
    if content is None:
        return False
    return any(
        cloudsmith_python_index(text(source.get("url")))
        and (source.get("priority") == "primary" or source.get("default") is True)
        for source in poetry_sources(content)
    )


def install_observations(root: Path, workflow: dict, job: dict, step: Any, index: int) -> list[InstallObservation]:
    if not isinstance(step, dict):
        return []

    directory = effective_working_directory(root, workflow, job, step)
    lookup_directory = directory or root
    source_text = source_configuration_text(root, lookup_directory)
    observations = []

    def observe(command: str, trace: str, sources: str, unknown: bool = False) -> None:
        observations.append(InstallObservation(index, command, trace, sources, lookup_directory, unknown))

    for segment in shell_segments(step.get("run")):
        if direct_install_command(segment):
            observe(segment, f"Workflow command `{segment}`.", f"{segment}\n{source_text}")
            continue

        if MAKE_COMMAND.search(segment):
            if directory:
                invocation = parse_make_invocation(segment, directory)
            else:
                invocation = MakeInvocation(unknown=True, targets=[])
            if invocation.unknown:
                if any(install_like_target(target) for target in invocation.targets):
                    observe(segment, f"Could not statically resolve `{segment}`.", f"{segment}\n{source_text}", True)
                continue

            for resolved in invocation.commands:
                for command in shell_segments(resolved.command):
                    if direct_install_command(command) or uv_run_install(command, root, lookup_directory):
                        observe(
                            command,
                            f"`{segment}` -> {resolved.trace} -> `{command}`.",
                            f"{segment}\n{resolved.source_text}\n{source_text}",
                        )
            continue

        if uv_run_install(segment, root, lookup_directory):
            if UV_EPHEMERAL.search(segment):
                trace = f"Workflow command `{segment}` requests an ephemeral uv package."
            else:
                trace = f"Workflow command `{segment}` resolved through project metadata."
            observe(segment, trace, f"{segment}\n{source_text}")

    return observations


def action_matches(step: Any, patterns: list[re.Pattern[str]]) -> bool:
    uses = text(step.get("uses")) if isinstance(step, dict) else ""
    return any(pattern.search(uses) for pattern in patterns)


def cloudsmith_configuration(value: Any) -> bool:
    content = scalar_text(value).lower()
    return any(host in content for host in CLOUDSMITH_HOSTS)


def public_registry(value: Any) -> bool:
    content = scalar_text(value).lower()
    return any(host in content for host in PUBLIC_REGISTRIES)


def permission_value(workflow: dict, job: dict, name: str) -> str:
    """Return a job's permission, falling back to the workflow only when the job sets none."""
    for permissions in (job.get("permissions"), workflow.get("permissions")):
        if isinstance(permissions, dict):
            return text(permissions.get(name))
        if permissions == "write-all":
            return "write"
        if permissions == "read-all":
            return "read"
        if permissions is not None:
            return ""
    return ""


def step_label(step: Any, index: int) -> str:
    name = text(step.get("name")).strip() if isinstance(step, dict) else ""
    return name or f"step {index + 1}"


def env_flag(job: dict, name: str) -> bool:
    value = mapping(job.get("env")).get(name)
    if value is None or value is False:
        return False
    return text(value).strip().lower() not in FALSE_VALUES


def lint_job(root: Path, workflow: dict, relative_file: str, job_name: str, job: dict) -> list[Finding]:
    steps = job.get("steps")
    if "uses" in job or not isinstance(steps, list) or env_flag(job, EXEMPT_VARIABLE):
        return []

    observations = [
        observation
        for index, step in enumerate(steps)
        for observation in install_observations(root, workflow, job, step, index)
    ]

    findings: list[Finding] = []
    reported: set[tuple[str, int]] = set()

    def report(code: str, observation: InstallObservation) -> None:
        if (code, observation.index) in reported:
            return
        reported.add((code, observation.index))
        step = step_label(steps[observation.index], observation.index)
        findings.append(Finding(code, relative_file, job_name, step, observation.trace))

    oidc_missing = permission_value(workflow, job, "id-token") != "write"
    allow_public = env_flag(job, ALLOW_PUBLIC_VARIABLE)

    for observation in observations:
        if observation.unknown:
            report("CS004", observation)
            continue

        prior_steps = steps[:observation.index]
        shared_setup = any(action_matches(step, CLOUDSMITH_SETUP_ACTIONS) for step in prior_steps)
        oidc_auth = shared_setup or any(action_matches(step, CLOUDSMITH_AUTH_ACTIONS) for step in prior_steps)
        registry_configuration = (
            cloudsmith_configuration(job.get("env"))
            or any(cloudsmith_configuration(step) for step in prior_steps)
            or cloudsmith_configuration(observation.source_text)
        )
        if not (shared_setup or (oidc_auth and registry_configuration)):
            report("CS001", observation)

        # Permissions belong to the job, so report them once, on its first resolved install.
        if oidc_missing and not any(code == "CS002" for code, _index in reported):
            report("CS002", observation)

        public_before_install = (
            public_registry(job.get("env"))
            or any(public_registry(step) for step in steps[:observation.index + 1])
            or public_registry(observation.source_text)
        )
        if public_before_install and not allow_public:
            report("CS003", observation)

        if POETRY_COMMAND.search(observation.command) and not poetry_uses_cloudsmith(root, observation.directory):
            report("CS005", observation)

    return findings


def parse_workflow(path: Path) -> dict:
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as error:
        raise LintError(f"Cannot read {path}: {error}") from error
    except yaml.YAMLError as error:
        raise LintError(f"Cannot parse {path}: {error}") from error
    if not isinstance(content, dict):
        raise LintError(f"Cannot lint {path}: the workflow is not a YAML mapping.")
    return content


def workflow_files(root: Path) -> list[Path]:
    base = root / ".github" / "workflows"
    files = [
        Path(directory, name)
        for directory, _subdirectories, names in os.walk(base)
        for name in names
        if Path(name).suffix in (".yml", ".yaml") and Path(directory, name).is_file()
    ]
    return sorted(files, key=lambda path: path.parts)


def lint_workflow(root: Path, path: Path) -> list[Finding]:
    workflow = parse_workflow(path)
    relative_file = str(path.relative_to(root))
    jobs = workflow.get("jobs")
    if jobs is None:
        return []
    if not isinstance(jobs, dict):
        raise LintError(f"Cannot lint {path}: jobs is not a mapping.")

    findings = []
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            raise LintError(f"Cannot lint {path}: job {job_name} is not a mapping.")
        findings.extend(lint_job(root, workflow, relative_file, text(job_name), job))
    return findings


def lint_repository(root: Path) -> tuple[list[Finding], list[tuple[str, str]]]:
    # One unreadable workflow is reported and skipped. It must not fail a
    # required check that is still in warning mode.
    findings, skipped = [], []
    for path in workflow_files(root):
        try:
            findings.extend(lint_workflow(root, path))
        except LintError as error:
            skipped.append((str(path.relative_to(root)), str(error)))
    return findings, skipped


def escape_command(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def emit(finding: Finding, mode: str) -> None:
    level = "error" if mode == "error" else "warning"
    title = f"Cloudsmith CI lint {finding.code}"
    detail = "\n".join([
        finding.message,
        f"Job: {finding.job}.",
        f"Step: {finding.step}.",
        f"Resolution: {finding.trace}",
        f"Fix: {finding.fix}",
    ])
    print(f"::{level} file={escape_command(finding.file)},title={escape_command(title)}::{escape_command(detail)}")
    print(f"{finding.file}: {finding.code}: {finding.message}")
    print(f"  Job: {finding.job}")
    print(f"  Step: {finding.step}")
    print(f"  Resolution: {finding.trace}")
    print(f"  Fix: {finding.fix}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root.")
    parser.add_argument("--mode", choices=["warn", "error"], default="warn", help="Output mode.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = normalize(args.root.expanduser())

    findings, skipped = lint_repository(root)
    level = "error" if args.mode == "error" else "warning"
    for file, reason in skipped:
        print(f"::{level} file={escape_command(file)},title=Cloudsmith CI lint::{escape_command(reason)}")
        print(f"{file}: skipped: {reason}")

    for finding in findings:
        emit(finding, args.mode)

    if findings:
        print(f"Cloudsmith CI lint found {len(findings)} issue(s).")
    else:
        print("Cloudsmith CI lint found no issues.")

    return 1 if args.mode == "error" and (findings or skipped) else 0


if __name__ == "__main__":
    sys.exit(main())
