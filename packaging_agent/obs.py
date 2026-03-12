"""
OBS client for the openSUSE Packaging Agent.
Delegates ALL OBS/osc operations to osc-mcp via MCP protocol.

osc-mcp is the single source of truth for OBS operations.
This module is a thin async-to-sync bridge that calls osc-mcp tools.

osc-mcp returns JSON from most tools:
  - list_source_files: {"files": [{"name", "size", "content"?, ...}]}
  - get_project_meta:  {"packages": [{"name", "status": {repo/arch: status}}], "num_packages", ...}
  - checkout_bundle:   {"path": "/tmp/mcp-workdir/...", "project_name", "package_name"}
  - branch_bundle:     {"path": "...", "project_name": "home:user:branches:..."}
  - get_build_log:     plain text (build log content)
  - run_build:         plain text (build output)
  - commit:            plain text (commit result)
"""

import asyncio
import json
import re

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession


class OBSClient:
    """Thin wrapper that delegates OBS operations to osc-mcp via MCP.

    All methods are synchronous (run async MCP calls under the hood)
    so the rest of the packaging-agent doesn't need to be async.
    """

    def __init__(self, config):
        self.mcp_url = config.get("mcp_url", "http://localhost:8666/mcp")
        self._loop = None

    def available(self):
        """Check if osc-mcp is reachable."""
        try:
            self._call_tool("search_bundle", {"package_name": "_ping"})
            return True
        except Exception:
            return False

    # ─── MCP Tool Call Infrastructure ──────────────────────────────────────────

    def _call_tool(self, tool_name, arguments, timeout=300, retries=2):
        """Call an osc-mcp tool synchronously. Works from both sync and async contexts.
        Retries on transient MCP connection errors (TaskGroup, connection reset)."""
        last_error = None
        for attempt in range(retries + 1):
            try:
                try:
                    asyncio.get_running_loop()
                    # We're inside an async context (e.g., FastMCP server) —
                    # run in a separate thread to avoid event loop conflicts
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(self._call_tool_sync, tool_name, arguments, timeout)
                        return future.result(timeout=timeout)
                except RuntimeError:
                    # No running loop — safe to use asyncio.run
                    return asyncio.run(self._async_call_tool(tool_name, arguments, timeout))
            except Exception as e:
                last_error = e
                err_str = str(e)
                # Retry on transient MCP/network errors, not on logical errors
                is_transient = any(marker in err_str.lower() for marker in [
                    "taskgroup", "connection", "timeout", "eof", "reset",
                    "broken pipe", "stream", "cancelled",
                ])
                if is_transient and attempt < retries:
                    import time
                    wait = (attempt + 1) * 2
                    print(f"         [MCP retry {attempt+1}/{retries}] {err_str[:80]}... waiting {wait}s")
                    time.sleep(wait)
                    continue
                raise
        raise last_error

    def _call_tool_sync(self, tool_name, arguments, timeout=300):
        """Run async MCP call in a fresh event loop (for thread pool use)."""
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._async_call_tool(tool_name, arguments, timeout))
        finally:
            loop.close()

    def _call_tool_json(self, tool_name, arguments, timeout=300):
        """Call an osc-mcp tool and parse the JSON response."""
        raw = self._call_tool(tool_name, arguments, timeout)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    async def _async_call_tool(self, tool_name, arguments, timeout=300):
        """Call an osc-mcp tool via MCP Streamable HTTP.

        Uses extended timeouts to handle long-running operations like
        builds (5-30 minutes). The `timeout` param controls both the
        general HTTP timeout and the SSE read timeout.
        """
        async with streamablehttp_client(
            self.mcp_url,
            timeout=timeout,
            sse_read_timeout=timeout,
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
                texts = []
                for content in result.content:
                    if hasattr(content, "text"):
                        texts.append(content.text)
                return "\n".join(texts)

    # ─── Read Operations ──────────────────────────────────────────────────────

    def list_packages(self, project):
        """List all packages in an OBS project. Returns list of package names."""
        try:
            data = self._call_tool_json("get_project_meta", {
                "project_name": project,
            })
            if isinstance(data, dict) and "packages" in data:
                return [p["name"] for p in data["packages"] if "name" in p]
            return []
        except Exception:
            return []

    def get_project_meta(self, project):
        """Get full project metadata including build status per package.
        Returns dict with packages, maintainers, description, etc."""
        try:
            return self._call_tool_json("get_project_meta", {
                "project_name": project,
            })
        except Exception:
            return {}

    def read_file(self, project, package, filename):
        """Read a source file from an OBS package. Returns content string or None."""
        try:
            data = self._call_tool_json("list_source_files", {
                "project_name": project,
                "package_name": package,
                "filename": filename,
            })
            if isinstance(data, dict) and "files" in data:
                for f in data["files"]:
                    if f.get("name") == filename and "content" in f:
                        return f["content"]
            return None
        except Exception:
            return None

    def list_source_files(self, project, package):
        """List all source files for a package. Returns list of file dicts."""
        try:
            data = self._call_tool_json("list_source_files", {
                "project_name": project,
                "package_name": package,
            })
            if isinstance(data, dict) and "files" in data:
                return data["files"]
            return []
        except Exception:
            return []

    def spec_file(self, project, package):
        """Fetch and parse spec file. Returns dict with content, version, build_requires, patches."""
        spec = self.read_file(project, package, f"{package}.spec")
        if not spec:
            return None
        result = {"content": spec, "version": "", "build_requires": [], "patches": []}
        for line in spec.split("\n"):
            if line.startswith("Version:"):
                result["version"] = line.split(":", 1)[1].strip()
            elif line.startswith("BuildRequires:"):
                result["build_requires"].append(line.split(":", 1)[1].strip())
            elif re.match(r"Patch\d*:", line):
                result["patches"].append(line.strip())
        return result

    def version_history(self, project, package):
        """Get package version from spec file via osc-mcp."""
        try:
            spec_info = self.spec_file(project, package)
            if spec_info and spec_info["version"]:
                return {"version": spec_info["version"], "user": "", "time": ""}
        except Exception:
            pass
        return {"version": "unknown", "user": "", "time": ""}

    def build_log(self, project, package, repository, arch="x86_64", tail=80):
        """Fetch build log via osc-mcp. Returns log text or None."""
        try:
            result = self._call_tool("get_build_log", {
                "project_name": project,
                "package_name": package,
                "repository_name": repository,
                "architecture_name": arch,
            })
            if not result:
                return None
            lines = result.split("\n")
            return "\n".join(lines[-tail:])
        except Exception:
            return None

    def build_results(self, project, package):
        """Check build status across all repos/arches using project meta.
        Returns {results: [{repository, arch, status}], summary: {status: count}}."""
        try:
            data = self._call_tool_json("get_project_meta", {
                "project_name": project,
            })
            if not isinstance(data, dict) or "packages" not in data:
                return None
            # Find this package in project meta
            for pkg in data["packages"]:
                if pkg.get("name") == package:
                    results = []
                    for repo_arch, status in pkg.get("status", {}).items():
                        parts = repo_arch.rsplit("/", 1)
                        repo = parts[0] if len(parts) == 2 else repo_arch
                        arch = parts[1] if len(parts) == 2 else "x86_64"
                        results.append({"repository": repo, "arch": arch, "status": status})
                    counts = {}
                    for r in results:
                        counts[r["status"]] = counts.get(r["status"], 0) + 1
                    return {"results": results, "summary": counts}
            return None
        except Exception:
            return None

    def get_failed_build_log(self, project, package, build_info=None, tail=80):
        """Find first failed build and return its log."""
        if build_info is None:
            build_info = self.build_results(project, package)
        if not build_info:
            return None
        for r in build_info["results"]:
            if r["status"] in ("failed", "unresolvable"):
                log = self.build_log(project, package, r["repository"], r["arch"], tail)
                if log:
                    return {
                        "repository": r["repository"],
                        "arch": r["arch"],
                        "status": r["status"],
                        "log": log,
                    }
        return None

    def discover_packages(self, project):
        """Auto-discover packages in a project with metadata."""
        from packaging_agent.knowledge import detect_ecosystem, get_osv_ecosystem, strip_ecosystem_prefix

        pkg_names = self.list_packages(project)
        if not pkg_names:
            return []

        packages = []
        for name in pkg_names:
            ecosystem = detect_ecosystem(name, project)
            osv_eco = get_osv_ecosystem(ecosystem)
            osv_name = strip_ecosystem_prefix(name, ecosystem)
            github = ""
            if "ansible" in name.lower():
                github = f"ansible/{name}"
            elif name.startswith("python-"):
                github = f"pypa/{name[7:]}"
            packages.append({
                "name": name, "project": project,
                "ecosystem": ecosystem, "osv_ecosystem": osv_eco,
                "osv": osv_name, "github": github,
            })
        return packages

    # ─── Write Operations (all via osc-mcp) ──────────────────────────────────

    def branch_package(self, project, package):
        """Branch a package via osc-mcp. Returns (success, branch_project_name)."""
        import os
        obs_user = os.environ.get("OBS_USER", "")

        try:
            data = self._call_tool_json("branch_bundle", {
                "project_name": project,
                "bundle_name": package,
            })
            branch_project = ""
            if isinstance(data, dict):
                branch_project = (data.get("project_name", "")
                                  or data.get("target_project", ""))
                if not branch_project:
                    # Try to extract from path/checkout_dir
                    path = data.get("path", "") or data.get("checkout_dir", "")
                    m = re.search(r'(home:[^/]+:branches:[^/]+)', path)
                    if not m:
                        m = re.search(r'(home:[^/]+)', path)
                    branch_project = m.group(1) if m else ""
            else:
                # Fallback: parse text
                text = str(data)
                m = re.search(r'(home:\S+:branches:\S+)', text)
                if m:
                    branch_project = m.group(1)

            # Ultimate fallback: construct expected branch project name
            if not branch_project and obs_user:
                branch_project = f"home:{obs_user}:branches:{project}"
                print(f"         [branch] Using constructed name: {branch_project}")

            return True, branch_project
        except Exception as e:
            return False, str(e)

    def checkout(self, project, package):
        """Checkout a package locally via osc-mcp. Returns work_dir path or None."""
        default_path = f"/tmp/mcp-workdir/{project}/{package}"
        try:
            data = self._call_tool_json("checkout_bundle", {
                "project_name": project,
                "package_name": package,
            })
            if isinstance(data, dict) and "path" in data:
                return data["path"]
            # Already checked out — the error text contains "already initialized"
            text = str(data)
            if "already" in text.lower():
                import os
                if os.path.isdir(default_path):
                    return default_path
            return default_path
        except Exception:
            import os
            if os.path.isdir(default_path):
                return default_path
            return None

    def build_local(self, project, package, distribution=None, arch=None):
        """Run a local build via osc-mcp. Returns (success, log_output)."""
        args = {
            "project_name": project,
            "bundle_name": package,
        }
        if distribution:
            args["distribution"] = distribution
        if arch:
            args["arch"] = arch
        try:
            result = self._call_tool("run_build", args, timeout=1800)
            # osc-mcp run_build returns build log text
            success = "error" not in result.lower() or "succeeded" in result.lower()
            return success, result
        except Exception as e:
            return False, str(e)

    def run_services(self, project, package, services=None):
        """Run OBS source services via osc-mcp."""
        if services is None:
            services = ["download_files"]
        try:
            return self._call_tool("run_services", {
                "project_name": project,
                "bundle_name": package,
                "services": services,
            })
        except Exception as e:
            return f"Error: {e}"

    def commit(self, project, package, message, directory=None,
               added_files=None, removed_files=None):
        """Commit via osc-mcp. Returns (success, output).
        added_files: list of filenames to `osc add` before committing
        removed_files: list of filenames to `osc rm` before committing
        """
        args = {
            "message": message,
            "directory": directory or f"/tmp/mcp-workdir/{project}/{package}",
        }
        if added_files:
            args["added_files"] = added_files
        if removed_files:
            args["removed_files"] = removed_files
        try:
            result = self._call_tool("commit", args)
            success = "error" not in result.lower()
            return success, result
        except Exception as e:
            return False, str(e)

    # ─── Submit Request Operations ──────────────────────────────────────────

    def list_open_requests(self, project, package=None, user=None):
        """List open submit requests targeting a project/package.
        Returns list of request dicts with id, state, source, target, description."""
        args = {"states": "new,review", "types": "submit"}
        if project:
            args["project"] = project
        if package:
            args["package"] = package
        if user:
            args["user"] = user
        try:
            data = self._call_tool_json("list_requests", args)
            if isinstance(data, dict):
                return data.get("Requests", []) or []
            return []
        except Exception:
            return []

    def has_open_sr(self, project, package):
        """Check if there's an open submit request for this package.
        Checks both SRs targeting the project and SRs from the branch project."""
        # Check SRs targeting the devel project for this package
        srs = self.list_open_requests(project, package)
        if srs:
            return True, srs
        return False, []

    # ─── Branch Cleanup ────────────────────────────────────────────────────

    def delete_branch_project(self, branch_project):
        """Delete a branch project via OBS API. Returns (success, message)."""
        import os
        import urllib.request
        import urllib.error
        import base64

        obs_user = os.environ.get("OBS_USER", "")
        obs_pass = os.environ.get("OBS_PASS", "")
        obs_api = os.environ.get("OBS_API_URL", "https://api.opensuse.org")
        if not obs_user or not obs_pass:
            return False, "No OBS credentials for branch cleanup"

        url = f"{obs_api}/source/{branch_project}?force=1"
        auth = base64.b64encode(f"{obs_user}:{obs_pass}".encode()).decode()
        req = urllib.request.Request(url, method="DELETE",
                                      headers={"Authorization": f"Basic {auth}"})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return True, f"Deleted {branch_project}"
        except urllib.error.HTTPError as e:
            body = e.read().decode() if hasattr(e, "read") else ""
            if e.code == 404:
                return True, f"{branch_project} does not exist (already clean)"
            return False, f"Delete failed ({e.code}): {body[:200]}"
        except Exception as e:
            return False, f"Delete failed: {e}"

    def delete_branch_package(self, branch_project, package):
        """Delete a single package from a branch project via OBS API."""
        import os
        import urllib.request
        import urllib.error
        import base64

        obs_user = os.environ.get("OBS_USER", "")
        obs_pass = os.environ.get("OBS_PASS", "")
        obs_api = os.environ.get("OBS_API_URL", "https://api.opensuse.org")
        if not obs_user or not obs_pass:
            return False, "No OBS credentials for branch cleanup"

        url = f"{obs_api}/source/{branch_project}/{package}?force=1"
        auth = base64.b64encode(f"{obs_user}:{obs_pass}".encode()).decode()
        req = urllib.request.Request(url, method="DELETE",
                                      headers={"Authorization": f"Basic {auth}"})
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return True, f"Deleted {package} from {branch_project}"
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True, f"{package} not in {branch_project} (already clean)"
            body = e.read().decode() if hasattr(e, "read") else ""
            return False, f"Delete failed ({e.code}): {body[:200]}"
        except Exception as e:
            return False, f"Delete failed: {e}"

    def cleanup_stale_branch(self, project, package, obs_user=None):
        """Clean up a stale branch for a package if no open SR exists.
        Returns (cleaned, message)."""
        if not obs_user:
            import os
            obs_user = os.environ.get("OBS_USER", "")

        branch_project = f"home:{obs_user}:branches:{project}"

        # Check if branch exists
        meta = self.get_project_meta(branch_project)
        if not meta or not isinstance(meta, dict) or not meta.get("packages"):
            return True, f"No branch exists for {project}"

        # Check if this package is in the branch
        pkg_names = [p["name"] for p in meta.get("packages", []) if "name" in p]
        if package not in pkg_names:
            return True, f"{package} not in branch {branch_project}"

        # Check for open SRs — do NOT delete if SR is pending
        has_sr, srs = self.has_open_sr(project, package)
        if has_sr:
            sr_ids = [str(sr.get("id", sr.get("Id", "?"))) for sr in srs[:3]]
            return False, f"Open SR(s) exist for {package}: {', '.join(sr_ids)} — skipping cleanup"

        # Safe to delete the stale branch package
        if len(pkg_names) == 1:
            # Only package in the branch project — delete the whole project
            ok, msg = self.delete_branch_project(branch_project)
        else:
            # Multiple packages — just delete this one
            ok, msg = self.delete_branch_package(branch_project, package)

        # Also clean up local checkout if it exists
        if ok:
            import os
            import shutil
            local_dir = f"/tmp/mcp-workdir/{branch_project}/{package}"
            if os.path.isdir(local_dir):
                shutil.rmtree(local_dir, ignore_errors=True)
            # Also clean parent if empty
            parent = f"/tmp/mcp-workdir/{branch_project}"
            if os.path.isdir(parent) and not os.listdir(parent):
                shutil.rmtree(parent, ignore_errors=True)

        return ok, msg

    def write_file_local(self, work_dir, filename, content):
        """Write a file to the local checkout directory.
        This is a local op — push to OBS via commit()."""
        import os
        path = os.path.join(work_dir, filename)
        with open(path, "w") as f:
            f.write(content)
        return True

    def download_source(self, spec_content, target_version, pkg_name):
        """Download source tarball from spec's Source URL.
        Returns (success, filename, tarball_bytes) or (False, error_msg, None).

        This downloads from upstream (PyPI, GitHub, etc.), not from OBS.
        osc-mcp's run_services handles _service-based downloads.

        Falls back to the PyPI JSON API when the spec URL fails (handles
        filename renames like ruamel.yaml → ruamel_yaml).
        """
        import urllib.request
        from packaging_agent.config import SSL_CTX

        source_match = re.search(r'^Source\d*:\s*(\S+)', spec_content, re.MULTILINE)
        if not source_match:
            return False, "No Source: line found in spec", None

        source_url = source_match.group(1)
        name_match = re.search(r'^Name:\s*(\S+)', spec_content, re.MULTILINE)
        rpm_name = name_match.group(1) if name_match else pkg_name

        source_url = source_url.replace("%{version}", target_version)
        source_url = source_url.replace("%{name}", rpm_name)
        for macro_m in re.finditer(r'%(?:global|define)\s+(\S+)\s+(\S+)', spec_content):
            source_url = source_url.replace(f"%{{{macro_m.group(1)}}}", macro_m.group(2))

        filename = source_url.rsplit("/", 1)[-1]

        # Skip direct download if Source is just a filename (no URL scheme)
        if source_url.startswith(("http://", "https://", "ftp://")):
            try:
                req = urllib.request.Request(source_url,
                                             headers={"User-Agent": "obs-maintenance-agent/2.0"})
                resp = urllib.request.urlopen(req, timeout=120, context=SSL_CTX)
                tarball_data = resp.read()
                return True, filename, tarball_data
            except Exception as e:
                print(f"         Direct download failed: {e}")
                # Fall through to PyPI fallback
        else:
            print(f"         Source is not a URL ({source_url[:60]}), trying PyPI fallback...")

        # PyPI fallback: query the JSON API for the actual download URL
        pypi_result = self._pypi_download(pkg_name, target_version, SSL_CTX)
        if pypi_result:
            return pypi_result

        return False, f"Download failed for {filename} (spec URL and PyPI fallback both failed)", None

    @staticmethod
    def _pypi_download(pkg_name, version, ssl_ctx):
        """Try downloading the sdist tarball from PyPI JSON API.
        Returns (True, filename, bytes) or None on failure."""
        import urllib.request

        # Strip common RPM prefixes to get the PyPI name
        pypi_name = pkg_name
        for prefix in ("python-", "python3-"):
            if pypi_name.startswith(prefix):
                pypi_name = pypi_name[len(prefix):]
                break

        try:
            url = f"https://pypi.org/pypi/{pypi_name}/{version}/json"
            req = urllib.request.Request(url,
                                         headers={"User-Agent": "obs-maintenance-agent/2.0"})
            resp = urllib.request.urlopen(req, timeout=30, context=ssl_ctx)
            import json
            data = json.loads(resp.read())

            # Find the sdist (source distribution)
            for u in data.get("urls", []):
                if u.get("packagetype") == "sdist":
                    dl_url = u["url"]
                    dl_name = u["filename"]
                    print(f"         PyPI fallback: downloading {dl_name}")
                    req2 = urllib.request.Request(dl_url,
                                                   headers={"User-Agent": "obs-maintenance-agent/2.0"})
                    resp2 = urllib.request.urlopen(req2, timeout=120, context=ssl_ctx)
                    return True, dl_name, resp2.read()

            print(f"         PyPI: no sdist found for {pypi_name} {version}")
        except Exception as e:
            print(f"         PyPI fallback failed: {e}")
        return None
