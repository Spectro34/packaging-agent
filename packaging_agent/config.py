"""
Configuration loading for the openSUSE Packaging Agent.
Loads from config.json (same directory as main script), with env var fallback.
"""

import json
import os
import ssl


def load_config(config_dir=None):
    """Load config from config.json. Returns dict with all settings."""
    if config_dir is None:
        # Default: look in the production/ directory (parent of packaging_agent/)
        config_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    config_path = os.path.join(config_dir, "config.json")
    cfg = {}
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = json.load(f)
        except Exception as e:
            print(f"  [Warning] Failed to load {config_path}: {e}")

    # Environment variables take precedence over config.json
    return {
        "openai_api_key": os.environ.get("OPENAI_API_KEY") or cfg.get("openai_api_key", ""),
        "obs_api_url": os.environ.get("OBS_API_URL") or cfg.get("obs_api_url", "https://api.opensuse.org"),
        "obs_user": os.environ.get("OBS_USER") or cfg.get("obs_user", ""),
        "obs_pass": os.environ.get("OBS_PASS") or cfg.get("obs_pass", ""),
        "obs_project": os.environ.get("OBS_PROJECT") or cfg.get("obs_project", "systemsmanagement:ansible"),
        "mcp_url": os.environ.get("MCP_URL") or cfg.get("mcp_url", "http://localhost:8666/mcp"),
        "openai_model": os.environ.get("OPENAI_MODEL") or cfg.get("openai_model", "gpt-4o"),
    }


# API endpoint constants
REPOLOGY_API = "https://repology.org/api/v1/project/{name}"
OSV_QUERY_API = "https://api.osv.dev/v1/query"
OSV_VULN_API = "https://api.osv.dev/v1/vulns/{id}"
GITHUB_API = "https://api.github.com/repos/{owner}/{repo}/releases?per_page=5"
OPENAI_API = "https://api.openai.com/v1/chat/completions"

# Shared SSL context
SSL_CTX = ssl.create_default_context()
