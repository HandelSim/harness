"""
Harbor agent adapter for `harness claude`.

EXECUTION MODEL:
- Harbor (host) launches a per-task container per benchmark task.
- Inside the task container, this adapter clones harness, runs
  harness-install.sh, then invokes `harness claude -p "<instruction>"`
  per task.
- harness's `docker compose up` requires host Docker access — Harbor
  must mount /var/run/docker.sock into the task container (rw).
- LLM calls flow: harness agent (claude code SDK) -> ollama stub
  -> harness proxy -> upstream LLM API.

ARCHITECTURE NOTE (host = ARM64, TB task images = mostly x86_64):
- Task containers run under QEMU user-mode emulation via binfmt_misc.
- See tests/benchmarks/README.md for one-time binfmt registration.

ENVIRONMENT VARIABLES (set by the runner before harbor invocation):
- PROXY_API_KEY        : upstream LLM API key
- PROXY_API_URL        : upstream LLM endpoint
- PROXY_API_MODEL      : model identifier
- PROXY_PROMPT_MODE    : current prod injection mode
                         (user_front | user_bookend | user | system | hybrid)
- HARNESS_PROXY_SCHEME : reserved for future named-scheme support; see
                         tests/benchmarks/schemes/*.json
- HARNESS_GIT_REF      : git ref to clone (default: dev)
- HARNESS_REPO         : git repo URL (default: github.com/HandelSim/harness)
- HARNESS_DIR          : clone target (default: /opt/harness)
"""

from __future__ import annotations

# Path-relative import of the shared common module. When packaged for
# Harbor via pyproject.toml, the parent directory is on sys.path.
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from _common import HarnessAgentBase  # noqa: E402


class HarnessClaudeAgent(HarnessAgentBase):
    """Harbor agent that runs `harness claude -p <instruction>`."""

    HARNESS_AGENT = "claude"
    OUTPUT_FILENAME = "harness-claude.txt"

    @staticmethod
    def name() -> str:
        return "harness-claude"
