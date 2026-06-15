# Module 01 — The Problem This Project Solves

## What is Verity?

Verity is a **governance platform for AI decisions** — specifically for high-stakes domains
like insurance underwriting. When an AI model makes a consequential decision (approve a
loan, decline a claim, issue a policy), regulators, auditors, and the business all need
to know:

- **Who** made the decision (which model, which version)
- **What** it was given as input
- **Why** it decided what it decided (reasoning, tool calls, intermediate steps)
- **When** a human was required to approve
- **What** the final output was, and where it was written

This is not just logging. It is a **legally defensible audit trail** — a requirement in
regulated industries.

---

## The core tension: AI models vs. governance

A raw API call to Claude looks like this:

```
Your app ──► Claude API ──► Response
```

That's fine for a chatbot. It is not fine for underwriting a $500,000 insurance policy,
because:

- You don't know which model responded (different model versions may exist under the hood)
- You have no record of what tools were called or what data was fetched
- There is no human approval gate before a consequential action is taken
- If Claude is unavailable, your process stops — there is no fallback
- You cannot reproduce the run later for an audit

---

## What an "agent" is

A regular LLM call is **one shot**: you send a prompt, you get a response. Done.

An **agent** is a loop. The model can say "I need more information — call this tool"
and the loop runs the tool, sends the result back to the model, and asks again. This
repeats until the model says "I'm done" or hits a limit.

```
┌─────────────────────────────────────────────────────┐
│                   AGENT LOOP                        │
│                                                     │
│  ┌──────────┐    "call tool X"    ┌──────────────┐  │
│  │  Model   │ ──────────────────► │     Tool     │  │
│  │ (Claude/ │                     │  (Postgres,  │  │
│  │  Gemini) │ ◄────────────────── │   MCP, etc.) │  │
│  └──────────┘    tool result      └──────────────┘  │
│       │                                             │
│       │ "end_turn" (done)                           │
│       ▼                                             │
│    Final Answer                                     │
└─────────────────────────────────────────────────────┘
```

This loop can go many turns. Each turn costs tokens, time, and money. Each tool call
is a side effect (a database read, an API call, a file write). All of it needs to be
governed.

---

## Why not just call Claude directly from your app?

You could. But then every team that wants to use an AI agent has to:

1. Implement the loop themselves (non-trivial)
2. Handle tool routing, auth, errors
3. Build their own audit trail (expensive, inconsistent)
4. Handle model fallbacks (Claude is down → use Gemini)
5. Implement HITL (human-in-the-loop) gates
6. Wire up Postgres, S3, MCP, etc.
7. Reproduce a past run for an audit

This is the same infrastructure problem solved over and over. The **harness** solves it once.

---

## What the harness is

The harness is a **provider-agnostic execution engine** that sits between your application
and any AI model. It owns:

```
┌────────────────────────────────────────────────────────────────┐
│                        YOUR APPLICATION                        │
│                   (underwriting service, etc.)                 │
└────────────────────────────┬───────────────────────────────────┘
                             │  "run this agent with this input"
                             ▼
┌────────────────────────────────────────────────────────────────┐
│                      AGENT HARNESS                             │
│                                                                │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────────────┐  │
│  │  Execution  │  │   Provider   │  │    Tool Gateway       │  │
│  │   Engine    │  │    Chain     │  │ (python / MCP / HITL) │  │
│  │  (the loop) │  │ Claude - GPT │  └───────────────────────┘  │
│  │             │  │  - Gemini    │  ┌───────────────────────┐  │
│  └─────────────┘  └──────────────┘  │   Connector Binder    │  │
│                                     │   (Postgres / S3)     │  │
│  ┌──────────────────────────────┐   └───────────────────────┘  │
│  │      Decision Assembler      │   ┌───────────────────────┐  │
│  │  (canonical governance log)  │   │   HITL Continuation   │  │
│  └──────────────────────────────┘   │   (suspend / resume)  │  │
│                                     └───────────────────────┘  │
└────────────────────────────────────────────────────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
           Claude          Gemini      (OpenAI)
            API             API          API
```

**What each box does:**

| Box | What it is | File |
|-----|-----------|------|
| **Execution Engine** | The loop itself. Drives turns: call model → check stop reason → dispatch tools → repeat. Knows nothing about which model or which tool — it only reads neutral messages. | `harness/core/engine.py` |
| **Provider Chain** | The ordered list of model providers. Tries Claude first; if it fails or is unavailable, falls back to Gemini, then OpenAI. Every provider translates to/from the same neutral message format. | `harness/providers/` |
| **Tool Gateway** | The router for tool calls. Receives a tool name + inputs from the engine, figures out where to send it (Python function? MCP server? built-in delegation?), enforces auth, returns a result. | `harness/tools/gateway.py` |
| **Connector Binder** | Handles data movement. Before the loop: fetches source data (e.g. pulls applicant row from Postgres, binds it into the prompt context). After the loop: writes output to a target (e.g. saves decision JSON to S3). | `harness/connectors/binder.py` |
| **Decision Assembler** | Accumulates everything that happened during a run — inputs, outputs, model used, tokens, tool calls, source reads, target writes — and writes it as a single governance record at the end. | `harness/decisions/assembler.py` |
| **HITL Continuation** | When a human-approval gate fires, this serializes the full loop state to disk and raises a suspension. When the human decides, it loads the state back and hands it to the engine to resume. | `harness/hitl/continuation.py` |

**Key insight:** The engine is identical whether Claude, Gemini, or the mock provider answers each turn. The providers are swappable. The governance record is always the same shape, regardless of which model ran.

---

## The three example scenarios in this repo

| Package | Kind | What it demonstrates |
|---|---|---|
| `classify_document` | **Task** | Single-turn, no tools, forced structured output |
| `underwriting_agent` | **Agent** | Full loop: Postgres source, python tool, MCP tool, delegation, HITL, S3 target |
| `research_subagent` | **Agent** | Delegation target — called by underwriting_agent, writes its own decision record |

### OBSERVATION:
When we ran `classify_document` live with both Claude and Gemini. The interesting divergence (Claude said "complaint", Gemini said "claim") happened because the model is not deterministic, and the harness captured both runs identically — same format,
same governance fields.

---

## The HITL gate explained

When a HITL gate fires mid-loop (e.g., Turn 3, model wants to call `issue_binder`),
the engine does **not** block and wait. Instead:

```
Turn 3: model says "call issue_binder"
                    │
                    ▼
          Is issue_binder gated?  YES
                    │
                    ▼
     Serialize entire loop state to disk:
       - full message history so far
       - the pending tool call + its inputs
       - which turn we're on, the run ID, channel
                    │
                    ▼
     Raise HITLSuspended exception → worker catches it
     → Postgres row marked "suspended"
     → process is FREED (no thread sitting idle)
```

Hours or days can pass. A human reviews the pending tool call, then records a
decision (approve / deny / edit the inputs). When they do:

```
Human records: approve
                    │
                    ▼
     Load serialized state from disk
                    │
                    ▼
     Rebuild loop: restore messages, turn counter, mock context
                    │
                    ▼
     Execute issue_binder with approved inputs
                    │
                    ▼
     Continue from Turn 4 onward → final answer
```

Think of it as a **save game**: the agent serializes itself, closes, and picks up
exactly where it left off when the human acts.

One important constraint: **sub-agents cannot be suspended**. If a sub-agent (depth > 0)
tries to call a gated tool, the engine denies it cleanly. Reason: the parent agent
is synchronously waiting for the sub-agent to return — suspending the sub-agent would
block the parent forever. This limitation is called out explicitly in the design docs.

> Full code walkthrough of `HITLSuspended`, `Continuation`, and `FileContinuationStore`
> comes in a later module (HITL deep dive).

---

## The key vocabulary you need for every document that follows

| Term | Meaning |
|---|---|
| **Package** | A YAML file that declares what an agent/task does: which model, which tools, which sources/targets |
| **Task** | Single-turn: one prompt in, one structured answer out |
| **Agent** | Multi-turn loop with tools, delegation, HITL |
| **Provider** | A model API (Anthropic/Claude, Google/Gemini, OpenAI) |
| **Chain** | The ordered list of providers to try: if #1 fails, try #2 |
| **Tool** | Something the model can call: a Python function, an MCP endpoint, or a built-in like `delegate_to_agent` |
| **HITL** | Human-in-the-loop: a gate where a human must approve before a tool executes |
| **Decision log** | The canonical governance record written at the end of every run |
| **Continuation** | The serialized state of a suspended agent run, waiting for a human decision |
| **Mock context** | A seam that lets you run the full engine with scripted responses instead of real APIs |

---

## Checkpoint — before moving on to the next

Answer these in your own words (no looking):

1. What is the difference between a **task** and an **agent** in this project?
2. Why does the harness use a **chain** of providers instead of calling Claude directly?
3. What is a **decision log** and why does it exist?
4. What happens when a HITL gate fires — what does "suspend" mean?

When you can answer these, we move to **Module 02: How LLM Tool Use Actually Works**
(the request/response cycle, what a tool call looks like on the wire, and why the
loop needs to exist at all).