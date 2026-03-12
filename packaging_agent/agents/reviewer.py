"""
Reviewer Agent — Pre-commit quality gate for spec files and packages.

Produces a VERDICT:
  COMMIT      — all checks pass, safe to commit
  NEEDS_HUMAN — builds locally but has issues requiring human judgement
  REJECT      — critical problems, do NOT commit
"""

import os
import re
import json

from packaging_agent.agents.base import BaseAgent, AgentResult
from packaging_agent.knowledge import detect_ecosystem, get_spec_context, ECOSYSTEMS


class ReviewerAgent(BaseAgent):
    """Pre-commit quality gate. Returns a verdict: COMMIT / NEEDS_HUMAN / REJECT."""

    def run(self, package=None, project=None, work_dir=None,
            spec_content=None, ecosystem=None, branch_project=None,
            upgrade_context=None, **kwargs):
        """Review a package before commit.

        Args:
            package: Package name
            project: Source OBS project
            work_dir: Local osc checkout path
            spec_content: Raw spec content (alternative to work_dir)
            ecosystem: Package ecosystem
            branch_project: OBS branch project (for remote build check)
            upgrade_context: Dict with upgrade details (current, target, changelog, dep_diff)
        """
        checks = []
        upgrade_context = upgrade_context or {}

        # Get spec content
        if not spec_content and work_dir:
            for f in os.listdir(work_dir):
                if f.endswith(".spec"):
                    with open(os.path.join(work_dir, f)) as fh:
                        spec_content = fh.read()
                    break

        if not spec_content:
            return AgentResult(
                success=False, action="review", package=package or "",
                summary="No spec content", errors=["Cannot review without spec"])

        ecosystem = ecosystem or detect_ecosystem(package or "", project or "", spec_content)

        # Run all checks
        checks.extend(self._lint_spec(spec_content, ecosystem))
        checks.extend(self._check_changelog(work_dir, package))
        checks.extend(self._check_ecosystem_macros(spec_content, ecosystem))
        checks.extend(self._check_dep_consistency(spec_content, upgrade_context))

        # Skip OBS remote build check pre-commit — the branch hasn't been committed
        # yet so OBS results reflect the SOURCE project, not our changes.
        # Post-commit OBS verification (step 9) handles this correctly.

        # AI review with upgrade context
        if self.api_key:
            ai_review = self._ai_review(spec_content, ecosystem, package, upgrade_context)
            checks.append({
                "check": "ai_review",
                "severity": "info",
                "message": ai_review,
            })

        # Compute verdict
        errors = [c for c in checks if c.get("severity") == "error"]
        warnings = [c for c in checks if c.get("severity") == "warning"]
        verdict, reason = self._compute_verdict(checks, upgrade_context)

        return AgentResult(
            success=verdict == "COMMIT",
            action="review",
            package=package or "",
            project=project or "",
            summary=f"{verdict}: {reason}",
            details={
                "verdict": verdict,
                "verdict_reason": reason,
                "checks": checks,
                "ecosystem": ecosystem,
                "error_count": len(errors),
                "warning_count": len(warnings),
            },
        )

    def _lint_spec(self, spec, ecosystem):
        """Regex-based spec file quality checks."""
        checks = []

        # Must have Version
        if not re.search(r'^Version:\s*\S+', spec, re.MULTILINE):
            checks.append({"check": "version_missing", "severity": "error",
                           "message": "No Version: tag found"})

        # Must have License
        if not re.search(r'^License:\s*\S+', spec, re.MULTILINE):
            checks.append({"check": "license_missing", "severity": "error",
                           "message": "No License: tag found"})

        # Should use %license not %doc for license files
        if re.search(r'^%doc.*LICENSE', spec, re.MULTILINE | re.IGNORECASE):
            checks.append({"check": "license_as_doc", "severity": "warning",
                           "message": "Use %license instead of %doc for license files"})

        # Should not have %changelog (openSUSE uses .changes)
        if re.search(r'^%changelog', spec, re.MULTILINE):
            checks.append({"check": "changelog_in_spec", "severity": "warning",
                           "message": "Remove %changelog section — openSUSE uses .changes files"})

        # Should not have %defattr (deprecated)
        if "%defattr" in spec:
            checks.append({"check": "defattr", "severity": "warning",
                           "message": "%defattr is deprecated, RPM handles permissions automatically"})

        # Should not have BuildRoot (RPM handles it)
        if re.search(r'^BuildRoot:', spec, re.MULTILINE):
            checks.append({"check": "buildroot", "severity": "warning",
                           "message": "BuildRoot: tag is deprecated, RPM handles it automatically"})

        # Source URL should be valid (not a placeholder)
        source_match = re.search(r'^Source\d*:\s*(\S+)', spec, re.MULTILINE)
        if source_match:
            source_url = source_match.group(1)
            if not source_url.startswith(("http://", "https://", "%{", "ftp://")):
                checks.append({"check": "source_url", "severity": "warning",
                               "message": f"Source URL doesn't look like a download link: {source_url[:60]}"})

        # Release should be numeric
        release_match = re.search(r'^Release:\s*(\S+)', spec, re.MULTILINE)
        if release_match:
            rel = release_match.group(1)
            if not re.match(r'^\d+', rel):
                checks.append({"check": "release_format", "severity": "warning",
                               "message": f"Release should start with a number: {rel}"})

        return checks

    def _check_changelog(self, work_dir, package):
        """Check .changes file exists and has proper format."""
        checks = []
        if not work_dir:
            return checks

        changes_files = [f for f in os.listdir(work_dir) if f.endswith(".changes")]
        if not changes_files:
            checks.append({"check": "no_changes", "severity": "error",
                           "message": "No .changes file found — required for openSUSE"})
            return checks

        with open(os.path.join(work_dir, changes_files[0])) as f:
            content = f.read()

        if len(content) < 50:
            checks.append({"check": "empty_changes", "severity": "warning",
                           "message": ".changes file is very short"})

        # Check format: should start with dashes separator
        if not content.startswith("---"):
            checks.append({"check": "changes_format", "severity": "warning",
                           "message": ".changes should start with separator line (---...)"})

        return checks

    def _check_ecosystem_macros(self, spec, ecosystem):
        """Check that the correct ecosystem macros are used."""
        checks = []
        eco = ECOSYSTEMS.get(ecosystem, {})

        if ecosystem == "python":
            # Should use %pyproject or %py3 macros
            if "%pyproject_wheel" not in spec and "%py3_build" not in spec:
                if "setup.py" in spec:
                    checks.append({"check": "python_legacy_build", "severity": "warning",
                                   "message": "Using setup.py directly — consider %pyproject_wheel or %py3_build macros"})

            # Should use %pytest for tests
            if "%check" in spec and "%pytest" not in spec and "pytest" in spec.lower():
                checks.append({"check": "python_test_macro", "severity": "info",
                               "message": "Consider using %pytest macro instead of direct pytest call"})

        elif ecosystem == "go":
            if "%gobuild" not in spec and "%go_build" not in spec:
                checks.append({"check": "go_no_macro", "severity": "info",
                               "message": "Consider using %gobuild macro from golang-packaging"})

        elif ecosystem == "rust":
            if "%cargo_build" not in spec:
                checks.append({"check": "rust_no_macro", "severity": "info",
                               "message": "Consider using %cargo_build macro from cargo-packaging"})

        return checks

    def _check_dep_consistency(self, spec, upgrade_context):
        """Verify that dependency changes from upstream were applied to spec."""
        checks = []
        dep_diff = upgrade_context.get("dep_diff")
        if not dep_diff:
            return checks

        spec_lower = spec.lower()

        # Check removed deps are actually gone
        for dep in dep_diff.get("removed", []):
            dep_name = dep.split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip()
            # Check for python3-<name> or %{ansible_python}-<name>
            patterns = [f"python3-{dep_name}", dep_name.replace("-", "_")]
            still_present = any(p.lower() in spec_lower for p in patterns)
            if still_present:
                checks.append({
                    "check": "dep_not_removed",
                    "severity": "warning",
                    "message": f"Upstream removed '{dep_name}' but it may still be in Requires/BuildRequires",
                })

        # Check added deps are present
        for dep in dep_diff.get("added", []):
            dep_name = dep.split("[")[0].split(">")[0].split("<")[0].split("=")[0].strip()
            patterns = [f"python3-{dep_name}", dep_name.replace("-", "_")]
            is_present = any(p.lower() in spec_lower for p in patterns)
            if not is_present:
                checks.append({
                    "check": "dep_not_added",
                    "severity": "warning",
                    "message": f"Upstream added '{dep_name}' but it's missing from Requires/BuildRequires",
                })

        return checks

    def _check_obs_builds(self, branch_project, package):
        """Check OBS remote build status and fetch unresolvable reasons."""
        checks = []
        try:
            from packaging_agent.obs import OBSClient
            obs = OBSClient(self.config)
            results = obs.build_results(branch_project, package)
            if not results:
                checks.append({
                    "check": "obs_build_unknown",
                    "severity": "warning",
                    "message": "Could not fetch OBS build results for branch",
                })
                return checks

            summary = results.get("summary", {})
            total = sum(summary.values())
            succeeded = summary.get("succeeded", 0)
            failed = summary.get("failed", 0)
            unresolvable = summary.get("unresolvable", 0)

            if unresolvable > 0:
                # Fetch actual unresolvable reasons from OBS buildinfo API
                missing_deps = self._fetch_unresolvable_reasons(
                    branch_project, package, results.get("results", []))
                if missing_deps:
                    dep_list = ", ".join(missing_deps[:5])
                    extra = f" (+{len(missing_deps)-5} more)" if len(missing_deps) > 5 else ""
                    severity = "error" if unresolvable == total else "warning"
                    checks.append({
                        "check": "obs_unresolvable_deps",
                        "severity": severity,
                        "message": f"{unresolvable}/{total} OBS builds unresolvable. "
                                   f"Missing: {dep_list}{extra}",
                    })
                elif unresolvable == total:
                    checks.append({
                        "check": "obs_all_unresolvable",
                        "severity": "error",
                        "message": f"ALL {total} OBS builds are unresolvable — missing dependencies in repos",
                    })
                else:
                    checks.append({
                        "check": "obs_some_unresolvable",
                        "severity": "warning",
                        "message": f"{unresolvable}/{total} OBS builds unresolvable (may be expected for older repos)",
                    })

            if failed > 0:
                checks.append({
                    "check": "obs_build_failed",
                    "severity": "error",
                    "message": f"{failed}/{total} OBS builds FAILED",
                })
            if succeeded > 0:
                checks.append({
                    "check": "obs_build_succeeded",
                    "severity": "info",
                    "message": f"{succeeded}/{total} OBS builds succeeded",
                })
            if succeeded == 0 and failed == 0 and unresolvable == 0:
                building = summary.get("building", 0) + summary.get("scheduled", 0) + summary.get("dispatching", 0)
                if building > 0:
                    checks.append({
                        "check": "obs_builds_pending",
                        "severity": "info",
                        "message": f"{building} OBS builds still running/scheduled",
                    })

        except Exception as e:
            checks.append({
                "check": "obs_build_check_error",
                "severity": "info",
                "message": f"Could not check OBS builds: {str(e)[:100]}",
            })
        return checks

    def _fetch_unresolvable_reasons(self, project, package, build_results):
        """Query OBS _buildinfo API to find actual missing dependency names,
        then look up correct RPM names from OBS repos."""
        import urllib.request
        import urllib.error
        import base64

        obs_user = self.config.get("obs_user", "")
        obs_pass = self.config.get("obs_pass", "")
        if not obs_user or not obs_pass:
            obs_user = os.environ.get("OBS_USER", "")
            obs_pass = os.environ.get("OBS_PASS", "")
        if not obs_user:
            return []

        missing = set()
        repo_used = ""
        for r in build_results:
            if r.get("status") != "unresolvable":
                continue
            repo = r.get("repository", "")
            arch = r.get("arch", "x86_64")
            url = (f"https://api.opensuse.org/build/{project}/{repo}/{arch}"
                   f"/{package}/_buildinfo")
            auth = base64.b64encode(f"{obs_user}:{obs_pass}".encode()).decode()
            req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
            try:
                resp = urllib.request.urlopen(req, timeout=10)
                data = resp.read().decode()
            except urllib.error.HTTPError as e:
                data = e.read().decode() if hasattr(e, "read") else ""
            except Exception:
                continue

            for m in re.finditer(r'nothing provides\s+(\S+(?:\s*[><=]+\s*\S+)?)', data):
                missing.add(m.group(1))
            for m in re.finditer(r'unresolvable:\s*(.+?)(?:<|$)', data):
                reason = m.group(1).strip()
                if "nothing provides" in reason:
                    for sub in re.finditer(r'nothing provides\s+(\S+(?:\s*[><=]+\s*\S+)?)', reason):
                        missing.add(sub.group(1))
                elif reason:
                    missing.add(reason)
            if missing:
                repo_used = repo
                break

        if not missing:
            return []

        # Look up correct RPM names from OBS repos for each missing dep
        suggestions = self._suggest_correct_names(missing, repo_used)
        result = []
        for dep in sorted(missing):
            if dep in suggestions:
                result.append(f"{dep} (did you mean: {suggestions[dep]}?)")
            else:
                result.append(dep)
        return result

    def _suggest_correct_names(self, missing_deps, repository):
        """Search OBS repos for correct RPM package names matching missing deps."""
        from packaging_agent.obs import OBSClient
        obs = OBSClient(self.config)
        suggestions = {}

        for dep in missing_deps:
            # Extract the base name without version constraints
            base = re.split(r'[><=\s]', dep)[0].strip()
            if not base:
                continue

            # For python deps like python311-pyyaml, search case-insensitively
            search_term = base.split("-")[-1] if "-" in base else base
            prefix = base.rsplit("-", 1)[0] + "-" if "-" in base else ""

            try:
                # Map repo name: openSUSE_Tumbleweed -> openSUSE:Tumbleweed
                obs_path = repository.replace("_", ":")
                result = obs._call_tool("search_packages", {
                    "path": obs_path,
                    "path_repository": "standard",
                    "pattern": search_term,
                })
                if not result:
                    continue

                # Parse the search results to find case-correct match
                import json
                try:
                    data = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    data = result

                # Look for the matching package with correct casing
                if isinstance(data, dict) and "packages" in data:
                    pkgs = data["packages"]
                elif isinstance(data, list):
                    pkgs = data
                elif isinstance(data, str):
                    # Parse package names from text
                    pkgs = [{"name": line.strip()} for line in data.split("\n")
                            if line.strip() and search_term.lower() in line.lower()]
                else:
                    continue

                for pkg in pkgs:
                    name = pkg.get("name", pkg) if isinstance(pkg, dict) else str(pkg)
                    if name.lower() == base.lower() and name != base:
                        suggestions[dep] = name
                        break
            except Exception:
                continue

        return suggestions

    def _compute_verdict(self, checks, upgrade_context):
        """Determine COMMIT / NEEDS_HUMAN / REJECT based on all checks."""
        errors = [c for c in checks if c.get("severity") == "error"]
        warnings = [c for c in checks if c.get("severity") == "warning"]

        # REJECT: critical structural problems
        structural_errors = [e for e in errors if e["check"] in (
            "version_missing", "license_missing", "no_changes")]
        if structural_errors:
            return "REJECT", "; ".join(e["message"] for e in structural_errors)

        # REJECT: all OBS builds unresolvable (not just older repos)
        obs_all_unresolvable = [e for e in errors if e["check"] == "obs_all_unresolvable"]
        if obs_all_unresolvable:
            return "REJECT", obs_all_unresolvable[0]["message"]

        # REJECT: OBS builds failed
        obs_failed = [e for e in errors if e["check"] == "obs_build_failed"]
        if obs_failed:
            return "REJECT", obs_failed[0]["message"]

        # NEEDS_HUMAN: has warnings that suggest manual review
        # Note: obs_some_unresolvable is NOT included — unresolvable on older repos
        # (SLE 15 etc.) is expected and the post-commit OBS verification handles it
        human_warnings = [w for w in warnings if w["check"] in (
            "dep_not_removed", "dep_not_added")]
        if human_warnings:
            return "NEEDS_HUMAN", "; ".join(w["message"] for w in human_warnings[:3])

        # NEEDS_HUMAN: HIGH/CRITICAL risk upgrade
        risk = upgrade_context.get("risk_level", "UNKNOWN")
        if risk in ("HIGH", "CRITICAL"):
            return "NEEDS_HUMAN", f"Upgrade risk is {risk} — human review recommended"

        # Any remaining errors
        if errors:
            return "NEEDS_HUMAN", "; ".join(e["message"] for e in errors[:3])

        # Any remaining warnings
        if warnings:
            return "COMMIT", f"Passed with {len(warnings)} warning(s)"

        return "COMMIT", "All checks passed"

    def _ai_review(self, spec, ecosystem, package, upgrade_context=None):
        """AI-powered spec review with upgrade context."""
        spec_context = get_spec_context(ecosystem)
        ctx = ""
        if upgrade_context:
            ctx = (f"\nUpgrade context: {upgrade_context.get('current', '?')} → "
                   f"{upgrade_context.get('target', '?')}\n"
                   f"Risk: {upgrade_context.get('risk_level', 'UNKNOWN')}\n")
            if upgrade_context.get("dep_diff_text"):
                ctx += f"Dep changes: {upgrade_context['dep_diff_text'][:300]}\n"
        return self.gpt(
            f"You are an openSUSE package reviewer.\n{spec_context}\n"
            "Review this spec file for:\n"
            "1. Correctness (proper macros, deps, file lists)\n"
            "2. Best practices (openSUSE guidelines)\n"
            "3. Security (no world-writable files, proper permissions)\n"
            f"{ctx}"
            "Give a brief review (5-8 lines). Flag issues as [ERROR], [WARN], or [OK].",
            f"Package: {package} (ecosystem: {ecosystem})\n\n"
            f"Spec:\n{spec[:4000]}",
            temperature=0.2, max_tokens=400
        )
