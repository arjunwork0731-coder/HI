"""Static analysis of generated Python code.

Detects hallucinated / incorrect API usage *without* running the code:
  * imports of modules that do not exist in this environment
  * `module.attr` references where attr does not exist (e.g. statistics.average)
  * `from module import name` where name does not exist
  * calls whose arguments cannot bind to the real signature (wrong kwargs, arity)
  * methods that do not exist on literal receivers ("abc".reverse())
  * names that are used but never defined, imported, or builtin (lenght(x))
  * dangerous operations (process execution, deletion, network, eval/exec)
"""
from __future__ import annotations

import ast
import builtins
import importlib
import importlib.util
import inspect
import sys

# modules we are willing to import inside the server process for introspection
SAFE_INTROSPECT = {
    "math", "statistics", "json", "re", "datetime", "collections", "itertools", "functools", "random", "string",
    "heapq", "bisect", "fractions", "decimal", "operator", "typing", "dataclasses", "enum", "textwrap", "time",
    "calendar", "copy", "hashlib", "base64", "uuid", "csv", "io", "unicodedata", "array", "cmath", "numbers",
    "os", "os.path", "pathlib", "zoneinfo", "tomllib", "difflib", "pprint", "secrets", "struct", "urllib.parse",
    "shutil", "glob", "sys", "abc", "contextlib", "graphlib", "queue", "sqlite3",
}

DANGEROUS_CALLS = {
    "os.system": "executes a shell command", "os.popen": "executes a shell command", "os.remove": "deletes a file",
    "os.unlink": "deletes a file", "os.rmdir": "deletes a directory", "os.removedirs": "deletes directories",
    "shutil.rmtree": "recursively deletes a directory tree", "subprocess.run": "spawns a process",
    "subprocess.Popen": "spawns a process", "subprocess.call": "spawns a process", "subprocess.check_output": "spawns a process",
    "eval": "evaluates arbitrary code", "exec": "executes arbitrary code", "__import__": "dynamic import",
    "os.kill": "kills a process", "os.chmod": "changes permissions", "socket.socket": "opens a network socket",
    "requests.post": "sends data over the network", "requests.get": "network access", "urllib.request.urlopen": "network access",
    "pathlib.Path.unlink": "deletes a file", "os.fork": "forks the process",
}
DANGEROUS_ATTR_SUFFIX = {"unlink": "deletes a file", "rmtree": "deletes a directory tree", "rmdir": "deletes a directory"}

LITERAL_TYPES = {ast.List: list, ast.Dict: dict, ast.Set: set, ast.Tuple: tuple}


def _module_exists(name: str) -> bool:
    if name in sys.builtin_module_names:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _resolve(mod_name: str):
    if mod_name not in SAFE_INTROSPECT and mod_name.split(".")[0] not in SAFE_INTROSPECT:
        return None
    try:
        return importlib.import_module(mod_name)
    except Exception:
        return None


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self):
        self.defined: set[str] = set()
        self.used: list[tuple[str, int]] = []

    def visit_FunctionDef(self, n):
        self.defined.add(n.name)
        for a in n.args.args + n.args.kwonlyargs + n.args.posonlyargs:
            self.defined.add(a.arg)
        if n.args.vararg:
            self.defined.add(n.args.vararg.arg)
        if n.args.kwarg:
            self.defined.add(n.args.kwarg.arg)
        self.generic_visit(n)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, n):
        for a in n.args.args:
            self.defined.add(a.arg)
        self.generic_visit(n)

    def visit_ClassDef(self, n):
        self.defined.add(n.name)
        self.generic_visit(n)

    def visit_Name(self, n):
        if isinstance(n.ctx, (ast.Store, ast.Del)):
            self.defined.add(n.id)
        else:
            self.used.append((n.id, n.lineno))

    def visit_arg(self, n):
        self.defined.add(n.arg)

    def visit_ExceptHandler(self, n):
        if n.name:
            self.defined.add(n.name)
        self.generic_visit(n)

    def visit_Import(self, n):
        for a in n.names:
            self.defined.add((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, n):
        for a in n.names:
            self.defined.add(a.asname or a.name)

    def visit_Global(self, n):
        self.defined.update(n.names)

    def visit_Nonlocal(self, n):
        self.defined.update(n.names)

    def visit_MatchAs(self, n):
        if n.name:
            self.defined.add(n.name)
        self.generic_visit(n)

    def visit_MatchStar(self, n):
        if n.name:
            self.defined.add(n.name)


def analyze_code(code: str) -> dict:
    issues: list[dict] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as ex:
        return {"ok": False, "issues": [{"type": "syntax_error", "severity": "error", "line": ex.lineno, "detail": str(ex)}], "imports": []}

    alias_to_module: dict[str, str] = {}
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append(a.name)
                if not _module_exists(a.name.split(".")[0]):
                    issues.append({"type": "unknown_module", "severity": "error", "line": node.lineno,
                                   "detail": f"module '{a.name}' is not installed / does not exist"})
                    continue
                if a.asname:
                    alias_to_module[a.asname] = a.name
                else:
                    alias_to_module[a.name.split(".")[0]] = a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.append(node.module)
            if not _module_exists(node.module.split(".")[0]):
                issues.append({"type": "unknown_module", "severity": "error", "line": node.lineno,
                               "detail": f"module '{node.module}' is not installed / does not exist"})
                continue
            mod = _resolve(node.module)
            for a in node.names:
                if mod is not None and a.name != "*" and not hasattr(mod, a.name) and not _module_exists(f"{node.module}.{a.name}"):
                    issues.append({"type": "invalid_api", "severity": "error", "line": node.lineno,
                                   "detail": f"'{a.name}' does not exist in module '{node.module}'",
                                   "suggestion": _suggest(mod, a.name)})

    # attribute access on imported modules + signature binding + dangerous calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            dotted = _dotted(node)
            if not dotted:
                continue
            head = dotted.split(".")[0]
            if head in alias_to_module:
                real = alias_to_module[head] + dotted[len(head):]
                parts = real.split(".")
                obj = _resolve(parts[0])
                path = parts[0]
                for p in parts[1:]:
                    if obj is None:
                        break
                    if not hasattr(obj, p):
                        sub = _resolve(f"{path}.{p}")
                        if sub is None:
                            issues.append({"type": "invalid_api", "severity": "error", "line": node.lineno,
                                           "detail": f"'{path}' has no attribute '{p}'", "suggestion": _suggest(obj, p)})
                            obj = None
                            break
                        obj = sub
                    else:
                        obj = getattr(obj, p)
                    path = f"{path}.{p}"
        if isinstance(node, ast.Call):
            fname = _dotted(node.func)
            if fname:
                head = fname.split(".")[0]
                real = (alias_to_module.get(head, head) + fname[len(head):]) if head in alias_to_module else fname
                if real in DANGEROUS_CALLS:
                    issues.append({"type": "dangerous_call", "severity": "unsafe", "line": node.lineno,
                                   "detail": f"{real}(): {DANGEROUS_CALLS[real]}"})
                elif fname.split(".")[-1] in DANGEROUS_ATTR_SUFFIX and "." in fname:
                    issues.append({"type": "dangerous_call", "severity": "unsafe", "line": node.lineno,
                                   "detail": f"{fname}(): {DANGEROUS_ATTR_SUFFIX[fname.split('.')[-1]]}"})
                if head in alias_to_module:
                    _check_signature(real, node, issues)
            # methods on literals: "abc".reverse(), [].push(1)
            if isinstance(node.func, ast.Attribute):
                recv = node.func.value
                typ = None
                if isinstance(recv, ast.Constant) and isinstance(recv.value, (str, bytes, int, float)):
                    typ = type(recv.value)
                elif type(recv) in LITERAL_TYPES:
                    typ = LITERAL_TYPES[type(recv)]
                if typ is not None and not hasattr(typ, node.func.attr):
                    issues.append({"type": "invalid_api", "severity": "error", "line": node.lineno,
                                   "detail": f"'{typ.__name__}' object has no method '{node.func.attr}'",
                                   "suggestion": _suggest(typ, node.func.attr)})

    # undefined names (hallucinated helpers such as lenght() or calculate_average())
    col = _Collector()
    col.visit(tree)
    builtin_names = set(dir(builtins))
    seen = set()
    for name, line in col.used:
        if name not in col.defined and name not in builtin_names and name not in seen:
            seen.add(name)
            issues.append({"type": "undefined_name", "severity": "error", "line": line,
                           "detail": f"name '{name}' is used but never defined or imported",
                           "suggestion": _suggest_names(name, col.defined | builtin_names)})

    ok = not any(i["severity"] in ("error", "unsafe") for i in issues)
    return {"ok": ok, "issues": issues, "imports": sorted(set(imports))}


def _check_signature(real: str, node: ast.Call, issues: list):
    parts = real.split(".")
    obj = _resolve(parts[0])
    for p in parts[1:]:
        if obj is None or not hasattr(obj, p):
            return
        obj = getattr(obj, p)
    if obj is None or not callable(obj):
        return
    try:
        sig = inspect.signature(obj)
    except (TypeError, ValueError):
        return
    if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
        return
    try:
        sig.bind(*([None] * len(node.args)), **{k.arg: None for k in node.keywords})
    except TypeError as ex:
        issues.append({"type": "invalid_api", "severity": "error", "line": node.lineno,
                       "detail": f"{real}{sig}: {ex}"})


def _suggest(obj, name: str) -> str | None:
    import difflib
    try:
        cands = [n for n in dir(obj) if not n.startswith("_")]
    except Exception:
        return None
    synonyms = {"average": ["mean", "fmean"], "avg": ["mean"], "std": ["stdev", "pstdev"], "push": ["append"],
                "length": ["__len__"], "reverse": ["[::-1]"], "contains": ["__contains__", "find"], "size": ["len()"]}
    m = [s for s in synonyms.get(name, []) if s in cands or s.startswith("[") or s.endswith("()")]
    m += [c for c in difflib.get_close_matches(name, cands, n=3, cutoff=0.5) if c not in m]
    return f"did you mean: {', '.join(m[:3])}?" if m else None


def _suggest_names(name: str, pool: set) -> str | None:
    import difflib
    m = difflib.get_close_matches(name, [p for p in pool if not p.startswith("_")], n=3, cutoff=0.6)
    return f"did you mean: {', '.join(m)}?" if m else None
