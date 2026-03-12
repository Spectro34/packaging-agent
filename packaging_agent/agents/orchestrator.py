"""
Orchestrator Agent — Parses user intent, delegates to specialized agents,
manages the review-fix loop.
"""

import json
import sys

from packaging_agent.agents.base import BaseAgent, AgentResult
from packaging_agent.agents.analyzer import AnalyzerAgent
from packaging_agent.agents.builder import BuilderAgent
from packaging_agent.agents.upgrade import UpgradeAgent
from packaging_agent.agents.reviewer import ReviewerAgent


class Orchestrator(BaseAgent):
    """Master orchestrator that routes tasks to specialized agents.

    Supports both CLI commands and free-text intent parsing.
    All results are JSON-serializable for n8n/MCP integration.
    """

    def __init__(self, config):
        super().__init__(config)
        self.analyzer = AnalyzerAgent(config)
        self.builder = BuilderAgent(config)
        self.upgrade = UpgradeAgent(config)
        self.reviewer = ReviewerAgent(config)

    def run(self, command, args=None, live=False, **kwargs):
        """Execute a command through the orchestrator.

        Args:
            command: One of "scan", "analyze", "upgrade", "build", "review", "report", "ask"
            args: Command-specific arguments (dict or list)
            live: Execute live operations (vs dry run)
        """
        args = args or {}

        if command == "scan":
            return self._do_scan(args)
        elif command == "analyze":
            return self._do_analyze(args)
        elif command == "upgrade":
            return self._do_upgrade(args, live)
        elif command == "build":
            return self._do_build(args)
        elif command == "review":
            return self._do_review(args)
        elif command == "report":
            return self._do_report(args)
        elif command == "ask":
            return self._do_ask(args)
        else:
            return AgentResult(
                success=False, action="orchestrate",
                summary=f"Unknown command: {command}",
                errors=[f"Valid commands: scan, analyze, upgrade, build, review, report, ask"])

    # ─── Command Handlers ─────────────────────────────────────────────────────

    def _do_scan(self, args):
        """Scan all packages in a project."""
        project = args.get("project")
        result = self.analyzer.scan_all(project)
        self._print_scan_results(result)
        return result

    def _do_analyze(self, args):
        """Deep analysis of a single package."""
        package = args.get("package")
        project = args.get("project")
        if not package:
            return AgentResult(success=False, action="analyze",
                               summary="Package name required",
                               errors=["Provide 'package' argument"])
        result = self.analyzer.analyze_one(package, project)
        self._print_analysis(result)
        return result

    def _do_upgrade(self, args, live=False):
        """Version upgrade with optional live execution."""
        package = args.get("package")
        target = args.get("target_version")
        project = args.get("project")
        github = args.get("github", "")

        if not package or not target:
            return AgentResult(success=False, action="upgrade",
                               summary="Package and target_version required",
                               errors=["Provide 'package' and 'target_version'"])

        result = self.upgrade.run(
            package=package, target_version=target,
            project=project, live=live, github_slug=github)

        # Review is now integrated into upgrade pipeline (pre-commit gate).
        # Print review details if available.
        review_data = result.details.get("review")
        if review_data:
            verdict = review_data.get("verdict", "UNKNOWN")
            reason = review_data.get("reason", "")
            print(f"\n  Review verdict: {verdict}")
            if reason:
                print(f"  Reason: {reason}")
            for check in review_data.get("checks", []):
                if check.get("severity") in ("error", "warning"):
                    icon = "X" if check["severity"] == "error" else "!"
                    print(f"    [{icon}] {check.get('check', '')}: {check['message'][:100]}")

        self._print_upgrade_result(result)
        return result

    def _do_build(self, args):
        """Build a package locally."""
        package = args.get("package")
        work_dir = args.get("work_dir")
        project = args.get("project")

        if not work_dir:
            return AgentResult(success=False, action="build",
                               summary="work_dir required",
                               errors=["Provide 'work_dir' (osc checkout path)"])

        result = self.builder.run(
            package=package, project=project, work_dir=work_dir)
        return result

    def _do_review(self, args):
        """Review a package's spec file."""
        package = args.get("package")
        project = args.get("project")
        work_dir = args.get("work_dir")
        spec_content = args.get("spec_content")

        result = self.reviewer.run(
            package=package, project=project,
            work_dir=work_dir, spec_content=spec_content)
        self._print_review(result)
        return result

    def _do_report(self, args):
        """Security intelligence report."""
        project = args.get("project")
        scan = self.analyzer.scan_all(project)

        if not scan.success:
            return scan

        # Generate AI security briefing
        if self.api_key:
            details = scan.details
            pkg_summaries = []
            for p in details.get("packages", []):
                cves = p.get("cves", [])
                status = "current"
                if p.get("outdated"):
                    status = "OUTDATED"
                if p.get("build_failure"):
                    status = "BUILD_FAIL"
                if cves:
                    status = f"{len(cves)} CVEs"
                pkg_summaries.append(f"  {p['package']}: v{p.get('obs_version', '?')} "
                                     f"(upstream: {p.get('upstream_version', '?')}) — {status}")

            briefing = self.gpt(
                "You are a security analyst for openSUSE package maintenance. "
                "Produce a prioritized security briefing.",
                f"Package scan results:\n" + "\n".join(pkg_summaries) +
                f"\n\nTotal: {details['package_count']} packages, "
                f"{details['total_cves']} CVEs, {details['outdated_count']} outdated, "
                f"{details['build_failure_count']} build failures\n\n"
                "Provide:\n1. PRIORITY ACTIONS (critical first)\n"
                "2. RISK ASSESSMENT\n3. RECOMMENDED NEXT STEPS",
                temperature=0.2, max_tokens=800
            )
            scan.details["security_briefing"] = briefing

        return scan

    def _do_ask(self, args):
        """Free-text question about packaging — uses AI + knowledge base."""
        question = args.get("question", "")
        if not question:
            return AgentResult(success=False, action="ask",
                               summary="No question provided",
                               errors=["Provide 'question'"])

        from packaging_agent.knowledge import ECOSYSTEMS
        eco_summary = "\n".join(
            f"- {k}: {v.get('spec_template_hints', '')[:100]}"
            for k, v in ECOSYSTEMS.items()
        )

        answer = self.gpt(
            "You are an expert openSUSE package maintainer. "
            "You know OBS, osc, RPM macros, spec files, source services, and "
            "all ecosystem-specific packaging patterns.\n\n"
            f"Known ecosystems:\n{eco_summary}",
            question,
            temperature=0.3, max_tokens=1000
        )

        return AgentResult(
            success=True, action="ask",
            summary=answer[:100],
            details={"question": question, "answer": answer})

    # ─── Review-Fix Loop ──────────────────────────────────────────────────────

    def _retry_with_review(self, original, review, max_retries=1):
        """Attempt to fix issues found by the reviewer."""
        for attempt in range(max_retries):
            if not original.work_dir:
                break

            # Get review feedback
            errors = [c for c in review.details.get("checks", [])
                      if c.get("severity") == "error"]
            if not errors:
                break  # Only warnings, no action needed

            feedback = "\n".join(f"- {e['message']}" for e in errors)
            print(f"  Review errors to fix:\n{feedback}")

            # Find and fix spec
            import os
            spec_file = None
            for f in os.listdir(original.work_dir):
                if f.endswith(".spec"):
                    spec_file = f
                    break
            if not spec_file:
                break

            fix_result = self.builder.fix(
                package=original.package,
                project=original.project,
                spec_path=os.path.join(original.work_dir, spec_file),
                build_log=f"Review feedback:\n{feedback}",
                ecosystem=original.details.get("ecosystem"))

            if fix_result:
                # Re-build
                build_result = self.builder.run(
                    package=original.package,
                    project=original.project,
                    work_dir=original.work_dir)

                if build_result.success:
                    re_review = self.reviewer.run(
                        package=original.package,
                        work_dir=original.work_dir)
                    if re_review.success:
                        return build_result
                    review = re_review

        return original

    # ─── Output Formatting ────────────────────────────────────────────────────

    def _print_scan_results(self, result):
        """Pretty-print scan results."""
        if not result.success:
            print(f"\n  Scan failed: {result.summary}")
            return

        details = result.details
        print(f"\n{'=' * 65}")
        print(f"  PACKAGE SCAN RESULTS")
        print(f"  {result.summary}")
        print(f"{'=' * 65}")

    def _print_analysis(self, result):
        """Pretty-print analysis results."""
        d = result.details
        print(f"\n{'=' * 65}")
        print(f"  PACKAGE ANALYSIS: {result.package}")
        print(f"{'=' * 65}")
        print(f"  Version: {d.get('obs_version', '?')} "
              f"(upstream: {d.get('upstream_version', '?')})")
        print(f"  Ecosystem: {d.get('ecosystem', '?')}")
        print(f"  Build: {d.get('build_status', 'unknown')}")
        print(f"  CVEs: {d.get('cve_count', 0)}")
        if d.get("ai_summary"):
            print(f"\n  AI Summary: {d['ai_summary']}")
        print(f"{'=' * 65}")

    def _print_review(self, result):
        """Pretty-print review results."""
        print(f"\n  Review: {result.summary}")
        for check in result.details.get("checks", []):
            icon = {"error": "X", "warning": "!", "info": "i"}.get(check["severity"], "?")
            print(f"    [{icon}] {check.get('check', '')}: {check['message'][:100]}")

    def _print_upgrade_result(self, result):
        """Pretty-print upgrade result."""
        d = result.details
        print(f"\n  {'=' * 65}")
        if result.success:
            print(f"  UPGRADE SUCCEEDED: {d.get('current', '?')} → {d.get('target', '?')}")
        else:
            print(f"  UPGRADE FAILED: {d.get('current', '?')} → {d.get('target', '?')}")
        if d.get("obs_url"):
            print(f"  OBS: {d['obs_url']}")
        if result.work_dir:
            print(f"  Work dir: {result.work_dir}")
        print(f"  {'=' * 65}")

    # ─── JSON API (for n8n/MCP integration) ───────────────────────────────────

    def to_json(self, result):
        """Convert any AgentResult to a JSON-serializable dict."""
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
