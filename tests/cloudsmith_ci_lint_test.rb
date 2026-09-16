# frozen_string_literal: true

require "fileutils"
require "minitest/autorun"
require "open3"
require "tmpdir"

class CloudsmithCiLintTest < Minitest::Test
  SCRIPT = File.expand_path("../scripts/cloudsmith_ci_lint.rb", __dir__)

  def lint(workflow, mode: "error", files: {})
    Dir.mktmpdir do |root|
      directory = File.join(root, ".github", "workflows")
      FileUtils.mkdir_p(directory)
      File.write(File.join(directory, "ci.yml"), workflow)
      files.each do |path, content|
        target = File.join(root, path)
        FileUtils.mkdir_p(File.dirname(target))
        File.write(target, content)
      end
      return Open3.capture3(
        "ruby",
        SCRIPT,
        "--root",
        root,
        "--mode",
        mode
      )
    end
  end

  def test_accepts_setup_before_install
    workflow = <<~YAML
      permissions:
        contents: read
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
              with:
                npm-repository: javascript-all
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    assert status.success?
    assert_includes stdout, "found no issues"
  end

  def test_rejects_install_without_setup
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS001"
    assert_includes stdout, "Add the shared setup-cloudsmith action"
  end

  def test_recognizes_supported_install_commands
    commands = [
      "npm ci",
      "pnpm install --frozen-lockfile",
      "yarn install --immutable",
      "pip install -r requirements.txt",
      "pip3 install -r requirements.txt",
      "python -m pip install -r requirements.txt",
      "npm install -g actionlint",
      "uv sync",
      "uv pip install -r requirements.txt",
      "uv tool install actionlint",
      "poetry install",
      "pipenv install"
    ]

    commands.each do |command|
      workflow = <<~YAML
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: #{command}
      YAML

      stdout, _stderr, status = lint(workflow)

      refute status.success?, command
      assert_includes stdout, "CS001", command
    end
  end

  def test_ignores_non_install_commands
    commands = [
      "npm publish",
      "npm run test",
      "pip list",
      "echo npm install"
    ]

    commands.each do |command|
      workflow = <<~YAML
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: #{command}
      YAML

      stdout, _stderr, status = lint(workflow)

      assert status.success?, command
      assert_includes stdout, "found no issues", command
    end
  end

  def test_detects_installs_in_compound_commands
    ["npm ci && npm test", "echo ready; pip install six", "uv sync || echo failed"].each do |command|
      stdout, _stderr, status = lint(<<~YAML)
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - run: #{command}
      YAML

      refute status.success?, command
      assert_includes stdout, "CS001", command
    end
  end

  def test_rejects_setup_after_install
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - run: uv sync
            - uses: cloudsmith-io/cloudsmith-cli-action@v2
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS001"
  end

  def test_rejects_missing_oidc_permission
    workflow = <<~YAML
      permissions:
        contents: read
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: cloudsmith-io/cloudsmith-cli-action@v2
            - run: pip install -r requirements.txt
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS002"
    assert_includes stdout, "permissions.id-token"
  end

  def test_job_permissions_do_not_inherit_omitted_workflow_permission
    workflow = <<~YAML
      permissions:
        contents: read
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          permissions:
            contents: read
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS002"
  end

  def test_accepts_job_write_all_permission
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          permissions: write-all
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
              with:
                npm-repository: georgian-test
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    assert status.success?, stdout
    assert_includes stdout, "found no issues"
  end

  def test_rejects_job_read_all_permission
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          permissions: read-all
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS002"
  end

  def test_accepts_public_setup_action
    workflow = <<~YAML
      permissions:
        contents: read
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: georgian-io/global-github-actions-public/.github/actions/setup-cloudsmith@main
              with:
                npm-repository: georgian-test
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    assert status.success?
    assert_includes stdout, "found no issues"
  end

  def test_reports_setup_and_oidc_findings_together
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS001"
    assert_includes stdout, "CS002"
  end

  def test_rejects_public_registry_in_install_job
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
            - run: npm ci --registry https://registry.npmjs.org
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS003"
  end

  def test_allows_reviewed_public_registry_reason
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        release:
          runs-on: ubuntu-latest
          env:
            CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY: Public npm release.
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
            - run: npm ci --registry https://registry.npmjs.org
    YAML

    stdout, _stderr, status = lint(workflow)

    assert status.success?
    assert_includes stdout, "found no issues"
  end

  def test_ignores_public_registry_in_publish_only_job
    workflow = <<~YAML
      jobs:
        release:
          runs-on: ubuntu-latest
          steps:
            - uses: actions/setup-node@v4
              with:
                registry-url: https://registry.npmjs.org
            - run: npm publish
    YAML

    stdout, _stderr, status = lint(workflow)

    assert status.success?
    assert_includes stdout, "found no issues"
  end

  def test_ignores_public_registry_after_cloudsmith_install
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        release:
          runs-on: ubuntu-latest
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
            - run: npm ci
            - uses: actions/setup-node@v4
              with:
                registry-url: https://registry.npmjs.org
            - run: npm publish
    YAML

    stdout, _stderr, status = lint(workflow)

    assert status.success?
    assert_includes stdout, "found no issues"
  end

  def test_accepts_direct_auth_with_registry_configuration
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          env:
            NPM_CONFIG_REGISTRY: https://npm.cloudsmith.io/georgian/javascript-all/
          steps:
            - uses: cloudsmith-io/cloudsmith-cli-action@v2
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    assert status.success?
    assert_includes stdout, "found no issues"
  end

  def test_resolves_make_install_target
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - name: Install dependencies
              run: make install-dev
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: { "Makefile" => "install-dev:\n\tuv sync --all-extras\n" }
    )

    refute status.success?
    assert_includes stdout, "CS001"
    assert_includes stdout, "CS002"
    assert_includes stdout, "Makefile"
    assert_includes stdout, "uv sync --all-extras"
  end

  def test_accepts_make_install_after_cloudsmith_setup
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
            - name: Install dependencies
              run: make install-dev
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: { "Makefile" => "install-dev:\n\tuv sync --all-extras\n" }
    )

    assert status.success?, stdout
    assert_includes stdout, "found no issues"
  end

  def test_reports_public_registry_in_resolved_make_recipe
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: georgian-io/terraform-infra/.github/actions/setup-cloudsmith@main
            - name: Install dependencies
              run: make install-dev
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: { "Makefile" => "install-dev:\n\tuv sync --index-url https://pypi.org/simple\n" }
    )

    refute status.success?
    assert_includes stdout, "CS003"
    assert_includes stdout, "Makefile"
  end

  def test_resolves_make_working_directory
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - name: Install dependencies
              working-directory: services/api
              run: make install-dev
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: { "services/api/Makefile" => "install-dev:\n\tuv sync\n" }
    )

    refute status.success?
    assert_includes stdout, "CS001"
    assert_includes stdout, "services/api/Makefile"
  end

  def test_detects_uv_run_with_external_project_dependencies
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - name: Run generator
              run: uv run scripts/generate.py
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: {
        "pyproject.toml" => "[project]\nname = \"demo\"\ndependencies = [\n  \"requests\",\n]\n"
      }
    )

    refute status.success?
    assert_includes stdout, "CS001"
    assert_includes stdout, "CS002"
    assert_includes stdout, "project metadata"
  end

  def test_ignores_uv_run_with_no_sync
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - run: uv run --no-sync scripts/generate.py
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: {
        "pyproject.toml" => "[project]\nname = \"demo\"\ndependencies = [\n  \"requests\",\n]\n"
      }
    )

    assert status.success?, stdout
    assert_includes stdout, "found no issues"
  end

  def test_ignores_uv_run_without_external_dependencies
    workflow = <<~YAML
      jobs:
        release:
          runs-on: ubuntu-latest
          steps:
            - run: uv run scripts/create_archive.py
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: {
        "pyproject.toml" => "[project]\nname = \"demo\"\ndependencies = []\n",
        "uv.lock" => "version = 1\n\n[[package]]\nname = \"demo\"\n"
      }
    )

    assert status.success?, stdout
    assert_includes stdout, "found no issues"
  end

  def test_detects_uv_run_with_ephemeral_package
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - run: uv run --no-project --with pytest scripts/test.py
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS001"
    assert_includes stdout, "CS002"
  end

  def test_reports_unresolved_make_install_wrapper
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - name: Install dependencies
              run: make install-ci
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS004"
    refute_includes stdout, "CS001"
  end

  def test_accepts_direct_auth_with_cloudsmith_source_in_makefile
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: cloudsmith-io/cloudsmith-cli-action@v2
            - run: make install-dev
    YAML

    stdout, _stderr, status = lint(
      workflow,
      files: { "Makefile" => "install-dev:\n\tuv sync --index-url https://dl.cloudsmith.io/georgian/python/georgian-test/simple/\n" }
    )

    assert status.success?, stdout
    assert_includes stdout, "found no issues"
  end

  def test_rejects_direct_auth_without_registry_configuration
    workflow = <<~YAML
      permissions:
        id-token: write
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - uses: cloudsmith-io/cloudsmith-cli-action@v2
            - run: npm ci
    YAML

    stdout, _stderr, status = lint(workflow)

    refute status.success?
    assert_includes stdout, "CS001"
  end

  def test_warn_mode_reports_without_failure
    workflow = <<~YAML
      jobs:
        test:
          runs-on: ubuntu-latest
          steps:
            - run: poetry install
    YAML

    stdout, _stderr, status = lint(workflow, mode: "warn")

    assert status.success?
    assert_includes stdout, "::warning"
    assert_includes stdout, "CS001"
  end
end
