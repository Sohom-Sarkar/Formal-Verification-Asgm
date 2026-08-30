"""SAT solver selection.

PySAT bundles about twenty solvers, but not all of them build cleanly on every
platform. On this environment (Windows, CPython 3.14, python-sat 1.9.dev15)
three of them abort the *process* rather than raising a Python exception, which
cannot be caught with try/except - the interpreter is simply gone.

So the names are screened here instead. `verify_solver` is called before any
solver is constructed, turning what would be a silent segfault into an ordinary
error message.

Run `python -m eqcheck.solvers` to re-probe this list on another machine: each
solver is exercised in a subprocess, so a crashing one cannot take the probe
down with it.
"""

import subprocess
import sys

# Confirmed to crash the interpreter on construction in this environment.
KNOWN_BROKEN = {
    "cryptosat": "requires CryptoMiniSat support that this build lacks",
    "kissat404": "segfaults on construction in this PySAT build",
    "minisatgh": "segfaults on construction in this PySAT build",
}

DEFAULT_SOLVER = "cadical153"

# A representative spread across solver generations, used by benchmark.py.
BENCHMARK_SOLVERS = [
    "minisat22",     # the 2010 baseline
    "glucose4",      # aggressive clause-database reduction
    "maplesat",      # learning-rate branching
    "cadical153",    # modern inprocessing (default)
    "cadical195",
    "lingeling",
]


def available_solvers():
    from pysat.solvers import SolverNames
    names = [n for n in dir(SolverNames) if not n.startswith("_")]
    return [n for n in sorted(names) if n not in KNOWN_BROKEN]


def verify_solver(name):
    """Raise ValueError if `name` is unknown or known to crash this build."""
    from pysat.solvers import SolverNames

    known = {n for n in dir(SolverNames) if not n.startswith("_")}
    if name in KNOWN_BROKEN:
        raise ValueError(
            "solver %r is unusable in this environment (%s).\nAvailable: %s"
            % (name, KNOWN_BROKEN[name], ", ".join(available_solvers())))
    if name not in known:
        raise ValueError(
            "unknown solver %r.\nAvailable: %s"
            % (name, ", ".join(available_solvers())))
    return name


_PROBE = (
    "from pysat.solvers import Solver\n"
    "s = Solver(name=%r, bootstrap_with=[[1, 2], [-1, -2], [1, -2]])\n"
    "assert s.solve()\n"
    "s.get_model()\n"
    "s.delete()\n"
    "print('ok')\n"
)


def probe(timeout=60):
    """Exercise every bundled solver in a subprocess. Returns (working, broken)."""
    from pysat.solvers import SolverNames

    names = sorted(n for n in dir(SolverNames) if not n.startswith("_"))
    working, broken = [], []
    for name in names:
        try:
            result = subprocess.run([sys.executable, "-c", _PROBE % name],
                                    capture_output=True, timeout=timeout)
            ok = result.returncode == 0 and b"ok" in result.stdout
        except subprocess.TimeoutExpired:
            ok = False
        (working if ok else broken).append(name)
    return working, broken


if __name__ == "__main__":
    working, broken = probe()
    print("working (%d): %s" % (len(working), ", ".join(working)))
    print("broken  (%d): %s" % (len(broken), ", ".join(broken)))
