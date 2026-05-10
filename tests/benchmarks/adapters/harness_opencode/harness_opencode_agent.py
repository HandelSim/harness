"""
Harbor agent adapter for `harness opencode`.

See harness_claude_agent.py for the full execution-model description;
this file differs only in which underlying agent is invoked.

ENVIRONMENT VARIABLES: identical to harness_claude_agent.py.
"""

from __future__ import annotations

import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_THIS_DIR)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

from _common import HarnessAgentBase  # noqa: E402


class HarnessOpencodeAgent(HarnessAgentBase):
    """Harbor agent that runs `harness opencode -p <instruction>`."""

    HARNESS_AGENT = "opencode"
    OUTPUT_FILENAME = "harness-opencode.txt"

    @staticmethod
    def name() -> str:
        return "harness-opencode"
