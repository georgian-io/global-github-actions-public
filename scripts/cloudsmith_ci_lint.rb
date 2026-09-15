#!/usr/bin/env ruby
# frozen_string_literal: true

require "find"
require "optparse"
require "pathname"
require "yaml"

INSTALL_COMMANDS = [
  /\Anpm\s+(?:ci|install|i)(?:\s|\z)/,
  /\Apnpm\s+(?:install|i)(?:\s|\z)/,
  /\Ayarn\s+install(?:\s|\z)/,
  /\Apip3?\s+install(?:\s|\z)/,
  /\Apython3?\s+-m\s+pip\s+install(?:\s|\z)/,
  /\Auv\s+sync(?:\s|\z)/,
  /\Auv\s+pip\s+install(?:\s|\z)/,
  /\Apoetry\s+install(?:\s|\z)/,
  /\Apipenv\s+install(?:\s|\z)/
].freeze

PUBLIC_REGISTRIES = [
  "registry.npmjs.org",
  "registry.npmjs.com",
  "pypi.org/simple",
  "files.pythonhosted.org"
].freeze

CLOUDSMITH_HOSTS = [
  "npm.cloudsmith.io",
  "dl.cloudsmith.io"
].freeze

CLOUDSMITH_AUTH_ACTIONS = [
  %r{\Acloudsmith-io/cloudsmith-cli-action@}
].freeze

CLOUDSMITH_SETUP_ACTIONS = [
  %r{\A\./\.github/actions/setup-cloudsmith(?:@|\z)},
  %r{\Ageorgian-io/terraform-infra/\.github/actions/setup-cloudsmith@},
  %r{\Ageorgian-io/global-github-actions-public/\.github/actions/setup-cloudsmith@}
].freeze

Finding = Struct.new(
  :code,
  :file,
  :job,
  :step,
  :message,
  :fix,
  keyword_init: true
)

def scalar_text(value)
  case value
  when String
    value
  when Hash
    value.values.map { |item| scalar_text(item) }.join("\n")
  when Array
    value.map { |item| scalar_text(item) }.join("\n")
  else
    ""
  end
end

def shell_segments(script)
  script.to_s
        .lines
        .reject { |line| line.lstrip.start_with?("#") }
        .flat_map { |line| line.split(/&&|\|\||;/) }
        .map(&:strip)
        .reject(&:empty?)
        .map { |line| line.sub(/\A(?:sudo\s+)?(?:env\s+)?(?:[A-Z_][A-Z0-9_]*=\S+\s+)*/i, "") }
end

def install_step?(step)
  return false unless step.is_a?(Hash)

  shell_segments(step["run"]).any? do |segment|
    INSTALL_COMMANDS.any? { |pattern| pattern.match?(segment) }
  end
end

def action_matches?(step, patterns)
  uses = step.is_a?(Hash) ? step["uses"].to_s : ""
  patterns.any? { |pattern| pattern.match?(uses) }
end

def cloudsmith_configuration?(value)
  text = scalar_text(value).downcase
  CLOUDSMITH_HOSTS.any? { |host| text.include?(host) }
end

def public_registry?(value)
  text = scalar_text(value).downcase
  PUBLIC_REGISTRIES.any? { |host| text.include?(host) }
end

def permission_value(workflow, job, name)
  job_permissions = job["permissions"]
  workflow_permissions = workflow["permissions"]

  if job_permissions.is_a?(Hash)
    return job_permissions.fetch(name, "").to_s
  end
  return "write" if job_permissions == "write-all"
  return "read" if job_permissions == "read-all"
  return "" unless job_permissions.nil?

  return workflow_permissions[name].to_s if workflow_permissions.is_a?(Hash)
  return "write" if workflow_permissions == "write-all"
  return "read" if workflow_permissions == "read-all"

  ""
end

def step_label(step, index)
  name = step.is_a?(Hash) ? step["name"].to_s.strip : ""
  name.empty? ? "step #{index + 1}" : name
end

def exemption_reason(job)
  env = job["env"]
  return "" unless env.is_a?(Hash)

  env["CLOUDSMITH_LINT_EXEMPT"].to_s.strip
end

def public_registry_reason(job)
  env = job["env"]
  return "" unless env.is_a?(Hash)

  env["CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY"].to_s.strip
end

def lint_job(workflow, relative_file, job_name, job)
  return [] unless job.is_a?(Hash)
  return [] if job.key?("uses")

  steps = job["steps"]
  return [] unless steps.is_a?(Array)
  return [] if steps.none? { |step| install_step?(step) }

  exemption = exemption_reason(job)
  return [] unless exemption.empty?

  findings = []
  first_install = steps.index { |step| install_step?(step) }
  prior_steps = steps[0...first_install]
  through_install = steps[0..first_install]
  shared_setup = prior_steps.any? do |step|
    action_matches?(step, CLOUDSMITH_SETUP_ACTIONS)
  end
  oidc_auth = shared_setup || prior_steps.any? do |step|
    action_matches?(step, CLOUDSMITH_AUTH_ACTIONS)
  end
  registry_configuration = cloudsmith_configuration?(job["env"]) ||
                           prior_steps.any? { |step| cloudsmith_configuration?(step) }
  setup_present = shared_setup || (oidc_auth && registry_configuration)

  unless setup_present
    install = steps[first_install]
    findings << Finding.new(
      code: "CS001",
      file: relative_file,
      job: job_name,
      step: step_label(install, first_install),
      message: "This install job does not configure Cloudsmith before dependency installation.",
      fix: "Add the shared setup-cloudsmith action before the install step."
    )
  end

  if permission_value(workflow, job, "id-token") != "write"
    findings << Finding.new(
      code: "CS002",
      file: relative_file,
      job: job_name,
      step: step_label(steps[first_install], first_install),
      message: "This Cloudsmith install job cannot request a GitHub OIDC token.",
      fix: "Set permissions.id-token to write for the workflow or job."
    )
  end

  allow_public = public_registry_reason(job)
  public_before_install = public_registry?(job["env"]) ||
                          through_install.any? { |step| public_registry?(step) }
  if allow_public.empty? && public_before_install
    findings << Finding.new(
      code: "CS003",
      file: relative_file,
      job: job_name,
      step: step_label(steps[first_install], first_install),
      message: "This install job contains an explicit public package registry.",
      fix: "Use Cloudsmith, or set CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY with a review reason."
    )
  end

  findings
end

def parse_workflow(path)
  content = YAML.safe_load(
    path.read,
    permitted_classes: [],
    permitted_symbols: [],
    aliases: true
  )
  content.is_a?(Hash) ? content : {}
rescue Psych::SyntaxError => error
  raise "Cannot parse #{path}: #{error.message}"
end

def workflow_files(root)
  base = root.join(".github", "workflows")
  return [] unless base.directory?

  files = []
  Find.find(base) do |path|
    next unless File.file?(path)
    next unless %w[.yml .yaml].include?(File.extname(path))

    files << Pathname(path)
  end
  files.sort
end

def escape_command(value)
  value.to_s.gsub("%", "%25").gsub("\r", "%0D").gsub("\n", "%0A")
end

def emit(finding, mode)
  level = mode == "error" ? "error" : "warning"
  title = "Cloudsmith CI lint #{finding.code}"
  location = "file=#{escape_command(finding.file)},title=#{escape_command(title)}"
  detail = [
    finding.message,
    "Job: #{finding.job}.",
    "Step: #{finding.step}.",
    "Fix: #{finding.fix}"
  ].join("\n")
  puts "::#{level} #{location}::#{escape_command(detail)}"
  puts "#{finding.file}: #{finding.code}: #{finding.message}"
  puts "  Job: #{finding.job}"
  puts "  Step: #{finding.step}"
  puts "  Fix: #{finding.fix}"
end

options = {
  root: Pathname.pwd,
  mode: "warn"
}

OptionParser.new do |parser|
  parser.banner = "Usage: cloudsmith_ci_lint.rb [options]"
  parser.on("--root PATH", "Repository root.") { |value| options[:root] = Pathname(value) }
  parser.on("--mode MODE", %w[warn error], "Output mode.") { |value| options[:mode] = value }
end.parse!

root = options[:root].expand_path
findings = []

workflow_files(root).each do |path|
  workflow = parse_workflow(path)
  relative_file = path.relative_path_from(root).to_s
  jobs = workflow["jobs"]
  next unless jobs.is_a?(Hash)

  jobs.each do |job_name, job|
    findings.concat(lint_job(workflow, relative_file, job_name, job))
  end
end

findings.each { |finding| emit(finding, options[:mode]) }

if findings.empty?
  puts "Cloudsmith CI lint found no issues."
else
  puts "Cloudsmith CI lint found #{findings.length} issue(s)."
end

exit(options[:mode] == "error" && findings.any? ? 1 : 0)
