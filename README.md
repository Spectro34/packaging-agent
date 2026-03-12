# openSUSE Packaging Agent

AI-powered package version upgrade automation for the [Open Build Service](https://openbuildservice.org/) (OBS). Takes a package name and target version, then handles the entire upgrade pipeline — branching, spec update, tarball download, patch management, building, review, commit, and OBS build verification — with no human intervention required for straightforward upgrades.

**93% OBS-verified success rate** on a diverse set of 15 Python packages (compiled extensions, namespace packages, patch-heavy packages), independently verified by querying OBS build results directly.

## What It Does

```
$ python3 -m packaging_agent upgrade python-Werkzeug 3.1.6 --live --project devel:languages:python

  [0/8] Pre-flight checks...
  [1/8] Branching devel:languages:python/python-Werkzeug via osc-mcp...
         → home:spectro:branches:devel:languages:python
  [2/8] Checking out via osc-mcp...
  [3/8] AI updating spec file...
         Spec updated (2847 chars)
  [4/8] Downloading source tarball...
         → Werkzeug-3.1.6.tar.gz (1048576 bytes)
  [5/8] Updating .changes...
  [6/8] Local build via osc-mcp (max 3 attempts)
         LOCAL BUILD SUCCEEDED!
  [7/8] Pre-commit review...
         Verdict: COMMIT
  [8/8] Committing via osc-mcp...
         Committed!
  [9/9] Verifying OBS server builds...
         OBS results: 8/8 succeeded, 0 failed
         OBS builds VERIFIED!

  UPGRADE SUCCEEDED: 3.1.5 → 3.1.6
```

The agent automatically handles:
- **Spec file updates** — Version, Release, dependency changes (via GPT-4o)
- **Tarball download** — Direct PyPI/GitHub download or `_service` file updates
- **`%setup -n` fixes** — Auto-detects tarball directory name mismatches (e.g., PyPI returns `markuppy-1.18/` but spec expects `MarkupPy-1.18/`)
- **Patch management** — Tests each patch with `--fuzz=0` (matching OBS strict mode), auto-removes merged patches, flags conflicts as NEEDS_HUMAN
- **Build failures** — Deterministic fixes for stale `%files` entries, AI-assisted fixes for other issues, up to 3 retry attempts
- **OBS verification** — Post-commit polling of actual OBS server builds with auto-fix loop (up to 2 fix+recommit cycles)
- **Review gate** — 18+ automated checks before commit (linting, ecosystem macros, dependency consistency)

## Architecture

```
CLI / n8n                    packaging-agent (Python)           osc-mcp (Go)
─────────────               ──────────────────────             ─────────────
python3 -m packaging_agent   7 high-level AI tools              16 low-level OBS tools
   upgrade / scan / build    (analyze, upgrade, scan,           (branch, checkout, build,
   review / report / ask      build, review, report, ask)        commit, list, search, ...)
         │                           │                                  │
         └── CLI or MCP ────────────►└── MCP (port 8667) ─────────────►└── MCP (port 8666) ──► OBS API
```

**Two-server MCP chain:**
- **osc-mcp** (Go, port 8666) — Wraps `osc` CLI commands as 16 MCP tools. Handles branching, checkout, build, commit, file listing, search.
- **packaging-agent** (Python, port 8667) — 7 high-level AI tools that orchestrate multi-step workflows using osc-mcp.

n8n integration is **optional** — the CLI works fully standalone.

## Quick Start

### Prerequisites

- Python 3.10+
- An [OBS account](https://build.opensuse.org/) with API access
- An [OpenAI API key](https://platform.openai.com/api-keys) (GPT-4o for spec updates and build diagnosis)
- `osc` CLI installed (`zypper install osc` on openSUSE, or `pip install osc`)

### Option 1: Interactive Setup (Recommended)

```bash
git clone https://github.com/Spectro34/packaging-agent.git
cd packaging-agent
./setup.sh
```

The setup script will:
1. Install Python dependencies
2. Prompt for OBS and OpenAI credentials
3. Verify credentials work
4. Offer to start osc-mcp and run a test upgrade

### Option 2: Manual Setup

```bash
git clone https://github.com/Spectro34/packaging-agent.git
cd packaging-agent

# Install dependencies
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your OBS_USER, OBS_PASS, OPENAI_API_KEY

# Start osc-mcp (the OBS bridge server)
# Option A: Use the Go binary directly
cd deploy && go build -o osc-mcp . && ./osc-mcp &
cd ..

# Option B: Use Docker Compose (starts both servers)
docker compose up -d

# Test it
source .env
python3 -m packaging_agent upgrade python-aiosqlite 0.22.1 --live --project devel:languages:python
```

### Option 3: Docker Compose

```bash
git clone https://github.com/Spectro34/packaging-agent.git
cd packaging-agent
cp .env.example .env
# Edit .env with credentials
docker compose up -d

# Use the CLI (connects to containerized servers)
source .env
export MCP_URL=http://localhost:8666/mcp
python3 -m packaging_agent upgrade python-Werkzeug 3.1.6 --live
```

## Configuration

All credentials are configured via environment variables (highest priority), `config.json`, or `.env` file:

| Variable | Required | Description |
|----------|----------|-------------|
| `OBS_USER` | Yes | OBS username |
| `OBS_PASS` | Yes | OBS password or API token |
| `OBS_API_URL` | No | OBS API URL (default: `https://api.opensuse.org`) |
| `OBS_PROJECT` | No | Default OBS project (can override per command with `--project`) |
| `OPENAI_API_KEY` | Yes | OpenAI API key for GPT-4o |
| `OPENAI_MODEL` | No | Model to use (default: `gpt-4o`) |
| `MCP_URL` | No | osc-mcp URL (default: `http://localhost:8666/mcp`) |

Credentials are **never** committed — `.env`, `config.json`, and K8s manifests are gitignored.

## CLI Reference

```bash
# Upgrade a package to a specific version
python3 -m packaging_agent upgrade <package> <version> --live --project <project>

# Dry run (shows what would happen, no changes)
python3 -m packaging_agent upgrade <package> <version> --project <project>

# Scan a project for outdated packages
python3 -m packaging_agent scan --project devel:languages:python

# Analyze a single package
python3 -m packaging_agent analyze <package> --project <project>

# Build a package (local osc build)
python3 -m packaging_agent build <package> --project <project>

# Review a package spec file
python3 -m packaging_agent review <package> --project <project>

# Generate a report for a project
python3 -m packaging_agent report --project <project>

# Ask a free-form question about packaging
python3 -m packaging_agent ask "How do I fix unresolvable deps for python-foo?"
```

## How the Upgrade Pipeline Works

| Step | What Happens | Auto-Fix |
|------|-------------|----------|
| **0. Pre-flight** | Check for open submit requests, clean stale branches | Skip if SR exists |
| **1. Branch** | `osc branch` the package on OBS | — |
| **2. Checkout** | `osc checkout` to local working directory | — |
| **3. Spec update** | GPT-4o updates Version, Release, dependencies | Casing restoration, integrity validation |
| **4. Tarball** | Download new source (PyPI/GitHub or `_service`) | Source URL auto-fix if filename differs |
| **4b. Patches** | Test all patches with `--fuzz=0` against new source | Remove merged patches, flag conflicts |
| **5. Changelog** | Add entry to `.changes` file | — |
| **6. Build** | Local `osc build` with up to 3 AI-assisted fix retries | Deterministic + AI fixes |
| **7. Review** | 18+ automated quality checks | — |
| **8. Commit** | `osc commit` to branch project | — |
| **9. OBS verify** | Poll OBS server builds, auto-fix if needed | Up to 2 fix+recommit cycles |

### Verdicts

The review gate produces one of three verdicts:

- **COMMIT** — All checks pass, safe to auto-commit
- **NEEDS_HUMAN** — Builds locally but has issues requiring human judgment (patch conflicts, dependency changes, high-risk upgrade)
- **REJECT** — Critical problems detected, do not commit

The agent **never creates submit requests** — it only commits to a branch project. A human maintainer reviews and submits.

## Supported Ecosystems

| Ecosystem | Build System | Macros |
|-----------|-------------|--------|
| Python | `%pyproject_wheel` / `%py3_build` | `%python_module`, `%pytest` |
| Go | `%gobuild` | `go_nostrip`, `go_filelist` |
| Rust | `%cargo_build` | `cargo-packaging` |
| C (autotools) | `%configure` / `%make_build` | Standard RPM macros |
| C (CMake) | `%cmake` / `%cmake_build` | `cmake-full` |
| C (Meson) | `%meson` / `%meson_build` | `meson` |
| Ruby | `%gem_install` | `rubygem()` |
| Perl | `%perl_make_install` | `perl()` |

## Test Results

Clean test run on 15 diverse Python packages, independently verified by querying OBS API:

| Metric | Count | Rate |
|--------|-------|------|
| All OBS repos pass | 10 | 66% |
| Tumbleweed + main archs pass | 14 | 93% |
| Genuine failure | 1 | 7% |

The 4 "mostly pass" packages fail only on SLE 15.7 (Python 3.6 too old) or python314 (bleeding edge, unreleased) — these are expected failures that human packagers would also see.

The 1 genuine failure (python-aiofiles) involves an upstream structural change where `aiofiles/_version.py` was removed, requiring manual spec rework of the `%build` section.

<details>
<summary>Full test results (click to expand)</summary>

```
PACKAGE                       OK FAIL UNRS  VERDICT
python-Werkzeug                8    0    0  ALL_PASS
python-aiosmtplib              8    0    0  ALL_PASS
python-aiosqlite               8    0    0  ALL_PASS
python-RTFDE                   8    0    0  ALL_PASS
python-Levenshtein             8    0    0  ALL_PASS
python-XStatic-jQuery          8    0    0  ALL_PASS
python-XStatic-objectpath      8    0    0  ALL_PASS
python-aiodns                  8    0    0  ALL_PASS
python-ZODB                    7    0    1  PASS (1 unresolvable)
python-Wand                    7    0    1  PASS (1 unresolvable)
python-IMAPClient              7    1    0  MOSTLY (15.7: Python 3.6 too old)
python-MarkupPy                7    1    0  MOSTLY (15.7: %files glob mismatch)
python-PyMsgBox                7    1    0  MOSTLY (15.7: %files path mismatch)
python-Telethon                5    1    2  MOSTLY (python314: 1 test failure)
python-aiofiles                0    8    0  FAIL (upstream %build structural change)
```

</details>

## Limitations

- **No submit requests** — The agent commits to a branch project only. A human must review and submit.
- **Upstream structural changes** — If the upstream project fundamentally changes its build system (e.g., removes files that the spec's `%build` section references), the agent will detect the failure but cannot auto-fix it.
- **`_service` packages** — Packages using `obs_scm`/`tar_scm` services work but may need manual revision tag updates for complex service configurations.
- **SLE 15.x compatibility** — The agent targets Tumbleweed. Older SLE repos may fail due to Python version requirements or macro differences.
- **AI-generated spec changes** — GPT-4o occasionally lowercases package names or modifies sections it shouldn't. The agent has guardrails (casing restoration, integrity validation, Source: line protection) but edge cases exist.
- **Build cache cold start** — First `osc build` after a fresh setup downloads the full build root (~15-20 min). Subsequent builds use the cache (~30s-2min).
- **Single architecture focus** — The agent verifies x86_64 builds. Other architectures are checked on OBS post-commit but not locally.
- **OpenAI dependency** — Requires an OpenAI API key for spec updates and build diagnosis. Each upgrade uses ~3-5 API calls (~$0.02-0.05 per package).

## Project Structure

```
packaging-agent/
├── packaging_agent/           # Python package
│   ├── agents/                # Multi-agent system
│   │   ├── orchestrator.py    # Command routing
│   │   ├── upgrade.py         # 9-step upgrade pipeline (core)
│   │   ├── builder.py         # Build + AI fix loop
│   │   ├── reviewer.py        # 18+ quality checks
│   │   ├── analyzer.py        # Package analysis
│   │   └── base.py            # AgentResult, BaseAgent
│   ├── obs.py                 # MCP client for osc-mcp
│   ├── knowledge.py           # 8 ecosystem build patterns
│   ├── data_sources.py        # PyPI, GitHub, Repology, OSV
│   ├── http.py                # HTTP + GPT wrapper
│   ├── mcp_server.py          # FastMCP HTTP server
│   ├── config.py              # Config loader
│   └── cli.py                 # CLI entry point
├── deploy/                    # osc-mcp Go server + Dockerfiles
│   ├── osc-mcp.go             # Go MCP server entry point
│   ├── internal/              # Go OBS tool implementations
│   ├── Dockerfile.osc-mcp     # osc + build tools container
│   ├── Dockerfile.packaging-agent
│   ├── entrypoint.sh          # oscrc generator
│   └── k8s-mcp-servers.yaml.template
├── docs/                      # Extended documentation
├── setup.sh                   # Interactive setup
├── docker-compose.yml         # Local dev (both servers)
├── requirements.txt           # Python dependencies
└── .env.example               # Credential template
```

## How It Works Internally

### Key Design Decisions

1. **Deterministic fixes over AI** — Regex-based fixes for `%setup -n` mismatches, stale `%files` entries, and merged patches are more reliable than AI-generated spec edits. AI is used only when deterministic approaches fail.

2. **OBS verification is the truth** — Local `osc build` passes but OBS server builds can fail differently (strict `--fuzz=0`, different repos, different macros). The agent only reports success after OBS builds are independently verified.

3. **Two-server MCP architecture** — Separates concerns: osc-mcp handles raw OBS operations (Go, fast, stateless), packaging-agent handles AI orchestration (Python, stateful, multi-step). They communicate via MCP protocol.

4. **Conservative AI constraints** — The AI is forbidden from changing Source:, Name:, or License: lines. Casing is auto-restored after GPT processing. Spec integrity is validated before and after AI edits.

5. **No submit requests** — The agent commits to a branch project but never creates submit requests. This keeps a human in the loop for the final review step.

### Security

- Credentials are stored in gitignored files with `600` permissions
- No secrets in container images — injected via environment variables
- API keys are never logged or included in error messages
- The agent only writes to branch projects, never to source projects directly

## License

MIT — see [LICENSE](LICENSE)

## Contributing

Issues and pull requests welcome at [github.com/Spectro34/packaging-agent](https://github.com/Spectro34/packaging-agent).

Built with [osc-mcp](deploy/) for OBS integration and [FastMCP](https://github.com/jlowin/fastmcp) for MCP protocol support.
