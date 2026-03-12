"""
openSUSE Packaging Knowledge Base

Ecosystem-specific patterns, macros, build systems, and common error fixes.
Used by agents to generate context-aware AI prompts and diagnose build failures.
"""

import re

# ─── Ecosystem Definitions ───────────────────────────────────────────────────

ECOSYSTEMS = {
    "python": {
        "detect_prefixes": ["python-", "python3-"],
        "project_hints": ["python", "ansible", "saltstack", "openstack"],
        "spec_markers": ["%python_module", "%pyproject_wheel", "%py3_build", "%pytest"],
        "osv_ecosystem": "PyPI",
        "osv_strip_prefix": "python-",
        "source_registry": "https://files.pythonhosted.org/packages/source/{initial}/{name}/{name}-%{{version}}.tar.gz",
        "macros": {
            "build_modern": "%pyproject_wheel / %pyproject_install",
            "build_legacy": "%py3_build / %py3_install",
            "test": "%pytest",
            "module": "%{python_module <name>}",
            "files": "%python_files",
            "sitelib": "%python_sitelib",
            "sitearch": "%python_sitearch",
        },
        "required_build_deps": [
            "python-rpm-macros",
            "%{python_module pip}",
            "%{python_module wheel}",
        ],
        "common_build_deps": [
            "%{python_module setuptools}",
            "%{python_module flit-core}",
            "%{python_module hatchling}",
            "%{python_module poetry-core}",
        ],
        "upgrade_hints": [
            "Check if setup.py was replaced by pyproject.toml → switch to %pyproject macros",
            "Check pyproject.toml [dependencies] for new/removed deps → update Requires",
            "Check [build-system] requires → may need different build backend (hatchling, flit, etc.)",
            "Python version constraints may have changed (e.g., >=3.9 instead of >=3.7)",
            "If click-help-colors or similar optional deps removed, update Requires",
        ],
        "build_error_patterns": {
            r"ModuleNotFoundError: No module named '(\w+)'":
                "Missing BuildRequires: %{{python_module {0}}}",
            r"No module named pip":
                "Add BuildRequires: %{python_module pip}",
            r"pyproject\.toml.*backend":
                "Switch from %py3_build to %pyproject_wheel / %pyproject_install. "
                "Add BuildRequires for the PEP 517 backend (hatchling, flit-core, poetry-core, etc.)",
            r"error: invalid command 'bdist_wheel'":
                "Add BuildRequires: %{python_module wheel} %{python_module setuptools}",
            r"Failed.*%check|FAILED tests/":
                "Test failures — check if new test deps needed or skip flaky tests with "
                "%pytest -k 'not test_name'",
        },
        "spec_template_hints": """
Python package spec patterns for openSUSE:
- Name: python-{modname} (source package produces python3-{modname})
- Use %{python_module ...} for BuildRequires
- Modern build: %pyproject_wheel in %build, %pyproject_install in %install
- Legacy build: %py3_build in %build, %py3_install in %install
- Tests: %pytest (or %pytest_arch for arch-dependent)
- Files: %files %{python_files}
- Use %python_subpackages in preamble for singlespec
- Source from PyPI: https://files.pythonhosted.org/packages/source/{initial}/{name}/{name}-%{version}.tar.gz
""",
    },

    "go": {
        "detect_prefixes": ["golang-", "go-"],
        "project_hints": ["go", "container", "virtualization"],
        "spec_markers": ["%gobuild", "%go_build", "%goprep", "golang-packaging"],
        "osv_ecosystem": "Go",
        "osv_strip_prefix": "golang-",
        "macros": {
            "build": "%gobuild or %go_build",
            "test": "%gotest",
            "prep": "%goprep",
            "install": "%goinstall",
        },
        "required_build_deps": [
            "golang-packaging",
            "golang(API) >= 1.21",
        ],
        "upgrade_hints": [
            "Go module path may have changed (v2 → v3 major version bump in go.mod)",
            "Vendor tarball must be regenerated: go mod vendor && tar czf vendor.tar.gz vendor/",
            "Check go.mod for minimum Go version requirement changes",
            "New CGO dependencies may require system -devel packages",
            "If using obs_scm service, update revision/tag parameter",
        ],
        "build_error_patterns": {
            r"cannot find module providing package (.+)":
                "Vendor tarball needs regeneration or missing BuildRequires for {0}",
            r"go: updates to go\.sum needed":
                "Run 'go mod tidy' and regenerate vendor tarball",
            r"cannot find package":
                "Missing dependency in vendor tarball — regenerate with 'go mod vendor'",
            r"cgo:.*not found":
                "Missing C library for CGO — add BuildRequires: <lib>-devel",
        },
        "spec_template_hints": """
Go package spec patterns for openSUSE:
- Applications: Name is the binary name, BuildRequires: golang-packaging
- Libraries: Name: golang-<org>-<name>
- Vendor deps: separate vendor.tar.gz alongside source tarball
- Build: %goprep then %gobuild
- Install: %goinstall
- Test: %gotest
- For applications with vendor: Source0: ..., Source1: vendor.tar.gz
""",
    },

    "rust": {
        "detect_prefixes": ["rust-"],
        "project_hints": ["rust"],
        "spec_markers": ["%cargo_build", "%cargo_install", "%cargo_prep"],
        "osv_ecosystem": "crates.io",
        "osv_strip_prefix": "rust-",
        "macros": {
            "build": "%cargo_build",
            "test": "%cargo_test",
            "install": "%cargo_install",
            "prep": "%cargo_prep",
        },
        "required_build_deps": [
            "cargo-packaging",
            "rust",
        ],
        "upgrade_hints": [
            "Vendor tarball must be regenerated with cargo_vendor service",
            "Check Cargo.toml for new dependency requirements",
            "Feature flags may have changed — check default features",
            "Minimum Rust version (MSRV) may have increased",
        ],
        "build_error_patterns": {
            r"error\[E\d+\]":
                "Rust compilation error — check if minimum Rust version increased",
            r"failed to select a version for":
                "Dependency conflict in vendor — regenerate vendor tarball",
            r"can't find crate":
                "Missing crate in vendor tarball — regenerate with cargo_vendor",
        },
        "spec_template_hints": """
Rust package spec patterns for openSUSE:
- Name: <binary-name> for applications, rust-<crate> for libraries
- BuildRequires: cargo-packaging, rust
- Vendor deps: use obs-service-cargo_vendor or pre-generated vendor.tar.xz
- Build: %cargo_prep, %cargo_build
- Install: %cargo_install
- Test: %cargo_test
""",
    },

    "c_autotools": {
        "detect_prefixes": [],
        "project_hints": [],
        "spec_markers": ["%configure", "%make_build", "%autosetup"],
        "osv_ecosystem": "OSS-Fuzz",
        "macros": {
            "configure": "%configure",
            "build": "%make_build",
            "install": "%make_install",
            "setup": "%autosetup -p1",
        },
        "required_build_deps": [
            "autoconf", "automake", "libtool",
        ],
        "upgrade_hints": [
            "Check configure.ac for new --enable/--disable options",
            "Library soname may have bumped — update %files for new .so version",
            "New optional dependencies may produce new subpackages",
            "Check if build system switched to CMake or Meson",
        ],
        "build_error_patterns": {
            r"configure: error: (.+) not found":
                "Missing BuildRequires: {0}-devel (or pkgconfig({0}))",
            r"undefined reference to `(.+)'":
                "Missing library link — add BuildRequires for the library providing {0}",
            r"Installed \(but unpackaged\) file\(s\) found":
                "New files installed — add them to %files or %exclude them",
            r"File not found: (.+)":
                "File path changed upstream — update %files section for {0}",
        },
        "spec_template_hints": """
C/C++ autotools spec patterns for openSUSE:
- %autosetup -p1 in %prep (applies patches automatically)
- %configure in %build (runs ./configure with standard paths)
- %make_build (parallel make)
- %make_install in %install
- Library packages: lib<name><soversion> + <name>-devel
- Use pkgconfig() style BuildRequires when possible
""",
    },

    "c_cmake": {
        "detect_prefixes": [],
        "project_hints": [],
        "spec_markers": ["%cmake", "%cmake_build", "%cmake_install"],
        "osv_ecosystem": "OSS-Fuzz",
        "macros": {
            "configure": "%cmake",
            "build": "%cmake_build",
            "install": "%cmake_install",
        },
        "required_build_deps": [
            "cmake", "cmake-rpm-macros",
        ],
        "upgrade_hints": [
            "Check CMakeLists.txt for new find_package() calls → new BuildRequires",
            "CMake options may have changed — check -D flags in spec",
            "Library soname bumps in CMake use set_target_properties(VERSION ...)",
        ],
        "build_error_patterns": {
            r"Could NOT find (.+)":
                "Missing BuildRequires: cmake({0}) or {0}-devel",
            r"CMake Error":
                "CMake configuration error — check CMakeLists.txt requirements",
        },
        "spec_template_hints": """
C/C++ CMake spec patterns for openSUSE:
- BuildRequires: cmake, cmake-rpm-macros
- %cmake (configures with proper paths)
- %cmake_build (builds)
- %cmake_install (installs to buildroot)
- Use cmake(<package>) style BuildRequires when possible
""",
    },

    "c_meson": {
        "detect_prefixes": [],
        "project_hints": [],
        "spec_markers": ["%meson", "%meson_build", "%meson_install"],
        "osv_ecosystem": "OSS-Fuzz",
        "macros": {
            "configure": "%meson",
            "build": "%meson_build",
            "install": "%meson_install",
            "test": "%meson_test",
        },
        "required_build_deps": [
            "meson", "meson-rpm-macros",
        ],
        "upgrade_hints": [
            "Check meson.build for new dependency() calls",
            "Meson options may have changed — check meson_options.txt",
        ],
        "build_error_patterns": {
            r"Dependency (.+) found: NO":
                "Missing BuildRequires: pkgconfig({0}) or {0}-devel",
            r"meson\.build:\d+:\d+: ERROR":
                "Meson configuration error",
        },
        "spec_template_hints": """
C/C++ Meson spec patterns for openSUSE:
- BuildRequires: meson, meson-rpm-macros
- %meson (configures), %meson_build, %meson_install
- %meson_test for running tests
- Use pkgconfig() style BuildRequires
""",
    },

    "ruby": {
        "detect_prefixes": ["rubygem-"],
        "project_hints": ["ruby"],
        "spec_markers": ["%gem_install", "%gem_packages"],
        "osv_ecosystem": "RubyGems",
        "osv_strip_prefix": "rubygem-",
        "macros": {
            "install": "%gem_install",
            "packages": "%gem_packages",
            "cleanup": "%gem_cleanup",
        },
        "required_build_deps": [
            "ruby-macros",
        ],
        "upgrade_hints": [
            "Check gemspec for new runtime dependencies",
            "Native extensions may need updated -devel packages",
        ],
        "build_error_patterns": {
            r"Gem::.*could not find (.+)":
                "Missing Requires: rubygem({0})",
            r"mkmf\.rb can't find header files":
                "Missing BuildRequires for native extension headers",
        },
        "spec_template_hints": """
Ruby gem spec patterns for openSUSE:
- Name: rubygem-<gemname>
- %gem_install in %build
- %gem_packages in %files
""",
    },

    "perl": {
        "detect_prefixes": ["perl-"],
        "project_hints": ["perl"],
        "spec_markers": ["%perl_process_packlist", "perl("],
        "osv_ecosystem": "CPAN",
        "osv_strip_prefix": "perl-",
        "macros": {
            "build_mm": "%{__perl} Makefile.PL && %make_build",
            "build_mb": "%{__perl} Build.PL && ./Build",
            "install_mm": "%make_install",
            "install_mb": "./Build install destdir=%{buildroot}",
            "packlist": "%perl_process_packlist",
        },
        "required_build_deps": [
            "perl-macros",
        ],
        "upgrade_hints": [
            "Check META.json/META.yml for new prereqs",
            "Module::Build vs ExtUtils::MakeMaker may have changed",
        ],
        "build_error_patterns": {
            r"Can't locate (.+) in @INC":
                "Missing BuildRequires: perl({0})",
        },
        "spec_template_hints": """
Perl module spec patterns for openSUSE:
- Name: perl-<Module-Name> (hyphens replace ::)
- ExtUtils::MakeMaker: perl Makefile.PL, make, make install
- Module::Build: perl Build.PL, ./Build, ./Build install
- %perl_process_packlist in %install
""",
    },
}


# ─── Ecosystem Detection ─────────────────────────────────────────────────────

def detect_ecosystem(pkg_name, project="", spec_content=None):
    """Auto-detect ecosystem from package name, project context, and optionally spec content.
    Returns ecosystem key (e.g., 'python', 'go', 'c_cmake')."""

    # 1. Check spec content for definitive markers (most reliable)
    if spec_content:
        for eco_key, eco in ECOSYSTEMS.items():
            for marker in eco.get("spec_markers", []):
                if marker in spec_content:
                    return eco_key

    # 2. Check package name prefixes
    name_lower = pkg_name.lower()
    for eco_key, eco in ECOSYSTEMS.items():
        for prefix in eco.get("detect_prefixes", []):
            if name_lower.startswith(prefix):
                return eco_key

    # 3. Check project context
    proj_lower = project.lower()
    for eco_key, eco in ECOSYSTEMS.items():
        for hint in eco.get("project_hints", []):
            if hint in proj_lower:
                return eco_key

    # 4. Default fallback — try to infer from common patterns
    if spec_content:
        if "%cmake" in spec_content:
            return "c_cmake"
        if "%meson" in spec_content:
            return "c_meson"
        if "%configure" in spec_content or "%autosetup" in spec_content:
            return "c_autotools"

    return "unknown"  # Could not detect ecosystem — will use generic handling


def get_osv_ecosystem(ecosystem):
    """Get the OSV ecosystem name for a given ecosystem key."""
    eco = ECOSYSTEMS.get(ecosystem, {})
    return eco.get("osv_ecosystem", "PyPI")


def strip_ecosystem_prefix(pkg_name, ecosystem):
    """Strip ecosystem prefix from package name for OSV lookups."""
    eco = ECOSYSTEMS.get(ecosystem, {})
    prefix = eco.get("osv_strip_prefix", "")
    if prefix and pkg_name.startswith(prefix):
        return pkg_name[len(prefix):]
    return pkg_name


# ─── Build Error Diagnosis ───────────────────────────────────────────────────

def diagnose_build_error(log_text, ecosystem=None):
    """Match build log against known error patterns.
    Returns list of {pattern, match, suggestion} dicts."""
    results = []
    ecosystems_to_check = [ecosystem] if ecosystem else list(ECOSYSTEMS.keys())

    for eco_key in ecosystems_to_check:
        eco = ECOSYSTEMS.get(eco_key, {})
        for pattern, suggestion in eco.get("build_error_patterns", {}).items():
            m = re.search(pattern, log_text)
            if m:
                # Format suggestion with capture groups
                try:
                    formatted = suggestion.format(*m.groups()) if m.groups() else suggestion
                except (IndexError, KeyError):
                    formatted = suggestion
                results.append({
                    "ecosystem": eco_key,
                    "pattern": pattern,
                    "match": m.group(0),
                    "suggestion": formatted,
                })

    return results


def get_upgrade_context(ecosystem):
    """Return ecosystem-specific upgrade hints as a formatted string for AI prompts."""
    eco = ECOSYSTEMS.get(ecosystem, {})
    hints = eco.get("upgrade_hints", [])
    if not hints:
        return ""
    return "Ecosystem-specific upgrade considerations:\n" + "\n".join(f"- {h}" for h in hints)


def get_spec_context(ecosystem):
    """Return ecosystem-specific spec template knowledge for AI prompts."""
    eco = ECOSYSTEMS.get(ecosystem, {})
    return eco.get("spec_template_hints", "").strip()


def get_build_fix_context(ecosystem, log_text):
    """Return targeted fix suggestions based on build log and ecosystem."""
    matches = diagnose_build_error(log_text, ecosystem)
    if not matches:
        return ""
    lines = ["Known error patterns detected:"]
    for m in matches:
        lines.append(f"- Error: {m['match'][:100]}")
        lines.append(f"  Fix: {m['suggestion']}")
    return "\n".join(lines)


# ─── Service File Knowledge ─────────────────────────────────────────────────

SERVICE_MODES = {
    "default": "Runs on commit/server-side, results stored in package",
    "localonly": "Only via 'osc service localrun', user commits results",
    "serveronly": "Only on OBS server, results stored",
    "buildtime": "Runs during each build, not stored (fresh each time)",
    "manual": "Only via explicit 'osc service localrun', user commits results",
    "disabled": "Never runs automatically",
}

SERVICE_TYPES = {
    "obs_scm": "Fetch from Git/SVN/Hg, create tarball (modern, replaces tar_scm)",
    "tar_scm": "Legacy: fetch from VCS and create tarball",
    "download_files": "Download source files from URLs in spec Source: tags",
    "download_url": "Download a single URL",
    "recompress": "Recompress tarballs (e.g., gz → xz)",
    "set_version": "Update Version: in spec from source metadata",
    "cargo_vendor": "Vendor Rust crate dependencies",
    "go_modules": "Vendor Go module dependencies",
    "verify_file": "Verify checksums of downloads",
    "format_spec_file": "Auto-format spec (like spec-cleaner)",
}


def get_service_mode(service_xml):
    """Parse a _service XML string and return info about each service."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(service_xml)
    except ET.ParseError:
        return []
    services = []
    for svc in root.findall("service"):
        name = svc.get("name", "unknown")
        mode = svc.get("mode", "default")
        params = {p.get("name"): p.text for p in svc.findall("param")}
        services.append({"name": name, "mode": mode, "params": params})
    return services
