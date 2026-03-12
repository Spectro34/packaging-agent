# AI Package Maintenance Agent

*Automating openSUSE package upgrades with AI agents and the Open Build Service*

---

## 1. The Problem

- Maintaining hundreds of packages across multiple devel projects is tedious, repetitive work
- A single version upgrade involves:
  - Checking upstream for new releases
  - Downloading new source tarballs
  - Updating the spec file (version, dependencies, macros)
  - Updating the .changes file
  - Building locally to verify
  - Reviewing the result for correctness
  - Committing to a branch and creating a submit request
- Each upgrade takes **30-60 minutes** of manual work -- and that assumes nothing goes wrong
- Humans miss things: changed dependencies, casing mismatches in package names, stale branches from previous attempts, pending submit requests
- Multiply this by dozens of packages per project, and maintenance becomes a bottleneck

---

## 2. The Solution: AI Package Maintenance Agent

- An **autonomous agent** that handles the entire upgrade pipeline end-to-end
- Uses GPT-4o to intelligently update spec files, diagnose build failures, and review changes
- Human oversight is built in: every upgrade produces a verdict
  - **COMMIT** -- all checks passed, safe to commit to branch
  - **NEEDS_HUMAN** -- high-risk change or dependency issue, needs manual review
  - **REJECT** -- structural problems found, do not commit
- Works within the existing OBS workflow:
  - Always branches first (never touches devel projects directly)
  - Never creates submit requests automatically
  - Humans decide when to submit

---

## 3. What is MCP?

**MCP (Model Context Protocol)** is a standard for connecting AI agents to tools and data sources. Think of it like a USB port for AI -- a universal way for an AI agent to discover and use tools.

- Each MCP server advertises a list of **tools** with typed parameters and descriptions
- AI agents (clients) can discover tools at runtime and call them
- Transport is HTTP-based (streamable HTTP), so servers can run anywhere
- This system uses two MCP servers:
  - **osc-mcp** (Go): 16 low-level OBS tools (branch, checkout, build, commit, ...)
  - **packaging-agent** (Python): 7 high-level AI tools (analyze, upgrade, scan, review, ...)

Why two servers? osc-mcp is a general-purpose OBS tool server usable by any AI agent. The packaging-agent adds AI intelligence on top -- knowledge bases, GPT prompts, multi-step orchestration -- specific to package maintenance.

---

## 4. How It Works (Simple Flow)

```
                                     +-----------+
                                     |  Upstream  |
                                     | (PyPI,     |
                                     |  GitHub,   |
                                     |  Repology) |
                                     +-----+-----+
                                           |
                                           v
User / Cron ---> n8n -----------> Packaging Agent -----------> osc-mcp -----------> OBS
                (orchestration)    (AI brain)                  (OBS hands)       api.opensuse.org
                 port 5678         port 8667                   port 8666
                                       |
                                       v
                                   OpenAI GPT-4o
                                (spec updates, reviews,
                                 build failure diagnosis)
```

### The Data Flow

1. **Trigger**: A user sends a webhook to n8n, or the weekly cron fires
2. **n8n AI Agent**: GPT-4o decides which tools to call based on the command
3. **packaging-agent**: Executes the high-level operation (e.g., full upgrade pipeline)
4. **osc-mcp**: Performs low-level OBS operations (branch, checkout, build, commit)
5. **Result**: Flows back through n8n, formatted as a Slack message
6. **Slack**: The `#all-obsagent` channel gets a notification with the verdict and details

### What Does n8n Do?

n8n is the workflow automation platform that ties everything together:

- **Triggers**: Webhook (POST /webhook/package-maintainer) and weekly cron (Monday 06:00 UTC)
- **Prepare Input**: A code node that parses the command, package, version, and project from the trigger payload
- **AI Agent**: A langchain agent node with GPT-4o that decides which MCP tools to call. It has access to BOTH MCP servers (23 total tools)
- **Format Slack**: A code node that parses the AI agent's output, detects the verdict (COMMITTED/NEEDS_HUMAN/REJECT), and formats it for Slack with appropriate emoji
- **Send to Slack**: Posts the message to `#all-obsagent`

The n8n workflow ID is `jXKz9GP2bc9aZ8Ig`.

---

## 5. The Upgrade Pipeline (Step by Step)

```
Pre-flight ---> Version Check ---> Changelog Analysis ---> Branch ---> Checkout
                                        |
                                   [DRY RUN?] ---> Return plan + risk level
                                        |
AI Update Spec ---> Download Tarball ---> Update .changes ---> Local Build
                                                                 (up to 3
                                                                  attempts)
                                                                    |
                                                              Review Gate
                                                            /     |      \
                                                        COMMIT  NH    REJECT
```

### Pre-flight: Clean Slate

- **Check for open submit requests** -- if one already exists for this package, skip the upgrade (no duplicate work)
- **Delete stale branches** -- remove leftover branch projects from previous failed attempts

### Intelligence Gathering

- **Version check** -- compare OBS version against target; skip if already current
- **Changelog analysis** -- fetch GitHub releases, use AI to assess risk level (LOW / MEDIUM / HIGH / CRITICAL)
- Dry run mode stops here, returning the plan without making any changes

### Branch and Checkout

- **Branch** the package on OBS (`home:user:branches:project/package`)
- **Checkout** files to local disk for editing

### AI Updates the Spec File (BEFORE tarball download)

This is the core AI step:

1. **Fetch upstream dependency diff** -- for Python packages, compare PyPI metadata between versions to find added/removed/changed dependencies
2. **GPT-4o rewrites the spec** -- given the current spec, ecosystem context, and dependency changes, produces an updated spec with new version, reset release, and updated dependencies
3. **Integrity validation** -- verify the AI did not strip SLE15 macros, headers, or conditionals
4. **Restore dependency casing** -- GPT tends to lowercase names like `PyYAML` to `pyyaml`; a post-processing step restores the original casing from the source project's spec

The spec is updated BEFORE downloading the tarball so that the Version field is correct for Source URL substitution.

### Download New Source

Two paths depending on the package type:

- **`_service` packages** (e.g., Go packages with `tar_scm`): Updates the revision tag in the `_service` file, runs the appropriate source services locally (tar_scm, set_version, recompress, go_modules), tracks old/new tarballs for proper osc add/rm
- **Standard packages**: Direct download from the spec's Source URL (PyPI, GitHub, etc.), with OBS source services as fallback
- Old tarballs are properly removed from both disk and osc tracking (`.osc/_to_be_deleted`)

### Update .changes

- Prepend a new changelog entry with the version update

### Local Build (Build-Fix Loop)

- Build locally with `osc build` targeting Tumbleweed/x86_64
- On failure: knowledge base matches error patterns first, then AI diagnoses the build log and produces a fixed spec
- Up to **3 attempts** before giving up with a diagnosis

### Review Gate

The Reviewer agent runs 18+ checks before any commit:

| Category | Examples |
|----------|----------|
| Structural | Version tag present, License tag present, .changes file exists |
| Ecosystem | Correct macros used (%pyproject_wheel, %gobuild, etc.) |
| Dependencies | Upstream-added deps present, upstream-removed deps dropped |
| OBS status | Remote builds resolving, no failures |
| AI review | GPT-4o reviews the full spec in upgrade context |

**Verdict outcomes:**
- **COMMIT** -- only warnings or info, no errors. Agent commits to the branch.
- **NEEDS_HUMAN** -- dependency issues, HIGH/CRITICAL risk, or unresolved warnings. Work directory preserved for manual review.
- **REJECT** -- structural errors (missing version, missing license, all builds broken). No commit.

---

## 6. Safety and Human Oversight

- **Never commits to devel projects** -- all changes go to `home:user:branches:*` only
- **Never creates submit requests** -- a human must review the branch and submit manually
- **Review gate before every commit** -- 18+ automated checks plus AI review
- **Risk-aware** -- HIGH and CRITICAL risk upgrades always produce NEEDS_HUMAN, even if all checks pass
- **Dry run by default** -- callers must explicitly opt in to live mode (`--live` flag)
- **Build verification** -- local build must succeed before the review gate even runs
- **Casing restoration** -- GPT's tendency to lowercase RPM package names (PyYAML -> pyyaml) is automatically corrected
- **Header preservation** -- verifies AI did not strip SLE15 macros or conditionals
- **SR awareness** -- will not start an upgrade if a submit request is already pending
- **Stale branch cleanup** -- removes dead branches from previous attempts before starting

---

## 7. Multi-Ecosystem Support

The knowledge base covers 8 packaging ecosystems:

| Ecosystem | Packages | Key Macros | Notes |
|-----------|----------|------------|-------|
| **Python** | `python-*`, `python3-*` | `%pyproject_wheel`, `%pytest`, `%python_module` | PyPI dep diffing, singlespec support |
| **Go** | `golang-*`, `go-*` | `%gobuild`, `%goprep`, `%gotest` | Vendor tarball handling |
| **Rust** | `rust-*` | `%cargo_build`, `%cargo_test`, `%cargo_prep` | crates.io integration |
| **C (autotools)** | detected by spec | `%configure`, `%make_build`, `%autosetup` | Classic ./configure workflow |
| **C (CMake)** | detected by spec | `%cmake`, `%cmake_build`, `%cmake_install` | Out-of-source builds |
| **C (Meson)** | detected by spec | `%meson`, `%meson_build`, `%meson_test` | Modern C/C++ projects |
| **Ruby** | `rubygem-*` | `%gem_install`, `%gem_packages` | RubyGems source registry |
| **Perl** | `perl-*` | `%perl_process_packlist` | CPAN ecosystem |

Each ecosystem provides:
- Detection rules (name prefixes + spec file markers)
- Required and common build dependencies
- Upgrade hints fed to AI prompts
- Build error patterns with fix suggestions
- Spec template guidelines for AI context

---

## 8. Architecture (For the Curious)

### Kubernetes Deployment

```
+--[ Pod: osc-mcp ]-----------------------------------------------+
|                                                                   |
|  +---------------------+       +-------------------------+        |
|  | Container: osc-mcp  |       | Container: pkg-agent    |        |
|  |                     |       |                         |        |
|  | Go binary           |       | Python 3.13 + FastMCP   |        |
|  | Port 8666           |       | Port 8667               |        |
|  | Privileged (builds) |       | Unprivileged            |        |
|  | 512Mi-8Gi RAM       |       | 256Mi-1Gi RAM           |        |
|  +----------+----------+       +------------+------------+        |
|             |                               |                     |
|             +-------- Shared PVC -----------+                     |
|             |   /tmp/mcp-workdir (10Gi)                           |
|             |   localhost:8666 (loopback)                          |
|                                                                   |
|  Volume: buildroot (emptyDir 20Gi, osc-mcp only)                 |
+-------------------------------------------------------------------+
```

### Why Sidecar?

- packaging-agent needs **direct filesystem access** to osc checkout directories (read/write spec files, tarballs, .changes)
- Same-pod guarantees same node, so ReadWriteOnce PVC works
- localhost communication is fast, no service discovery needed
- The two services are tightly coupled (packaging-agent depends on osc-mcp)

### MCP Chain

```
n8n AI Agent (GPT-4o)
    |
    +-- MCP Client --> packaging-agent (port 8667) -- 7 high-level tools
    |                       |
    |                       +-- MCP Client --> osc-mcp (port 8666) -- 16 low-level tools
    |                                              |
    |                                              +-- OBS REST API
    |
    +-- MCP Client --> osc-mcp (port 8666)   [direct, for fine-grained control]
```

n8n connects to **both** servers. It typically uses packaging-agent for high-level operations (upgrade, scan, analyze) and can fall back to osc-mcp for granular control (read individual files, check build logs).

### Internal Multi-Agent Architecture

Inside the packaging-agent, the Orchestrator routes commands to specialized agents:

```
                    +------------------+
                    |   Orchestrator   |
                    | Routes commands  |
                    | Manages results  |
                    +--------+---------+
                             |
           +---------+-------+-------+---------+
           |         |               |         |
     +-----v---+ +---v-----+ +------v--+ +----v------+
     | Analyzer | | Builder | | Upgrade | | Reviewer  |
     |          | |         | |         | |           |
     | CVEs     | | osc     | | Full    | | Lint      |
     | Versions | | build   | | upgrade | | Macros    |
     | Health   | | AI fix  | | pipeline| | AI review |
     | Upstream | | loop    | | 8 steps | | Verdicts  |
     +----------+ +---------+ +---------+ +-----------+
```

### Credentials

- All credentials stored in Kubernetes Secrets (`mcp-credentials`)
- Injected as environment variables into containers
- Never committed to code or config files
- Required: OBS API credentials, OpenAI API key

---

## 9. Demo Walkthrough

### Prerequisites

- The system is deployed and running (see [DEPLOYMENT.md](DEPLOYMENT.md))
- You have access to the n8n instance
- You have the Slack `#all-obsagent` channel open

### Demo 1: Trigger a Package Upgrade

**Step 1**: Send a webhook to trigger an upgrade:

```bash
curl -X POST https://n8n.private-ai.suse.demo/webhook/package-maintainer \
  -H "Content-Type: application/json" \
  -d '{"command": "upgrade", "package": "molecule", "version": "26.3.0"}'
```

**Step 2**: Watch the n8n execution:
- Open the n8n workflow editor (workflow `jXKz9GP2bc9aZ8Ig`)
- Click on the running execution to see live progress
- The AI Agent node shows the tool calls being made in real-time:
  1. `analyze_package("molecule")` -- check current state
  2. `upgrade_package("molecule", "26.3.0", live=true)` -- run the pipeline

**Step 3**: Wait for the result (typically 5-10 minutes, mostly build time):
- The Slack `#all-obsagent` channel receives a message with:
  - Verdict emoji: check (COMMITTED), warning (NEEDS_HUMAN), or X (REJECT)
  - Version change: e.g., "25.6.0 -> 26.3.0"
  - Branch URL: link to the OBS branch where changes were committed
  - Review details: any warnings or issues found

**Step 4**: Review the branch on OBS:
- Click the branch URL in the Slack message
- Inspect the spec file diff, build results, and changelog

### Demo 2: Scan a Project

```bash
curl -X POST https://n8n.private-ai.suse.demo/webhook/package-maintainer \
  -H "Content-Type: application/json" \
  -d '{"command": "scan"}'
```

This scans all packages in the default project and reports:
- Total package count
- Outdated packages (with upstream versions)
- CVE findings
- Build failures

### Demo 3: Ask a Packaging Question

```bash
curl -X POST https://n8n.private-ai.suse.demo/webhook/package-maintainer \
  -H "Content-Type: application/json" \
  -d '{"command": "ask", "package": "How do I package a Python project that uses pyproject.toml?"}'
```

The AI agent uses the built-in knowledge base to answer packaging questions about OBS, osc, RPM macros, and all 8 supported ecosystems.

### Demo 4: Weekly Cron (Automatic)

Every Monday at 06:00 UTC, the cron trigger fires with the `scan` command. The system:
1. Scans the project for outdated packages and CVEs
2. Posts a summary to `#all-obsagent`

---

## 10. Summary

| | Manual Process | With AI Agent |
|---|---|---|
| **Time per upgrade** | 30-60 minutes | 5-10 minutes (mostly build time) |
| **Dependency tracking** | Human memory + reading changelogs | Automated PyPI diff + AI analysis |
| **Build verification** | Run `osc build`, read logs, fix, repeat | Automated 3-attempt build-fix loop |
| **Quality gate** | Eyeball the diff | 18+ automated checks + AI review |
| **Risk of mistakes** | Casing errors, missed deps, stale branches | Automated casing restoration, dep checks, cleanup |
| **Human oversight** | Trust the packager | Explicit COMMIT/NEEDS_HUMAN/REJECT verdicts |
