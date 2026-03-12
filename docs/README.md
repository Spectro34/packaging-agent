# openSUSE Packaging Agent

AI-powered package maintenance for the Open Build Service (OBS). This system automates version upgrades, CVE scanning, build verification, and spec file review for openSUSE packages.

## What It Does

- **Scans** OBS projects for outdated packages, CVEs, and build failures
- **Analyzes** individual packages with AI-powered health assessments
- **Upgrades** packages to new versions with automated spec updates, local builds, and review gates
- **Reviews** spec files for quality issues, missing macros, and ecosystem best practices
- **Reports** security intelligence summaries with prioritized actions
- **Answers** free-text packaging questions using a built-in knowledge base covering 8 ecosystems

## Architecture Overview

```
+------------------+
|       n8n        |    Webhook / Cron triggers
|  AI Agent Node   |    GPT-4o for orchestration
|  (Port 5678)     |
+--------+---------+
         |
         |  MCP (Streamable HTTP)
         v
+------------------+         MCP (Streamable HTTP)        +------------------+
| packaging-agent  | -------------------------------------> |    osc-mcp       |
|  (Port 8667)     |         localhost (sidecar)           |  (Port 8666)     |
|                  |                                       |                  |
|  7 AI tools      |    Shared PVC: /tmp/mcp-workdir       |  16 OBS tools    |
|  Knowledge base  | <-- - - - - - - - - - - - - - - - - > |  osc + build     |
|  GPT prompts     |         (filesystem access)           |  toolchain       |
+------------------+                                       +--------+---------+
                                                                    |
                                                                    |  HTTPS
                                                                    v
                                                           +------------------+
                                                           |   OBS API        |
                                                           | api.opensuse.org |
                                                           +------------------+

External Data Sources:
  - Repology (version tracking)
  - OSV (CVE database)
  - GitHub Releases (changelogs)
  - PyPI (dependency metadata)
```

## Quick Start

### 1. Build Container Images

```bash
cd agent-factory/production/deploy

# Build osc-mcp (Go binary must be pre-compiled)
docker build -f Dockerfile.osc-mcp -t localhost/osc-mcp:latest .

# Build packaging-agent
docker build -f Dockerfile.packaging-agent -t localhost/packaging-agent:latest ../
```

### 2. Deploy to Kubernetes

```bash
# Edit k8s-mcp-servers.yaml — update the Secret with your credentials
kubectl apply -f k8s-mcp-servers.yaml
```

### 3. Import n8n Workflow

Import `deploy/n8n-package-maintainer.json` into your n8n instance. Configure the OpenAI and Slack credentials.

### 4. CLI Usage (Local)

```bash
# Scan all packages in a project
python3 -m packaging_agent scan

# Analyze a single package
python3 -m packaging_agent analyze --package molecule

# Upgrade a package (dry run)
python3 -m packaging_agent upgrade --package molecule --version 26.3.0

# Upgrade a package (live)
python3 -m packaging_agent upgrade --package molecule --version 26.3.0 --live

# Run the MCP server
python3 -m packaging_agent.mcp_server --http 8667
```

## Documentation

| Document | Description |
|----------|-------------|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Multi-agent design, n8n workflow, MCP chain, data flow, knowledge base |
| [PRESENTATION.md](PRESENTATION.md) | Presentation-ready overview, simple explanations, demo walkthrough |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Building images, K8s manifests, credential management, n8n setup |
| [MCP_TOOLS.md](MCP_TOOLS.md) | Complete MCP tool reference with parameters and examples |
| [UPGRADE_PIPELINE.md](UPGRADE_PIPELINE.md) | Detailed upgrade pipeline (8 steps), review gate, known issues |

## Key Design Decisions

- **Two MCP servers**: osc-mcp (Go) handles low-level OBS operations; packaging-agent (Python) handles AI analysis. Separate concerns, separate scaling.
- **Sidecar deployment**: Both containers run in the same K8s pod, sharing a PVC for file access. packaging-agent talks to osc-mcp via localhost.
- **No direct OBS API calls**: All OBS operations go through osc-mcp. The packaging-agent never calls `osc` or the OBS REST API directly.
- **No automated submit requests**: The system commits to branch projects only. Human review is required before submitting to the source project.
- **Review gate**: Every upgrade goes through a pre-commit quality gate (COMMIT / NEEDS_HUMAN / REJECT verdicts) before any OBS commit happens.

## Tech Stack

| Component | Language | Framework |
|-----------|----------|-----------|
| osc-mcp | Go | Custom MCP server |
| packaging-agent | Python 3.13 | FastMCP v2 |
| n8n workflow | JavaScript | n8n langchain AI Agent |
| AI model | - | GPT-4o (via OpenAI API) |
| Kubernetes | - | RKE2 |
