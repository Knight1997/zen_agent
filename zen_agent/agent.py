"""ReAct-style agent loop.

The agent emits structured ``Thought / Action / Action Input`` steps. After each
action it receives an ``Observation`` and decides — without any hardcoded
sequence — which tool (if any) to call next, until it produces a
``Final Answer``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm import LLMClient
from .tools import run_tool, tools_description

SYSTEM_PROMPT = """You are a careful analytical agent that solves problems step by step using tools.

You MUST answer using this exact structured format, one step at a time:

Thought: <your reasoning about what to do next>
Action: <one of the tool names listed below>
Action Input: <a JSON object with the tool's arguments>

After you write an Action you will STOP and receive an Observation with the tool result.
Use the Observation to decide the next step. Repeat Thought/Action/Action Input as many times as needed.

When you have enough information to answer, respond with:

Thought: <final reasoning>
Final Answer: <the concise final answer to the user's question>

Rules:
- Choose the tool that best fits the current sub-task. Do not follow a fixed order.
- Never invent numbers. Use knowledge_lookup for facts and calculator for arithmetic.
- Action Input MUST be valid JSON on a single line.
- Output only ONE Thought and ONE Action (or Final Answer) per step. Do not write "Observation:" yourself.
- AS SOON AS an Observation already contains the number you need, STOP calling tools and write "Final Answer:". Do not re-run a calculation you have already done.
- The calculator already returns the final numeric value (e.g. "2 + 2 = 4"); never look it up or convert it again.

Available tools:
{tools}

--- Example 1 (single calculation) ---
Question: What is 12 times 8, plus 4?
Thought: I will compute this with the calculator.
Action: calculator
Action Input: {{"expression": "12 * 8 + 4"}}
Observation: 12 * 8 + 4 = 100
Thought: The calculator gives 100, which fully answers the question.
Final Answer: 100
--- End example 1 ---

--- Example 2 (look up facts, THEN calculate with the numbers) ---
Question: How many more people live in Spain than in Portugal?
Thought: I need both populations from the knowledge base. First Spain.
Action: knowledge_lookup
Action Input: {{"query": "population of spain"}}
Observation: Spain has a population of about 47,000,000 people. (source: KB:demographics/spain)
Thought: Now Portugal.
Action: knowledge_lookup
Action Input: {{"query": "population of portugal"}}
Observation: Portugal has a population of about 10,000,000 people. (source: KB:demographics/portugal)
Thought: Now I subtract using the ACTUAL numbers from the observations.
Action: calculator
Action Input: {{"expression": "47000000 - 10000000"}}
Observation: 47000000 - 10000000 = 37000000
Thought: I have the difference.
Final Answer: 37,000,000 more people live in Spain than in Portugal.
--- End example 2 ---
"""

ACTION_RE = re.compile(r"Action\s*:\s*(.+)", re.IGNORECASE)
ACTION_INPUT_RE = re.compile(r"Action\s*Input\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)
THOUGHT_RE = re.compile(r"Thought\s*:\s*(.+)", re.IGNORECASE)
FINAL_RE = re.compile(r"Final\s*Answer\s*:\s*(.+)", re.IGNORECASE | re.DOTALL)


@dataclass
class Step:
    raw: str
    thought: str | None = None
    action: str | None = None
    action_input: Any = None
    observation: str | None = None
    final_answer: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "thought": self.thought,
            "action": self.action,
            "action_input": self.action_input,
            "observation": self.observation,
            "final_answer": self.final_answer,
            "error": self.error,
        }


@dataclass
class AgentResult:
    question: str
    steps: list[Step] = field(default_factory=list)
    final_answer: str | None = None
    stopped_reason: str = "completed"

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "final_answer": self.final_answer,
            "stopped_reason": self.stopped_reason,
            "steps": [s.to_dict() for s in self.steps],
        }


def _extract_json(text: str) -> Any:
    """Best-effort extraction of a JSON value (or raw string) from action input."""
    text = text.strip()
    # Cut off anything after the first line break that starts a new directive.
    for marker in ("\nThought", "\nObservation", "\nAction", "\nFinal"):
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]
    text = text.strip().strip("`").strip()
    if not text:
        return ""
    # Try to locate a JSON object substring.
    if text[0] in "{[":
        depth, end = 0, None
        for i, ch in enumerate(text):
            if ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        candidate = text[:end] if end else text
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    # Fall back to a plain (possibly quoted) string.
    return text.strip().strip('"').strip("'")


def parse_step(text: str) -> Step:
    """Parse one model turn into a structured Step."""
    step = Step(raw=text)

    thought_match = THOUGHT_RE.search(text)
    if thought_match:
        # Keep only the first line of the thought to avoid swallowing later fields.
        step.thought = thought_match.group(1).split("\n")[0].strip()

    final_match = FINAL_RE.search(text)
    if final_match:
        step.final_answer = final_match.group(1).strip()
        return step

    action_match = ACTION_RE.search(text)
    if action_match:
        # The action name is the first token on the line, cleaned of punctuation.
        raw_action = action_match.group(1).split("\n")[0].strip()
        raw_action = raw_action.strip("`").strip().strip(".")
        step.action = raw_action.split()[0] if raw_action else None

    input_match = ACTION_INPUT_RE.search(text)
    if input_match:
        step.action_input = _extract_json(input_match.group(1))

    if step.action is None and step.final_answer is None:
        step.error = "could not parse an Action or Final Answer from model output"
    return step


def _build_scratchpad(steps: list[Step]) -> str:
    """Render prior steps back into the ReAct transcript for the next prompt."""
    parts: list[str] = []
    for s in steps:
        if s.thought:
            parts.append(f"Thought: {s.thought}")
        if s.action:
            parts.append(f"Action: {s.action}")
            parts.append(f"Action Input: {json.dumps(s.action_input)}")
        if s.observation is not None:
            parts.append(f"Observation: {s.observation}")
    return "\n".join(parts)


def run_agent(
    question: str,
    llm: LLMClient,
    max_steps: int = 8,
    extra_guidance: str | None = None,
    logger=None,
) -> AgentResult:
    """Run the ReAct loop until a final answer or step budget is exhausted."""
    result = AgentResult(question=question)
    system = SYSTEM_PROMPT.format(tools=tools_description())
    seen_calls: dict[str, int] = {}  # repeated identical (action, input) guard

    base_task = f"Question: {question}"
    if extra_guidance:
        base_task += (
            f"\n\nIMPORTANT correction guidance from the verifier "
            f"(a previous attempt was wrong): {extra_guidance}"
        )

    for step_idx in range(max_steps):
        scratchpad = _build_scratchpad(result.steps)
        user = base_task
        if scratchpad:
            user += "\n\n" + scratchpad
        user += "\n\nThought:"

        # "Observation:" is a stop token so the model can't hallucinate results.
        raw = llm.chat(system=system, user=user, stop=["Observation:", "\nObservation"])
        step = parse_step("Thought:" + raw if not raw.lstrip().lower().startswith("thought") else raw)

        if logger:
            logger(f"  step {step_idx + 1}: thought={step.thought!r}")

        # Final answer reached.
        if step.final_answer is not None:
            result.steps.append(step)
            result.final_answer = step.final_answer
            if logger:
                logger(f"  -> Final Answer: {step.final_answer}")
            return result

        # Parsing failure: feed the error back as an observation and retry.
        if step.error or not step.action:
            step.observation = (
                "Your last message was not in the required format. "
                "Reply with either 'Action:' + 'Action Input:' or 'Final Answer:'."
            )
            result.steps.append(step)
            if logger:
                logger(f"  -> parse error, nudging model: {step.error}")
            continue

        # Execute the chosen tool (errors are captured, not raised).
        tool_result = run_tool(step.action, step.action_input)
        step.observation = tool_result.observation()

        # Loop guard: if the model keeps issuing the exact same call, append an
        # explicit nudge so it changes tack instead of spinning.
        signature = f"{step.action}|{json.dumps(step.action_input, sort_keys=True)}"
        seen_calls[signature] = seen_calls.get(signature, 0) + 1
        if seen_calls[signature] >= 2:
            step.observation += (
                " (NOTE: you have already made this exact call before. Do NOT "
                "repeat it. Either call a different tool, fix the input, or give "
                "your Final Answer.)"
            )

        result.steps.append(step)
        if logger:
            logger(
                f"  -> Action: {step.action} "
                f"Input: {json.dumps(step.action_input)} "
                f"Obs: {step.observation}"
            )

        # Hard stop: the model is stuck repeating a call. Bail out of the loop
        # and synthesize a final answer from the trace below.
        if seen_calls[signature] >= 3:
            if logger:
                logger("  -> stuck in a loop; forcing final-answer synthesis")
            result.stopped_reason = "loop_detected"
            break
    else:
        # Loop completed without break => the step budget was exhausted.
        result.stopped_reason = "max_steps_exhausted"

    # Last-ditch synthesis: ask the model to answer using the observations it
    # already gathered. We bias it toward reusing a concrete tool result.
    scratchpad = _build_scratchpad(result.steps)
    user = (
        base_task
        + "\n\n"
        + scratchpad
        + "\n\nYou must stop now and answer using ONLY the Observations above. "
        "Do not call any more tools. Reply with a single line:\nFinal Answer:"
    )
    raw = llm.chat(system=system, user=user)
    forced = parse_step("Final Answer:" + raw if "final answer" not in raw.lower() else raw)
    result.final_answer = forced.final_answer or raw.strip()
    result.steps.append(forced)
    if logger:
        logger(f"  -> synthesized Final Answer: {result.final_answer}")
    return result
