"""Tools available to the agent.

Every tool exposes a uniform interface: it receives the raw ``action_input``
(either a string or a parsed dict) and returns a :class:`ToolResult`. Tools never
raise to the agent loop; instead they capture failures in ``ToolResult.ok`` so
the agent can observe the error text and self-correct.
"""

from __future__ import annotations

import ast
import math
import operator
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolResult:
    ok: bool
    output: str

    def observation(self) -> str:
        return self.output if self.ok else f"ERROR: {self.output}"


# --------------------------------------------------------------------------- #
# Tool 1: calculator / code-executor                                          #
# --------------------------------------------------------------------------- #

# Whitelisted binary / unary operators for the safe expression evaluator.
_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

# Math helpers the expression is allowed to reference.
_ALLOWED_NAMES: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "log": math.log,
    "log10": math.log10,
    "log2": math.log2,
    "exp": math.exp,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "floor": math.floor,
    "ceil": math.ceil,
    "factorial": math.factorial,
    "pow": pow,
}


def _resolve_callable(node: ast.AST) -> Any:
    """Resolve a call target, allowing both ``sqrt(..)`` and ``math.sqrt(..)``."""
    if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
        return _ALLOWED_NAMES[node.id]
    # Support the very common `math.<fn>` form that small models love to emit.
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "math"
        and node.attr in _ALLOWED_NAMES
    ):
        return _ALLOWED_NAMES[node.attr]
    raise ValueError("only whitelisted math functions may be called")


def _eval_node(node: ast.AST) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"unsupported constant: {node.value!r}")
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported operator: {type(node.op).__name__}")
        return op(_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(f"unsupported unary operator: {type(node.op).__name__}")
        return op(_eval_node(node.operand))
    if isinstance(node, ast.Call):
        func = _resolve_callable(node.func)
        args = [_eval_node(a) for a in node.args]
        return func(*args)
    if isinstance(node, (ast.Attribute, ast.Name)):
        # A bare `math.pi` / `pi` constant reference.
        if isinstance(node, ast.Name) and node.id in _ALLOWED_NAMES:
            return _ALLOWED_NAMES[node.id]
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "math"
            and node.attr in _ALLOWED_NAMES
        ):
            return _ALLOWED_NAMES[node.attr]
        name = getattr(node, "id", getattr(node, "attr", "?"))
        raise ValueError(f"unknown name: {name}")
    raise ValueError(f"unsupported expression element: {type(node).__name__}")


def _strip_thousands(expression: str) -> str:
    """Remove thousands separators (``68,000,000`` -> ``68000000``).

    Only commas that sit between digits and are followed by exactly three
    digits are removed, so genuine function-argument commas are preserved.
    """
    import re

    return re.sub(r"(?<=\d),(?=\d{3}(\D|$))", "", expression)


def safe_eval(expression: str) -> float:
    """Evaluate an arithmetic expression without executing arbitrary code."""
    tree = ast.parse(_strip_thousands(expression), mode="eval")
    return _eval_node(tree)


def calculator(action_input: Any) -> ToolResult:
    """Evaluate an arithmetic expression.

    Accepts either a bare string (``"2 + 2"``) or a dict with an
    ``"expression"`` key.
    """
    if isinstance(action_input, dict):
        expr = action_input.get("expression") or action_input.get("expr") or ""
    else:
        expr = str(action_input)
    expr = expr.strip().strip("`")
    if not expr:
        return ToolResult(False, "no expression provided")
    try:
        value = safe_eval(expr)
    except ZeroDivisionError:
        return ToolResult(False, "division by zero")
    except (ValueError, SyntaxError, TypeError) as exc:
        return ToolResult(False, f"could not evaluate {expr!r}: {exc}")
    # Render whole floats as ints for cleaner downstream reasoning.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return ToolResult(True, f"{expr} = {value}")


# --------------------------------------------------------------------------- #
# Tool 2: knowledge lookup                                                    #
# --------------------------------------------------------------------------- #

# A tiny grounded knowledge base. Each entry carries a value and a source label
# so the self-correction step can verify source grounding.
KNOWLEDGE_BASE: dict[str, dict[str, Any]] = {
    "population of france": {
        "value": "68000000",
        "text": "France has a population of about 68,000,000 people.",
        "source": "KB:demographics/france",
    },
    "population of germany": {
        "value": "83000000",
        "text": "Germany has a population of about 83,000,000 people.",
        "source": "KB:demographics/germany",
    },
    "population of japan": {
        "value": "125000000",
        "text": "Japan has a population of about 125,000,000 people.",
        "source": "KB:demographics/japan",
    },
    "speed of light": {
        "value": "299792458",
        "text": "The speed of light in vacuum is 299,792,458 meters per second.",
        "source": "KB:physics/constants",
    },
    "earth radius": {
        "value": "6371",
        "text": "The mean radius of Earth is 6371 kilometers.",
        "source": "KB:astronomy/earth",
    },
    "distance earth to moon": {
        "value": "384400",
        "text": "The average distance from Earth to the Moon is 384,400 kilometers.",
        "source": "KB:astronomy/moon",
    },
    "water per person per day": {
        "value": "2",
        "text": "A human needs roughly 2 liters of drinking water per day.",
        "source": "KB:health/hydration",
    },
}


def knowledge_lookup(action_input: Any) -> ToolResult:
    """Look up a fact in the local knowledge base via substring matching."""
    if isinstance(action_input, dict):
        query = action_input.get("query") or action_input.get("topic") or ""
    else:
        query = str(action_input)
    query = query.strip().strip("`").lower()
    if not query:
        return ToolResult(False, "no query provided")

    # Exact key match first, then token-overlap scoring.
    if query in KNOWLEDGE_BASE:
        entry = KNOWLEDGE_BASE[query]
        return ToolResult(True, f"{entry['text']} (source: {entry['source']})")

    # Score by token rarity (IDF-style) so a distinctive entity ("france")
    # outweighs generic words ("population"). This stops "population of mars"
    # from spuriously matching "population of france".
    stop = {"of", "the", "a", "an", "in", "per", "to", "is", "between", "and"}
    df: dict[str, int] = {}
    for key in KNOWLEDGE_BASE:
        for tok in set(key.split()) - stop:
            df[tok] = df.get(tok, 0) + 1

    q_tokens = set(query.replace("?", " ").split()) - stop
    best_key, best_score = None, 0.0
    for key in KNOWLEDGE_BASE:
        k_tokens = set(key.split()) - stop
        score = sum(1.0 / df[tok] for tok in (q_tokens & k_tokens))
        if score > best_score:
            best_key, best_score = key, score
    # Require at least one reasonably distinctive token match.
    if best_key and best_score >= 0.5:
        entry = KNOWLEDGE_BASE[best_key]
        return ToolResult(True, f"{entry['text']} (source: {entry['source']})")

    available = ", ".join(sorted(KNOWLEDGE_BASE))
    return ToolResult(
        False,
        f"no matching fact for {query!r}. Known topics: {available}",
    )


# --------------------------------------------------------------------------- #
# Tool 3: unit converter (the "tool of our choice")                           #
# --------------------------------------------------------------------------- #

# Conversion factors expressed relative to a base unit per dimension.
_UNIT_DIMENSIONS: dict[str, dict[str, float]] = {
    "length": {  # base: meter
        "m": 1.0,
        "meter": 1.0,
        "meters": 1.0,
        "km": 1000.0,
        "kilometer": 1000.0,
        "kilometers": 1000.0,
        "mi": 1609.344,
        "mile": 1609.344,
        "miles": 1609.344,
        "ft": 0.3048,
        "foot": 0.3048,
        "feet": 0.3048,
    },
    "volume": {  # base: liter
        "l": 1.0,
        "liter": 1.0,
        "liters": 1.0,
        "ml": 0.001,
        "gal": 3.785411784,
        "gallon": 3.785411784,
        "gallons": 3.785411784,
    },
    "mass": {  # base: kilogram
        "kg": 1.0,
        "kilogram": 1.0,
        "kilograms": 1.0,
        "g": 0.001,
        "lb": 0.45359237,
        "lbs": 0.45359237,
        "pound": 0.45359237,
        "pounds": 0.45359237,
    },
}


def _find_dimension(unit: str) -> str | None:
    for dim, table in _UNIT_DIMENSIONS.items():
        if unit in table:
            return dim
    return None


def unit_converter(action_input: Any) -> ToolResult:
    """Convert a value between compatible units.

    Expects a dict like ``{"value": 5, "from": "km", "to": "mi"}``.
    """
    if not isinstance(action_input, dict):
        return ToolResult(
            False,
            'expected JSON like {"value": 5, "from": "km", "to": "mi"}',
        )
    try:
        value = float(action_input.get("value"))
    except (TypeError, ValueError):
        return ToolResult(False, "value must be a number")
    from_unit = str(action_input.get("from", "")).strip().lower()
    to_unit = str(action_input.get("to", "")).strip().lower()
    if not from_unit or not to_unit:
        return ToolResult(False, "both 'from' and 'to' units are required")

    from_dim = _find_dimension(from_unit)
    to_dim = _find_dimension(to_unit)
    if from_dim is None:
        return ToolResult(False, f"unknown unit: {from_unit!r}")
    if to_dim is None:
        return ToolResult(False, f"unknown unit: {to_unit!r}")
    if from_dim != to_dim:
        return ToolResult(
            False,
            f"cannot convert {from_unit} ({from_dim}) to {to_unit} ({to_dim})",
        )

    table = _UNIT_DIMENSIONS[from_dim]
    base = value * table[from_unit]
    result = base / table[to_unit]
    rounded = round(result, 6)
    if isinstance(rounded, float) and rounded.is_integer():
        rounded = int(rounded)
    return ToolResult(True, f"{value} {from_unit} = {rounded} {to_unit}")


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #

@dataclass
class Tool:
    name: str
    description: str
    func: Callable[[Any], ToolResult]


TOOLS: dict[str, Tool] = {
    "calculator": Tool(
        name="calculator",
        description=(
            "Evaluate an arithmetic expression. "
            'Input: {"expression": "68000000 + 83000000"}. '
            "Supports + - * / ** % // and functions like sqrt, log, abs, round."
        ),
        func=calculator,
    ),
    "knowledge_lookup": Tool(
        name="knowledge_lookup",
        description=(
            "Look up a grounded fact (populations, physical constants, etc.). "
            'Input: {"query": "population of france"}.'
        ),
        func=knowledge_lookup,
    ),
    "unit_converter": Tool(
        name="unit_converter",
        description=(
            "Convert a value between units of the same dimension "
            "(length, volume, mass). "
            'Input: {"value": 5, "from": "km", "to": "mi"}.'
        ),
        func=unit_converter,
    ),
}


def run_tool(name: str, action_input: Any) -> ToolResult:
    """Dispatch a tool call by name, handling unknown tools gracefully."""
    tool = TOOLS.get(name)
    if tool is None:
        known = ", ".join(TOOLS)
        return ToolResult(False, f"unknown tool {name!r}. Available tools: {known}")
    try:
        return tool.func(action_input)
    except Exception as exc:  # defensive: tools must never crash the loop
        return ToolResult(False, f"tool {name!r} raised {type(exc).__name__}: {exc}")


def tools_description() -> str:
    return "\n".join(f"- {t.name}: {t.description}" for t in TOOLS.values())
