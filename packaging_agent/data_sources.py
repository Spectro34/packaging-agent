"""
External data source clients: Repology, OSV, GitHub Releases.
Pure data fetching — no AI, no OBS.
"""

from packaging_agent.config import REPOLOGY_API, OSV_QUERY_API, OSV_VULN_API, GITHUB_API
from packaging_agent.http import http_get, http_get_json, http_post_json


# ─── Repology ─────────────────────────────────────────────────────────────────

def repology_check(pkg_name):
    """Check Repology for version info across distros.
    Returns {newest, opensuse, opensuse_status, versions[]}."""
    result = {"newest": "unknown", "opensuse": "unknown",
              "opensuse_status": "unknown", "versions": []}
    try:
        repos = http_get_json(REPOLOGY_API.format(name=pkg_name))
        if not isinstance(repos, list):
            return result
        seen = set()
        for r in repos:
            ver = r.get("version", "")
            status = r.get("status", "")
            repo = r.get("repo", "")
            if status == "newest" and result["newest"] == "unknown":
                result["newest"] = ver
            if "opensuse_tumbleweed" in repo and result["opensuse"] == "unknown":
                result["opensuse"] = ver
                result["opensuse_status"] = status
            key = f"{repo}:{ver}"
            if key not in seen and len(result["versions"]) < 8:
                seen.add(key)
                result["versions"].append({"repo": repo, "version": ver, "status": status})
    except Exception as e:
        result["error"] = str(e)
    return result


# ─── OSV (CVE Scanning) ──────────────────────────────────────────────────────

def _extract_cvss_severity(vuln):
    """Extract severity from CVSS scores."""
    for sev in vuln.get("severity", []):
        score_str = sev.get("score", "")
        if "CVSS:" in score_str:
            if "/S:C/" in score_str or "A:H/I:H" in score_str:
                return "CRITICAL"
            if "A:H" in score_str or "I:H" in score_str or "C:H" in score_str:
                return "HIGH"
            if "A:L" in score_str or "I:L" in score_str or "C:L" in score_str:
                return "MEDIUM"
            return "LOW"
    db_sev = vuln.get("database_specific", {}).get("severity", "")
    if db_sev:
        return db_sev.upper()
    return "UNKNOWN"


def osv_query(osv_name, ecosystem, version=None, max_results=30):
    """Query OSV for vulnerabilities. Returns list of CVE dicts."""
    cves = []
    try:
        query = {"package": {"name": osv_name, "ecosystem": ecosystem}}
        if version and version != "unknown":
            query["version"] = "v" + version if not version.startswith("v") else version
        result = http_post_json(OSV_QUERY_API, query)
        seen = set()
        for v in result.get("vulns", []):
            cve_id = next((a for a in v.get("aliases", []) if a.startswith("CVE-")), v["id"])
            if cve_id in seen:
                continue
            seen.add(cve_id)
            severity = _extract_cvss_severity(v)
            summary = (v.get("summary") or "")[:150]
            fix_commit = ""
            refs = []
            for ref in v.get("references", []):
                url = ref.get("url", "")
                refs.append(url)
                if ref.get("type") == "FIX" or "/commit/" in url:
                    if not fix_commit:
                        fix_commit = url
            fixed_ver = ""
            for aff in v.get("affected", []):
                for rng in aff.get("ranges", []):
                    for ev in rng.get("events", []):
                        if "fixed" in ev:
                            fixed_ver = ev["fixed"]
            cves.append({
                "id": cve_id, "severity": severity, "summary": summary,
                "fix_commit": fix_commit, "fixed_version": fixed_ver,
                "references": refs[:5], "published": v.get("published", ""),
            })
            if len(cves) >= max_results:
                break
    except Exception:
        pass
    return cves


def osv_get_details(cve_id):
    """Fetch full CVE details from OSV."""
    try:
        return http_get_json(OSV_VULN_API.format(id=cve_id))
    except Exception:
        return None


def verify_cve_fix(cve):
    """Verify that a CVE's fix commit exists and the patch downloads.
    Returns dict with verification status."""
    from packaging_agent.config import SSL_CTX
    import urllib.request
    result = {"verified": False, "patch_url": "", "patch_lines": 0,
              "files_changed": [], "error": "", "fix_commit": cve.get("fix_commit", "")}
    fix_url = cve.get("fix_commit", "")
    if not fix_url:
        result["error"] = "No fix commit URL in CVE data"
        return result

    patch_url = fix_url
    if "/commit/" in patch_url and not patch_url.endswith(".patch"):
        patch_url += ".patch"
    elif "/pull/" in patch_url:
        if not patch_url.endswith(".patch"):
            patch_url += ".patch"

    result["patch_url"] = patch_url
    try:
        req = urllib.request.Request(patch_url,
                                     headers={"User-Agent": "obs-maintenance-agent/2.0"})
        resp = urllib.request.urlopen(req, timeout=30, context=SSL_CTX)
        patch_content = resp.read().decode("utf-8", errors="replace")
        result["patch_lines"] = len(patch_content.split("\n"))
        import re
        files = re.findall(r'^diff --git a/(.+?) b/', patch_content, re.MULTILINE)
        result["files_changed"] = files[:20]
        if result["patch_lines"] > 5 and len(files) > 0:
            result["verified"] = True
        else:
            result["error"] = f"Patch too small ({result['patch_lines']} lines, {len(files)} files)"
    except Exception as e:
        result["error"] = f"Download failed: {e}"
    return result


# ─── PyPI Metadata ────────────────────────────────────────────────────────────

def pypi_metadata(package_name, version=None):
    """Fetch PyPI package metadata. Returns dict with deps, python_requires, etc."""
    try:
        clean = package_name
        for prefix in ("python-", "python3-"):
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
        url = f"https://pypi.org/pypi/{clean}/json"
        if version:
            url = f"https://pypi.org/pypi/{clean}/{version}/json"
        data = http_get_json(url)
        if not data or "info" not in data:
            return None
        info = data["info"]
        # Parse requires_dist into clean list
        deps = []
        for dep in (info.get("requires_dist") or []):
            # Skip extras: 'foo ; extra == "test"'
            if "extra ==" in dep:
                continue
            deps.append(dep.split(";")[0].strip())
        return {
            "name": info.get("name", ""),
            "version": info.get("version", ""),
            "python_requires": info.get("requires_python", ""),
            "dependencies": deps,
            "summary": info.get("summary", ""),
            "home_page": info.get("home_page", ""),
            "project_url": info.get("project_url", ""),
        }
    except Exception:
        return None


def pypi_dep_diff(package_name, old_version, new_version):
    """Compare PyPI dependencies between two versions.
    Returns {added: [], removed: [], changed: []}."""
    old = pypi_metadata(package_name, old_version)
    new = pypi_metadata(package_name, new_version)
    if not old or not new:
        return None

    def parse_dep(dep_str):
        """Split 'foo>=1.2' into ('foo', '>=1.2')."""
        import re
        m = re.match(r'^([a-zA-Z0-9_.-]+)\s*(.*)', dep_str)
        if m:
            return m.group(1).lower().replace("-", "_"), m.group(2).strip()
        return dep_str.lower(), ""

    old_deps = {parse_dep(d)[0]: d for d in old["dependencies"]}
    new_deps = {parse_dep(d)[0]: d for d in new["dependencies"]}

    added = [new_deps[n] for n in new_deps if n not in old_deps]
    removed = [old_deps[n] for n in old_deps if n not in new_deps]
    changed = []
    for n in old_deps:
        if n in new_deps and old_deps[n] != new_deps[n]:
            # Skip case-only changes in package name (PyYAML vs pyyaml)
            old_norm = parse_dep(old_deps[n])
            new_norm = parse_dep(new_deps[n])
            if old_norm[1] == new_norm[1]:
                continue  # Same version constraint, just name casing changed
            changed.append({"old": old_deps[n], "new": new_deps[n]})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "old_python_requires": old.get("python_requires", ""),
        "new_python_requires": new.get("python_requires", ""),
    }


# ─── GitHub Releases ──────────────────────────────────────────────────────────

def github_releases(github_slug, max_releases=10):
    """Fetch recent GitHub releases. Returns list of {version, date, body, url}."""
    if not github_slug or "/" not in github_slug:
        return []
    try:
        parts = github_slug.split("/")
        url = GITHUB_API.format(owner=parts[0], repo=parts[1])
        url = url.replace("per_page=5", f"per_page={max_releases}")
        releases = http_get_json(url)
        return [
            {
                "version": r.get("tag_name", ""),
                "date": (r.get("published_at") or "")[:10],
                "body": (r.get("body") or "")[:500],
                "url": r.get("html_url", ""),
                "prerelease": r.get("prerelease", False),
            }
            for r in releases if not r.get("draft", False)
        ]
    except Exception:
        return []
