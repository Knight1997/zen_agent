# Zen Agent

A multi-step **ReAct-style agentic workflow** that answers analytical questions
using tools, with a **separate self-correction LLM pass** that verifies
arithmetic, logical consistency, and source grounding — and re-runs steps when
something is wrong. LLM inference runs locally through **Ollama**.

## What it does

- **ReAct loop with structured output.** The agent emits
  `Thought → Action → Action Input` steps, receives an `Observation`, and decides
  the next step dynamically (no hardcoded tool sequence). Parsing is regex-based
  with a JSON extractor and graceful fallbacks.
- **Three tools** (the agent chooses which to call each step):
  1. `calculator` — a safe arithmetic *code executor* built on Python's `ast`
     (no `eval`); supports `+ - * / ** % //`, `sqrt`, `log`, `abs`, `round`, …
  2. `knowledge_lookup` — grounded fact lookup against a local knowledge base
     (populations, physical constants) that returns a **source label**.
  3. `unit_converter` — converts between units of the same dimension
     (length / volume / mass). *(the "tool of our choice")*
- **Graceful tool-error handling.** Tools never raise into the loop; failures
  come back as `Observation: ERROR ...` so the agent can recover. Unknown tools,
  bad inputs, division by zero, and unparseable model output are all handled.
- **Self-correction.** A *separate* verifier LLM call judges the answer and
  emits a structured verdict. It is backed by a **deterministic arithmetic /
  grounding re-check** so verification doesn't depend solely on the small model.
  If either flags an issue, the agent re-runs with the verifier's guidance.

## Project layout

```
zen_agent/
  llm.py           # stdlib Ollama /api/chat client
  tools.py         # calculator (ast), knowledge_lookup, unit_converter
  agent.py         # ReAct loop + structured thought/action/action_input parsing
  self_correct.py  # separate verifier LLM call + deterministic re-check
  runner.py        # solve(): agent -> verify -> re-run with guidance loop
run.py             # runs 3 queries, writes traces/, generates RUN_REPORT.md
traces/            # per-query .log (human) and .json (machine) traces
RUN_REPORT.md      # auto-generated narrative of the latest run
```

## Prerequisites

```bash
# Install Ollama from https://ollama.com/download, then:
ollama pull llama3.2:3b      # ~2 GB; default model (follows ReAct reliably)
ollama serve                  # usually already running on :11434

# Optional lighter fallback (lower quality, triggers more self-corrections):
# ollama pull llama3.2:1b   and run with  ZEN_MODEL=llama3.2:1b python3 run.py
```

No `pip install` is required — the project uses only the Python standard library.

## Run

```bash
python3 run.py
```

This executes three queries of increasing complexity (simple → medium →
complex), prints the live ReAct trace, writes per-query logs to `traces/`, and
regenerates `RUN_REPORT.md`.

Configure via environment variables:

```bash
ZEN_MODEL=llama3.2:3b OLLAMA_HOST=http://localhost:11434 python3 run.py
```

## The three test queries

| Complexity | Question (abbreviated) | Expected tools |
|-----------|------------------------|----------------|
| Simple    | `sqrt(1024) + 17`      | `calculator` |
| Medium    | Germany pop − France pop | `knowledge_lookup` ×2, `calculator` |
| Complex   | (FR+DE) × 2 L/day → gallons | `knowledge_lookup` ×3, `calculator`, `unit_converter` |

## Robustness mechanisms

Small local models drift from the ReAct format, repeat calls, and occasionally
mangle digits when writing the final answer. The agent defends against this:

- **Stop token + format nudges.** `Observation:` is a stop token so the model
  can't hallucinate tool output; malformed turns are fed back with a format
  reminder.
- **Loop guard.** Identical repeated tool calls are flagged, and after three
  repeats the loop breaks and synthesizes an answer.
- **Grounded fallback.** If the model can't articulate a clean `Final Answer`,
  the agent answers directly from the actual tool observations (never an
  invented number).
- **Deterministic verifier.** Independent of the (fallible) verifier LLM, it
  re-evaluates every calculator expression and flags any number in the final
  answer that no tool produced — this is what catches hallucinated/transposed
  digits.
- **Best-attempt selection.** Across the initial answer and any corrections, the
  highest-scoring (deterministically clean, verifier-approved, numeric) answer
  is chosen, so a bad re-run can never replace a good answer.

`llama3.2:1b` also works as a fallback but produces messier traces and more
frequent (sometimes spurious) self-corrections — useful for stress-testing the
error-handling paths.
