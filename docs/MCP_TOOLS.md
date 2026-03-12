# MCP Tool Reference

## packaging-agent Tools (Port 8667)

The packaging-agent exposes 7 MCP tools via FastMCP v2 on HTTP Streamable transport. All tools return JSON strings.

---

### analyze_package

Deep analysis of a single OBS package including version, build status, CVEs, upstream tracking, and AI health assessment.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `package` | string | yes | Package name (e.g., `molecule`, `python-ansible-core`) |
| `project` | string | no | OBS project (default: from config, usually `systemsmanagement:ansible`) |

**Returns:**

```json
{
  "success": true,
  "action": "analyze",
  "package": "molecule",
  "project": "systemsmanagement:ansible",
  "summary": "molecule: v25.6.0, 0 CVEs, outdated",
  "details": {
    "obs_version": "25.6.0",
    "upstream_version": "26.3.0",
    "outdated": true,
    "ecosystem": "python",
    "build_status": {"succeeded": 4, "unresolvable": 2},
    "cves": [],
    "cve_count": 0,
    "ai_summary": "Package is healthy but outdated...",
    "recent_releases": [
      {"version": "v26.3.0", "date": "2026-02-15", "body": "..."}
    ]
  },
  "errors": [],
  "needs_review": false
}
```

---

### scan_packages

Scan all packages in an OBS project for CVEs, version drift, and build failures.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `project` | string | no | OBS project to scan (default: from config) |

**Returns:**

```json
{
  "success": true,
  "action": "scan",
  "package": "",
  "project": "systemsmanagement:ansible",
  "summary": "38 packages | 5 CVEs | 12 outdated | 3 build failures",
  "details": {
    "package_count": 38,
    "total_cves": 5,
    "outdated_count": 12,
    "build_failure_count": 3,
    "packages": [
      {
        "package": "molecule",
        "obs_version": "25.6.0",
        "upstream_version": "26.3.0",
        "outdated": true,
        "cve_count": 0,
        "ecosystem": "python"
      }
    ]
  },
  "errors": [],
  "needs_review": false
}
```

---

### upgrade_package

Upgrade a package to a new version. Supports dry run (default) and live mode.

**Dry run**: Returns changelog analysis, risk assessment, and planned steps.
**Live mode**: Branches on OBS, downloads tarball, AI-updates spec, builds locally, reviews, and commits on success.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `package` | string | yes | Package name (e.g., `molecule`) |
| `target_version` | string | yes | Target version (e.g., `26.3.0`) |
| `project` | string | no | OBS project (default: from config) |
| `live` | boolean | no | Execute the upgrade (default: `false` = dry run) |
| `github_slug` | string | no | GitHub `owner/repo` for changelog (auto-detected if not provided) |

**Returns (live mode):**

```json
{
  "success": true,
  "action": "upgrade",
  "package": "molecule",
  "project": "home:user:branches:systemsmanagement:ansible",
  "summary": "COMMITTED: 25.6.0 -> 26.3.0",
  "details": {
    "verdict": "COMMIT",
    "verdict_reason": "All checks passed",
    "committed": true,
    "current": "25.6.0",
    "target": "26.3.0",
    "ecosystem": "python",
    "changelog": {
      "releases": [...],
      "risk_analysis": "RISK LEVEL: LOW...",
      "risk_level": "LOW"
    },
    "build_result": {
      "success": true,
      "attempts": 1,
      "summary": "PASSED after 1 attempt(s)"
    },
    "review": {
      "verdict": "COMMIT",
      "reason": "All checks passed",
      "checks": [...],
      "error_count": 0,
      "warning_count": 1
    },
    "branch_project": "home:user:branches:systemsmanagement:ansible",
    "obs_url": "https://build.opensuse.org/package/show/home:user:branches:systemsmanagement:ansible/molecule"
  },
  "errors": [],
  "needs_review": false
}
```

---

### build_package

Build a package locally from an osc checkout directory with AI-powered build failure diagnosis and fix loop.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `work_dir` | string | yes | Path to osc checkout directory containing .spec file |
| `package` | string | no | Package name (auto-detected from spec if not provided) |

**Returns:**

```json
{
  "success": true,
  "action": "build",
  "package": "molecule",
  "summary": "PASSED after 1 attempt(s)",
  "details": {
    "ecosystem": "python",
    "target_repo": "openSUSE_Tumbleweed",
    "target_arch": "x86_64",
    "attempts": 1,
    "build_logs": ["...last 5000 chars of build log..."]
  },
  "errors": [],
  "needs_review": true
}
```

---

### review_package

Review a package spec file for quality issues. Runs lint checks, changelog validation, ecosystem macro checks, and AI-powered review.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `work_dir` | string | no | Path to osc checkout directory (reads .spec from disk) |
| `spec_content` | string | no | Raw spec file content (alternative to `work_dir`) |
| `package` | string | no | Package name |
| `project` | string | no | OBS project |

Provide either `work_dir` or `spec_content` (not both).

**Returns:**

```json
{
  "success": true,
  "action": "review",
  "package": "molecule",
  "summary": "COMMIT: All checks passed",
  "details": {
    "verdict": "COMMIT",
    "verdict_reason": "All checks passed",
    "checks": [
      {"check": "license_as_doc", "severity": "warning", "message": "Use %license instead of %doc..."},
      {"check": "python_test_macro", "severity": "info", "message": "Consider using %pytest macro..."},
      {"check": "ai_review", "severity": "info", "message": "[OK] Spec follows openSUSE guidelines..."}
    ],
    "ecosystem": "python",
    "error_count": 0,
    "warning_count": 1
  },
  "errors": [],
  "needs_review": false
}
```

---

### security_report

Generate a security intelligence report with AI-prioritized briefing.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `project` | string | no | OBS project (default: from config) |

**Returns:**

```json
{
  "success": true,
  "action": "scan",
  "summary": "38 packages | 5 CVEs | 12 outdated | 3 build failures",
  "details": {
    "packages": [...],
    "total_cves": 5,
    "outdated_count": 12,
    "build_failure_count": 3,
    "package_count": 38,
    "security_briefing": "PRIORITY ACTIONS:\n1. Fix CVE-2025-1234 in ansible-core (CRITICAL)...\n\nRISK ASSESSMENT:\n..."
  },
  "errors": [],
  "needs_review": false
}
```

---

### ask_packaging

Ask a free-text question about openSUSE packaging. Uses AI with built-in knowledge of OBS, osc, RPM macros, spec files, and all 8 supported ecosystems.

**Parameters:**

| Name | Type | Required | Description |
|------|------|----------|-------------|
| `question` | string | yes | Packaging question (e.g., "how do I package a Python project with pyproject.toml?") |

**Returns:**

```json
{
  "success": true,
  "action": "ask",
  "summary": "For Python projects using pyproject.toml...",
  "details": {
    "question": "how do I package a Python project with pyproject.toml?",
    "answer": "For Python projects using pyproject.toml, use the modern build macros:\n\n1. Add BuildRequires for the PEP 517 backend...\n2. Use %pyproject_wheel in %build...\n3. Use %pyproject_install in %install..."
  },
  "errors": [],
  "needs_review": false
}
```

---

## osc-mcp Tools (Port 8666)

osc-mcp is a Go-based MCP server wrapping the `osc` CLI and OBS REST API. It provides 16+ low-level OBS tools. All tools below are called by the packaging-agent via its `OBSClient` MCP wrapper (`obs.py`), and are also directly accessible by the n8n AI Agent.

### Read Operations

| Tool | Parameters | Returns | Description |
|------|-----------|---------|-------------|
| `get_project_meta` | `project_name` | JSON: packages list with build status per repo/arch | Project metadata, all packages and build states in one call |
| `list_source_files` | `project_name`, `package_name`, `filename`(opt) | JSON: `{"files": [{"name", "size", "content"}]}` | List or read source files in a package |
| `get_build_log` | `project_name`, `package_name`, `repository_name`, `architecture_name` | Plain text: build log content | Fetch build log for a specific repo/arch |
| `search_bundle` | `package_name` | JSON: search results | Search for packages across OBS |
| `search_packages` | `path`, `path_repository`, `pattern` | JSON: matching packages | Search packages in a specific repository |
| `list_requests` | `project_name`, `package_name`(opt), `states`(opt) | JSON: submit requests list | List submit requests for a project/package (used for SR check in pre-flight) |

### Write Operations

| Tool | Parameters | Returns | Description |
|------|-----------|---------|-------------|
| `branch_bundle` | `project_name`, `bundle_name` | JSON: `{"target_project", "checkout_dir"}` | Branch a package to `home:user:branches:...`. Note: field names may vary (`target_project` or `project_name`, `checkout_dir` or `path`) |
| `checkout_bundle` | `project_name`, `package_name` | JSON: `{"path"}` | Checkout package files to `/tmp/mcp-workdir/...` |
| `run_build` | `project_name`, `bundle_name`, `distribution`(opt), `arch`(opt) | Plain text: build output | Run local `osc build` (can take minutes, 1800s timeout) |
| `run_services` | `project_name`, `bundle_name`, `services` | Plain text: service output | Run OBS source services locally (e.g., `download_files`, `tar_scm`, `set_version`, `recompress`, `go_modules`). For `_service` packages with `mode="manual"`, this runs `osc service localrun` |
| `commit` | `message`, `directory`, `added_files`(opt), `removed_files`(opt), `skip_changes`(opt) | JSON: `{"revision": "N"}` | Commit changes to OBS. Uses external `osc` path by default: runs `osc status` to detect changes, `osc add` for untracked files, `osc remove` for deleted files, then `osc commit`. Note: the `removed_files` param is only used in the internal commit path; for external path, write to `.osc/_to_be_deleted` instead |

### Response Format Notes

- `get_project_meta` returns build status as `{"packages": [{"name": "pkg", "status": {"repo/arch": "succeeded"}}]}`
- `list_source_files` with a `filename` parameter returns file content in the `content` field
- `branch_bundle` returns the branch project name in `target_project` (or `project_name` in some versions) and the checkout directory in `checkout_dir` (or `path`). The packaging-agent `obs.py` handles both field name variants
- `checkout_bundle` returns the local path in `path` (e.g., `/tmp/mcp-workdir/home:user:branches:.../molecule`)
- `run_build` returns plain text (build output)
- `commit` returns JSON `{"revision": "N"}` with the committed revision number

## MCP Transport

Both servers use **MCP Streamable HTTP** transport:

- **packaging-agent**: `http://packaging-agent:8667/mcp`
- **osc-mcp**: `http://osc-mcp:8666/mcp`

When connecting from n8n, use the MCP Client Tool node with:
- `typeVersion: 1.2`
- `serverTransport: "httpStreamable"`
- `authentication: "none"`
- `timeout: 600000` (10 minutes)
