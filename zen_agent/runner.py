"""Orchestration: run the agent, verify, and self-correct in a loop."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .agent import AgentResult, run_agent
from .llm import LLMClient
from .self_correct import Verification, verify


@dataclass
class Attempt:
    index: int
    result: AgentResult
    verification: Verification
    guidance_used: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "guidance_used": self.guidance_used,
            "result": self.result.to_dict(),
            "verification": self.verification.to_dict(),
        }


@dataclass
class SolveOutcome:
    question: str
    attempts: list[Attempt] = field(default_factory=list)
    final_answer: str | None = None
    corrected: bool = False
    chosen_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "final_answer": self.final_answer,
            "corrected": self.corrected,
            "chosen_index": self.chosen_index,
            "num_attempts": len(self.attempts),
            "attempts": [a.to_dict() for a in self.attempts],
        }


_NUM = __import__("re").compile(r"\d")


def _attempt_score(attempt: Attempt) -> float:
    """Higher is better. A deterministically-clean, grounded, verifier-approved
    answer beats a flagged one, so a bad rerun can never replace a good answer.
    """
    v = attempt.verification
    answer = attempt.result.final_answer or ""
    # Our queries are numeric; an answer with no number is not useful, so it
    # can never out-score a real numeric answer.
    if not _NUM.search(answer):
        return 0.0
    score = 1.0  # produced a concrete numeric answer
    if not v.deterministic_notes:
        score += 100.0  # no arithmetic / grounding violations found
    if v.is_correct:
        score += 10.0  # the separate LLM verifier approved it
    return score


def solve(
    question: str,
    llm: LLMClient,
    max_corrections: int = 2,
    max_steps: int = 8,
    logger: Callable[[str], None] | None = None,
) -> SolveOutcome:
    """Run the agent, verify, and re-run with guidance until correct or budget hit."""
    outcome = SolveOutcome(question=question)
    guidance: str | None = None

    for attempt_idx in range(max_corrections + 1):
        if logger:
            tag = "INITIAL" if attempt_idx == 0 else f"CORRECTION #{attempt_idx}"
            logger(f"\n=== Attempt {attempt_idx + 1} ({tag}) ===")
            if guidance:
                logger(f"  guidance: {guidance}")

        result = run_agent(
            question, llm, max_steps=max_steps, extra_guidance=guidance, logger=logger
        )
        verification = verify(result, llm, logger=logger)
        outcome.attempts.append(
            Attempt(
                index=attempt_idx,
                result=result,
                verification=verification,
                guidance_used=guidance,
            )
        )
        outcome.final_answer = result.final_answer

        if verification.is_correct:
            break
        # Prepare guidance for the next attempt.
        guidance = verification.guidance or "; ".join(verification.issues)

    # Select the best attempt: a later (corrective) run only wins if it scores
    # strictly higher, so a degrading rerun never overwrites a good answer.
    best_idx, best_score = 0, _attempt_score(outcome.attempts[0])
    for i, attempt in enumerate(outcome.attempts[1:], start=1):
        s = _attempt_score(attempt)
        if s > best_score:
            best_idx, best_score = i, s
    outcome.chosen_index = best_idx
    outcome.final_answer = outcome.attempts[best_idx].result.final_answer
    outcome.corrected = best_idx > 0
    if logger:
        logger(
            f"  selected attempt #{best_idx + 1} as final "
            f"(score={best_score}); corrected={outcome.corrected}"
        )
    return outcome
