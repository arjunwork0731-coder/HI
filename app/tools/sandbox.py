"""Sandboxed execution of generated Python code.

Defense in depth (this is a prototype sandbox, not a security boundary for
hostile multi-tenant workloads - in production use gVisor/Firecracker/nsjail):
  1. separate process, isolated interpreter (-I), empty environment
  2. OS resource limits: CPU seconds, address space, max file size, open files
  3. wall-clock timeout
  4. a PEP 578 audit hook installed before user code runs. It blocks network,
     process creation, file deletion/renames, writes outside the temp dir and
     reads outside the temp dir / Python installation.
  5. throw-away working directory
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time

try:
    import resource
except ImportError:  # pragma: no cover - windows
    resource = None

PRELUDE = r'''
import sys, os
_ALLOWED_READ = tuple(p for p in {sys.prefix, sys.base_prefix, sys.exec_prefix, os.path.dirname(os.__file__)} if p)
_CWD = os.getcwd()
_BLOCKED_PREFIX = ("socket.", "subprocess.", "os.system", "os.exec", "os.spawn", "os.fork", "os.posix_spawn",
                   "os.kill", "os.remove", "os.unlink", "os.rmdir", "os.rename", "os.chmod", "os.chown",
                   "shutil.rmtree", "shutil.move", "ctypes.", "urllib.Request", "http.client.", "ftplib.", "smtplib.",
                   "os.putenv", "os.truncate", "os.link", "os.symlink", "pty.", "webbrowser.")
def _vm_hook(event, args):
    if event.startswith(_BLOCKED_PREFIX):
        raise PermissionError(f"[sandbox] blocked operation: {event}")
    if event == "open":
        path, mode = args[0], (args[1] or "r")
        if isinstance(path, int):
            return
        p = os.path.abspath(str(path))
        writing = isinstance(mode, str) and any(c in mode for c in "wax+")
        if isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writing = True
        if writing and not p.startswith(_CWD):
            raise PermissionError(f"[sandbox] blocked write outside sandbox: {p}")
        if not writing and not (p.startswith(_CWD) or p.startswith(_ALLOWED_READ)):
            raise PermissionError(f"[sandbox] blocked read outside sandbox: {p}")
sys.addaudithook(_vm_hook)
del _vm_hook
'''


def _limits(cpu_s: int, mem_mb: int):
    def apply():  # runs in the child before exec
        if resource is None:
            return
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem_mb * 1024 * 1024, mem_mb * 1024 * 1024))
        except (ValueError, OSError):
            pass
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        os.setsid()
    return apply


def run_python(code: str, tests: str = "", timeout: float = 5.0, mem_mb: int = 512) -> dict:
    """Execute code (+ optional test code) in the sandbox. Never raises."""
    program = PRELUDE + "\n# ---- generated code ----\n" + code + "\n# ---- tests ----\n" + (tests or "")
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="vm_sbx_") as tmp:
        path = os.path.join(tmp, "main.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(program)
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "main.py"],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={"PATH": "/usr/bin:/bin", "PYTHONIOENCODING": "utf-8"},
                preexec_fn=_limits(int(timeout) + 1, mem_mb) if os.name == "posix" else None,
            )
            rc, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
        except subprocess.TimeoutExpired as ex:
            rc, timed_out = -9, True
            out = (ex.stdout or b"").decode() if isinstance(ex.stdout, bytes) else (ex.stdout or "")
            err = "Timed out after %.1fs" % timeout
    blocked = [line.split("[sandbox]", 1)[1].strip() for line in err.splitlines() if line.startswith("PermissionError") and "[sandbox]" in line]
    # hide the prelude line numbers from tracebacks shown to agents
    prelude_lines = PRELUDE.count("\n") + 1
    err_clean = err.replace('File "main.py"', f'File "main.py" (prelude={prelude_lines} lines)')
    return {
        "ok": rc == 0 and not timed_out,
        "returncode": rc,
        "timed_out": timed_out,
        "stdout": out[-4000:],
        "stderr": err_clean[-4000:],
        "blocked": blocked,
        "ms": int((time.time() - t0) * 1000),
    }


def dedent(code: str) -> str:
    return textwrap.dedent(code or "").strip("\n")
