"""
Analyzer Agent — Deep package analysis: OBS status, CVEs, build failures, upstream tracking.
"""

import sys

from packaging_agent.agents.base import BaseAgent, AgentResult
from packaging_agent.obs import OBSClient
from packaging_agent.data_sources import repology_check, osv_query, github_releases, verify_cve_fix
from packaging_agent.knowledge import detect_ecosystem, get_osv_ecosystem, strip_ecosystem_prefix, get_build_fix_context


class AnalyzerAgent(BaseAgent):
    """Analyzes packages for CVEs, version drift, build failures, and overall health."""

    def __init__(self, config):
        super().__init__(config)
        self.obs = OBSClient(config)

    def run(self, package=None, project=None, **kwargs):
        """Analyze a single package or scan all packages in a project."""
        if package:
            return self.analyze_one(package, project)
        return self.scan_all(project)

    def analyze_one(self, pkg_name, project=None):
        """Deep analysis of a single package. Returns structured result."""
        project = project or self.config.get("obs_project", "")
        ecosystem = detect_ecosystem(pkg_name, project)
        osv_eco = get_osv_ecosystem(ecosystem)
        osv_name = strip_ecosystem_prefix(pkg_name, ecosystem)

        findings = {}

        # OBS version & build status
        if self.obs.available():
            obs_info = self.obs.version_history(project, pkg_name)
            findings["obs_version"] = obs_info["version"]
            findings["obs_last_user"] = obs_info["user"]
            findings["obs_last_time"] = obs_info["time"]

            build = self.obs.build_results(project, pkg_name)
            if build:
                findings["build_status"] = build["summary"]
                findings["build_results"] = build["results"]

                # Get failed build log + diagnosis
                failed = self.obs.get_failed_build_log(project, pkg_name, build)
                if failed:
                    findings["build_failure"] = {
                        "repository": failed["repository"],
                        "arch": failed["arch"],
                        "log_tail": failed["log"][-2000:],
                    }
                    # Knowledge-based diagnosis
                    kb_hints = get_build_fix_context(ecosystem, failed["log"])
                    if kb_hints:
                        findings["build_failure"]["kb_diagnosis"] = kb_hints

                    # AI diagnosis
                    if self.api_key:
                        diagnosis = self.gpt(
                            "You are an RPM build error expert for openSUSE. Analyze this build log.\n"
                            "Give: ROOT CAUSE, FAILED STEP, exact FIX needed. Be specific and concise.",
                            f"Package: {pkg_name} ({ecosystem})\n"
                            f"Build: {failed['repository']}/{failed['arch']}\n\n"
                            f"{kb_hints}\n\n"
                            f"Build log:\n{failed['log'][-3000:]}",
                            temperature=0.1, max_tokens=500
                        )
                        findings["build_failure"]["ai_diagnosis"] = diagnosis

            # Spec file info
            spec_info = self.obs.spec_file(project, pkg_name)
            if spec_info:
                findings["spec_version"] = spec_info["version"]
                findings["patches"] = spec_info["patches"]
                findings["build_requires_count"] = len(spec_info["build_requires"])

        # CVE scan
        version = findings.get("obs_version", "unknown")
        cves = osv_query(osv_name, osv_eco, version)
        findings["cves"] = cves
        findings["cve_count"] = len(cves)

        # Verify CVE fixes
        for cve in cves[:5]:  # Verify top 5
            if cve.get("fix_commit"):
                cve["fix_verified"] = verify_cve_fix(cve)

        # Upstream version
        upstream_ver = "unknown"
        repo = repology_check(pkg_name)
        if repo["newest"] != "unknown":
            upstream_ver = repo["newest"]
        findings["upstream_version"] = upstream_ver
        findings["outdated"] = (version != "unknown" and upstream_ver != "unknown"
                                and version != upstream_ver)

        # GitHub releases
        github_slug = self._infer_github(pkg_name, ecosystem)
        if github_slug:
            releases = github_releases(github_slug)
            findings["recent_releases"] = releases[:3]

        findings["ecosystem"] = ecosystem
        findings["package"] = pkg_name
        findings["project"] = project

        # AI summary
        if self.api_key:
            summary_text = self.gpt(
                "You are a package maintenance expert for openSUSE. "
                "Give a concise health assessment (3-4 sentences).",
                f"Package: {pkg_name} ({ecosystem})\n"
                f"OBS version: {findings.get('obs_version', 'unknown')}\n"
                f"Upstream: {upstream_ver}\n"
                f"CVEs: {len(cves)}\n"
                f"Build: {findings.get('build_status', 'unknown')}\n"
                f"Patches: {len(findings.get('patches', []))}\n",
                temperature=0.2, max_tokens=200
            )
            findings["ai_summary"] = summary_text

        return AgentResult(
            success=True,
            action="analyze",
            package=pkg_name,
            project=project,
            summary=f"{pkg_name}: v{version}, {len(cves)} CVEs, "
                    f"{'outdated' if findings['outdated'] else 'current'}",
            details=findings,
        )

    def scan_all(self, project=None):
        """Scan all packages in a project. Returns aggregated result."""
        project = project or self.config.get("obs_project", "")
        packages = self.obs.discover_packages(project)
        if not packages:
            return AgentResult(success=False, action="scan", project=project,
                               summary="No packages found", errors=["Empty project"])

        all_findings = []
        total_cves = 0
        outdated = 0
        build_failures = 0

        for pkg in packages:
            name = pkg["name"]
            sys.stdout.write(f"  Scanning {name}...")
            sys.stdout.flush()

            result = self.analyze_one(name, project)
            f = result.details
            all_findings.append(f)
            total_cves += f.get("cve_count", 0)
            if f.get("outdated"):
                outdated += 1
            if f.get("build_failure"):
                build_failures += 1

            print(f" {f.get('cve_count', 0)} CVEs, "
                  f"{'OUTDATED' if f.get('outdated') else 'current'}, "
                  f"{'BUILD FAIL' if f.get('build_failure') else 'ok'}")

        summary = (f"{len(packages)} packages | {total_cves} CVEs | "
                   f"{outdated} outdated | {build_failures} build failures")

        return AgentResult(
            success=True,
            action="scan",
            project=project,
            summary=summary,
            details={
                "packages": all_findings,
                "total_cves": total_cves,
                "outdated_count": outdated,
                "build_failure_count": build_failures,
                "package_count": len(packages),
            },
        )

    def _infer_github(self, pkg_name, ecosystem):
        """Try to infer GitHub slug from package name."""
        clean = pkg_name
        if pkg_name.startswith("python-"):
            clean = pkg_name[7:]
        elif pkg_name.startswith("golang-"):
            clean = pkg_name[7:]

        # Common patterns
        known = {
            "ansible": "ansible/ansible",
            "ansible-core": "ansible/ansible",
            "ansible-lint": "ansible/ansible-lint",
            "ansible-runner": "ansible/ansible-runner",
            "molecule": "ansible-community/molecule",
            "semaphore": "semaphoreui/semaphore",
        }
        if clean in known:
            return known[clean]
        if pkg_name in known:
            return known[pkg_name]

        # Try common org patterns
        if "ansible" in pkg_name.lower():
            return f"ansible/{clean}"
        return ""

    def to_json(self, result):
        """Convert AgentResult to JSON-serializable dict for n8n integration."""
        return {
            "success": result.success,
            "action": result.action,
            "package": result.package,
            "project": result.project,
            "summary": result.summary,
            "details": result.details,
            "errors": result.errors,
        }
