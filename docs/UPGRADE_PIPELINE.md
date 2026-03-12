# Upgrade Pipeline

The upgrade pipeline is the core workflow of the packaging agent. It takes a package from its current version to a target version through 8 numbered steps (0-7), with a review gate before any OBS commit.

## Pipeline Overview

```
[0/8] Pre-flight: SR Check + Branch Cleanup
    |   - Check for open submit requests -> SKIP if SR exists
    |   - Delete stale branch project (if no open SR)
    |
[*] Version Check (current == target? -> done)
    |
[*] Changelog Analysis (GitHub + AI risk assessment)
    |
    +-- [DRY RUN?] --> Return plan + risk level (stops here)
    |
[1/8] Branch on OBS (via osc-mcp)
    |
[2/8] Checkout (via osc-mcp, files at /tmp/mcp-workdir/...)
    |
[3/8] AI Update Spec File       <-- BEFORE tarball download
    |   - Fetch upstream dep diff (PyPI)
    |   - GPT-4o rewrites spec
    |   - Post-GPT integrity validation
    |   - Dep casing restoration
    |
[4/8] Download Source Tarball    <-- AFTER spec update (version must match)
    |
[5/8] Update .changes file
    |
[6/8] Local Build (up to 3 attempts with AI fix loop)
    |   |
    |   +-- Build fails --> AI diagnoses log --> Fixes spec --> Retry
    |
[7/8] Review Gate
    |   |
    |   +-- COMMIT     --> osc commit to branch project
    |   +-- NEEDS_HUMAN --> Stop, preserve work_dir for manual review
    |   +-- REJECT      --> Stop, do not commit
    |
    v
Done (AgentResult with verdict, build info, review checks, branch URL + source URL)
```

**Key ordering detail**: The spec file is updated (step 3) _before_ the tarball is downloaded (step 4). This ensures the `Version:` field in the spec matches the target version, which is needed for Source URL macro substitution (`%{version}`) and OBS source service runs.

## Step-by-Step Detail

### [0/8] Pre-flight: SR Check + Branch Cleanup

Before starting any upgrade work:

1. **Open SR check**: Queries OBS for open submit requests (`new` or `review` state) targeting this package in the devel project. If any exist, the upgrade is **SKIPPED** -- the existing SR should be reviewed/merged or withdrawn first.

2. **Stale branch cleanup**: Checks `home:<user>:branches:<project>` for leftover packages from previous upgrade attempts. If the package exists in the branch AND there is no open SR, the branch package (or entire branch project if it is the only package) is deleted via OBS API. Local checkout files are also cleaned up.

This prevents:
- Duplicate work when an SR is already pending review
- Branch conflicts from stale previous attempts
- Accumulation of dead branch projects in the user's home

### Version Check (before live steps)

Query current OBS version via osc-mcp (`version_history` -- reads spec file). If osc-mcp is unavailable, fall back to Repology.

If current version equals target version, return immediately with "Already at target."

### Changelog Analysis (before live steps)

1. **Infer GitHub slug**: From package name using known mappings (e.g., `molecule` -> `ansible-community/molecule`) or pattern matching
2. **Fetch GitHub releases**: Up to 20 releases from the GitHub API
3. **AI risk assessment**: GPT analyzes the changelog between versions and assigns a risk level:
   - **LOW**: Minor bugfixes, no dependency changes
   - **MEDIUM**: New features, some dependency changes
   - **HIGH**: Breaking changes, major dependency shifts
   - **CRITICAL**: API changes, major version bumps, security-critical

The risk level affects the review gate verdict (HIGH/CRITICAL -> NEEDS_HUMAN).

**Dry run stops here**, returning the planned steps, risk level, and changelog.

### [1/8] Branch on OBS

Calls osc-mcp `branch_bundle` to create `home:<user>:branches:<project>/<package>`.

This creates a private copy of the package where changes can be made without affecting the source project.

### [2/8] Checkout

Calls osc-mcp `checkout_bundle` to download package files to `/tmp/mcp-workdir/<branch_project>/<package>/`.

Locates the `.spec` file in the checkout. Also reads the **original spec from the source project** (not the branch) to use as a casing reference for dependency names.

### [3/8] AI Update Spec File

This step runs BEFORE the tarball download so that the `Version:` field is correct for Source URL macro substitution.

#### 3a: Fetch Upstream Dependency Diff

For Python packages, queries PyPI metadata for both old and new versions to compute:
- **Added dependencies**: New packages to add to Requires/BuildRequires
- **Removed dependencies**: Packages to drop from Requires/BuildRequires
- **Changed dependencies**: Updated version constraints

#### 3b: AI Spec Rewrite

GPT-4o receives:
- The current spec file content
- Ecosystem-specific context (macros, conventions) from the knowledge base
- Version upgrade instructions (version, release reset, dep changes)
- Explicit instructions to preserve package name casing

The prompt specifically instructs the AI:
- Update `Version:` to target version
- Reset `Release:` to 0
- Apply upstream dependency changes (add/remove/update)
- Do NOT change Source URL patterns or _service files
- Do NOT remove or modify patches unless certain they conflict
- Do NOT change the `Name:` field or existing macros
- RPM package names are CASE-SENSITIVE (keep `python3-PyYAML`, not `python3-pyyaml`)

The response is stripped of any markdown code block wrappers (`strip_markdown()`), since GPT often wraps output in triple backticks despite being told not to.

#### 3c: Post-GPT Spec Integrity Validation

The `_validate_spec_integrity()` function checks that the AI did not:
- **Change the Name field**: Restores it if changed
- **Strip the file header**: If >50% of the lines before `Name:` (copyright blocks, SLE15 macros, `%bcond` conditionals) are missing, restores the original header
- **Remove critical macros**: Checks for `%{?sle15_python_module_pythons}`, `%bcond_without test`, `%bcond_with test`, `%define ansible_python`, `%define pythons` and restores the header if any are missing

#### 3d: Dependency Casing Restoration

The `_restore_dep_casing()` function fixes a persistent GPT problem: lowercasing package names in Requires/BuildRequires lines.

**How it works:**
1. Scans the **original spec** (from source project) for mixed-case package names in Requires/BuildRequires lines
2. Builds a `{lowercase: original}` casing map (e.g., `pyyaml` -> `PyYAML`, `jinja2` -> `Jinja2`)
3. Applies case-insensitive replacements on Requires/BuildRequires lines in the updated spec

This uses the original source project spec (not the branch) as the reference, since a branch from a previous failed attempt may already have AI-lowercased names.

### [4/8] Download Source Tarball

The pipeline handles two types of packages differently:

#### Packages WITH `_service` file (e.g., lazygit, go packages)

Many packages use OBS source services (`_service` file) with `mode="manual"` to generate tarballs. The pipeline:

1. **Updates `_service` revision**: Replaces `<param name="revision">v0.59.0</param>` with the new version tag
2. **Snapshots old tarballs**: Records all existing `.tar.gz`, `.tar.bz2`, `.tar.xz`, `.zip`, `.tgz` files
3. **Runs source services**: Extracts service names from the `_service` XML (e.g., `tar_scm`, `set_version`, `recompress`, `go_modules`) and runs them via osc-mcp `run_services`
4. **Compares tarballs**: Detects added/removed tarballs by name diff
5. **Marks old tarballs for deletion**: Writes removed filenames to `.osc/_to_be_deleted` so `osc status` reports them as `D` (deleted), enabling proper cleanup on commit

If service execution fails, falls back to direct download.

#### Packages WITHOUT `_service` file (standard packages)

1. **Removes old tarballs**: Deletes from disk and marks in `.osc/_to_be_deleted`
2. **Direct download**: Parses the `Source:` URL from the spec (which now has the correct `Version:`), substitutes `%{version}` and `%{name}` macros, and downloads from upstream (PyPI, GitHub, etc.)
3. **Fallback**: If direct download fails, tries osc-mcp `run_services` with `download_files`

If both fail, the pipeline returns with an error.

#### osc File Tracking

When tarballs change names between versions (e.g., `lazygit-0.59.0.tar.gz` -> `lazygit-0.60.0.tar.gz`), both `osc add` (for new files) and `osc rm` (for old files) must happen before commit. The pipeline:

- Tracks `added_files` and `removed_files` lists
- Writes `.osc/_to_be_deleted` for removed files (osc internal tracking mechanism)
- Passes both lists to osc-mcp's `commit` tool

**Note**: `cmd=runservice` on OBS only works for services with `mode="default"` or `mode="serveronly"`. Packages using `mode="manual"` must run services locally via `osc service localrun` (which is what osc-mcp's `run_services` does).

### [5/8] Update .changes File

Prepends a new changelog entry to the `.changes` file:

```
-------------------------------------------------------------------
<date> - packaging-agent@opensuse.org

- Update to version <target_version>
```

### [6/8] Local Build (Build-Fix Loop)

Uses the Builder agent for up to 3 build attempts:

1. **Build attempt**: Calls osc-mcp `run_build` for local `osc build`
2. **On failure**:
   - Knowledge base matches build log against known error patterns (e.g., `ModuleNotFoundError` -> missing BuildRequires)
   - AI analyzes the build log and produces a fixed spec file
   - Fixed spec is written to disk, loop restarts
3. **On final failure**: AI produces a diagnosis but no more retries

The build targets `openSUSE_Tumbleweed/x86_64` by default.

If the build fails after all attempts, the pipeline returns immediately with verdict `REJECT` and the branch URL for manual inspection.

### [7/8] Review Gate

The Reviewer agent performs a comprehensive quality check:

#### Checks Performed

| Check | Severity | Description |
|-------|----------|-------------|
| `version_missing` | error | No Version: tag in spec |
| `license_missing` | error | No License: tag in spec |
| `no_changes` | error | No .changes file found |
| `license_as_doc` | warning | Using %doc for license files instead of %license |
| `changelog_in_spec` | warning | %changelog section present (openSUSE uses .changes) |
| `defattr` | warning | %defattr is deprecated |
| `buildroot` | warning | BuildRoot: tag is deprecated |
| `source_url` | warning | Source URL does not look like a download link |
| `release_format` | warning | Release does not start with a number |
| `changes_format` | warning | .changes does not start with separator |
| `python_legacy_build` | warning | Using setup.py directly instead of macros |
| `python_test_macro` | info | Could use %pytest macro |
| `go_no_macro` | info | Could use %gobuild macro |
| `rust_no_macro` | info | Could use %cargo_build macro |
| `dep_not_removed` | warning | Upstream removed dep still in spec |
| `dep_not_added` | warning | Upstream added dep missing from spec |
| `obs_all_unresolvable` | error | All OBS builds unresolvable |
| `obs_build_failed` | error | OBS builds failed |
| `obs_some_unresolvable` | warning | Some OBS builds unresolvable |
| `obs_build_succeeded` | info | OBS builds succeeded |
| `ai_review` | info | AI-powered spec review output |

#### Verdict Logic

```
REJECT if:
  - Structural errors (version_missing, license_missing, no_changes)
  - All OBS builds unresolvable
  - OBS builds failed

NEEDS_HUMAN if:
  - Dependency consistency warnings (dep_not_removed, dep_not_added, obs_some_unresolvable)
  - HIGH or CRITICAL risk upgrade
  - Any remaining errors

COMMIT if:
  - Only warnings (no errors)
  - All checks passed
```

#### OBS Buildinfo Checking

For unresolvable builds, the reviewer queries the OBS `_buildinfo` API to extract actual missing dependency names. It then calls osc-mcp `search_packages` to find the correct RPM package name with proper casing.

This produces actionable messages like: `python311-pyyaml (did you mean: python311-PyYAML?)`

### Commit (inside step 7)

Only if the verdict is **COMMIT**, the agent calls osc-mcp `commit` with the message `Update <package> to <target_version>`.

For **NEEDS_HUMAN**: The work directory is preserved for manual inspection. The OBS branch URL is included in the result.

For **REJECT**: No commit, no branch cleanup. The result includes all check details.

## Safety Guarantees

1. **No submit requests**: The agent only commits to branch projects (`home:user:branches:...`). Submitting to the source project requires human action.
2. **Review gate before commit**: Every upgrade goes through the full review before any commit.
3. **Risk-aware**: HIGH/CRITICAL risk upgrades always get NEEDS_HUMAN, even if all checks pass.
4. **Dry run by default**: `live=false` is the default; callers must explicitly opt into live mode.
5. **Build verification**: Local build must succeed before review. No commit without a green build.
6. **Spec integrity checks**: Post-GPT validation prevents AI from stripping headers, SLE macros, or conditionals.
7. **Casing restoration**: Automated fix for GPT lowercasing RPM package names in dependency lines.
8. **SR awareness**: Will not start an upgrade if a submit request is already pending.

## Known Issues and Fixes

### GPT Wraps Spec in Markdown

**Problem**: GPT-4o often wraps the spec file in triple backticks even when explicitly told not to.

**Fix**: `strip_markdown()` in `http.py` removes code block wrappers from all GPT responses used as file content.

### GPT Lowercases Dependency Names

**Problem**: GPT changes `python3-PyYAML` to `python3-pyyaml`, `python3-Jinja2` to `python3-jinja2`. RPM package names are case-sensitive; this breaks builds.

**Fix**: `_restore_dep_casing()` in `upgrade.py` restores original casing from the source project's spec. Uses case-insensitive regex matching on Requires/BuildRequires lines.

### GPT Strips Spec Headers

**Problem**: GPT sometimes removes the file header (SLE15 macros, `%bcond` conditionals, copyright blocks) when rewriting the spec.

**Fix**: `_validate_spec_integrity()` in `upgrade.py` detects when >50% of the header is missing and restores it from the original spec. Also checks for specific critical macros.

### OBS `run_services` vs `mode="manual"`

**Problem**: `cmd=runservice` on OBS only works for services with `mode="default"` or `mode="serveronly"`. Packages using `mode="manual"` (user must run `osc service localrun` and commit results) fail silently.

**Fix**: For packages with `_service` files, the pipeline extracts service names from the XML and runs them locally via osc-mcp's `run_services` tool (which calls `osc service localrun`). For packages without `_service`, direct tarball download is the primary method with `download_files` as fallback.

### Old Tarballs Not Removed from OBS

**Problem**: When a package upgrade changes the tarball filename (e.g., `pkg-1.0.tar.gz` -> `pkg-2.0.tar.gz`), deleting the old file from disk with `os.remove()` is not enough -- `osc` still tracks it in `.osc/_files` and the commit won't remove it from OBS.

**Fix**: The pipeline writes removed filenames to `.osc/_to_be_deleted`, which is the osc internal tracking file for deleted files. This makes `osc status` report them as `D` (deleted), and the subsequent `osc commit` properly removes them from OBS.

### osc-mcp Commit `removed_files` Parameter

**Problem**: osc-mcp's external `osc` commit path (the default) ignores the `removed_files` parameter -- it only processes files that `osc status` already shows as `D` (deleted via `osc rm`).

**Workaround**: Instead of relying on the `removed_files` parameter, write to `.osc/_to_be_deleted` before calling commit. This makes `osc status` report the files as `D`, which the external `osc` path handles correctly.

### SLE 15.x Unresolvable Dependencies

**Problem**: Modern Python packages (requiring Python 3.9+) are unresolvable on SLE 15.x repos due to older Python versions.

**Status**: Structural issue, not fixable by spec changes. The reviewer marks these as warnings (not errors) unless ALL repos are unresolvable. Focus on Tumbleweed builds.

### Build Timeout

**Problem**: `osc build` can take 10+ minutes for large packages, especially first-time builds that need to download the full build environment.

**Fix**: osc-mcp `run_build` has a 1800-second (30 minute) timeout. The `OBSClient._call_tool()` default timeout is 300 seconds, extended for builds. The n8n MCP Client Tool should have a timeout of at least 600000ms (10 minutes).

### Nested Event Loops

**Problem**: When packaging-agent MCP server (async FastMCP) calls osc-mcp (also async MCP client), nested event loops fail.

**Fix**: `OBSClient._call_tool()` detects the running event loop and dispatches to a `ThreadPoolExecutor` with a fresh event loop. This avoids `RuntimeError: This event loop is already running`.

### Transient MCP Connection Errors

**Problem**: MCP connections can fail intermittently with TaskGroup errors, connection resets, or timeouts, especially during long operations.

**Fix**: `OBSClient._call_tool()` has retry logic (default 2 retries) for transient errors, with exponential backoff (2s, 4s waits). Non-transient logical errors are not retried.
