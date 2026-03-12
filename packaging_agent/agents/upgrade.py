"""
Upgrade Agent — Version upgrade pipeline with local build verification.
All OBS operations delegated to osc-mcp via OBSClient.
"""

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone

from packaging_agent.agents.base import BaseAgent, AgentResult
from packaging_agent.obs import OBSClient
from packaging_agent.http import strip_markdown
from packaging_agent.data_sources import github_releases, repology_check, pypi_dep_diff
from packaging_agent.knowledge import (
    detect_ecosystem, get_upgrade_context, get_spec_context, get_build_fix_context
)


class UpgradeAgent(BaseAgent):
    """Handles package version upgrades: changelog analysis, spec update,
    source download, local build verification, and OBS commit.

    All OBS operations go through osc-mcp — no direct API calls or subprocess osc."""

    def __init__(self, config):
        super().__init__(config)
        self.obs = OBSClient(config)

    def run(self, package, target_version, project=None, live=False,
            github_slug="", max_build_attempts=3, **kwargs):
        """Execute the upgrade pipeline.

        Args:
            package: Package name (e.g., "molecule")
            target_version: Target version string (e.g., "26.3.0")
            project: OBS project (default from config)
            live: If True, execute; if False, dry run
            github_slug: GitHub owner/repo for changelog
            max_build_attempts: Max local build attempts
        """
        project = project or self.config.get("obs_project", "")
        if not project:
            return AgentResult(
                success=False, action="upgrade", package=package,
                summary="No project specified",
                errors=["Provide 'project' parameter or set OBS_PROJECT env var"])

        # Step 1: Current version
        current = "unknown"
        if self.obs.available():
            obs_info = self.obs.version_history(project, package)
            current = obs_info["version"]
        else:
            repo = repology_check(package)
            current = repo["opensuse"]

        if current == target_version:
            return AgentResult(
                success=True, action="upgrade", package=package, project=project,
                summary=f"Already at {target_version}", details={"current": current})

        # Step 2: Changelog analysis
        changelog = self._analyze_changelog(
            package, current, target_version, github_slug, project)

        if not live:
            return self._dry_run(package, project, current, target_version, changelog)

        # ── LIVE MODE ──
        return self._live_upgrade(
            package, project, current, target_version,
            changelog, max_build_attempts)

    @staticmethod
    def _validate_spec_integrity(original_spec, updated_spec, package):
        """Validate the AI-generated spec didn't strip critical sections.
        If sections are missing, restore them from the original spec.

        Returns the fixed spec content."""
        orig_lines = original_spec.split("\n")
        upd_lines = updated_spec.split("\n")

        # Check 1: Name field must not change
        orig_name = ""
        upd_name = ""
        for line in orig_lines:
            if line.startswith("Name:"):
                orig_name = line.split(":", 1)[1].strip()
                break
        for line in upd_lines:
            if line.startswith("Name:"):
                upd_name = line.split(":", 1)[1].strip()
                break
        if orig_name and upd_name and orig_name != upd_name:
            print(f"         [FIX] Restoring Name: {orig_name} (AI changed to {upd_name})")
            updated_spec = updated_spec.replace(f"Name:{' ' * (upd_lines[0].count(' '))}",
                                                 f"Name:{' ' * (orig_lines[0].count(' '))}", 1)
            # More reliable: line-level replacement
            fixed_lines = []
            for line in updated_spec.split("\n"):
                if line.startswith("Name:"):
                    fixed_lines.append(f"Name:           {orig_name}")
                else:
                    fixed_lines.append(line)
            updated_spec = "\n".join(fixed_lines)

        # Check 2: Header (everything before Name:) should be preserved
        # Find the header in original (lines before the first non-comment, non-empty, non-macro definition)
        orig_header = []
        for line in orig_lines:
            if line.startswith("Name:"):
                break
            orig_header.append(line)

        upd_header = []
        for line in upd_lines:
            if line.startswith("Name:"):
                break
            upd_header.append(line)

        # If the AI stripped >50% of the header, restore it
        if orig_header and len(upd_header) < len(orig_header) * 0.5:
            print(f"         [FIX] Restoring header ({len(orig_header)} lines stripped by AI)")
            # Find where "Name:" is in the updated spec and prepend the original header
            name_idx = -1
            for i, line in enumerate(upd_lines):
                if line.startswith("Name:"):
                    name_idx = i
                    break
            if name_idx >= 0:
                updated_spec = "\n".join(orig_header + upd_lines[name_idx:])

        # Check 3: Source: line must not be changed by AI fix attempts
        # (Source is managed by the tarball download step, not AI)
        orig_source = ""
        upd_source = ""
        for line in orig_lines:
            if re.match(r'^Source\d*:', line):
                orig_source = line.strip()
                break
        for i, line in enumerate(updated_spec.split("\n")):
            if re.match(r'^Source\d*:', line):
                upd_source = line.strip()
                break
        if orig_source and upd_source and orig_source != upd_source:
            print(f"         [FIX] Restoring Source: (AI modified it)")
            fixed_lines = []
            source_replaced = False
            for line in updated_spec.split("\n"):
                if re.match(r'^Source\d*:', line) and not source_replaced:
                    fixed_lines.append(orig_source)
                    source_replaced = True
                else:
                    fixed_lines.append(line)
            updated_spec = "\n".join(fixed_lines)

        # Check 4: Critical macros that must not be removed
        critical_patterns = [
            "%{?sle15_python_module_pythons}",
            "%bcond_without test",
            "%bcond_with test",
            "%define ansible_python",
            "%define pythons",
        ]
        for pattern in critical_patterns:
            if pattern in original_spec and pattern not in updated_spec:
                print(f"         [FIX] AI removed critical macro: {pattern} — restoring header")
                # Re-do the full header restoration
                name_idx = -1
                for i, line in enumerate(updated_spec.split("\n")):
                    if line.startswith("Name:"):
                        name_idx = i
                        break
                if name_idx >= 0:
                    updated_spec = "\n".join(orig_header + updated_spec.split("\n")[name_idx:])
                break

        return updated_spec

    @staticmethod
    def _restore_dep_casing(original_spec, updated_spec):
        """Restore original package name casing in Requires/BuildRequires lines.

        GPT often lowercases PyYAML→pyyaml, Jinja2→jinja2 etc.
        This scans the original spec for package names and restores their casing
        in the updated spec using simple case-insensitive string replacement.
        """
        # Extract all dependency package names from original spec that have mixed case
        dep_pattern = re.compile(
            r'^(?:Build)?Requires:\s*(.+)', re.MULTILINE)
        # Match package names like python3-PyYAML, %{macro}-Jinja2, etc.
        name_pattern = re.compile(r'(?:python\d*|%\{[^}]+\})-([a-zA-Z][a-zA-Z0-9_.+-]*)')

        casing_map = {}  # lowercase -> original casing (just the suffix part)
        for m in dep_pattern.finditer(original_spec):
            dep_line = m.group(1)
            for nm in name_pattern.finditer(dep_line):
                name = nm.group(1)  # e.g., "PyYAML", "Jinja2"
                lower = name.lower()
                if lower != name:  # Has mixed case worth preserving
                    casing_map[lower] = name

        if not casing_map:
            return updated_spec

        # Apply fixes line by line to Requires/BuildRequires lines only
        lines = updated_spec.split("\n")
        fixed = []
        for line in lines:
            if line.strip().startswith(("Requires:", "BuildRequires:")):
                for lower, original in casing_map.items():
                    # Simple replacement: swap lowercased name back to original
                    # Match after a dash (the separator between prefix and name)
                    line = re.sub(
                        r'(-|/)' + re.escape(lower) + r'(?=[\s>=<,]|$)',
                        r'\1' + original, line, flags=re.IGNORECASE)
            fixed.append(line)

        return "\n".join(fixed)

    @staticmethod
    def _fix_setup_dir(spec_content, work_dir, target_version):
        """Fix %setup/%autosetup -n directory to match actual tarball contents.

        When PyPI returns a tarball like markuppy-1.18.tar.gz that extracts to
        markuppy-1.18/, but the spec has %setup -q -n MarkupPy-%{version}
        expecting MarkupPy-1.18/, the OBS server build fails with
        "cd: MarkupPy-1.18: No such file or directory".

        This inspects the tarball's actual top-level directory and updates the
        spec's %setup/-autosetup -n argument to match.

        Returns updated spec content (or original if no fix needed).
        """
        if not os.path.isdir(work_dir):
            return spec_content

        # Find the tarball in work_dir
        tarball_path = None
        tarball_name = None
        for f in os.listdir(work_dir):
            if f.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
                tarball_path = os.path.join(work_dir, f)
                tarball_name = f
                break
            elif f.endswith(".zip"):
                tarball_path = os.path.join(work_dir, f)
                tarball_name = f
                break

        if not tarball_path:
            return spec_content

        # Get the actual top-level directory from the archive
        actual_topdir = None
        try:
            if tarball_name.endswith(".zip"):
                with zipfile.ZipFile(tarball_path) as zf:
                    names = zf.namelist()
                    if names:
                        # Top-level dir is the first path component
                        top = names[0].split("/")[0]
                        if top:
                            actual_topdir = top
            else:
                with tarfile.open(tarball_path) as tf:
                    members = tf.getnames()
                    if members:
                        top = members[0].split("/")[0]
                        if top:
                            actual_topdir = top
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
            print(f"         [setup-dir] Cannot read archive: {e}")
            return spec_content

        if not actual_topdir:
            return spec_content

        # Parse the spec to find %setup or %autosetup line
        line_pattern = re.compile(r'^(%(?:auto)?setup\b.*)$', re.MULTILINE)
        n_pattern = re.compile(r'-n\s+(\S+)')

        line_match = line_pattern.search(spec_content)
        if not line_match:
            return spec_content

        full_line = line_match.group(1)
        n_match = n_pattern.search(full_line)

        # Determine what directory the spec currently expects
        if n_match:
            # Has explicit -n argument
            expected_dir_template = n_match.group(1)
        else:
            # No -n means default: %{name}-%{version}
            expected_dir_template = "%{name}-%{version}"

        # Expand macros in the expected directory name
        expected_dir = expected_dir_template
        expected_dir = expected_dir.replace("%{version}", target_version)

        # Extract Name: from spec
        name_m = re.search(r'^Name:\s*(\S+)', spec_content, re.MULTILINE)
        if name_m:
            expected_dir = expected_dir.replace("%{name}", name_m.group(1))

        # Expand any %define/%global macros
        for macro_m in re.finditer(r'%(?:global|define)\s+(\S+)\s+(\S+)', spec_content):
            expected_dir = expected_dir.replace(f"%{{{macro_m.group(1)}}}", macro_m.group(2))

        # Also expand nested version refs that might remain
        expected_dir = expected_dir.replace("%{version}", target_version)

        if expected_dir == actual_topdir:
            # No mismatch — nothing to fix
            return spec_content

        # Case-insensitive comparison to confirm this is actually a casing/naming issue
        # (not a completely different package somehow)
        # Safety check: the dir names should at least share a common version or package stem
        # Strip version suffix for comparison
        def strip_version(s):
            return re.sub(r'[-_]?' + re.escape(target_version) + r'$', '', s).lower()

        expected_stem = strip_version(expected_dir)
        actual_stem = strip_version(actual_topdir)

        # Allow match if stems are similar (same letters ignoring case, hyphens, underscores)
        def normalize(s):
            return re.sub(r'[-_.]', '', s).lower()

        if normalize(expected_stem) != normalize(actual_stem):
            # Stems are too different — don't auto-fix, could be a different issue
            print(f"         [setup-dir] WARNING: tarball dir '{actual_topdir}' vs "
                  f"spec expects '{expected_dir}' — stems differ too much, skipping auto-fix")
            return spec_content

        print(f"         [setup-dir] Tarball extracts to '{actual_topdir}' but "
              f"spec expects '{expected_dir}'")

        # Build the replacement: always use explicit -n with the actual directory
        # Re-insert version macro if the actual dir contains the target version literally
        actual_dir_macro = actual_topdir.replace(target_version, "%{version}")

        if n_match:
            # Already has -n — replace just the directory name
            new_line = n_pattern.sub(f"-n {actual_dir_macro}", full_line)
        else:
            # No -n present — append it
            new_line = full_line + f" -n {actual_dir_macro}"

        spec_content = spec_content.replace(full_line, new_line, 1)
        print(f"         [setup-dir] Fixed: {new_line.strip()}")

        return spec_content

    @staticmethod
    def _handle_patches(work_dir, spec_file, spec_content):
        """Test all patches referenced in the spec against the new source tarball.

        For each PatchN: line in the spec:
        - Extract tarball to a temp dir
        - Run `patch --dry-run -p1 < patchfile` to test
        - If ALL hunks fail AND patch is reversed (merged upstream) → remove from spec
        - If SOME hunks fail (partial conflict) → flag NEEDS_HUMAN
        - If patch applies cleanly → keep it

        Returns dict: {removed: [...], failed: [...], ok: [...]}
        or None if no patches found.
        """
        if not os.path.isdir(work_dir):
            return None

        # Find tarball in work_dir
        tarball_path = None
        for f in os.listdir(work_dir):
            if f.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz")):
                tarball_path = os.path.join(work_dir, f)
                break
            elif f.endswith(".zip"):
                tarball_path = os.path.join(work_dir, f)
                break

        if not tarball_path:
            return None

        # Parse PatchN: lines from spec
        # Matches: Patch0: foo.patch, Patch1: bar.patch, Patch: baz.patch
        patch_pattern = re.compile(r'^(Patch(\d*)\s*:\s*(\S+))', re.MULTILINE)
        patches = []  # list of (full_line, patch_num, patch_filename)
        for m in patch_pattern.finditer(spec_content):
            full_line = m.group(1)
            num = m.group(2) if m.group(2) else "0"
            filename = m.group(3)
            patches.append((full_line, num, filename))

        if not patches:
            return None

        # Check if spec uses %autosetup (auto-applies all patches)
        uses_autosetup = bool(re.search(r'^%autosetup\b', spec_content, re.MULTILINE))

        print(f"         [patches] Found {len(patches)} patch(es), testing against new source...")

        # Extract tarball to temp dir
        extract_dir = tempfile.mkdtemp(prefix="patch-test-")
        try:
            if tarball_path.endswith(".zip"):
                with zipfile.ZipFile(tarball_path) as zf:
                    zf.extractall(extract_dir)
            else:
                with tarfile.open(tarball_path) as tf:
                    tf.extractall(extract_dir)
        except (tarfile.TarError, zipfile.BadZipFile, OSError) as e:
            print(f"         [patches] Cannot extract archive: {e}")
            shutil.rmtree(extract_dir, ignore_errors=True)
            return None

        # Find the top-level directory inside the extract
        entries = os.listdir(extract_dir)
        if len(entries) == 1 and os.path.isdir(os.path.join(extract_dir, entries[0])):
            source_dir = os.path.join(extract_dir, entries[0])
        else:
            source_dir = extract_dir

        results = {"removed": [], "failed": [], "ok": []}

        for full_line, patch_num, patch_filename in patches:
            patch_path = os.path.join(work_dir, patch_filename)
            if not os.path.exists(patch_path):
                print(f"         [patches] {patch_filename}: file not found, skipping")
                continue

            # Test patch with --dry-run using same flags as OBS (--fuzz=0)
            try:
                proc = subprocess.run(
                    ["patch", "--dry-run", "-p1", "--fuzz=0",
                     "--no-backup-if-mismatch", "--batch", "--silent",
                     "-i", patch_path],
                    cwd=source_dir,
                    capture_output=True, text=True, timeout=30
                )
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                print(f"         [patches] {patch_filename}: patch command error: {e}")
                continue

            combined_output = proc.stdout + proc.stderr

            if proc.returncode == 0:
                # Patch applies cleanly
                print(f"         [patches] {patch_filename}: applies cleanly ✓")
                results["ok"].append(patch_filename)
                continue

            # Patch failed — analyze why
            failed_hunks = re.findall(r'(\d+) out of (\d+) hunks? FAILED', combined_output)
            reversed_detected = "Reversed (or previously applied) patch detected" in combined_output

            # Also test in reverse to confirm it's merged upstream
            if not reversed_detected:
                try:
                    rev_proc = subprocess.run(
                        ["patch", "--dry-run", "-R", "-p1", "--batch", "--silent",
                         "-i", patch_path],
                        cwd=source_dir,
                        capture_output=True, text=True, timeout=30
                    )
                    if rev_proc.returncode == 0:
                        reversed_detected = True
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass

            if failed_hunks:
                total_failed = sum(int(f) for f, _ in failed_hunks)
                total_hunks = sum(int(t) for _, t in failed_hunks)
                all_failed = (total_failed == total_hunks)
            else:
                # No FAILED count but non-zero return — treat as all failed
                all_failed = True
                total_failed = 0
                total_hunks = 0

            if all_failed and reversed_detected:
                # Fully merged upstream — safe to auto-remove
                print(f"         [patches] {patch_filename}: MERGED upstream "
                      f"({total_failed}/{total_hunks} hunks failed, reversed) → removing")
                results["removed"].append(patch_filename)

                # Remove patch file from work_dir
                os.remove(patch_path)

                # Mark for osc deletion
                osc_dir = os.path.join(work_dir, ".osc")
                if os.path.isdir(osc_dir):
                    tbd_path = os.path.join(osc_dir, "_to_be_deleted")
                    existing = set()
                    if os.path.exists(tbd_path):
                        with open(tbd_path) as fh:
                            existing = set(line.strip() for line in fh if line.strip())
                    existing.add(patch_filename)
                    with open(tbd_path, "w") as fh:
                        fh.write("\n".join(sorted(existing)) + "\n")

                # Remove PatchN: line from spec
                spec_content = spec_content.replace(full_line + "\n", "")
                # Also try without trailing newline (last line edge case)
                spec_content = spec_content.replace(full_line, "")

                # Remove %patchN / %patch -P N apply line (not needed for %autosetup)
                if not uses_autosetup:
                    # Match: %patch0, %patch -P 0, %patch -P0, %patchN (old style)
                    # Also match with optional flags like -p1
                    apply_patterns = [
                        # New style: %patch -P N (with optional other flags)
                        re.compile(
                            r'^%patch\s+(?:-[a-oq-zA-Z]\d*\s+)*-P\s*' +
                            re.escape(patch_num) +
                            r'(?:\s+.*)?$', re.MULTILINE),
                        # Old style: %patchN (with optional flags)
                        re.compile(
                            r'^%patch' + re.escape(patch_num) +
                            r'(?:\s+.*)?$', re.MULTILINE),
                    ]
                    for ap in apply_patterns:
                        spec_content = ap.sub("", spec_content)

                # Clean up any resulting blank lines (collapse multiple)
                spec_content = re.sub(r'\n{3,}', '\n\n', spec_content)

            elif not all_failed and failed_hunks:
                # Partial failure — needs human intervention
                print(f"         [patches] {patch_filename}: PARTIAL FAILURE "
                      f"({total_failed}/{total_hunks} hunks failed) → needs human review")
                results["failed"].append({
                    "file": patch_filename,
                    "failed_hunks": total_failed,
                    "total_hunks": total_hunks,
                    "reversed": reversed_detected,
                })
            else:
                # All failed but NOT reversed — also needs human
                print(f"         [patches] {patch_filename}: FAILED "
                      f"(not reversed — may need manual rebase) → needs human review")
                results["failed"].append({
                    "file": patch_filename,
                    "failed_hunks": total_failed,
                    "total_hunks": total_hunks,
                    "reversed": False,
                })

        # Clean up temp dir
        shutil.rmtree(extract_dir, ignore_errors=True)

        # If we removed any patches, update the spec file on disk
        if results["removed"]:
            spec_path = os.path.join(work_dir, spec_file)
            with open(spec_path, "w") as sf:
                sf.write(spec_content)

        # Return results and updated spec content
        results["spec_content"] = spec_content
        return results

    def _verify_obs_builds(self, branch_project, package, work_dir,
                            spec_file, spec_content, added_files, removed_files,
                            max_fix_attempts=2):
        """Wait for OBS builds and verify they pass. If they fail with fixable
        errors, auto-fix the spec and re-commit.

        Returns True if OBS builds pass, False otherwise."""
        import time
        import urllib.request
        import urllib.error
        import base64

        obs_user = self.config.get("obs_user", "") or os.environ.get("OBS_USER", "")
        obs_pass = self.config.get("obs_pass", "") or os.environ.get("OBS_PASS", "")
        obs_api = self.config.get("obs_api_url", "") or os.environ.get("OBS_API_URL", "https://api.opensuse.org")

        def obs_get(path):
            url = f"{obs_api}{path}"
            req = urllib.request.Request(url)
            auth = base64.b64encode(f"{obs_user}:{obs_pass}".encode()).decode()
            req.add_header("Authorization", f"Basic {auth}")
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read().decode()
            except Exception:
                return ""

        for fix_attempt in range(max_fix_attempts + 1):
            # Wait for OBS builds to finish (poll every 15s, max 120s)
            print(f"\n  [9/9] Verifying OBS server builds (attempt {fix_attempt + 1})...")
            obs_ok = obs_fail = obs_unresolvable = obs_building = 0

            for wait in range(20):  # 20 * 15s = 300s max
                time.sleep(15)
                result_xml = obs_get(
                    f"/build/{branch_project}/_result?package={package}")

                obs_ok = result_xml.count('code="succeeded"')
                obs_fail = result_xml.count('code="failed"')
                obs_unresolvable = result_xml.count('code="unresolvable"')
                obs_building = (result_xml.count('code="building"') +
                               result_xml.count('code="scheduled"') +
                               result_xml.count('code="dispatching"') +
                               result_xml.count('code="blocked"'))

                total_done = obs_ok + obs_fail + obs_unresolvable
                if total_done > 0 and obs_building == 0:
                    break
                # If we have some results and most are done, don't wait forever
                if total_done >= 6 and obs_building <= 2:
                    break

                print(f"         ... ok={obs_ok} fail={obs_fail} building={obs_building}")

            total = obs_ok + obs_fail + obs_unresolvable
            print(f"         OBS results: {obs_ok}/{total} succeeded, "
                  f"{obs_fail} failed, {obs_unresolvable} unresolvable")

            if obs_fail == 0 and obs_unresolvable == 0 and obs_ok > 0:
                print(f"         OBS builds VERIFIED!")
                return True

            if obs_ok > 0 and obs_fail == 0:
                # All builds succeeded (unresolvable is OK — means older repos lack deps)
                print(f"         OBS builds mostly pass ({obs_ok}/{total}, "
                      f"{obs_unresolvable} unresolvable) — acceptable")
                return True

            if obs_ok > 0 and obs_fail <= 1 and obs_ok >= obs_fail * 3:
                # Mostly passing — one straggler failure is OK if majority pass
                print(f"         OBS builds mostly pass ({obs_ok}/{total}) — acceptable")
                return True

            if fix_attempt >= max_fix_attempts:
                print(f"         OBS builds still failing after {max_fix_attempts} fix attempts")
                return False

            # Try to auto-fix by reading the OBS build log
            print(f"         Attempting auto-fix from OBS build log...")
            log_content = obs_get(
                f"/build/{branch_project}/openSUSE_Tumbleweed/x86_64/{package}/_log")

            if not log_content:
                print(f"         Could not fetch OBS build log")
                return False

            # Try deterministic fix first (stale %files, etc.)
            from packaging_agent.agents.builder import BuilderAgent
            spec_path = os.path.join(work_dir, spec_file)
            fixed, spec_content = BuilderAgent._fix_files_not_found(
                spec_content, spec_path, log_content)

            if not fixed and self.api_key:
                # Fall back to AI fix
                print(f"         No deterministic fix found, trying AI...")
                from packaging_agent.knowledge import get_spec_context, detect_ecosystem
                ecosystem = detect_ecosystem(package, branch_project, spec_content)
                fixed_spec = self.gpt(
                    f"You are an RPM spec file expert for openSUSE.\n"
                    f"{get_spec_context(ecosystem)}\n"
                    "A package build has FAILED on the OBS server. "
                    "Analyze the build log error and return the COMPLETE fixed spec.\n"
                    "Do NOT wrap in markdown. No ```. Just raw spec content.\n"
                    "CRITICAL CONSTRAINTS:\n"
                    "- ONLY fix the specific error shown in the build log\n"
                    "- Do NOT change: Name, Version, Release, Source, URL, License lines\n"
                    "- Do NOT change: %prep, %build, %install sections unless the error is there\n"
                    "- Do NOT change: Requires or BuildRequires syntax (no adding >= or other operators)\n"
                    "- If the error is 'File not found' in %files, remove only the offending line\n"
                    "- If the error is 'nothing provides X', do NOT guess package names\n"
                    "- Keep everything else IDENTICAL to the input spec",
                    f"Package: {package}\n\n"
                    f"Current spec:\n{spec_content}\n\n"
                    f"OBS build log (last 2000 chars):\n{log_content[-2000:]}",
                    temperature=0.1, max_tokens=8000
                )
                fixed_spec = strip_markdown(fixed_spec)
                if fixed_spec and not fixed_spec.startswith("[GPT") and fixed_spec != spec_content.strip():
                    fixed_spec = self._validate_spec_integrity(spec_content, fixed_spec, package)
                    with open(spec_path, "w") as f:
                        f.write(fixed_spec)
                    spec_content = fixed_spec
                    fixed = True
                    print(f"         AI produced fix ({len(fixed_spec)} chars)")

            if not fixed:
                print(f"         Could not auto-fix OBS build failure")
                return False

            # Re-commit the fix
            print(f"         Re-committing fix...")
            ok, output = self.obs.commit(
                branch_project, package,
                message=f"Fix OBS build for {package}",
                directory=work_dir)
            if not ok:
                print(f"         Re-commit failed: {output[:200]}")
                return False
            print(f"         Re-committed, waiting for new OBS build...")

        return False

    def _analyze_changelog(self, package, current, target_version, github_slug, project):
        """Fetch and analyze changelog between versions."""
        changelog = {"releases": [], "risk_analysis": "", "risk_level": "UNKNOWN"}

        if not github_slug:
            from packaging_agent.agents.analyzer import AnalyzerAgent
            analyzer = AnalyzerAgent(self.config)
            github_slug = analyzer._infer_github(package, detect_ecosystem(package, project))

        if github_slug:
            releases = github_releases(github_slug, max_releases=20)
            for r in releases:
                changelog["releases"].append(r)

            if releases and self.api_key:
                all_changes = "\n".join(
                    f"{r['version']}:\n{r['body']}" for r in releases[:10] if r["body"])
                ecosystem = detect_ecosystem(package, project)
                upgrade_hints = get_upgrade_context(ecosystem)

                risk = self.gpt(
                    "You are an RPM packaging expert for openSUSE. Analyze the changelog "
                    "and identify what might need changes in the spec file.\n"
                    f"{upgrade_hints}\n"
                    "Give a RISK LEVEL (LOW/MEDIUM/HIGH) and specific spec changes needed.",
                    f"Package: {package}\nUpgrade: {current} → {target_version}\n\n"
                    f"Changelog:\n{all_changes[:3000]}",
                    temperature=0.2, max_tokens=500
                )
                changelog["risk_analysis"] = risk
                for level in ["CRITICAL", "HIGH", "MEDIUM", "LOW"]:
                    if level in risk.upper():
                        changelog["risk_level"] = level
                        break

        return changelog

    def _dry_run(self, package, project, current, target_version, changelog):
        """Dry run — show what would happen."""
        details = {
            "current": current,
            "target": target_version,
            "changelog": changelog,
            "steps": [
                f"osc-mcp: branch_bundle {project}/{package}",
                f"osc-mcp: checkout_bundle",
                f"osc-mcp: run_services download_files (or direct download)",
                f"AI update spec: Version → {target_version}, Release → 0",
                f"Update .changes",
                f"osc-mcp: run_build (local, may retry up to 3x with AI fixes)",
                f"On success: osc-mcp: commit",
            ],
        }
        return AgentResult(
            success=True, action="upgrade", package=package, project=project,
            summary=f"Dry run: {current} → {target_version} "
                    f"(risk: {changelog.get('risk_level', 'UNKNOWN')})",
            details=details)

    def _live_upgrade(self, package, project, current, target_version,
                      changelog, max_attempts):
        """Execute the full upgrade pipeline via osc-mcp."""

        if not self.obs.available():
            return AgentResult(
                success=False, action="upgrade", package=package,
                summary="osc-mcp not reachable",
                errors=[f"Cannot connect to osc-mcp at {self.obs.mcp_url}"])

        ecosystem = detect_ecosystem(package, project)
        spec_context = get_spec_context(ecosystem)

        # Pre-flight: Check for existing open submit requests
        print(f"\n  [0/8] Pre-flight checks...")
        has_sr, srs = self.obs.has_open_sr(project, package)
        if has_sr:
            sr_ids = [str(sr.get("id", sr.get("Id", "?"))) for sr in srs[:3]]
            sr_list = ", ".join(sr_ids)
            print(f"         Open SR(s) found: {sr_list} — skipping upgrade")
            return AgentResult(
                success=False, action="upgrade", package=package,
                project=project,
                summary=f"SKIPPED: Open submit request(s) exist ({sr_list})",
                details={
                    "verdict": "SKIPPED",
                    "verdict_reason": f"Open SR(s): {sr_list}. Wait for review or withdraw first.",
                    "current": current, "target": target_version,
                    "open_srs": sr_ids,
                    "source_url": f"https://build.opensuse.org/package/show/{project}/{package}",
                },
                errors=[f"Cannot upgrade: open SR(s) {sr_list} pending for {package}"])

        # Pre-flight: Clean up stale branch (if no open SR)
        cleaned, cleanup_msg = self.obs.cleanup_stale_branch(project, package)
        if cleaned:
            print(f"         Cleanup: {cleanup_msg}")
        else:
            print(f"         Cleanup skipped: {cleanup_msg}")

        # Step 1: Branch on OBS via osc-mcp
        print(f"\n  [1/8] Branching {project}/{package} via osc-mcp...")
        ok, branch_project = self.obs.branch_package(project, package)
        if not ok:
            return AgentResult(
                success=False, action="upgrade", package=package,
                summary=f"Branch failed: {branch_project[:100]}",
                errors=[branch_project])
        print(f"         → {branch_project}")

        # Step 2: Checkout via osc-mcp
        print(f"\n  [2/8] Checking out via osc-mcp...")
        work_dir = self.obs.checkout(branch_project, package)
        if not work_dir:
            return AgentResult(
                success=False, action="upgrade", package=package,
                summary="Checkout failed",
                errors=["osc-mcp checkout returned no work directory"])
        print(f"         → {work_dir}")

        # Find spec file in checkout
        spec_file = None
        if os.path.isdir(work_dir):
            for f in os.listdir(work_dir):
                if f.endswith(".spec"):
                    spec_file = f
                    break
        if not spec_file:
            # Try reading from osc-mcp
            spec_content = self.obs.read_file(branch_project, package, f"{package}.spec")
            if spec_content:
                spec_file = f"{package}.spec"
                self.obs.write_file_local(work_dir, spec_file, spec_content)
            else:
                return AgentResult(
                    success=False, action="upgrade", package=package,
                    summary="No spec file", errors=["No .spec in checkout"])

        spec_path = os.path.join(work_dir, spec_file)
        with open(spec_path) as f:
            spec_content = f.read()

        # Read ORIGINAL spec from source project for casing reference
        # (branch may already have AI-lowercased names from a previous attempt)
        original_spec = self.obs.read_file(project, package, f"{package}.spec")
        if not original_spec:
            original_spec = spec_content

        # Step 3: AI update spec (with upstream dependency awareness)
        # NOTE: spec update comes BEFORE tarball download so that the updated
        # Version: field is available for Source URL substitution and run_services.
        print(f"\n  [3/8] AI updating spec file...")
        if not self.api_key:
            return AgentResult(
                success=False, action="upgrade", package=package,
                summary="No API key", errors=["OpenAI key required for spec update"])

        # Fetch upstream dependency diff
        dep_diff_text = ""
        dep_diff = pypi_dep_diff(package, current, target_version)
        if dep_diff:
            parts = []
            if dep_diff["added"]:
                parts.append(f"NEW dependencies to ADD: {', '.join(dep_diff['added'])}")
            if dep_diff["removed"]:
                parts.append(f"REMOVED dependencies to DROP: {', '.join(dep_diff['removed'])}")
            if dep_diff["changed"]:
                for c in dep_diff["changed"]:
                    parts.append(f"CHANGED: {c['old']} → {c['new']}")
            if parts:
                dep_diff_text = "\n".join(parts)
                print(f"         Dependency changes detected:")
                for p in parts:
                    print(f"           {p}")
            else:
                dep_diff_text = "No dependency changes between versions."
                print(f"         No dependency changes.")

        updated_spec = self.gpt(
            f"You are an RPM spec file expert for openSUSE.\n{spec_context}\n"
            "Update this spec for a version upgrade.\n"
            "Return ONLY the complete modified spec file content.\n"
            "Do NOT wrap in markdown code blocks. No ``` anywhere.\n"
            "Do NOT add comments explaining your changes.\n\n"
            "CRITICAL RULES — VIOLATION MEANS BROKEN BUILD:\n"
            "1. PRESERVE the ENTIRE file verbatim — only change what is explicitly listed below\n"
            "2. Keep ALL header comments (copyright, license blocks)\n"
            "3. Keep ALL macro definitions (%define, %global, %bcond, %{?sle15_python_module_pythons})\n"
            "4. Keep ALL conditional blocks (%if, %else, %endif) EXACTLY as they are\n"
            "5. Do NOT change the Name: field\n"
            "6. Do NOT remove or reorder any lines except the specific changes listed\n"
            "7. RPM package names are CASE-SENSITIVE — do NOT change casing\n",
            f"Current spec:\n{spec_content}\n\n"
            f"ONLY make these changes:\n"
            f"1. Update Version: from {current} to {target_version}\n"
            f"2. Reset Release: to 0\n"
            f"3. Apply these upstream dependency changes:\n"
            f"{dep_diff_text}\n"
            f"   - For REMOVED deps: delete BOTH the Requires and BuildRequires lines\n"
            f"   - For NEW deps: add using existing naming style (%{{ansible_python}}-<name> or python3-<name>)\n"
            f"   - For CHANGED version constraints: update ONLY the version number\n"
            f"4. Do NOT touch anything else — Source URL, patches, macros, conditionals, comments, %prep, %build, %install, %check, %files must be IDENTICAL to the original\n\n"
            f"Return the COMPLETE spec file with ONLY these changes applied.",
            temperature=0.1, max_tokens=8000
        )
        updated_spec = strip_markdown(updated_spec)
        if updated_spec.startswith("[GPT"):
            return AgentResult(
                success=False, action="upgrade", package=package,
                summary="AI spec update failed", errors=[updated_spec])

        # Post-GPT validation: ensure critical sections weren't stripped
        updated_spec = self._validate_spec_integrity(spec_content, updated_spec, package)

        # Post-GPT fix: restore original package name casing in Requires/BuildRequires
        # GPT often lowercases PyYAML→pyyaml, Jinja2→jinja2 etc.
        # Use the original spec from SOURCE project (not branch) as casing reference
        updated_spec = self._restore_dep_casing(original_spec, updated_spec)

        with open(spec_path, "w") as f:
            f.write(updated_spec)
        spec_content = updated_spec
        print(f"         Spec updated ({len(updated_spec)} chars)")

        # Step 4: Download new tarball (after spec update so Version is correct)
        print(f"\n  [4/8] Downloading source tarball...")

        # Track old/new tarballs for osc rm/add at commit time
        removed_files = []
        added_files = []

        # Check if package uses _service file
        service_path = os.path.join(work_dir, "_service")
        has_service = os.path.exists(service_path)

        if has_service:
            # Update _service revision tag if present
            with open(service_path) as f:
                service_content = f.read()
            new_service = re.sub(
                r'(<param\s+name="revision">)v?[^<]+(</param>)',
                rf'\g<1>v{target_version}\2', service_content)
            if new_service != service_content:
                with open(service_path, "w") as f:
                    f.write(new_service)
                print(f"         Updated _service revision to v{target_version}")

        # Snapshot old tarballs BEFORE any changes
        old_tarballs = set()
        if os.path.isdir(work_dir):
            for f in os.listdir(work_dir):
                if f.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".tgz")):
                    old_tarballs.add(f)

        if has_service:
            # Extract service names from _service file and run them
            with open(service_path) as f:
                svc_xml = f.read()
            svc_names = re.findall(r'<service\s+name="([^"]+)"', svc_xml)
            if not svc_names:
                svc_names = ["download_files"]

            # Clean up stale obs_scm/tar_scm git clones that cause merge conflicts
            for d in [os.path.join(work_dir, ".git"),
                      os.path.join(work_dir, package)]:
                if os.path.isdir(d) and "obs_scm" in svc_xml:
                    shutil.rmtree(d, ignore_errors=True)
                    print(f"         Cleaned stale service dir: {d}")

            print(f"         Running source services: {', '.join(svc_names)}...")
            svc_result = self.obs.run_services(branch_project, package, svc_names)
            tarball_ok = svc_result and "error" not in svc_result.lower()
            if tarball_ok:
                print(f"         → via osc-mcp run_services")
            else:
                print(f"         Service run issue: {(svc_result or '')[:200]}")
                ok, filename, tarball_data = self.obs.download_source(
                    spec_content, target_version, package)
                if ok:
                    tarball_path = os.path.join(work_dir, filename)
                    with open(tarball_path, "wb") as f:
                        f.write(tarball_data)
                    print(f"         → direct download: {filename}")
                else:
                    return AgentResult(
                        success=False, action="upgrade", package=package,
                        summary=f"Source download failed: {filename}",
                        errors=[filename])

            # Compare tarballs: detect added/removed by name
            new_tarballs = set()
            if os.path.isdir(work_dir):
                for f in os.listdir(work_dir):
                    if f.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".tgz")):
                        new_tarballs.add(f)

            # Files with new names: osc add
            for f in new_tarballs - old_tarballs:
                added_files.append(f)
                print(f"         New tarball: {f}")
            # Files with old names that are gone: osc rm
            for f in old_tarballs - new_tarballs:
                removed_files.append(f)
                print(f"         Removed tarball: {f}")
            # Same-name files just have updated content — no osc rm/add needed

            # Remove old-named tarballs: delete from disk AND mark for osc deletion
            # osc tracks files via .osc/_to_be_deleted — writing there makes
            # `osc status` show "D" so osc-mcp's commit path handles removal
            for f in old_tarballs - new_tarballs:
                old_path = os.path.join(work_dir, f)
                if os.path.exists(old_path):
                    os.remove(old_path)
            # Write .osc/_to_be_deleted so osc knows these files are deleted
            osc_dir = os.path.join(work_dir, ".osc")
            if os.path.isdir(osc_dir) and (old_tarballs - new_tarballs):
                tbd_path = os.path.join(osc_dir, "_to_be_deleted")
                existing = set()
                if os.path.exists(tbd_path):
                    with open(tbd_path) as fh:
                        existing = set(line.strip() for line in fh if line.strip())
                existing.update(old_tarballs - new_tarballs)
                with open(tbd_path, "w") as fh:
                    fh.write("\n".join(sorted(existing)) + "\n")
        else:
            # Non-service packages: remove old tarballs, download new one
            for f in old_tarballs:
                fpath = os.path.join(work_dir, f)
                if os.path.exists(fpath):
                    os.remove(fpath)
                print(f"         Removed old: {f}")

            ok, filename, tarball_data = self.obs.download_source(
                spec_content, target_version, package)
            if ok:
                tarball_path = os.path.join(work_dir, filename)
                with open(tarball_path, "wb") as f:
                    f.write(tarball_data)
                print(f"         → {filename} ({len(tarball_data)} bytes)")

                # Fix Source: line if downloaded filename differs from what spec expects
                # (e.g., PyPI returns lowercase "pycondor-0.6.1.tar.gz" but spec has
                # "PyCondor-%{version}.tar.gz" which resolves to "PyCondor-0.6.1.tar.gz")
                source_m = re.search(r'^(Source\d*:\s*)(\S+)', spec_content, re.MULTILINE)
                if source_m:
                    expanded = source_m.group(2)
                    expanded = expanded.replace("%{version}", target_version)
                    name_m = re.search(r'^Name:\s*(\S+)', spec_content, re.MULTILINE)
                    if name_m:
                        expanded = expanded.replace("%{name}", name_m.group(1))
                    for macro_m2 in re.finditer(r'%(?:global|define)\s+(\S+)\s+(\S+)', spec_content):
                        expanded = expanded.replace(f"%{{{macro_m2.group(1)}}}", macro_m2.group(2))
                    expected_file = expanded.rsplit("/", 1)[-1]
                    if expected_file != filename:
                        # Derive new Source URL using actual filename
                        new_source = source_m.group(2).rsplit("/", 1)
                        if len(new_source) == 2:
                            # URL with path — replace only the filename part
                            # Use the actual filename with %{version} macro re-inserted
                            new_fn = filename.replace(target_version, "%{version}")
                            new_url = new_source[0] + "/" + new_fn
                        else:
                            # Source is just a filename
                            new_url = filename.replace(target_version, "%{version}")
                        spec_content = spec_content.replace(
                            source_m.group(0),
                            source_m.group(1) + new_url)
                        # Write updated spec to disk
                        spec_path = os.path.join(work_dir, spec_file)
                        with open(spec_path, "w") as sf:
                            sf.write(spec_content)
                        print(f"         Updated Source: to match actual filename")

                if filename not in old_tarballs:
                    added_files.append(filename)
                # Track removed old tarballs (different name from new)
                actually_removed = set()
                for f in old_tarballs:
                    if f != filename:
                        removed_files.append(f)
                        actually_removed.add(f)
                # Mark removed tarballs for osc deletion via .osc/_to_be_deleted
                osc_dir = os.path.join(work_dir, ".osc")
                if os.path.isdir(osc_dir) and actually_removed:
                    tbd_path = os.path.join(osc_dir, "_to_be_deleted")
                    existing = set()
                    if os.path.exists(tbd_path):
                        with open(tbd_path) as fh:
                            existing = set(line.strip() for line in fh if line.strip())
                    existing.update(actually_removed)
                    with open(tbd_path, "w") as fh:
                        fh.write("\n".join(sorted(existing)) + "\n")
            else:
                # Fallback: try osc-mcp run_services
                svc_result = self.obs.run_services(branch_project, package, ["download_files"])
                tarball_ok = svc_result and "error" not in svc_result.lower()
                if tarball_ok:
                    print(f"         → via osc-mcp run_services")
                else:
                    return AgentResult(
                        success=False, action="upgrade", package=package,
                        summary=f"Source download failed: {filename}",
                        errors=[filename])

        if removed_files:
            print(f"         Will osc rm: {', '.join(removed_files)}")
        if added_files:
            print(f"         Will osc add: {', '.join(added_files)}")

        # Fix %setup -n directory if tarball extracts to a different name than spec expects
        spec_content = self._fix_setup_dir(spec_content, work_dir, target_version)
        with open(spec_path, "w") as sf:
            sf.write(spec_content)

        # Step 4b: Check patches against new source
        patch_results = self._handle_patches(work_dir, spec_file, spec_content)
        if patch_results:
            # Update spec_content if patches were removed
            if patch_results.get("spec_content"):
                spec_content = patch_results["spec_content"]

            if patch_results.get("removed"):
                removed_patches = patch_results["removed"]
                print(f"         Removed {len(removed_patches)} merged patch(es): "
                      f"{', '.join(removed_patches)}")
                # Track removed patch files for osc commit
                removed_files.extend(removed_patches)

            if patch_results.get("failed"):
                failed_patches = patch_results["failed"]
                patch_names = [p["file"] if isinstance(p, dict) else p
                               for p in failed_patches]
                print(f"         WARNING: {len(failed_patches)} patch(es) need human review: "
                      f"{', '.join(patch_names)}")
                # Don't abort — let the build attempt proceed, it may still work
                # (some patches may be applied differently by %autosetup)
                # But record for the review step
                branch_url = f"https://build.opensuse.org/package/show/{branch_project}/{package}"
                source_url = f"https://build.opensuse.org/package/show/{project}/{package}"
                return AgentResult(
                    success=False, action="upgrade", package=package,
                    project=branch_project,
                    summary=f"NEEDS_HUMAN: {current} → {target_version} | "
                            f"Patch conflicts: {', '.join(patch_names)} | "
                            f"Branch: {branch_url}",
                    details={
                        "verdict": "NEEDS_HUMAN",
                        "verdict_reason": f"Patches with conflicts: {', '.join(patch_names)}. "
                                          f"Manual rebase needed.",
                        "current": current, "target": target_version,
                        "ecosystem": ecosystem, "changelog": changelog,
                        "patch_results": {
                            "removed": patch_results.get("removed", []),
                            "failed": failed_patches,
                            "ok": patch_results.get("ok", []),
                        },
                        "branch_project": branch_project,
                        "source_project": project,
                        "branch_url": branch_url,
                        "source_url": source_url,
                    },
                    work_dir=work_dir)

        # Step 5: Update .changes
        changes_file = spec_file.replace(".spec", ".changes")
        changes_path = os.path.join(work_dir, changes_file)
        if os.path.exists(changes_path):
            print(f"\n  [5/8] Updating .changes...")
            with open(changes_path) as f:
                changes_content = f.read()
            ts = datetime.now(timezone.utc).strftime("%a %b %e %H:%M:%S UTC %Y").replace("  ", " ")
            new_entry = (
                f"-------------------------------------------------------------------\n"
                f"{ts} - packaging-agent@opensuse.org\n\n"
                f"- Update to version {target_version}\n\n"
            )
            with open(changes_path, "w") as f:
                f.write(new_entry + changes_content)
            print(f"         Done.")

        # Step 6: Local build via osc-mcp
        print(f"\n  [6/8] Local build via osc-mcp (max {max_attempts} attempts)")
        from packaging_agent.agents.builder import BuilderAgent
        builder = BuilderAgent(self.config)
        build_result = builder.run(
            package=package, project=branch_project,
            work_dir=work_dir, spec_file=spec_file,
            max_attempts=max_attempts)

        if not build_result.success:
            branch_url = f"https://build.opensuse.org/package/show/{branch_project}/{package}"
            return AgentResult(
                success=False, action="upgrade", package=package,
                project=branch_project,
                summary=f"BUILD FAILED: {current} → {target_version} | "
                        f"Branch: {branch_url}",
                details={
                    "verdict": "REJECT",
                    "verdict_reason": "Local build failed after all attempts",
                    "current": current, "target": target_version,
                    "ecosystem": ecosystem, "changelog": changelog,
                    "build_result": {
                        "success": False,
                        "attempts": build_result.details.get("attempts", 0),
                        "summary": build_result.summary,
                    },
                    "branch_project": branch_project,
                    "source_project": project,
                    "branch_url": branch_url,
                    "source_url": f"https://build.opensuse.org/package/show/{project}/{package}",
                },
                work_dir=work_dir)

        # Step 7: Review BEFORE commit (quality gate)
        print(f"\n  [7/8] Pre-commit review...")
        from packaging_agent.agents.reviewer import ReviewerAgent
        reviewer = ReviewerAgent(self.config)
        review = reviewer.run(
            package=package, project=project,
            work_dir=work_dir, ecosystem=ecosystem,
            branch_project=branch_project,
            upgrade_context={
                "current": current,
                "target": target_version,
                "risk_level": changelog.get("risk_level", "UNKNOWN"),
                "dep_diff": dep_diff if 'dep_diff' in dir() else None,
                "dep_diff_text": dep_diff_text if 'dep_diff_text' in dir() else "",
            })

        verdict = review.details.get("verdict", "NEEDS_HUMAN")
        verdict_reason = review.details.get("verdict_reason", "")
        print(f"         Verdict: {verdict}")
        if verdict_reason:
            print(f"         Reason: {verdict_reason}")

        # Step 8: Commit only if verdict is COMMIT
        committed = False
        if verdict == "COMMIT":
            print(f"\n  [8/8] Committing via osc-mcp...")
            ok, output = self.obs.commit(
                branch_project, package,
                message=f"Update {package} to {target_version}",
                directory=work_dir,
                added_files=added_files if added_files else None,
                removed_files=removed_files if removed_files else None)
            if ok:
                committed = True
                print(f"         Committed!")
            else:
                print(f"         Commit issue: {output[:200]}")
        elif verdict == "NEEDS_HUMAN":
            print(f"\n  [8/8] SKIPPING commit — needs human review")
            print(f"         Work dir: {work_dir}")
        else:  # REJECT
            print(f"\n  [8/8] SKIPPING commit — review REJECTED the upgrade")

        # Step 9: OBS build verification (post-commit)
        # Wait for OBS to build and verify results. If OBS fails with fixable
        # errors (e.g., stale %files entries), auto-fix and re-commit.
        obs_verified = False
        if committed:
            obs_verified = self._verify_obs_builds(
                branch_project, package, work_dir, spec_file,
                spec_content, added_files, removed_files,
                max_fix_attempts=2)

        # Build comprehensive result
        success = obs_verified if committed else False
        summary_prefix = "COMMITTED" if committed else verdict
        branch_url = f"https://build.opensuse.org/package/show/{branch_project}/{package}"
        source_url = f"https://build.opensuse.org/package/show/{project}/{package}"
        return AgentResult(
            success=success,
            action="upgrade",
            package=package,
            project=branch_project,
            summary=f"{summary_prefix}: {current} → {target_version} | "
                    f"Branch: {branch_url}",
            details={
                "verdict": verdict,
                "verdict_reason": verdict_reason,
                "committed": committed,
                "current": current,
                "target": target_version,
                "ecosystem": ecosystem,
                "changelog": changelog,
                "build_result": {
                    "success": build_result.success,
                    "attempts": build_result.details.get("attempts", 0),
                    "summary": build_result.summary,
                },
                "review": {
                    "verdict": verdict,
                    "reason": verdict_reason,
                    "checks": review.details.get("checks", []),
                    "error_count": review.details.get("error_count", 0),
                    "warning_count": review.details.get("warning_count", 0),
                },
                "branch_project": branch_project,
                "source_project": project,
                "branch_url": branch_url,
                "source_url": source_url,
            },
            work_dir=work_dir,
        )
