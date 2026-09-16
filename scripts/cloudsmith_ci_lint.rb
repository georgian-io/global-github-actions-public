#!/usr/bin/env ruby
# frozen_string_literal: true

require "find"
require "optparse"
require "pathname"
require "shellwords"
require "yaml"

INSTALL_COMMANDS = [
  /\Anpm\s+(?:ci|install|i)(?:\s|\z)/,
  /\Apnpm\s+(?:install|i)(?:\s|\z)/,
  /\Ayarn\s+install(?:\s|\z)/,
  /\Apip3?\s+install(?:\s|\z)/,
  /\Apython3?\s+-m\s+pip\s+install(?:\s|\z)/,
  /\Auv\s+sync(?:\s|\z)/,
  /\Auv\s+pip\s+install(?:\s|\z)/,
  /\Auv\s+tool\s+install(?:\s|\z)/,
  /\Apoetry\s+install(?:\s|\z)/,
  /\Apipenv\s+install(?:\s|\z)/
].freeze

MAKEFILE_NAMES = %w[Makefile makefile GNUmakefile].freeze
SOURCE_CONFIG_FILES = %w[.npmrc .pypirc pip.conf pip.ini uv.toml pyproject.toml poetry.toml].freeze
MAX_MAKE_DEPTH = 5

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
  :trace,
  keyword_init: true
)

InstallObservation = Struct.new(
  :index,
  :command,
  :trace,
  :source_text,
  :unknown,
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

def direct_install_command?(segment)
  INSTALL_COMMANDS.any? { |pattern| pattern.match?(segment) }
end

def path_within?(root, path)
  relative = path.relative_path_from(root).to_s
  relative == "." || (relative != ".." && !relative.start_with?("../"))
rescue ArgumentError
  false
end

def effective_working_directory(root, workflow, job, step)
  value = step.is_a?(Hash) ? step["working-directory"] : nil
  value = job.dig("defaults", "run", "working-directory") if value.to_s.strip.empty?
  value = workflow.dig("defaults", "run", "working-directory") if value.to_s.strip.empty?
  return root if value.to_s.strip.empty?

  text = value.to_s.strip
  return nil if text.include?("$") || text.start_with?("~")

  path = Pathname(text)
  path = root.join(path) unless path.absolute?
  path = path.expand_path
  path_within?(root, path) ? path : nil
end

def ancestor_directories(root, directory)
  current = (directory || root).expand_path
  directories = []
  loop do
    break unless path_within?(root, current)

    directories << current
    break if current == root

    parent = current.parent
    break if parent == current

    current = parent
  end
  directories
end

def source_configuration_text(root, directory)
  paths = ancestor_directories(root, directory).flat_map do |ancestor|
    SOURCE_CONFIG_FILES.map { |name| ancestor.join(name) }
  end

  paths.uniq.map do |path|
    path.file? ? path.read : nil
  rescue SystemCallError
    nil
  end.compact.join("\n")
end

def logical_makefile_lines(path)
  logical = []
  pending = nil

  path.readlines.each_with_index do |raw, index|
    line = raw.chomp
    continuation = line.rstrip.end_with?("\\")
    fragment = continuation ? line.rstrip.sub(/\\\z/, "") : line

    if pending
      pending[0] << " " << fragment.strip
      if !continuation
        logical << pending
        pending = nil
      end
    elsif continuation
      pending = [fragment, index + 1]
    else
      logical << [line, index + 1]
    end
  end

  logical << pending if pending
  logical
end

def parse_makefile(path)
  rules = {}
  current_targets = []

  logical_makefile_lines(path).each do |line, line_number|
    stripped = line.strip
    next if stripped.empty? || stripped.start_with?("#")

    if line.start_with?("\t")
      current_targets.each do |target|
        rules[target][:recipes] << [stripped, line_number]
      end
      next
    end

    next if stripped.start_with?("include ")
    next unless line.include?(":")

    target_text, prerequisite_text = line.split(":", 2)
    next if target_text.include?("=")

    targets = target_text.split.map(&:strip).reject(&:empty?)
    next if targets.empty?

    prerequisites = prerequisite_text.to_s.split.reject { |item| item == "|" }
    current_targets = targets
    targets.each do |target|
      rules[target] ||= { prerequisites: [], recipes: [] }
      rules[target][:prerequisites].concat(prerequisites)
    end
  end

  rules
end

def makefile_for(directory, name = nil)
  candidates = name ? [directory.join(name)] : MAKEFILE_NAMES.map { |file| directory.join(file) }
  candidates.find(&:file?)
end

def resolve_make_target(path, target, depth = 0, visited = [])
  source_text = path.read
  return { commands: [], unknown: true, source_text: source_text } if depth > MAX_MAKE_DEPTH
  return { commands: [], unknown: true, source_text: source_text } if visited.include?(target)

  rule = parse_makefile(path)[target]
  return { commands: [], unknown: true, source_text: source_text } unless rule

  commands = []
  rule[:prerequisites].each do |prerequisite|
    next if prerequisite.include?("$") || prerequisite.include?("%")

    resolved = resolve_make_target(path, prerequisite, depth + 1, visited + [target])
    commands.concat(resolved[:commands])
  end
  rule[:recipes].each do |command, line_number|
    commands << {
      command: command.sub(/\A@+/, "").strip,
      trace: "#{path}:#{line_number}",
      source_text: source_text
    }
  end

  { commands: commands, unknown: false, source_text: source_text }
rescue SystemCallError
  { commands: [], unknown: true, source_text: "" }
end

def parse_make_invocation(segment, directory)
  tokens = Shellwords.split(segment)
  return nil unless %w[make gmake].include?(tokens.shift)

  make_directory = directory
  makefile_name = nil
  targets = []
  index = 0
  while index < tokens.length
    token = tokens[index]
    if %w[-C --directory].include?(token)
      index += 1
      value = tokens[index].to_s
      return { unknown: true, targets: targets } if value.empty? || value.include?("$")

      make_directory = Pathname(value)
      make_directory = directory.join(make_directory) unless make_directory.absolute?
    elsif token.start_with?("-C") && token.length > 2
      make_directory = directory.join(token[2..-1])
    elsif %w[-f --file].include?(token)
      index += 1
      makefile_name = tokens[index].to_s
    elsif token.start_with?("-f") && token.length > 2
      makefile_name = token[2..-1]
    elsif !token.start_with?("-")
      targets << token
    end
    index += 1
  end

  targets = ["all"] if targets.empty?
  make_directory = make_directory.expand_path
  return { unknown: true, targets: targets } unless path_within?(directory, make_directory)

  makefile = makefile_for(make_directory, makefile_name)
  return { unknown: true, targets: targets } unless makefile

  resolved_commands = []
  unknown = false
  targets.each do |target|
    resolved = resolve_make_target(makefile, target)
    resolved_commands.concat(resolved[:commands])
    unknown ||= resolved[:unknown] && resolved[:commands].empty?
  end

  {
    unknown: unknown,
    targets: targets,
    commands: resolved_commands,
    makefile: makefile
  }
rescue ArgumentError
  { unknown: true, targets: targets || [] }
end

def install_like_target?(target)
  target.match?(/\A(?:install|setup|bootstrap|deps?|dependencies)(?:[-_].*)?\z/i)
end

def nearest_file(root, directory, filename)
  ancestor_directories(root, directory).map { |ancestor| ancestor.join(filename) }.find(&:file?)
end

def toml_section(content, name)
  active = false
  lines = []
  content.each_line do |line|
    section = line[/^\s*\[([^\]]+)\]\s*$/, 1]
    if section
      active = section == name
      next
    end
    lines << line if active
  end
  lines.join
end

def nonempty_toml_array?(content, key)
  match = content.match(/^\s*#{Regexp.escape(key)}\s*=\s*\[(.*?)\]/m)
  return false unless match

  match[1].lines.any? { |line| line.match?(/^\s*["']/) }
end

def nonempty_toml_array_value?(content)
  content.scan(/^\s*[^#\s][^=\n]+\s*=\s*\[(.*?)\]/m).any? do |match|
    match[0].lines.any? { |line| line.match?(/^\s*["']/) }
  end
end

def poetry_dependencies?(content)
  section = toml_section(content, "tool.poetry.dependencies")

  section.lines.any? do |line|
    name = line[/^\s*([A-Za-z0-9_.-]+)\s*=/, 1]
    name && name.downcase != "python"
  end
end

def project_has_external_dependencies?(root, directory, command)
  pyproject = nearest_file(root, directory, "pyproject.toml")
  return false unless pyproject

  content = pyproject.read
  return true if nonempty_toml_array?(toml_section(content, "project"), "dependencies")
  if command.match?(/\s--(?:extra|all-extras)(?:[=\s]|\z)/)
    return true if nonempty_toml_array_value?(toml_section(content, "project.optional-dependencies"))
  end
  return true if poetry_dependencies?(content)

  lockfile = nearest_file(root, directory, "uv.lock")
  return false unless lockfile

  lockfile.read.scan(/^\[\[package\]\]\s*\nname\s*=\s*["']([^"']+)["']/).length > 1
rescue SystemCallError
  false
end

def uv_run_install?(command, root, directory)
  return false unless command.match?(/\Auv\s+run(?:\s|\z)/)
  return false if command.match?(/\Auv\s+run\s+playwright\s+install\b/)

  return true if command.match?(/\s--(?:with|with-editable|from)(?:=|\s)/)
  return false if command.match?(/\s--no-sync(?:\s|\z)/)
  return false if command.match?(/\s--no-project(?:\s|\z)/)

  project_has_external_dependencies?(root, directory, command)
rescue SystemCallError
  false
end

def install_observations(root, workflow, job, step, index)
  return [] unless step.is_a?(Hash)

  directory = effective_working_directory(root, workflow, job, step)
  source_text = source_configuration_text(root, directory || root)
  observations = []
  shell_segments(step["run"]).each do |segment|
    if direct_install_command?(segment)
      observations << InstallObservation.new(
        index: index,
        command: segment,
        trace: "Workflow command `#{segment}`.",
        source_text: "#{segment}\n#{source_text}",
        unknown: false
      )
      next
    end

    if segment.match?(/\A(?:make|gmake)(?:\s|\z)/)
      parsed = directory ? parse_make_invocation(segment, directory) : { unknown: true, targets: [] }
      if parsed[:unknown]
        if parsed[:targets].any? { |target| install_like_target?(target) }
          observations << InstallObservation.new(
            index: index,
            command: segment,
            trace: "Could not statically resolve `#{segment}`.",
            source_text: "#{segment}\n#{source_text}",
            unknown: true
          )
        end
        next
      end

      parsed[:commands].each do |resolved|
        shell_segments(resolved[:command]).each do |command|
          next unless direct_install_command?(command) || uv_run_install?(command, root, directory)

          observations << InstallObservation.new(
            index: index,
            command: command,
            trace: "`#{segment}` -> #{resolved[:trace]} -> `#{command}`.",
            source_text: "#{segment}\n#{resolved[:source_text]}\n#{source_text}",
            unknown: false
          )
        end
      end
      next
    end

    next unless uv_run_install?(segment, root, directory || root)

    observations << InstallObservation.new(
      index: index,
      command: segment,
      trace: "Workflow command `#{segment}` resolved through project metadata.",
      source_text: "#{segment}\n#{source_text}",
      unknown: false
    )
  end

  observations
rescue ArgumentError, SystemCallError
  []
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

def lint_job(root, workflow, relative_file, job_name, job)
  return [] unless job.is_a?(Hash)
  return [] if job.key?("uses")

  steps = job["steps"]
  return [] unless steps.is_a?(Array)

  exemption = exemption_reason(job)
  return [] unless exemption.empty?

  observations = steps.each_with_index.flat_map do |step, index|
    install_observations(root, workflow, job, step, index)
  end
  return [] if observations.empty?

  findings = []
  first_observation = observations.first
  first_install = first_observation.index
  prior_steps = steps[0...first_install]
  through_install = steps[0..first_install]

  if first_observation.unknown
    findings << Finding.new(
      code: "CS004",
      file: relative_file,
      job: job_name,
      step: step_label(steps[first_install], first_install),
      message: "This dependency-install wrapper could not be resolved statically.",
      fix: "Use an explicit package-manager command or add a bounded resolver for this wrapper.",
      trace: first_observation.trace
    )
    return findings
  end

  shared_setup = prior_steps.any? do |step|
    action_matches?(step, CLOUDSMITH_SETUP_ACTIONS)
  end
  oidc_auth = shared_setup || prior_steps.any? do |step|
    action_matches?(step, CLOUDSMITH_AUTH_ACTIONS)
  end
  registry_configuration = cloudsmith_configuration?(job["env"]) ||
                           prior_steps.any? { |step| cloudsmith_configuration?(step) } ||
                           cloudsmith_configuration?(first_observation.source_text)
  setup_present = shared_setup || (oidc_auth && registry_configuration)

  unless setup_present
    findings << Finding.new(
      code: "CS001",
      file: relative_file,
      job: job_name,
      step: step_label(steps[first_install], first_install),
      message: "This install job does not configure Cloudsmith before dependency installation.",
      fix: "Add the shared setup-cloudsmith action before the install step.",
      trace: first_observation.trace
    )
  end

  if permission_value(workflow, job, "id-token") != "write"
    findings << Finding.new(
      code: "CS002",
      file: relative_file,
      job: job_name,
      step: step_label(steps[first_install], first_install),
      message: "This Cloudsmith install job cannot request a GitHub OIDC token.",
      fix: "Set permissions.id-token to write for the workflow or job.",
      trace: first_observation.trace
    )
  end

  allow_public = public_registry_reason(job)
  public_before_install = public_registry?(job["env"]) ||
                          through_install.any? { |step| public_registry?(step) } ||
                          public_registry?(first_observation.source_text)
  if allow_public.empty? && public_before_install
    findings << Finding.new(
      code: "CS003",
      file: relative_file,
      job: job_name,
      step: step_label(steps[first_install], first_install),
      message: "This install job contains an explicit public package registry.",
      fix: "Use Cloudsmith, or set CLOUDSMITH_LINT_ALLOW_PUBLIC_REGISTRY with a review reason.",
      trace: first_observation.trace
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
    ("Resolution: #{finding.trace}" unless finding.trace.to_s.empty?),
    "Fix: #{finding.fix}"
  ].compact.join("\n")
  puts "::#{level} #{location}::#{escape_command(detail)}"
  puts "#{finding.file}: #{finding.code}: #{finding.message}"
  puts "  Job: #{finding.job}"
  puts "  Step: #{finding.step}"
  puts "  Resolution: #{finding.trace}" unless finding.trace.to_s.empty?
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
    findings.concat(lint_job(root, workflow, relative_file, job_name, job))
  end
end

findings.each { |finding| emit(finding, options[:mode]) }

if findings.empty?
  puts "Cloudsmith CI lint found no issues."
else
  puts "Cloudsmith CI lint found #{findings.length} issue(s)."
end

exit(options[:mode] == "error" && findings.any? ? 1 : 0)
