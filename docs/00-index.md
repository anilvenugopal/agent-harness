# Agent Harness — Learning Curriculum

Work through modules in order. Appendices are reference material — read them
when a module points you there, or when you want depth on a specific topic.

---

## Modules

| # | Title | Key file(s) |
|---|---|---|
| [01](01-the-problem.md) | The Problem This Project Solves | — |
| [02](02-llm-tool-use.md) | How LLM Tool Use Actually Works | `harness/core/ir.py` |
| [03](03-packages.md) | Packages — Declaring What an Agent Does | `harness/core/package.py`, `packages/*.yaml` |
| [04](04-execution-engine.md) | The Execution Engine — The Loop | `harness/core/engine.py` |
| [05](05-providers-and-chain.md) | Providers and the Model Chain | `harness/providers/` |
| [06](06-tool-gateway.md) | The Tool Gateway | `harness/tools/gateway.py` |
| [07](07-connectors.md) | Connectors — Sources and Targets | `harness/connectors/` |
| [08](08-hitl.md) | HITL — Human in the Loop | `harness/hitl/continuation.py` |
| [09](09-decision-log.md) | The Decision Log — Governance Record | `harness/decisions/assembler.py` |
| [10](10-worker.md) | The Worker — Distributed Dispatch | `harness/worker/worker.py` |
| [11](11-end-to-end.md) | End-to-End Walkthrough | all of the above |

---

## Appendices

| # | Title | When to read |
|---|---|---|
| [A01](appendix-01-agent-loop-basics.md) | Agent Loop Basics — Raw Claude, Gemini, OpenAI | After Module 02, to see what the harness replaces |
| [A02](appendix-02-async-python.md) | Async Python from First Principles | Any time — when `async/await` in the engine feels unclear |
| [A03](appendix-03-cope-underwriting.md) | COPE Underwriting — Rating, Binding, and Referral | After Module 11, or whenever the underwriting demo needs domain context |

---

## Reading map

```
01 (why) → 02 (IR + tool use wire format)
                │
                ├─► A01 (what the raw loop looks like without a harness)
                │
                ▼
           03 (packages — what you declare)
                │
                ▼
           04 (engine — how it executes)
                │
          ┌─────┼─────────────────────┐
          ▼     ▼                     ▼
         05    06                    07
      (chain) (tools)           (connectors)
          │     │                     │
          └─────┴──────────┬──────────┘
                           ▼
                      08 (HITL)
                           │
                           ▼
                      09 (decision log)
                           │
                           ▼
                      10 (worker)
                           │
                           ▼
                      11 (end-to-end)
```

`A02` (async Python) can be read at any point — it is most useful before or
alongside Module 04 when the `async/await` in the engine loop becomes relevant.
