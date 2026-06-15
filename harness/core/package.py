"""Package schema — the governed unit of execution.

This is the unsigned, YAML-loadable analog of Verity's signed `.vax` (agent)
/ `.vtx` (task) artifacts (design decision D8). It carries the *same logical
fields* the real artifact does, so adopting this engine into the Verity hub
later means wrapping these models in packaging + signing — not reshaping them.

A package fully determines a run's behaviour: the prompt, which models to try
(and in what order — the fallback chain), which tools/MCP servers it may call,
what it reads (sources) and writes (targets), its output schema, and which
sub-agents it may delegate to. Two runs of the same package on the same inputs
with the same recorded mocks reproduce identically — that's the audit-replay
guarantee.

Maps to Verity tables: agent_version / task_version (the package), tool /
tool_authorization (tools), task_version_source / task_version_target
(bindings), inference_config_model (the model chain), agent_version_delegation
(delegations).
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field


class EntityKind(str, enum.Enum):
    TASK = "task"      # single-shot structured transform (one model turn, forced structured output)
    AGENT = "agent"    # multi-turn agentic loop with tools / delegation


# ──────────────────────────────────────────────────────────────────────
# INFERENCE — the model reference chain (ADR-0019)
# ──────────────────────────────────────────────────────────────────────

class ModelRef(BaseModel):
    """One link in the priority fallback chain.

    The chain resolver tries priority 0 first; on *exhausted retries* (not on
    the first transient blip — retries happen within a link) it falls to
    priority 1, then 2, etc. A link names a provider and that provider's model
    string. Mixing providers across a chain is the whole point: Claude primary,
    Gemini fallback, OpenAI last resort, say.
    """
    provider: Literal["anthropic", "openai", "gemini", "mock"]
    model: str
    priority: int = 0
    # Per-link inference knobs. None = provider default.
    max_tokens: int = 4096
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    # Pass-through for provider-specific extras (e.g. anthropic thinking config,
    # openai reasoning effort). The adapter decides what to do with these.
    extra: dict[str, Any] = Field(default_factory=dict)


class InferenceConfig(BaseModel):
    chain: list[ModelRef]
    # Hard ceilings the quota enforcer checks against (per run).
    max_turns: int = 10
    max_total_tokens: Optional[int] = None
    max_usd: Optional[float] = None

    def ordered(self) -> list[ModelRef]:
        return sorted(self.chain, key=lambda m: m.priority)


# ──────────────────────────────────────────────────────────────────────
# TOOLS
# ──────────────────────────────────────────────────────────────────────

class ToolAuthorization(BaseModel):
    """Declares a tool the package may call AND how it's dispatched.

    transport:
      - python_inprocess : a Python callable registered in the worker
      - mcp_stdio        : an MCP server launched as a subprocess
      - mcp_http         : a remote MCP server over Streamable HTTP
      - verity_builtin   : engine meta-tool (delegate_to_agent)

    The tool authorization enforcer (ADR-0016 §2) checks every model-requested
    tool against this list BEFORE dispatch. A model cannot call a tool the
    package didn't authorize, however well-formed its request.
    """
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})
    transport: Literal["python_inprocess", "mcp_stdio", "mcp_http", "verity_builtin"] = "python_inprocess"
    # MCP routing (when transport is mcp_*)
    mcp_server: Optional[str] = None
    mcp_tool_name: Optional[str] = None    # remote name if it differs from `name`
    # Per-tool DB-style mock default (used only when no MockContext is supplied)
    mock_response: Optional[dict] = None


# ──────────────────────────────────────────────────────────────────────
# CONNECTOR BINDINGS — sources (inputs) and targets (outputs)
# ──────────────────────────────────────────────────────────────────────

class SourceBinding(BaseModel):
    """Resolve an input from an external system into the run's context.

    Example: pull a policy row from Postgres and bind it under context.policy.
      connector: pg_main
      method: query
      ref: "SELECT * FROM policy WHERE id = {{input.policy_id}}"
      bind_to: policy
      required: true
    """
    connector: str
    method: str
    ref: Any                       # query / key / path; may contain {{input.x}} templates
    bind_to: str                   # context key the resolved value is placed under
    required: bool = True


class TargetBinding(BaseModel):
    """Write an output field to an external system after the run produces output.

    Example: write the report to S3.
      connector: s3_main
      method: put_object
      from_path: "$.report"        # JSONPath-ish into the output dict
      container: "reports/{{run_id}}.json"
      required: true
    """
    connector: str
    method: str
    from_path: str                 # selector into the output dict ("$.field" or "$" for whole)
    container: Optional[str] = None
    required: bool = True


# ──────────────────────────────────────────────────────────────────────
# DELEGATION
# ──────────────────────────────────────────────────────────────────────

class DelegationAuthorization(BaseModel):
    """Which sub-agents this agent may spawn via delegate_to_agent, and
    optionally pinned to a specific child package version.
    """
    child_agent: str
    pinned_version: Optional[str] = None


# ──────────────────────────────────────────────────────────────────────
# HITL
# ──────────────────────────────────────────────────────────────────────

class HITLPolicy(BaseModel):
    """When to pause for human approval. The gate fires BEFORE a matching
    tool executes; the run suspends and resumes on the human decision.
    `require_approval_for` lists tool names that need sign-off.
    """
    enabled: bool = False
    require_approval_for: list[str] = Field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────
# PACKAGE
# ──────────────────────────────────────────────────────────────────────

class Package(BaseModel):
    """A complete agent or task definition. Load from YAML via `Package.load`."""
    name: str
    kind: EntityKind
    version: str = "v1"
    description: str = ""

    system_prompt: str = ""
    # For tasks: the user prompt template (input is substituted in).
    # For agents: the opening user message template.
    prompt_template: str = ""

    inference: InferenceConfig

    tools: list[ToolAuthorization] = Field(default_factory=list)
    sources: list[SourceBinding] = Field(default_factory=list)
    targets: list[TargetBinding] = Field(default_factory=list)
    delegations: list[DelegationAuthorization] = Field(default_factory=list)
    hitl: HITLPolicy = Field(default_factory=HITLPolicy)

    # Output schema (JSON Schema `properties`). For tasks this forces a
    # structured_output tool; for agents it enables submit_output enforcement.
    output_schema: Optional[dict[str, Any]] = None

    @classmethod
    def load(cls, path: str | Path) -> "Package":
        data = yaml.safe_load(Path(path).read_text())
        return cls.model_validate(data)

    def tool_def(self, name: str) -> Optional[ToolAuthorization]:
        return next((t for t in self.tools if t.name == name), None)


__all__ = [
    "EntityKind", "ModelRef", "InferenceConfig", "ToolAuthorization",
    "SourceBinding", "TargetBinding", "DelegationAuthorization",
    "HITLPolicy", "Package",
]
