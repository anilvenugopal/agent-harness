# SPDX-License-Identifier: AGPL-3.0-or-later
"""Execution engine — the provider-agnostic agentic loop and its siblings.

This is where the spine comes together. The engine owns the loop; the four
gateways (model / tool / source / target) are injected, each with its mock
seam. The loop reads only the neutral IR, so it is identical whether the model
turns come from Claude, OpenAI, Gemini, or the mock/replay provider.

Public surface:
  - run_task(...)      : single-turn structured transform (forced structured output)
  - run_agent(...)     : multi-turn agentic loop with tools / MCP / delegation / HITL
  - resume(...)        : continue a suspended agent run after a human decision

Cross-cutting concerns, all visible as discrete stages in the loop (and as
ExecutionEvents in the trace):
  quota check → model call (with fallback chain) → [HITL gate] → tool dispatch
  → decision assembly → target writes → decision sink.

Delegation: delegate_to_agent is a verity_builtin tool. When the model calls
it, the tool gateway routes to `self._delegate`, which re-enters run_agent with
incremented depth and the parent's correlation ids — a fully governed nested
run that writes its own decision record.

HITL: when a turn requests a gated tool, the loop serialises the full
continuation, raises HITLSuspended, and the worker checkpoints + releases.
`resume` rebuilds the loop state and continues. Scope: top-level agents only
(sub-agent gates are rejected, since synchronous delegation would block).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from harness.connectors.binder import Binder, _template as render_template
from harness.core.ir import (
    DocumentBlock, ImageBlock, Message, ModelResponse, StopReason, TextBlock, ThinkingBlock,
    ToolDef, ToolResultBlock, ToolUseBlock,
)
from harness.core.package import EntityKind, Package
from harness.core.result import (
    EventType, ExecutionEvent, ExecutionResult, HITLSuspended, RunStatus,
)
from harness.core.trace import Tracer
from harness.decisions.assembler import DecisionAssembler, DecisionRecord, DecisionSink, FileSink
from harness.hitl.continuation import (
    Continuation, ContinuationStore, FileContinuationStore, HumanDecision,
)
from harness.mock.context import MockContext
from harness.providers.base import ModelChain, ProviderError
from harness.providers.mock_provider import MockProvider
from harness.quota.enforcer import QuotaEnforcer, QuotaExceeded
from harness.tools.gateway import DelegateContext, ToolGateway

logger = logging.getLogger("harness.engine")

MAX_DECISION_DEPTH = 5


class ExecutionEngine:
    def __init__(
        self,
        *,
        chain: ModelChain,
        tool_gateway: ToolGateway,
        binder: Binder,
        packages: dict[str, Package],
        sink: Optional[DecisionSink] = None,
        continuation_store: Optional[ContinuationStore] = None,
        application: str = "harness",
        tracer: Optional[Tracer] = None,
        global_fallback_enabled: bool = True,
    ):
        self.chain = chain
        self.tools = tool_gateway
        self.binder = binder
        self.packages = packages
        self.sink = sink or FileSink()
        self.continuations = continuation_store or FileContinuationStore()
        self.application = application
        self.tracer = tracer or Tracer(enabled=False)
        self.global_fallback_enabled = global_fallback_enabled
        # let the gateway route delegate_to_agent back into the engine
        self.tools.delegate_fn = self._delegate

    # ──────────────────────────────────────────────────────────────
    # TASK — single-turn structured transform
    # ──────────────────────────────────────────────────────────────
    async def run_task(
        self,
        *,
        task_name: str,
        input_data: dict,
        channel: str = "production",
        mock: Optional[MockContext] = None,
        execution_run_id: Optional[UUID] = None,
        depth: int = 0,
    ) -> ExecutionResult:
        pkg = self._require_package(task_name, EntityKind.TASK)
        run_id = execution_run_id or uuid4()
        started = time.monotonic()
        assembler = self._new_assembler(pkg, run_id, channel, input_data, mock, depth)
        self.tracer.emit(ExecutionEvent(type=EventType.RUN_STARTED, run_id=run_id, depth=depth,
                                        detail={"entity": task_name, "kind": "task"}))
        try:
            context, src_audit = await self.binder.resolve_sources(pkg.sources, input_data, mock)
            for a in src_audit:
                assembler.add_source_resolution(a)
                self.tracer.emit(ExecutionEvent(type=EventType.SOURCE_RESOLVED, run_id=run_id,
                                                depth=depth, detail={"bind_to": a["bind_to"], "mocked": a["mocked"]}))

            system = pkg.system_prompt
            user = render_template(pkg.prompt_template or "{{input}}", {"input": input_data, "context": context})
            messages = [_first_user_message(pkg, user, input_data, context)]

            # Force structured output via a synthetic tool when a schema is declared.
            force_tool = None
            tools: list[ToolDef] = []
            if pkg.output_schema:
                tools = [ToolDef(name="structured_output",
                                 description="Return the final structured output for this task.",
                                 input_schema={"type": "object", "properties": pkg.output_schema})]
                force_tool = "structured_output"

            quota = QuotaEnforcer(max_turns=1, max_total_tokens=pkg.inference.max_total_tokens,
                                  max_usd=pkg.inference.max_usd)
            quota.check_turn()
            self.tracer.emit(ExecutionEvent(type=EventType.TURN_STARTED, run_id=run_id, depth=depth, detail={"turn": 0}))
            response = await self._model_call(pkg, system, messages, tools, force_tool, depth)
            quota.record(response.usage, response.model)
            assembler.add_turn(turn=0, response=response, neutral_blocks=[b.model_dump() for b in response.blocks])
            messages.append(Message.assistant_blocks(response.blocks))

            if force_tool:
                calls = response.tool_calls
                output = calls[0].input if calls else {}
            else:
                output = _try_json(response.text)
            reasoning = (response.thinking + ("\n" + response.text if not force_tool else "")).strip() or None

            tgt_audit = await self.binder.write_targets(pkg.targets, output, input_data, str(run_id), mock)
            for a in tgt_audit:
                assembler.add_target_write(a)
                et = EventType.TARGET_SUPPRESSED if a["suppressed"] else EventType.TARGET_WRITTEN
                self.tracer.emit(ExecutionEvent(type=et, run_id=run_id, depth=depth,
                                                detail={"connector": a["connector"], "container": a["container"]}))

            assembler.set_messages(messages)
            duration = int((time.monotonic() - started) * 1000)
            return await self._finish(assembler, output, reasoning, RunStatus.COMPLETE, None, duration, run_id, depth)
        except Exception as e:
            return await self._fail(assembler, e, started, run_id, depth)

    # ──────────────────────────────────────────────────────────────
    # AGENT — multi-turn loop
    # ──────────────────────────────────────────────────────────────
    async def run_agent(
        self,
        *,
        agent_name: str,
        context: dict,
        channel: str = "production",
        mock: Optional[MockContext] = None,
        execution_run_id: Optional[UUID] = None,
        depth: int = 0,
        parent_decision_id: Optional[UUID] = None,
        workflow_run_id: Optional[UUID] = None,
    ) -> ExecutionResult:
        pkg = self._require_package(agent_name, EntityKind.AGENT)
        run_id = execution_run_id or uuid4()
        started = time.monotonic()
        assembler = self._new_assembler(pkg, run_id, channel, context, mock, depth,
                                        parent_decision_id=parent_decision_id)
        assembler.record.hitl_required = False  # set True only if the gate actually fires
        self.tracer.emit(ExecutionEvent(type=EventType.RUN_STARTED, run_id=run_id, depth=depth,
                                        detail={"entity": agent_name, "kind": "agent"}))
        try:
            src_ctx, src_audit = await self.binder.resolve_sources(pkg.sources, context, mock)
            for a in src_audit:
                assembler.add_source_resolution(a)
                self.tracer.emit(ExecutionEvent(type=EventType.SOURCE_RESOLVED, run_id=run_id, depth=depth,
                                                detail={"bind_to": a["bind_to"], "mocked": a["mocked"]}))
            merged = {**context, **src_ctx}
            user = render_template(pkg.prompt_template or "{{input}}", {"input": context, "context": src_ctx})
            messages = [_first_user_message(pkg, user, merged, src_ctx)]

            quota = QuotaEnforcer(max_turns=pkg.inference.max_turns,
                                  max_total_tokens=pkg.inference.max_total_tokens,
                                  max_usd=pkg.inference.max_usd)
            return await self._agent_loop(
                pkg=pkg, run_id=run_id, channel=channel, run_input=context, mock=mock,
                messages=messages, assembler=assembler, quota=quota, started=started,
                depth=depth, start_turn=0,
                parent_decision_id=parent_decision_id, workflow_run_id=workflow_run_id,
            )
        except HITLSuspended:
            raise  # bubble to the worker; NOT an error
        except Exception as e:
            return await self._fail(assembler, e, started, run_id, depth)

    # ──────────────────────────────────────────────────────────────
    # RESUME — continue a suspended agent after a human decision
    # ──────────────────────────────────────────────────────────────
    async def resume(self, suspension_id: UUID) -> ExecutionResult:
        cont = await self.continuations.load(suspension_id)
        if cont.status != "resolved" or cont.decision is None:
            raise RuntimeError(f"continuation {suspension_id} has no recorded decision yet")
        pkg = self._require_package(cont.package_name, EntityKind.AGENT)
        run_id = cont.run_id
        started = time.monotonic()

        # Rebuild loop state from the checkpoint.
        messages = [Message.model_validate(m) for m in cont.messages]
        assembler = self._new_assembler(pkg, run_id, cont.channel, cont.run_input, None, cont.decision_depth,
                                        decision_id=cont.decision_id)
        assembler.record.hitl_required = True
        for tc in cont.tool_calls_made:
            assembler.add_tool_call(tc)
        for sr in cont.source_resolutions:
            assembler.add_source_resolution(sr)

        decision = cont.decision
        pending = cont.pending_tool_use
        # Reproduce the mock/suppress context that was in effect at suspension,
        # so the resumed tail writes targets / dispatches tools the same way.
        mock = MockContext.model_validate(cont.mock) if cont.mock else None
        self.tracer.emit(ExecutionEvent(type=EventType.HITL_RESUMED, run_id=run_id, depth=cont.decision_depth,
                                        detail={"gate": cont.gate_tool, "decision": decision.decision}))

        # Apply the human decision to the gated tool, producing its tool_result.
        if decision.decision == "approve":
            rec = await self.tools.call(name=pending["name"], tool_input=pending["input"],
                                        authorized=pkg.tools, call_order=len(cont.tool_calls_made) + 1, mock=mock,
                                        delegate_ctx=self._delegate_ctx(pkg, cont.decision_id, cont.decision_depth,
                                                                        None, cont.channel))
        elif decision.decision == "edit" and decision.edited_input is not None:
            rec = await self.tools.call(name=pending["name"], tool_input=decision.edited_input,
                                        authorized=pkg.tools, call_order=len(cont.tool_calls_made) + 1, mock=mock,
                                        delegate_ctx=self._delegate_ctx(pkg, cont.decision_id, cont.decision_depth,
                                                                        None, cont.channel))
        elif decision.decision == "edit" and decision.edited_result is not None:
            rec = {"tool_name": pending["name"], "call_order": len(cont.tool_calls_made) + 1,
                   "input_data": pending["input"], "output_data": decision.edited_result,
                   "transport": "hitl_edit", "error": False}
        else:  # deny
            rec = {"tool_name": pending["name"], "call_order": len(cont.tool_calls_made) + 1,
                   "input_data": pending["input"],
                   "output_data": {"error": f"Tool call denied by reviewer: {decision.note or 'no reason given'}"},
                   "transport": "hitl_deny", "error": True}
        assembler.add_tool_call(rec)
        messages.append(Message.tool_results([ToolResultBlock(
            tool_use_id=pending["id"], content=rec["output_data"], is_error=rec["error"])]))

        quota = QuotaEnforcer(max_turns=pkg.inference.max_turns,
                              max_total_tokens=pkg.inference.max_total_tokens, max_usd=pkg.inference.max_usd)
        # account for turns already spent before suspension
        for _ in range(cont.turn + 1):
            try:
                quota.check_turn()
            except QuotaExceeded:
                break

        return await self._agent_loop(
            pkg=pkg, run_id=run_id, channel=cont.channel, run_input=cont.run_input, mock=mock,
            messages=messages, assembler=assembler, quota=quota, started=started,
            depth=cont.decision_depth, start_turn=cont.turn + 1,
            parent_decision_id=None, workflow_run_id=None, resumed_decision_id=cont.decision_id,
        )

    # ──────────────────────────────────────────────────────────────
    # THE LOOP (shared by fresh runs and resume)
    # ──────────────────────────────────────────────────────────────
    async def _agent_loop(
        self, *, pkg, run_id, channel, run_input, mock, messages, assembler, quota, started,
        depth, start_turn, parent_decision_id, workflow_run_id, resumed_decision_id=None,
    ) -> ExecutionResult:
        tool_defs = [ToolDef(name=t.name, description=t.description, input_schema=t.input_schema)
                     for t in pkg.tools]

        # Optional structured-output enforcement: synthetic submit_output tool.
        submit_tool = None
        if pkg.output_schema:
            submit_tool = ToolDef(name="submit_output",
                                  description="Submit final structured output; calling this ends the run.",
                                  input_schema={"type": "object", "properties": pkg.output_schema})
            tool_defs = [*tool_defs, submit_tool]

        submit_input: Optional[dict] = None
        last_response: Optional[ModelResponse] = None
        status = RunStatus.MAX_TURNS

        turn = start_turn
        while True:
            try:
                quota.check_turn()
            except QuotaExceeded as e:
                self.tracer.emit(ExecutionEvent(type=EventType.QUOTA_CHECK, run_id=run_id, depth=depth,
                                                detail={"exceeded": str(e)}))
                status = RunStatus.MAX_TURNS
                break

            self.tracer.emit(ExecutionEvent(type=EventType.TURN_STARTED, run_id=run_id, depth=depth,
                                            detail={"turn": turn}))
            response = await self._model_call(pkg, pkg.system_prompt, messages, tool_defs, None, depth)
            quota.record(response.usage, response.model)
            last_response = response
            assembler.add_turn(turn=turn, response=response, neutral_blocks=[b.model_dump() for b in response.blocks])
            messages.append(Message.assistant_blocks(response.blocks))
            self.tracer.emit(ExecutionEvent(type=EventType.MODEL_RESPONDED, run_id=run_id, depth=depth,
                                            detail={"stop": response.stop_reason.value,
                                                    "tools": len(response.tool_calls), "model": response.model}))

            if response.stop_reason != StopReason.TOOL_USE:
                status = RunStatus.COMPLETE
                break

            # submit_output terminates the run (its input IS the answer).
            if submit_tool is not None:
                for tc in response.tool_calls:
                    if tc.name == "submit_output":
                        submit_input = tc.input
                        break
                if submit_input is not None:
                    status = RunStatus.COMPLETE
                    break

            # ── HITL GATE — fires BEFORE executing a gated tool ──
            for tc in response.tool_calls:
                if self.tools.needs_approval(tc.name, pkg.hitl):
                    if depth > 0:
                        # Sub-agent HITL is out of scope (would block the parent).
                        # Deny cleanly so the parent can decide what to do.
                        rec = {"tool_name": tc.name, "call_order": len(assembler.record.tool_calls_made) + 1,
                               "input_data": tc.input, "transport": "verity_builtin",
                               "output_data": {"error": "HITL gates are not permitted inside delegated sub-agents"},
                               "error": True}
                        assembler.add_tool_call(rec)
                        messages.append(Message.tool_results([ToolResultBlock(
                            tool_use_id=tc.id, content=rec["output_data"], is_error=True)]))
                        continue
                    self.tracer.emit(ExecutionEvent(type=EventType.HITL_GATE, run_id=run_id, depth=depth,
                                                    detail={"tool": tc.name}))
                    cont = Continuation(
                        run_id=run_id, decision_id=resumed_decision_id or assembler.record.id,
                        package_name=pkg.name, package_version=pkg.version, channel=channel,
                        run_input=run_input, decision_depth=depth, turn=turn,
                        messages=[m.model_dump() for m in messages],
                        tool_calls_made=list(assembler.record.tool_calls_made),
                        source_resolutions=list(assembler.record.source_resolutions),
                        usage={"input_tokens": assembler.record.input_tokens,
                               "output_tokens": assembler.record.output_tokens},
                        gate_tool=tc.name, pending_tool_use={"id": tc.id, "name": tc.name, "input": tc.input},
                        mock=mock.model_dump() if mock is not None else None,
                    )
                    assembler.record.hitl_required = True
                    await self.continuations.save(cont)
                    self.tracer.emit(ExecutionEvent(type=EventType.HITL_SUSPENDED, run_id=run_id, depth=depth,
                                                    detail={"suspension_id": str(cont.id), "tool": tc.name}))
                    raise HITLSuspended(cont.id, run_id, tc.name)

            # ── normal tool dispatch ──
            results: list[ToolResultBlock] = []
            for tc in response.tool_calls:
                self.tracer.emit(ExecutionEvent(type=EventType.TOOL_CALLED, run_id=run_id, depth=depth,
                                                detail={"tool": tc.name, "input": tc.input}))
                rec = await self.tools.call(
                    name=tc.name, tool_input=tc.input, authorized=pkg.tools,
                    call_order=len(assembler.record.tool_calls_made) + 1, mock=mock,
                    delegate_ctx=self._delegate_ctx(pkg, assembler.record.id, depth, workflow_run_id, channel),
                )
                assembler.add_tool_call(rec)
                self.tracer.emit(ExecutionEvent(type=(EventType.TOOL_DENIED if rec["error"] else EventType.TOOL_RESULT),
                                                run_id=run_id, depth=depth,
                                                detail={"tool": tc.name, "error": rec["error"]}))
                results.append(ToolResultBlock(tool_use_id=tc.id, content=rec["output_data"], is_error=rec["error"]))
            messages.append(Message.tool_results(results))
            turn += 1

        # ── finalize ──
        if submit_input is not None:
            output = submit_input
        elif last_response is not None and last_response.stop_reason != StopReason.TOOL_USE:
            output = _try_json(last_response.text)
        else:
            output = {"status": status.value, "note": "loop ended without a final answer"}
        reasoning = (last_response.thinking if last_response else "") or None

        tgt_audit = await self.binder.write_targets(pkg.targets, output, run_input, str(run_id), mock)
        for a in tgt_audit:
            assembler.add_target_write(a)
            et = EventType.TARGET_SUPPRESSED if a["suppressed"] else EventType.TARGET_WRITTEN
            self.tracer.emit(ExecutionEvent(type=et, run_id=run_id, depth=depth,
                                            detail={"connector": a["connector"]}))

        assembler.set_messages(messages)
        duration = int((time.monotonic() - started) * 1000)
        return await self._finish(assembler, output, reasoning, status, None, duration, run_id, depth)

    # ──────────────────────────────────────────────────────────────
    # DELEGATION
    # ──────────────────────────────────────────────────────────────
    async def _delegate(self, tool_input: dict, ctx: DelegateContext) -> dict:
        child_name = tool_input.get("agent_name")
        child_context = tool_input.get("context")
        if not isinstance(child_name, str) or not child_name.strip():
            return _builtin_err("delegate_to_agent", tool_input, "Missing/invalid 'agent_name'")
        if not isinstance(child_context, dict):
            return _builtin_err("delegate_to_agent", tool_input, "'context' must be a dict")

        next_depth = ctx.decision_depth + 1
        if next_depth >= MAX_DECISION_DEPTH:
            return _builtin_err("delegate_to_agent", tool_input,
                                f"Delegation refused: depth {next_depth} >= MAX_DECISION_DEPTH={MAX_DECISION_DEPTH}")

        # Governance gate: is the parent authorized to delegate to this child?
        parent = self.packages.get(ctx.parent_agent_name)
        allowed = {d.child_agent for d in (parent.delegations if parent else [])}
        if child_name not in allowed:
            return _builtin_err("delegate_to_agent", tool_input,
                                f"Not authorized to delegate to {child_name!r}. Allowed: {sorted(allowed) or 'none'}")

        self.tracer.emit(ExecutionEvent(type=EventType.DELEGATION_STARTED, depth=ctx.decision_depth,
                                        detail={"parent": ctx.parent_agent_name, "child": child_name,
                                                "depth": next_depth}))
        try:
            sub = await self.run_agent(
                agent_name=child_name, context=child_context, channel=ctx.channel,
                mock=None,  # mocks do not cross the delegation boundary (sub runs live)
                depth=next_depth, parent_decision_id=ctx.parent_decision_id,
                workflow_run_id=ctx.workflow_run_id,
            )
        except Exception as e:
            return _builtin_err("delegate_to_agent", tool_input, f"Sub-agent raised: {type(e).__name__}: {e}")

        self.tracer.emit(ExecutionEvent(type=EventType.DELEGATION_FINISHED, depth=ctx.decision_depth,
                                        detail={"child": child_name, "status": sub.status.value}))
        return {
            "tool_name": "delegate_to_agent", "call_order": 0, "input_data": tool_input,
            "transport": "verity_builtin", "error": sub.status != RunStatus.COMPLETE,
            "output_data": {
                "sub_decision_log_id": str(sub.decision_log_id) if sub.decision_log_id else None,
                "sub_status": sub.status.value, "output": sub.output,
                "sub_input_tokens": sub.usage.input_tokens, "sub_output_tokens": sub.usage.output_tokens,
            },
        }

    def _delegate_ctx(self, pkg, decision_id, depth, workflow_run_id, channel) -> DelegateContext:
        return DelegateContext(parent_decision_id=decision_id, decision_depth=depth,
                               workflow_run_id=workflow_run_id, channel=channel,
                               parent_agent_name=pkg.name)

    # ──────────────────────────────────────────────────────────────
    # MODEL CALL — wraps the chain, threads trace events
    # ──────────────────────────────────────────────────────────────
    async def _model_call(self, pkg, system, messages, tools, force_tool, depth) -> ModelResponse:
        def on_event(kind, **detail):
            self.tracer.chain_event(kind, depth=depth, **detail)
        return await self.chain.complete(
            chain=pkg.inference.chain, system=system, messages=messages,
            tools=tools or None, force_tool=force_tool, on_event=on_event,
            fallback_enabled=self._effective_fallback(pkg),
        )

    def _effective_fallback(self, pkg: Package) -> bool:
        """Resolve the two-tier fallback rule.

        Global OFF  → no fallback regardless of package setting.
        Global ON + package unset  → no fallback (explicit opt-in required).
        Global ON + package True   → fall through the chain on exhausted retries.
        Global ON + package False  → primary-only (package explicitly disables).
        """
        if not self.global_fallback_enabled:
            return False
        if pkg.inference.fallback_enabled is None:
            return False
        return pkg.inference.fallback_enabled

    # ──────────────────────────────────────────────────────────────
    # helpers
    # ──────────────────────────────────────────────────────────────
    def _require_package(self, name: str, kind: EntityKind) -> Package:
        pkg = self.packages.get(name)
        if pkg is None:
            raise ValueError(f"No package registered for {name!r}. Known: {sorted(self.packages)}")
        if pkg.kind != kind:
            raise ValueError(f"Package {name!r} is a {pkg.kind.value}, not a {kind.value}")
        return pkg

    def _new_assembler(self, pkg, run_id, channel, input_data, mock, depth,
                       parent_decision_id=None, decision_id=None) -> DecisionAssembler:
        rec = DecisionRecord(
            id=decision_id or uuid4(), entity_kind=pkg.kind.value, entity_name=pkg.name,
            entity_version=pkg.version, channel=channel, mock_mode=bool(mock and mock.active),
            execution_run_id=run_id, parent_decision_id=parent_decision_id, decision_depth=depth,
            input_json=input_data, application=self.application,
            model_chain=[m.model_dump() for m in pkg.inference.chain],
        )
        return DecisionAssembler(rec)

    async def _finish(self, assembler, output, reasoning, status, error, duration, run_id, depth) -> ExecutionResult:
        rec = assembler.finish(output=output, reasoning=reasoning, status=status.value,
                               error=error, duration_ms=duration)
        decision_id = await self.sink.write(assembler)
        self.tracer.emit(ExecutionEvent(type=EventType.DECISION_LOGGED, run_id=run_id, depth=depth,
                                        detail={"decision_id": str(decision_id), "status": status.value}))
        self.tracer.emit(ExecutionEvent(type=EventType.RUN_COMPLETE, run_id=run_id, depth=depth,
                                        detail={"status": status.value,
                                                "tokens": rec.input_tokens + rec.output_tokens}))
        return ExecutionResult(
            run_id=run_id, entity_kind=rec.entity_kind, entity_name=rec.entity_name, status=status,
            output=output if isinstance(output, dict) else {"value": output}, reasoning_text=reasoning,
            decision_log_id=decision_id, duration_ms=duration,
            usage=_usage_from(rec),
        )

    async def _fail(self, assembler, exc, started, run_id, depth) -> ExecutionResult:
        logger.exception("run failed: %s", run_id)
        duration = int((time.monotonic() - started) * 1000)
        assembler.finish(output={}, reasoning=None, status="failed", error=str(exc), duration_ms=duration)
        try:
            decision_id = await self.sink.write(assembler)
        except Exception:
            decision_id = None
        self.tracer.emit(ExecutionEvent(type=EventType.RUN_FAILED, run_id=run_id, depth=depth,
                                        detail={"error": f"{type(exc).__name__}: {exc}"}))
        return ExecutionResult(
            run_id=run_id, entity_kind=assembler.record.entity_kind, entity_name=assembler.record.entity_name,
            status=RunStatus.FAILED, output={}, decision_log_id=decision_id, duration_ms=duration,
            error_message=str(exc),
        )


def _first_user_message(pkg, user, fallback_input: dict, src_ctx: dict) -> Message:
    """Build the opening user message, appending image/document blocks from binary sources."""
    text = user if isinstance(user, str) else json.dumps(fallback_input)
    blocks: list = [TextBlock(text=text)]
    for src in pkg.sources:
        if not src.as_block:
            continue
        meta = src_ctx.get(src.bind_to)
        if not isinstance(meta, dict) or "_as_block" not in meta:
            continue
        if meta["_as_block"] == "image":
            blocks.append(ImageBlock(
                media_type=meta["_media_type"],
                data_b64=meta["_data_b64"],
                title=meta.get("_title"),
            ))
        elif meta["_as_block"] == "document":
            blocks.append(DocumentBlock(
                media_type=meta["_media_type"],
                data_b64=meta["_data_b64"],
                title=meta.get("_title"),
            ))
    return Message(role="user", content=blocks)


def _try_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        text = "\n".join(l for l in text.splitlines() if not l.startswith("```")).strip()
    try:
        return json.loads(text)
    except Exception:
        return {"raw_output": text}


def _builtin_err(name, tool_input, message) -> dict:
    return {"tool_name": name, "call_order": 0, "input_data": tool_input,
            "output_data": {"error": message}, "transport": "verity_builtin", "error": True}


def _usage_from(rec) -> Any:
    from harness.core.ir import Usage
    return Usage(input_tokens=rec.input_tokens, output_tokens=rec.output_tokens,
                 cache_read_tokens=rec.cache_read_tokens, cache_write_tokens=rec.cache_write_tokens)


__all__ = ["ExecutionEngine", "MAX_DECISION_DEPTH"]
