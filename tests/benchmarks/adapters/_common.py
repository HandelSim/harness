"""
Shared logic for harness Harbor adapters.

Harbor's BaseInstalledAgent interface (harbor>=2.x) is async and expects:

    async def install(self, environment: BaseEnvironment) -> None
    async def run(self, instruction, environment, context) -> None
    def populate_context_post_run(self, context: AgentContext) -> None

These adapters wrap `harness <agent> -p "<instruction>"`. The harness stack
itself runs inside Harbor's per-task container; that container needs host
Docker access (via /var/run/docker.sock bind mount) because harness boots
its own docker-compose stack (proxy + ollama + firewall + agent containers).

EXECUTION MODEL
---------------
Harbor (host) -> per-task container -> this adapter ->
    harness-install.sh + docker compose up -> harness <agent> -p "<task>"
        -> ollama stub -> harness proxy -> upstream LLM API

ARM64 + QEMU CAVEAT
-------------------
Terminal-Bench task images are predominantly x86_64. On an aarch64 host
(this codebase's target), each task container runs under QEMU user-mode
emulation via the kernel's binfmt_misc registration. Expect 5-10x slowdown
vs. native x86 and some tasks failing under emulation. Default
--n-concurrent=1 on aarch64 hosts. The runner library detects this and
warns; see tests/benchmarks/runners/_lib.sh.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import Any

# Best-effort import. Harbor is a runtime dependency declared in each
# adapter's pyproject.toml; this fallback exists so parse-checks and
# unit-style inspection work on hosts where harbor is not installed.
try:
    from harbor.agents.installed.base import BaseInstalledAgent  # type: ignore
    from harbor.environments.base import BaseEnvironment  # type: ignore
    from harbor.models.agent.context import AgentContext  # type: ignore
    _HARBOR_AVAILABLE = True
except ImportError:  # pragma: no cover
    _HARBOR_AVAILABLE = False

    class BaseInstalledAgent:  # type: ignore[no-redef]
        """Fallback shim when harbor is not installed."""

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

    class BaseEnvironment:  # type: ignore[no-redef]
        pass

    class AgentContext:  # type: ignore[no-redef]
        pass


HARNESS_REPO_DEFAULT = "https://github.com/HandelSim/harness.git"
HARNESS_DIR_DEFAULT = "/opt/harness"

# Env vars the runner sets BEFORE invoking harbor. The adapter reads them
# at install() time and writes them into harness's .env. These mirror the
# variables docker-compose.yml expects.
#
# PROXY_PROMPT_MODE is intentionally NOT here: it is no longer a .env knob
# (docker-compose.yml stopped interpolating it so a stale .env can't override
# the hybrid default). The benchmark scheme still selects a mode via the
# PROXY_PROMPT_MODE env var, but the adapter applies it through the ephemeral
# `harness restart --prompt-mode <mode>` flag after install — see
# harness_prompt_mode() and install().
HARNESS_ENV_KEYS = (
    "PROXY_API_KEY",
    "PROXY_API_URL",
    "DEFAULT_MODEL_NAME",
    "HARNESS_PROXY_SCHEME",
)


def render_harness_env() -> str:
    """Render the contents of harness's .env from the current process env."""
    lines = [f"{k}={os.environ[k]}" for k in HARNESS_ENV_KEYS if k in os.environ]
    return "\n".join(lines) + ("\n" if lines else "")


def harness_prompt_mode() -> str:
    """The cooperative-prompt mode the scheme selected, or "" if none.

    The benchmark scheme exports PROXY_PROMPT_MODE into the runner env (see
    tests/benchmarks/schemes/*.json + bench_apply_scheme). Since it is no
    longer fed to the proxy via .env, the adapter applies it post-install with
    `harness restart --prompt-mode <mode>`.
    """
    return os.environ.get("PROXY_PROMPT_MODE", "").strip()


def harness_dir() -> Path:
    return Path(os.environ.get("HARNESS_DIR", HARNESS_DIR_DEFAULT))


def harness_repo() -> str:
    return os.environ.get("HARNESS_REPO", HARNESS_REPO_DEFAULT)


def harness_git_ref() -> str:
    return os.environ.get("HARNESS_GIT_REF", "dev")


class HarnessAgentBase(BaseInstalledAgent):
    """
    Base class for `harness opencode` Harbor adapters.

    Subclasses define ``HARNESS_AGENT`` (e.g. "opencode") and optionally
    override ``OUTPUT_FILENAME``.
    """

    HARNESS_AGENT: str = "opencode"
    OUTPUT_FILENAME: str = "harness-output.txt"

    @staticmethod
    def name() -> str:  # pragma: no cover - overridden in subclasses
        return "harness"

    def get_version_command(self) -> str | None:
        return "harness --version 2>/dev/null || echo unknown"

    async def install(self, environment: "BaseEnvironment") -> None:
        """
        Inside the task container:
            1. Install prerequisites (git, docker CLI, bash).
            2. Clone the harness repo.
            3. Write .env from runner-provided env vars.
            4. Run harness-install.sh (idempotent installer).

        NOTE: Harbor must mount /var/run/docker.sock into the task container
        for harness-install.sh's docker-compose-up step to succeed. The
        runner script enforces this via Harbor task-container config.
        """
        await self.exec_as_root(
            environment,
            command=(
                "apt-get update && "
                "apt-get install -y --no-install-recommends "
                "git curl ca-certificates bash docker.io"
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        )

        repo = harness_repo()
        ref = harness_git_ref()
        clone_dir = str(harness_dir())

        # Clone or update. Quoted for shell safety.
        await self.exec_as_root(
            environment,
            command=(
                f"set -e; "
                f"if [ ! -d {shlex.quote(clone_dir)}/.git ]; then "
                f"  git clone --branch {shlex.quote(ref)} --depth 50 "
                f"      {shlex.quote(repo)} {shlex.quote(clone_dir)}; "
                f"else "
                f"  git -C {shlex.quote(clone_dir)} fetch origin {shlex.quote(ref)}; "
                f"  git -C {shlex.quote(clone_dir)} checkout {shlex.quote(ref)}; "
                f"fi"
            ),
        )

        env_contents = render_harness_env()
        # Write .env. Use printf to avoid heredoc quoting headaches.
        encoded = shlex.quote(env_contents)
        await self.exec_as_root(
            environment,
            command=f"printf %s {encoded} > {shlex.quote(clone_dir + '/.env')}",
        )

        # Run harness-install.sh. This boots docker-compose, which needs
        # /var/run/docker.sock mounted from the host.
        await self.exec_as_root(
            environment,
            command=f"bash {shlex.quote(clone_dir + '/harness-install.sh')}",
        )

        # Apply the scheme's cooperative-prompt mode, if any. It is no longer
        # carried in .env; the proxy defaults to hybrid, so we only restart to
        # switch when the scheme asked for a specific mode. The flag is
        # ephemeral, so it must be (re)applied here every install.
        mode = harness_prompt_mode()
        if mode:
            await self.exec_as_root(
                environment,
                command=f"harness restart --prompt-mode {shlex.quote(mode)}",
            )

    async def run(
        self,
        instruction: str,
        environment: "BaseEnvironment",
        context: "AgentContext",
    ) -> None:
        """
        Invoke `harness <agent> -p "<instruction>"` headlessly inside the
        task container. Captures stdout/stderr to the logs dir for
        post-run trajectory parsing.
        """
        out_path = Path(self.logs_dir) / self.OUTPUT_FILENAME

        quoted_instruction = shlex.quote(instruction)
        cmd = (
            f'harness {self.HARNESS_AGENT} -p {quoted_instruction} '
            f'2>&1 | tee {shlex.quote(str(out_path))}'
        )

        # exec_as_agent is the standard wrapper; falls back to default user.
        await self.exec_as_agent(environment, command=cmd)

    def populate_context_post_run(self, context: "AgentContext") -> None:
        """
        Best-effort trajectory ingest. harness does not currently emit a
        structured trajectory file; we capture the raw transcript so
        humans can inspect runs. Token accounting is left None — Harbor's
        default is to consult provider-reported usage if the trial's LLM
        client returned any. The harness proxy logs full request/response
        bodies under state/output/ which can be ingested separately.
        """
        out_path = Path(self.logs_dir) / self.OUTPUT_FILENAME
        if not out_path.exists():
            return

        try:
            content = out_path.read_text()
        except OSError:
            return

        # Stash the raw transcript under context.metadata so Harbor's
        # trajectory analyzer has something to surface.
        if context.metadata is None:
            context.metadata = {}
        context.metadata["raw_transcript_path"] = str(out_path)
        context.metadata["raw_transcript_bytes"] = len(content)
