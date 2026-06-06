"""Entry point: run three queries of increasing complexity through the agent,
write per-query trace logs, and generate RUN_REPORT.md.

Usage:
    python3 run.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

from zen_agent.llm import DEFAULT_MODEL, LLMClient, LLMError
from zen_agent.runner import SolveOutcome, solve

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE_DIR = os.path.join(HERE, "traces")

# Three queries of increasing complexity.
QUERIES = [
    {
        "id": "q1_simple",
        "complexity": "simple (1-2 tool calls)",
        "question": "What is the square root of 1024, plus 17?",
    },
    {
        "id": "q2_medium",
        "complexity": "medium (2-3 tool calls)",
        "question": (
            "How many more people live in Germany than in France? "
            "Use the knowledge base for both populations."
        ),
    },
    {
        "id": "q3_complex",
        "complexity": "complex (3+ tool calls)",
        "question": (
            "If every person in France and Germany combined drinks the daily "
            "recommended amount of water, how many total liters are needed per day, "
            "and how many US gallons is that? Use the knowledge base for the "
            "populations and the daily water amount."
        ),
    },
]


def make_logger(buffer: list[str]):
    def _log(msg: str) -> None:
        print(msg)
        buffer.append(msg)
    return _log


def write_trace(query: dict, outcome: SolveOutcome, log_lines: list[str]) -> None:
    os.makedirs(TRACE_DIR, exist_ok=True)
    base = os.path.join(TRACE_DIR, query["id"])
    with open(base + ".log", "w", encoding="utf-8") as fh:
        fh.write("\n".join(log_lines) + "\n")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(
            {"query": query, "outcome": outcome.to_dict()}, fh, indent=2, ensure_ascii=False
        )


def _fmt_steps(result) -> list[str]:
    lines = []
    for i, s in enumerate(result.steps, 1):
        if s.thought:
            lines.append(f"  {i}. Thought: {s.thought}")
        if s.action:
            lines.append(f"     Action: `{s.action}`  Input: `{json.dumps(s.action_input)}`")
        if s.observation:
            lines.append(f"     Observation: {s.observation}")
        if s.final_answer:
            lines.append(f"     **Final Answer: {s.final_answer}**")
    return lines


def generate_report(results: list[tuple[dict, SolveOutcome]], model: str, elapsed: float) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    out: list[str] = []
    out.append("# Zen Agent — Run Report")
    out.append("")
    out.append(f"- **Generated:** {now}")
    out.append(f"- **Model (Ollama):** `{model}`")
    out.append(f"- **Total queries:** {len(results)}")
    out.append(f"- **Wall-clock time:** {elapsed:.1f}s")
    out.append("")
    out.append(
        "This report is produced automatically by `run.py`. It documents what the "
        "agent did on each query: the ReAct trace (thought → action → observation), "
        "the separate self-correction verification, and any re-runs that were "
        "triggered."
    )
    out.append("")
    out.append(
        "**How to read the verdicts:** each answer is checked by two independent "
        "verifiers — (1) a *separate LLM call* that judges arithmetic, logical "
        "consistency, and source grounding, and (2) a *deterministic* checker that "
        "re-evaluates every calculator expression and flags any number in the final "
        "answer that no tool produced. The small LLM verifier is noisy and often "
        "raises spurious `INCORRECT` verdicts, so the deterministic checker is "
        "authoritative for arithmetic/grounding. Across the initial answer and any "
        "re-runs, a **best-attempt selector** keeps the highest-scoring (grounded, "
        "numeric) answer — which is why a query can show `INCORRECT` verdicts yet "
        "still surface a correct final answer."
    )
    out.append("")

    # Summary table.
    out.append("## Summary")
    out.append("")
    out.append("| Query | Complexity | Attempts | Self-corrected | Final answer |")
    out.append("|-------|------------|----------|----------------|--------------|")
    for q, outcome in results:
        ans = (outcome.final_answer or "—").replace("\n", " ")
        if len(ans) > 60:
            ans = ans[:57] + "..."
        out.append(
            f"| `{q['id']}` | {q['complexity']} | {len(outcome.attempts)} | "
            f"{'yes' if outcome.corrected else 'no'} | {ans} |"
        )
    out.append("")

    # Per-query detail.
    for q, outcome in results:
        out.append(f"## {q['id']} — {q['complexity']}")
        out.append("")
        out.append(f"**Question:** {q['question']}")
        out.append("")
        tools_used = sorted(
            {
                s.action
                for a in outcome.attempts
                for s in a.result.steps
                if s.action
            }
        )
        out.append(f"**Tools exercised:** {', '.join(f'`{t}`' for t in tools_used) or 'none'}")
        out.append("")
        for attempt in outcome.attempts:
            tag = "Initial attempt" if attempt.index == 0 else f"Correction attempt #{attempt.index}"
            if attempt.index == outcome.chosen_index:
                tag += "  ← accepted as final"
            out.append(f"### {tag}")
            if attempt.guidance_used:
                out.append("")
                out.append(f"> Re-run triggered with guidance: _{attempt.guidance_used}_")
            out.append("")
            out.extend(_fmt_steps(attempt.result))
            out.append("")
            v = attempt.verification
            out.append(f"- **Verifier verdict:** `{v.verdict}`")
            if v.issues:
                out.append(f"- **Issues:** {'; '.join(v.issues)}")
            if v.deterministic_notes:
                out.append(f"- **Deterministic check:** {'; '.join(v.deterministic_notes)}")
            out.append("")

    # Self-correction spotlight: any query where the verifier rejected the
    # initial answer and triggered at least one re-run.
    rerun = [(q, o) for q, o in results if len(o.attempts) > 1]
    out.append("## Self-correction before/after")
    out.append("")
    if not rerun:
        out.append(
            "_No query triggered a re-run on this run (the verifier accepted every "
            "initial answer). Re-run `python3 run.py` to see the correction path._"
        )
    else:
        for q, outcome in rerun:
            before = outcome.attempts[0]
            accepted = outcome.attempts[outcome.chosen_index]
            out.append(f"### `{q['id']}`")
            out.append("")
            out.append(f"- **Before (initial attempt):** {before.result.final_answer}")
            out.append(
                f"  - Verifier verdict: `{before.verification.verdict}` — "
                f"issues: {'; '.join(before.verification.issues) or 'n/a'}"
            )
            if outcome.chosen_index > 0:
                out.append(
                    f"- **After (accepted correction attempt #{outcome.chosen_index}):** "
                    f"{accepted.result.final_answer}"
                )
                out.append(f"  - Verifier verdict: `{accepted.verification.verdict}`")
                det = accepted.verification.deterministic_notes
                out.append(
                    "  - The re-run scored higher — its answer is "
                    + ("deterministically clean (every number is grounded in a tool "
                       "observation, no arithmetic errors)" if not det
                       else "preferred by the scorer")
                    + ", so it replaced the initial (ungrounded) answer."
                )
            else:
                out.append(
                    f"- **After:** the re-run(s) did not improve on the initial "
                    f"answer, so the best-attempt selector kept the initial "
                    f"answer (`{accepted.result.final_answer}`). This shows the "
                    "guard that prevents a bad correction from degrading a good answer."
                )
            out.append("")
    out.append("---")
    out.append("")
    out.append("Trace logs and machine-readable JSON for each query are in `traces/`.")
    out.append("")
    return "\n".join(out)


def main() -> int:
    llm = LLMClient(model=DEFAULT_MODEL)
    print(f"Using Ollama model: {llm.model} @ {llm.host}\n")

    results: list[tuple[dict, SolveOutcome]] = []
    start = time.time()
    for query in QUERIES:
        print("=" * 72)
        print(f"QUERY {query['id']} [{query['complexity']}]")
        print(f"  {query['question']}")
        print("=" * 72)
        buffer: list[str] = []
        logger = make_logger(buffer)
        try:
            outcome = solve(
                query["question"], llm, max_corrections=1, max_steps=8, logger=logger
            )
        except LLMError as exc:
            print(f"\nFATAL: {exc}", file=sys.stderr)
            return 1
        write_trace(query, outcome, buffer)
        results.append((query, outcome))
        print(f"\n>>> FINAL ({query['id']}): {outcome.final_answer}\n")

    elapsed = time.time() - start
    report = generate_report(results, llm.model, elapsed)
    report_path = os.path.join(HERE, "RUN_REPORT.md")
    with open(report_path, "w", encoding="utf-8") as fh:
        fh.write(report)
    print(f"Report written to {report_path}")
    print(f"Traces written to {TRACE_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
