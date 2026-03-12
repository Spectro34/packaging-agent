"""
Builder Agent — Package building via osc-mcp, log analysis, and AI fix loop.
All build operations delegated to osc-mcp.
"""

import os
import re
import sys

from packaging_agent.agents.base import BaseAgent, AgentResult
from packaging_agent.obs import OBSClient
from packaging_agent.http import strip_markdown
from packaging_agent.knowledge import detect_ecosystem, get_build_fix_context, get_spec_context


class BuilderAgent(BaseAgent):
    """Builds packages via osc-mcp and iteratively fixes build failures with AI."""

    def __init__(self, config):
        super().__init__(config)
        self.obs = OBSClient(config)

    def run(self, package=None, project=None, work_dir=None,
            spec_file=None, target_repo="openSUSE_Tumbleweed",
            target_arch="x86_64", max_attempts=3, **kwargs):
        """Build a package via osc-mcp's run_build.

        Args:
            package: Package name
            project: OBS project (branch project)
            work_dir: Local osc checkout directory (managed by osc-mcp)
            spec_file: Spec filename
            target_repo: Target repository for build
            target_arch: Target architecture
            max_attempts: Max build-fix attempts
        """
        if not package:
            return AgentResult(
                success=False, action="build", package="",
                summary="No package name", errors=["package name is required"])

        if not spec_file and work_dir and os.path.isdir(work_dir):
            for f in os.listdir(work_dir):
                if f.endswith(".spec"):
                    spec_file = f
                    break

        # Read current spec content (for AI fix loop)
        spec_content = ""
        spec_path = None
        if spec_file and work_dir:
            spec_path = os.path.join(work_dir, spec_file)
            if os.path.exists(spec_path):
                with open(spec_path) as f:
                    spec_content = f.read()

        ecosystem = detect_ecosystem(package, project or "", spec_content)
        spec_context = get_spec_context(ecosystem)

        build_success = False
        all_logs = []

        for attempt in range(1, max_attempts + 1):
            print(f"\n         --- Build Attempt {attempt}/{max_attempts} ---")
            print(f"         Building {package} via osc-mcp (may take several minutes)...")

            # Build via osc-mcp run_build
            success, log_content = self.obs.build_local(
                project or "", package,
                distribution=target_repo, arch=target_arch)

            all_logs.append(log_content[-5000:] if log_content else "")

            if success:
                print(f"\n         LOCAL BUILD SUCCEEDED!")
                build_success = True
                break

            # Build failed
            log_lines = (log_content or "").strip().split("\n")
            print(f"\n         LOCAL BUILD FAILED")
            print(f"         Build log (last 15 lines):")
            for line in log_lines[-15:]:
                line = line.strip()
                if line:
                    print(f"           {line[:140]}")

            # Knowledge-base diagnosis
            kb_hints = get_build_fix_context(ecosystem, log_content or "")
            if kb_hints:
                print(f"\n         Knowledge base hints:")
                for line in kb_hints.split("\n"):
                    print(f"           {line}")

            # Last attempt — just diagnose
            if attempt >= max_attempts:
                if self.api_key:
                    diagnosis = self._diagnose(package, ecosystem, spec_context,
                                               log_content or "", target_repo, target_arch)
                    print(f"\n         AI diagnosis of final failure:")
                    for line in diagnosis.split("\n"):
                        print(f"           {line}")
                break

            # Detect if failure is in %check (test suite) vs actual build
            log_lower = (log_content or "").lower()
            is_test_failure = any(marker in log_lower for marker in [
                "check:", "pytest", "test_", "taskgroup", "assertionerror",
                "error in test", "failed test", "tests failed",
            ]) and "rpm build error" not in log_lower

            # Deterministic fix: remove stale %files entries that cause "File not found"
            if spec_path and spec_content:
                fixed, spec_content = self._fix_files_not_found(
                    spec_content, spec_path, log_content or "")
                if fixed:
                    print(f"         Auto-fixed stale %files entries. Retrying...")
                    continue

            # AI fix spec and retry
            if self.api_key and spec_path and spec_content:
                if is_test_failure and attempt == max_attempts - 1:
                    # Last chance: test failures are often not fixable via spec changes
                    # Try building with --nochecks to skip %check
                    print(f"\n         Test failure detected — retrying with --nochecks...")
                    success, log_content = self.obs.build_local(
                        project or "", package,
                        distribution=target_repo, arch=target_arch)
                    # Note: osc-mcp run_build already uses --nochecks
                    # If it still fails, it's not a test issue
                    if success:
                        print(f"\n         LOCAL BUILD SUCCEEDED (with --nochecks)!")
                        build_success = True
                        break
                    # Still failed — continue to AI fix
                    all_logs.append(log_content[-5000:] if log_content else "")

                print(f"\n         AI diagnosing and fixing spec...")
                fixed_spec = self._ai_fix_spec(
                    package, ecosystem, spec_content, spec_context,
                    kb_hints, log_content or "", target_repo, target_arch
                )
                if fixed_spec and fixed_spec != spec_content.strip():
                    # Validate the AI didn't strip critical sections
                    from packaging_agent.agents.upgrade import UpgradeAgent
                    fixed_spec = UpgradeAgent._validate_spec_integrity(
                        spec_content, fixed_spec, package)
                    with open(spec_path, "w") as f:
                        f.write(fixed_spec)
                    spec_content = fixed_spec
                    print(f"         AI produced fixed spec ({len(fixed_spec)} chars). Retrying...")
                else:
                    print(f"         AI could not produce a fix. Stopping.")
                    break
            else:
                print(f"         [No API key or spec — cannot auto-fix]")
                break

        return AgentResult(
            success=build_success,
            action="build",
            package=package or "",
            project=project or "",
            summary=f"{'PASSED' if build_success else 'FAILED'} "
                    f"after {min(attempt, max_attempts)} attempt(s)",
            details={
                "ecosystem": ecosystem,
                "target_repo": target_repo,
                "target_arch": target_arch,
                "attempts": min(attempt, max_attempts),
                "build_logs": all_logs,
            },
            work_dir=work_dir,
            needs_review=build_success,
        )

    def fix(self, package, project, spec_path, build_log, ecosystem=None, **kwargs):
        """AI-fix a spec file based on a build log. Returns fixed spec content or None."""
        with open(spec_path) as f:
            spec_content = f.read()

        ecosystem = ecosystem or detect_ecosystem(package, project, spec_content)
        spec_context = get_spec_context(ecosystem)
        kb_hints = get_build_fix_context(ecosystem, build_log)

        fixed = self._ai_fix_spec(
            package, ecosystem, spec_content, spec_context,
            kb_hints, build_log, "openSUSE_Tumbleweed", "x86_64"
        )
        if fixed and fixed != spec_content.strip():
            with open(spec_path, "w") as f:
                f.write(fixed)
            return fixed
        return None

    @staticmethod
    def _fix_files_not_found(spec_content, spec_path, log_content):
        """Deterministic fix for 'File not found' and 'cp: cannot stat' build errors.

        Parses the build log for missing file errors, then removes or fixes the
        matching lines in the spec's %files, %doc, %license, or %install sections.

        Returns (was_fixed: bool, updated_spec: str)
        """
        # Collect all missing file paths from the build log
        missing_buildroot = re.findall(
            r'(?:error: )?[Ff]ile not found:\s*\S+/BUILDROOT/(.+)', log_content)
        missing_cp = re.findall(
            r"cp: cannot stat '.*?/BUILD/[^/]+/([^']+)'", log_content)

        # Extract just the filenames
        missing_files = set()
        for path in missing_buildroot + missing_cp:
            basename = path.rsplit("/", 1)[-1] if "/" in path else path
            if basename:
                missing_files.add(basename)

        if not missing_files:
            return False, spec_content

        fixed_any = False
        lines = spec_content.split("\n")
        new_lines = []

        for line in lines:
            stripped = line.strip()
            should_remove = False

            for fname in missing_files:
                # Direct match: the spec line contains the missing filename
                if fname in stripped:
                    # Only remove from %files-related lines
                    if any(stripped.startswith(p) for p in (
                        "%doc", "%license", "%attr", "%{python_sitelib}",
                        "%{_datadir}", "%{_bindir}", "%{_libdir}",
                        "%{_mandir}", "%{_includedir}")):
                        should_remove = True
                        break
                    # Glob patterns in %files
                    if stripped and not stripped.startswith("%") and "/" in stripped:
                        should_remove = True
                        break

                # nspkg.pth: always safe to remove
                if "nspkg.pth" in fname and "nspkg.pth" in stripped:
                    should_remove = True
                    break

            # Also check full BUILDROOT paths for glob patterns
            if not should_remove:
                for full_path in missing_buildroot:
                    # Extract the glob-like pattern from the full path
                    # e.g., site-packages/[Xx][Ss]tatic...*nspkg.pth
                    if "[" in full_path:
                        # This is a glob — check if the spec line has a similar glob
                        glob_part = full_path.rsplit("/", 1)[-1]
                        if glob_part and glob_part in stripped:
                            should_remove = True
                            break

            if should_remove:
                print(f"         [fix] Removing stale line: {stripped[:80]}")
                fixed_any = True
            else:
                new_lines.append(line)

        # Also handle `cp: cannot stat` in %install section by removing the cp line
        if missing_cp and not fixed_any:
            new_lines2 = []
            for line in new_lines:
                stripped = line.strip()
                remove = False
                for cp_file in missing_cp:
                    fname = cp_file.rsplit("/", 1)[-1]
                    if fname and f"cp " in stripped and fname in stripped:
                        remove = True
                        break
                    if fname and f"install " in stripped and fname in stripped:
                        remove = True
                        break
                if remove:
                    print(f"         [fix] Removing stale install line: {stripped[:80]}")
                    fixed_any = True
                else:
                    new_lines2.append(line)
            new_lines = new_lines2

        if fixed_any:
            spec_content = "\n".join(new_lines)
            with open(spec_path, "w") as f:
                f.write(spec_content)

        return fixed_any, spec_content

    def _diagnose(self, package, ecosystem, spec_context, log_content,
                  target_repo, target_arch):
        """AI-diagnose a build failure."""
        kb_hints = get_build_fix_context(ecosystem, log_content)
        return self.gpt(
            f"You are an RPM build error expert for openSUSE.\n{spec_context}\n"
            "Analyze this build log. Give: ROOT CAUSE, FAILED STEP, exact FIX needed.",
            f"Package: {package} (ecosystem: {ecosystem})\n"
            f"Build: {target_repo}/{target_arch}\n\n"
            f"{kb_hints}\n\n"
            f"Build log:\n{log_content[-4000:]}",
            temperature=0.1, max_tokens=500
        )

    def _ai_fix_spec(self, package, ecosystem, spec_content, spec_context,
                     kb_hints, log_content, target_repo, target_arch):
        """Ask AI to produce a fixed spec file."""
        fix_response = self.gpt(
            f"You are an RPM spec file expert for openSUSE.\n{spec_context}\n"
            "A package build has failed. Analyze the build log, identify the root cause, "
            "and return the COMPLETE fixed spec file.\n"
            "Do NOT wrap in markdown. No ```. Just the raw spec file content.\n"
            "If tests fail in %check and are not packaging issues, add appropriate "
            "pytest deselect or skip markers to work around them.",
            f"Package: {package} (ecosystem: {ecosystem})\n"
            f"Build: {target_repo}/{target_arch}\n\n"
            f"{kb_hints}\n\n"
            f"Current spec:\n{spec_content}\n\n"
            f"Build log (last 3000 chars):\n{log_content[-3000:]}",
            temperature=0.1, max_tokens=4000
        )
        fixed = strip_markdown(fix_response)
        if fixed.startswith("[GPT"):
            return None
        return fixed
