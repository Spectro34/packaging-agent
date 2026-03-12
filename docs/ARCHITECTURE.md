# Architecture

## Multi-Agent Design

The packaging agent uses a hierarchical multi-agent architecture. The **Orchestrator** receives commands and delegates to four specialized agents:

```
                    +------------------+
                    |   Orchestrator   |
                    |                  |
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

### Agent Responsibilities

**Orchestrator** (`agents/orchestrator.py`)
- Parses commands: `scan`, `analyze`, `upgrade`, `build`, `review`, `report`, `ask`
- Initializes all sub-agents with shared config
- Converts AgentResult objects to JSON for MCP/n8n integration
- Manages the review-fix loop (retry with reviewer feedback)

**Analyzer** (`agents/analyzer.py`)
- Deep analysis of individual packages (OBS version, build status, CVE scan, upstream version)
- Project-wide scanning (iterates all packages, aggregates findings)
- AI-powered health assessment via GPT
- GitHub slug inference for changelog lookups
- Knowledge-base build failure diagnosis

**Builder** (`agents/builder.py`)
- Local package builds via osc-mcp's `run_build` tool
- Build-fix loop: up to 3 attempts with AI-powered spec fixes between each attempt
- Knowledge-base error pattern matching before invoking AI
- Final failure diagnosis when all attempts exhausted

**Upgrade** (`agents/upgrade.py`)
- Full 8-step upgrade pipeline (see [UPGRADE_PIPELINE.md](UPGRADE_PIPELINE.md))
- Changelog analysis with risk assessment (LOW/MEDIUM/HIGH/CRITICAL)
- Upstream dependency diff via PyPI metadata
- AI-powered spec file updates with dep casing preservation
- `_service` file handling: updates revision tags, runs local services (tar_scm, go_modules, etc.)
- Tarball tracking: detects added/removed tarballs, writes `.osc/_to_be_deleted` for proper osc deletion
- Integrates Builder and Reviewer agents for build + review gate

**Reviewer** (`agents/reviewer.py`)
- Pre-commit quality gate producing verdicts: COMMIT, NEEDS_HUMAN, REJECT
- Regex-based spec linting (Version, License, Source URL, deprecated tags)
- Changelog validation (.changes file format)
- Ecosystem macro checks (Python: %pyproject_wheel, Go: %gobuild, etc.)
- Dependency consistency checks (added/removed deps applied correctly)
- OBS remote build status checking with unresolvable dependency resolution
- AI-powered spec review with upgrade context

## n8n Workflow (Package Maintainer Agent)

The n8n workflow (ID: `jXKz9GP2bc9aZ8Ig`) is the entry point for all operations. It consists of 7 nodes:

```
Weekly Scan (cron: Mon 06:00)  -+
                                +--> Prepare Input --> Package Maintainer (AI Agent)
Manual Trigger (webhook: POST) -+          |                    |
                                           |         +----------+----------+
                                           |         |          |          |
                                           |    OpenAI     pkg-agent   osc-mcp
                                           |    GPT-4o     MCP Tools   MCP Tools
                                           |
                                           +--> Format Slack Message --> Send to Slack
```

| Node | Type | Purpose |
|------|------|---------|
| **Weekly Scan** | Schedule Trigger | Cron: `0 6 * * 1` (Monday 06:00 UTC), fires `scan` command |
| **Manual Trigger** | Webhook | POST `/webhook/package-maintainer`, accepts `{command, package, version, project}` |
| **Prepare Input** | Code | Parses command/package/version/project from trigger payload, detects trigger source |
| **Package Maintainer** | AI Agent (langchain) | GPT-4o with maxIterations=15, temperature=0.2, 4096 max tokens. Has system prompt for OBS packaging expertise. Connects to both MCP tool sets |
| **Packaging Agent Tools** | MCP Client Tool | `http://packaging-agent:8667/mcp`, typeVersion 1.2, timeout 600000ms |
| **osc-mcp Tools** | MCP Client Tool | `http://osc-mcp:8666/mcp`, typeVersion 1.2, timeout 600000ms |
| **Format Slack Message** | Code | Detects verdict (COMMITTED/NEEDS_HUMAN/REJECT), adds emoji, converts markdown to Slack formatting, truncates to 2800 chars |
| **Send to Slack** | Slack | Posts to `#all-obsagent` channel |

Supported webhook commands: `scan`, `analyze`, `upgrade`, `check-updates`, `ask`.

The AI Agent node has access to **23 total tools** (7 from packaging-agent + 16 from osc-mcp). For high-level operations it uses packaging-agent tools; for fine-grained OBS access it can use osc-mcp tools directly.

## MCP Chain

The system uses a two-tier MCP (Model Context Protocol) architecture:

```
n8n AI Agent
    |
    +-- MCP Client --> packaging-agent MCP (port 8667)
    |                       |
    |                       +-- MCP Client --> osc-mcp (port 8666)
    |                                              |
    |                                              +-- OBS REST API
    |
    +-- MCP Client --> osc-mcp (port 8666)   [direct access]
                           |
                           +-- OBS REST API
```

**Why two tiers?**

1. **packaging-agent** (Python, port 8667): 7 high-level AI tools. Embeds knowledge base, GPT prompts, multi-step orchestration. Lightweight container.
2. **osc-mcp** (Go, port 8666): 16+ low-level OBS tools. Requires privileged container for `osc build`, large disk for build roots. General-purpose OBS tool used by other teams.

The n8n AI Agent has MCP clients connected to **both** servers. It typically uses packaging-agent tools for high-level operations (analyze, upgrade, scan) and can fall back to osc-mcp tools for fine-grained control (read files, check logs, manual commits).

## Sidecar Kubernetes Deployment

Both containers run in the **same pod** using the sidecar pattern:

```
+--[ Pod: osc-mcp ]------------------------------------------+
|                                                             |
|  +-------------------+    +------------------------+        |
|  | Container:        |    | Container:             |        |
|  | osc-mcp           |    | packaging-agent        |        |
|  |                   |    |                        |        |
|  | Port: 8666        |    | Port: 8667             |        |
|  | Privileged: true  |    | Privileged: false      |        |
|  | Mem: 512Mi-8Gi    |    | Mem: 256Mi-1Gi         |        |
|  +--------+----------+    +--------+---------------+        |
|           |                        |                        |
|           +------- Shared ---------+                        |
|           |  /tmp/mcp-workdir (PVC 10Gi)                    |
|           |  localhost:8666 (MCP over loopback)              |
|                                                             |
|  Volume: buildroot (emptyDir 20Gi, osc-mcp only)           |
+-------------------------------------------------------------+
```

**Why sidecar instead of separate deployments?**

- The packaging-agent needs **direct filesystem access** to osc checkout directories (read/write spec files, tarballs, .changes files, `.osc/_to_be_deleted`)
- ReadWriteOnce PVCs can only be mounted on one node; same-pod guarantees same node
- localhost communication is fast and requires no service discovery
- The two services are tightly coupled (packaging-agent depends on osc-mcp)

**Entrypoint script**: The osc-mcp container uses `entrypoint.sh` which auto-generates `/root/.config/osc/oscrc` from environment variables (`OBS_USER`, `OBS_PASS`, `OBS_API_URL`) before launching osc-mcp. This is required because `osc` CLI (used by osc-mcp for commits and builds) needs an oscrc file, while osc-mcp itself receives credentials via CLI arguments.

## Data Flow: Upgrade Pipeline

See [UPGRADE_PIPELINE.md](UPGRADE_PIPELINE.md) for the complete 8-step flow.

Summary (steps match the [0/8]-[7/8] numbering in the code):
```
pre-flight (SR check + cleanup) --> version_check --> analyze_changelog
    --> [dry run stops here]
    --> branch_package --> checkout --> ai_update_spec --> download_source
    --> update_changes --> local_build (up to 3 attempts)
    --> review_gate (COMMIT/NEEDS_HUMAN/REJECT) --> commit (if COMMIT)
```

Note: The spec file is updated BEFORE the tarball is downloaded, so the Version
field is correct for Source URL macro substitution.

## External Data Sources

The system queries four external APIs for package intelligence:

| Source | API | Purpose |
|--------|-----|---------|
| **Repology** | `repology.org/api/v1/project/{name}` | Version tracking across distros (newest, openSUSE status) |
| **OSV** | `api.osv.dev/v1/query` | CVE/vulnerability scanning by package+version |
| **GitHub Releases** | `api.github.com/repos/{owner}/{repo}/releases` | Changelog fetching, release notes analysis |
| **PyPI** | `pypi.org/pypi/{name}/json` | Python dependency metadata, version-to-version dep diffs |

All external API calls are in `data_sources.py` with no AI or OBS dependencies.

## Knowledge Base

The knowledge base (`knowledge.py`) provides ecosystem-specific patterns for 8 packaging ecosystems:

| Ecosystem | Detect Prefixes | OSV Ecosystem | Key Macros |
|-----------|----------------|---------------|------------|
| **python** | `python-`, `python3-` | PyPI | `%pyproject_wheel`, `%pytest`, `%python_module` |
| **go** | `golang-`, `go-` | Go | `%gobuild`, `%goprep`, `%gotest` |
| **rust** | `rust-` | crates.io | `%cargo_build`, `%cargo_test`, `%cargo_prep` |
| **c_autotools** | (spec markers) | OSS-Fuzz | `%configure`, `%make_build`, `%autosetup` |
| **c_cmake** | (spec markers) | OSS-Fuzz | `%cmake`, `%cmake_build`, `%cmake_install` |
| **c_meson** | (spec markers) | OSS-Fuzz | `%meson`, `%meson_build`, `%meson_test` |
| **ruby** | `rubygem-` | RubyGems | `%gem_install`, `%gem_packages` |
| **perl** | `perl-` | CPAN | `%perl_process_packlist` |

Each ecosystem definition includes:
- **Detection rules**: Package name prefixes, project hints, spec file markers
- **Macros**: Build, install, test macro names
- **Required build deps**: Minimum BuildRequires for the ecosystem
- **Upgrade hints**: Common pitfalls when upgrading (fed to AI prompts)
- **Build error patterns**: Regex patterns mapping build log errors to fix suggestions
- **Spec template hints**: Ecosystem-specific spec writing guidelines (used as AI context)

Ecosystem detection priority: spec content markers > package name prefix > project context > fallback.

## Async Bridge Pattern

The packaging-agent MCP server runs in an async context (FastMCP), but `OBSClient` calls to osc-mcp need synchronous execution. The bridge pattern in `obs.py`:

1. Check if there is a running asyncio event loop
2. **If yes** (inside FastMCP): Run the MCP call in a `ThreadPoolExecutor` with a fresh event loop
3. **If no** (CLI mode): Use `asyncio.run()` directly

This avoids the "nested event loop" problem without requiring `nest_asyncio`.

## Configuration

Configuration is loaded from `config.json` (in the `production/` directory) with environment variable fallback:

| Setting | Config Key | Env Var | Default |
|---------|-----------|---------|---------|
| OpenAI API Key | `openai_api_key` | `OPENAI_API_KEY` | (none) |
| OBS API URL | `obs_api_url` | `OBS_API_URL` | `https://api.opensuse.org` |
| OBS Username | `obs_user` | `OBS_USER` | (none) |
| OBS Password | `obs_pass` | `OBS_PASS` | (none) |
| OBS Project | `obs_project` | `OBS_PROJECT` | `systemsmanagement:ansible` |
| osc-mcp URL | `mcp_url` | `MCP_URL` | `http://localhost:8666/mcp` |
| AI Model | `openai_model` | - | `gpt-4o` |

In Kubernetes, all credentials come from the `mcp-credentials` Secret via environment variables. Never commit credentials to config.json.

## AgentResult Schema

All agents return an `AgentResult` dataclass:

```python
@dataclass
class AgentResult:
    success: bool              # Did the operation succeed?
    action: str                # "analyze", "build", "upgrade", "review", "scan"
    package: str = ""          # Package name
    project: str = ""          # OBS project
    summary: str = ""          # One-line human-readable summary
    details: dict = {}         # Agent-specific structured data
    errors: list = []          # Error messages if failed
    needs_review: bool = False # Should the orchestrator send to reviewer?
    needs_retry: bool = False  # Should the orchestrator retry via builder?
    retry_context: dict = {}   # Context for retry attempt
    work_dir: str = None       # Local osc checkout directory path
```

This is serialized to JSON for MCP tool responses with the fields: `success`, `action`, `package`, `project`, `summary`, `details`, `errors`, `needs_review`.
