"""
MCP Server for the openSUSE Packaging Agent.

Exposes high-level AI-powered packaging operations as MCP tools,
designed for n8n langchain AI Agent node integration alongside osc-mcp.

osc-mcp handles low-level OBS operations (branch, edit_file, commit, etc.)
This server handles high-level AI analysis (analyze, upgrade, scan, review, etc.)

Run:
    python -m packaging_agent.mcp_server             # stdio (default)
    python -m packaging_agent.mcp_server --sse 8667   # SSE transport on port 8667
"""

import json
import sys
from typing import Optional

from fastmcp import FastMCP

from packaging_agent.config import load_config
from packaging_agent.agents.orchestrator import Orchestrator

# ── Server Setup ─────────────────────────────────────────────────────────────

mcp = FastMCP(
    "packaging-agent",
    instructions=(
        "AI-powered openSUSE package maintenance agent. "
        "Provides high-level operations: package analysis, version upgrades "
        "with local build verification, CVE scanning, spec review, and "
        "security reporting. Works alongside osc-mcp for low-level OBS operations."
    ),
)

# Lazy-init orchestrator (loaded on first tool call)
_orchestrator = None


def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        config = load_config()
        _orchestrator = Orchestrator(config)
    return _orchestrator


def _result_to_dict(result):
    """Convert AgentResult to a JSON-serializable dict."""
    return {
        "success": result.success,
        "action": result.action,
        "package": result.package,
        "project": result.project,
        "summary": result.summary,
        "details": result.details,
        "errors": result.errors,
        "needs_review": result.needs_review,
    }


# ── Tools ────────────────────────────────────────────────────────────────────

@mcp.tool()
def analyze_package(
    package: str,
    project: Optional[str] = None,
) -> str:
    """Deep analysis of a single OBS package.

    Returns: OBS version, build status, CVE scan, upstream version check,
    ecosystem detection, AI health assessment, and build failure diagnosis.

    Args:
        package: Package name (e.g. "molecule", "python-ansible-core")
        project: OBS project (default: from config, usually systemsmanagement:ansible)
    """
    orch = _get_orchestrator()
    result = orch.run("analyze", {"package": package, "project": project})
    return json.dumps(_result_to_dict(result), indent=2, default=str)


@mcp.tool()
def scan_packages(
    project: Optional[str] = None,
) -> str:
    """Scan all packages in an OBS project for CVEs, version drift, and build failures.

    Returns aggregated results: total CVEs, outdated count, build failures,
    and per-package details.

    Args:
        project: OBS project to scan (default: from config)
    """
    orch = _get_orchestrator()
    result = orch.run("scan", {"project": project})
    return json.dumps(_result_to_dict(result), indent=2, default=str)


@mcp.tool()
def upgrade_package(
    package: str,
    target_version: str,
    project: Optional[str] = None,
    live: bool = False,
    github_slug: Optional[str] = None,
) -> str:
    """Upgrade a package to a new version.

    Dry run (default): Shows changelog analysis, risk assessment, and planned steps.
    Live mode: Branches on OBS, downloads tarball, AI-updates spec, builds locally,
    commits only on successful build.

    Args:
        package: Package name (e.g. "molecule")
        target_version: Target version string (e.g. "26.3.0")
        project: OBS project (default: from config)
        live: If true, execute the upgrade. If false, dry run only.
        github_slug: GitHub owner/repo for changelog (e.g. "ansible-community/molecule").
                     Auto-detected if not provided.
    """
    orch = _get_orchestrator()
    args = {
        "package": package,
        "target_version": target_version,
        "project": project,
    }
    if github_slug:
        args["github"] = github_slug
    result = orch.run("upgrade", args, live=live)
    return json.dumps(_result_to_dict(result), indent=2, default=str)


@mcp.tool()
def build_package(
    work_dir: str,
    package: Optional[str] = None,
) -> str:
    """Build a package locally from an osc checkout directory.

    Runs `osc build` locally with AI-powered build failure diagnosis and fix loop
    (up to 3 attempts).

    Args:
        work_dir: Path to the osc checkout directory containing the .spec file
        package: Package name (auto-detected from spec if not provided)
    """
    orch = _get_orchestrator()
    result = orch.run("build", {"work_dir": work_dir, "package": package or ""})
    return json.dumps(_result_to_dict(result), indent=2, default=str)


@mcp.tool()
def review_package(
    work_dir: Optional[str] = None,
    spec_content: Optional[str] = None,
    package: Optional[str] = None,
    project: Optional[str] = None,
) -> str:
    """Review a package spec file for quality issues.

    Runs lint checks, changelog validation, ecosystem macro checks, and
    AI-powered review. Provide either work_dir (reads spec from disk) or
    spec_content directly.

    Args:
        work_dir: Path to osc checkout directory (reads .spec from disk)
        spec_content: Raw spec file content (alternative to work_dir)
        package: Package name
        project: OBS project
    """
    orch = _get_orchestrator()
    result = orch.run("review", {
        "work_dir": work_dir,
        "spec_content": spec_content,
        "package": package or "",
        "project": project,
    })
    return json.dumps(_result_to_dict(result), indent=2, default=str)


@mcp.tool()
def security_report(
    project: Optional[str] = None,
) -> str:
    """Generate a security intelligence report for an OBS project.

    Scans all packages, then produces an AI-prioritized security briefing with
    priority actions, risk assessment, and recommended next steps.

    Args:
        project: OBS project (default: from config)
    """
    orch = _get_orchestrator()
    result = orch.run("report", {"project": project})
    return json.dumps(_result_to_dict(result), indent=2, default=str)


@mcp.tool()
def ask_packaging(
    question: str,
) -> str:
    """Ask a free-text question about openSUSE packaging.

    Uses AI with built-in knowledge of OBS, osc, RPM macros, spec files,
    source services, and ecosystem-specific packaging patterns
    (Python, Go, Rust, C, Ruby, Perl).

    Args:
        question: Your packaging question (e.g. "how do I package a Python project with pyproject.toml?")
    """
    orch = _get_orchestrator()
    result = orch.run("ask", {"question": question})
    data = _result_to_dict(result)
    # For ask, the answer is the main content
    data["answer"] = result.details.get("answer", result.summary)
    return json.dumps(data, indent=2, default=str)


# ── Entry Point ──────────────────────────────────────────────────────────────

def main():
    """Run the MCP server. Supports stdio (default) and SSE transport."""
    import argparse
    parser = argparse.ArgumentParser(description="Packaging Agent MCP Server")
    parser.add_argument("--http", type=int, metavar="PORT",
                        help="Run with HTTP transport on given port (default: stdio)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Host to bind HTTP server (default: 0.0.0.0)")
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.http)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
