"""Self-correction layer.

Runs a *separate* LLM call that verifies the agent's answer along three axes:
arithmetic correctness, logical consistency, and source grounding. It is backed
by a deterministic arithmetic re-check so verification does not depend solely on
the (small) model's reliability. When a problem is found, the caller re-runs the
agent with the verifier's guidance.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .agent import AgentResult
from .llm import LLMClient
from .tools import safe_eval

VERIFIER_SYSTEM = """You are a strict verification agent. You did NOT solve the problem; \
you only check someone else's work. Given a question, the step-by-step trace, and a final answer, \
judge whether the final answer is correct.

Check three things:
1. Arithmetic: trust the `calculator` Observations — they are always arithmetically correct, \
so NEVER claim a calculator Observation's result is wrong. Only flag arithmetic if a number in \
the Final Answer was never produced by a calculator Observation.
2. Logical consistency: do the steps actually answer the question that was asked?
3. Source grounding: is every fact/number in the Final Answer backed by an Observation \
(knowledge_lookup or calculator), and copied exactly (no altered digits)?

If the Final Answer's numbers all match the tool Observations and answer the question, say CORRECT.

Respond with ONLY a JSON object on a single line, no prose:
{"verdict": "CORRECT" or "INCORRECT", "issues": ["short issue", ...], "guidance": "concrete fix instruction, or empty string if correct"}"""


@dataclass
class Verification:
    verdict: str  # "CORRECT" or "INCORRECT"
    issues: list[str] = field(default_factory=list)
    guidance: str = ""
    llm_raw: str = ""
    deterministic_notes: list[str] = field(default_factory=list)

    @property
    def is_correct(self) -> bool:
        return self.verdict.upper() == "CORRECT"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "issues": self.issues,
            "guidance": self.guidance,
            "deterministic_notes": self.deterministic_notes,
            "llm_raw": self.llm_raw,
        }


_NUM_RE = re.compile(r"-?\d[\d,]*\.?\d*")


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for token in _NUM_RE.findall(text or ""):
        cleaned = token.replace(",", "")
        try:
            out.append(float(cleaned))
        except ValueError:
            continue
    return out


def _render_trace(result: AgentResult) -> str:
    lines: list[str] = []
    for i, s in enumerate(result.steps, 1):
        if s.thought:
            lines.append(f"[{i}] Thought: {s.thought}")
        if s.action:
            lines.append(f"    Action: {s.action} Input: {json.dumps(s.action_input)}")
        if s.observation:
            lines.append(f"    Observation: {s.observation}")
        if s.final_answer:
            lines.append(f"    Final Answer: {s.final_answer}")
    return "\n".join(lines)


def _deterministic_check(result: AgentResult) -> list[str]:
    """Independent, non-LLM verification of arithmetic and grounding.

    - Re-evaluates every calculator expression and flags any mismatch.
    - Flags "large" numbers in the final answer that no tool ever produced
      (a heuristic signal of fabricated / ungrounded arithmetic).
    """
    notes: list[str] = []
    observed_numbers: set[float] = set()

    for step in result.steps:
        if step.observation:
            observed_numbers.update(_numbers(step.observation))
        # Re-verify calculator arithmetic deterministically.
        if step.action == "calculator" and step.observation and "=" in step.observation:
            expr = step.observation.split("=", 1)[0].strip()
            try:
                expected = safe_eval(expr)
            except Exception:
                continue
            claimed = _numbers(step.observation.split("=", 1)[1])
            if claimed and abs(claimed[0] - expected) > max(1e-6, abs(expected) * 1e-6):
                notes.append(
                    f"arithmetic mismatch: '{expr}' should be {expected}, "
                    f"trace shows {claimed[0]}"
                )

    # Grounding heuristic on the final answer's numbers.
    if result.final_answer:
        for num in _numbers(result.final_answer):
            if abs(num) < 1000:
                continue  # small numbers are often restated context, skip
            grounded = any(
                abs(num - obs) <= max(1.0, abs(obs) * 1e-3) for obs in observed_numbers
            )
            if not grounded:
                notes.append(
                    f"final answer cites {num:g} which no tool observation produced "
                    "(possible ungrounded number)"
                )
    return notes


def _parse_verdict(raw: str) -> tuple[str, list[str], str]:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            verdict = str(data.get("verdict", "")).upper()
            issues = data.get("issues") or []
            if isinstance(issues, str):
                issues = [issues]
            guidance = str(data.get("guidance", ""))
            # Small models sometimes echo the prompt's schema text verbatim;
            # discard such non-actionable "guidance".
            if "empty string if correct" in guidance or "concrete instruction for a retry," in guidance:
                guidance = ""
            if verdict in ("CORRECT", "INCORRECT"):
                return verdict, [str(i) for i in issues], guidance
        except json.JSONDecodeError:
            pass
    # Fallback: scan for keywords.
    upper = raw.upper()
    if "INCORRECT" in upper:
        return "INCORRECT", ["verifier flagged an issue (unstructured output)"], raw.strip()[:200]
    return "CORRECT", [], ""


def verify(result: AgentResult, llm: LLMClient, logger=None) -> Verification:
    """Run the separate verification LLM call plus the deterministic re-check."""
    trace = _render_trace(result)
    user = (
        f"Question: {result.question}\n\n"
        f"Trace:\n{trace}\n\n"
        f"Final Answer: {result.final_answer}\n\n"
        "Verify the final answer now and respond with the JSON object."
    )
    raw = llm.chat(system=VERIFIER_SYSTEM, user=user)
    verdict, issues, guidance = _parse_verdict(raw)

    deterministic_notes = _deterministic_check(result)
    # The deterministic checker can veto a too-lenient LLM verdict.
    if deterministic_notes:
        verdict = "INCORRECT"
        issues = list(issues) + deterministic_notes
        det_guidance = (
            "Recompute carefully. Issues found: " + "; ".join(deterministic_notes)
        )
        guidance = (guidance + " " + det_guidance).strip() if guidance else det_guidance

    v = Verification(
        verdict=verdict,
        issues=issues,
        guidance=guidance,
        llm_raw=raw,
        deterministic_notes=deterministic_notes,
    )
    if logger:
        logger(f"  verifier verdict={v.verdict} issues={v.issues}")
    return v
