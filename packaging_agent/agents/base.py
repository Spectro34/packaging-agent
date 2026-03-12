"""
Base agent framework for the openSUSE Packaging Agent system.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentResult:
    """Standardized result from any agent execution."""
    success: bool
    action: str                          # "analyze", "build", "upgrade", "cve_fix", "review", "scan"
    package: str = ""
    project: str = ""
    summary: str = ""                    # Human-readable one-liner
    details: dict = field(default_factory=dict)  # Agent-specific structured data
    errors: list = field(default_factory=list)    # List of error strings
    needs_review: bool = False           # Orchestrator should send to reviewer
    needs_retry: bool = False            # Orchestrator should retry via builder
    retry_context: dict = field(default_factory=dict)  # Context for retry
    work_dir: Optional[str] = None       # Local working directory (for osc checkouts)


class BaseAgent:
    """Base class for all packaging agents.

    Provides:
    - Config access
    - GPT helper (delegates to shared gpt() function)
    - Ecosystem knowledge access
    """

    def __init__(self, config):
        """
        Args:
            config: dict with keys like openai_api_key, obs_user, obs_pass, etc.
        """
        self.config = config
        self.api_key = config.get("openai_api_key", "")

    def gpt(self, system, user, temperature=0.2, max_tokens=2000):
        """Call GPT via the shared helper. Imported lazily to avoid circular deps."""
        from packaging_agent.http import gpt as _gpt
        return _gpt(system, user, self.api_key,
                     temperature=temperature, max_tokens=max_tokens)

    def run(self, **kwargs) -> AgentResult:
        """Execute the agent's primary action. Subclasses must override."""
        raise NotImplementedError(f"{self.__class__.__name__}.run() not implemented")
