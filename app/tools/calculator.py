"""Safe arithmetic evaluation (AST whitelist) and extraction of checkable
arithmetic statements from free text."""
from __future__ import annotations

import ast
import math
import operator
import re

_BIN = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
}
_UN = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS = {
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10, "log2": math.log2, "exp": math.exp, "abs": abs,
    "round": round, "min": min, "max": max, "pow": pow, "floor": math.floor, "ceil": math.ceil,
    "sin": math.sin, "cos": math.cos, "tan": math.tan, "factorial": math.factorial, "comb": math.comb,
}
_CONSTS = {"pi": math.pi, "e": math.e}


class CalcError(ValueError):
    pass


def normalize_expression(expr: str) -> str:
    e = expr.strip()
    e = e.replace("×", "*").replace("÷", "/").replace("^", "**").replace("−", "-").replace("–", "-")
    e = re.sub(r"(?<=\d),(?=\d{2,3}\b)", "", e)  # 1,240 -> 1240
    e = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", e)
    e = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", "*", e)
    return e


def safe_eval(expr: str) -> float:
    try:
        tree = ast.parse(normalize_expression(expr), mode="eval")
    except SyntaxError as ex:
        raise CalcError(f"cannot parse expression: {expr!r}") from ex

    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _BIN:
            l, r = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Pow) and (abs(r) > 1000 or abs(l) > 1e12):
                raise CalcError("exponent too large")
            return _BIN[type(n.op)](l, r)
        if isinstance(n, ast.UnaryOp) and type(n.op) in _UN:
            return _UN[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        if isinstance(n, ast.Name) and n.id in _CONSTS:
            return _CONSTS[n.id]
        raise CalcError(f"disallowed element in expression: {ast.dump(n)[:60]}")

    try:
        val = ev(tree)
    except ZeroDivisionError as ex:
        raise CalcError("division by zero") from ex
    return float(val)


def numbers_close(stated: float, actual: float, stated_raw: str | None = None) -> bool:
    """True if `stated` is `actual` correctly rounded to the precision it was written with."""
    if actual == stated or abs(actual - stated) <= 1e-9 * max(abs(actual), 1):
        return True
    decimals = 0
    if stated_raw:
        m = re.search(r"\.(\d+)", str(stated_raw).replace(",", ""))
        decimals = len(m.group(1)) if m else 0
    return abs(actual - stated) <= 0.5 * 10 ** (-decimals) + 1e-9


_NUM = r"-?\d[\d,]*(?:\.\d+)?"
_ARITH = re.compile(rf"(?P<lhs>(?:\(?\s*{_NUM}\s*%?\s*\)?\s*[-+*/×x÷^]\s*)+\(?\s*{_NUM}\s*%?\s*\)?)\s*(?:=|equals|is|gives|yields)\s*(?P<rhs>{_NUM})", re.I)
_PCT_OF = re.compile(rf"(?P<p>{_NUM})\s*(?:%|percent)\s+of\s+(?:₹|\$|rs\.?\s*)?(?P<base>{_NUM})\s*(?:\w+\s+){{0,3}}?(?:is|=|equals|comes to|gives)\s*(?:₹|\$|rs\.?\s*)?(?P<rhs>{_NUM})", re.I)


def find_arithmetic(text: str) -> list[dict]:
    """Find statements like '12 * 7 = 84' or '15% of 2,400 is 360' and check them."""
    results = []
    for m in _PCT_OF.finditer(text):
        expr = f"{m.group('p')}/100*{m.group('base')}"
        results.append(_check(expr, m.group("rhs"), m.group(0)))
    for m in _ARITH.finditer(text):
        lhs = m.group("lhs")
        if not re.search(r"[-+*/×x÷^]", lhs.replace(",", "")):
            continue
        # skip ranges/dates such as 2019-2020 or 14-07-2023
        if re.fullmatch(r"\s*\d{4}\s*-\s*\d{2,4}\s*", lhs) or re.search(r"\d{1,2}-\d{1,2}-\d{2,4}", lhs):
            continue
        results.append(_check(lhs, m.group("rhs"), m.group(0)))
    return results


def _check(expr: str, rhs_raw: str, span: str) -> dict:
    try:
        actual = safe_eval(expr)
        stated = float(rhs_raw.replace(",", ""))
        ok = numbers_close(stated, actual, rhs_raw)
        return {"expression": expr.strip(), "stated": stated, "actual": round(actual, 6), "ok": ok, "span": span.strip()}
    except (CalcError, ValueError, OverflowError) as ex:
        return {"expression": expr.strip(), "stated": rhs_raw, "actual": None, "ok": None, "span": span.strip(), "error": str(ex)}
