"""Experimental study, written up in results/benchmark.md.

  A. SCALING    - multiplier width against plain SAT, SAT sweeping, and BDD.
  B. SWEEPING   - how much internal equivalence each design pair actually has,
                  which is what decides whether sweeping helps.
  C. ORDERING   - every BDD variable-order heuristic on every design.
  D. SOLVERS    - one CNF, several SAT solvers.
  E. WIDTH      - adder width against end-to-end runtime, including the
                  Verilog frontend, to show where the time really goes.

Run:  python benchmark.py [--max-mult-width N] [--quick]
"""

import argparse
import sys
import threading
import time

from eqcheck.equiv import (build_miter, check_sat, check_bdd, variable_order,
                           best_static_order, sift_order, STATIC_ORDERS)
from eqcheck.solvers import BENCHMARK_SOLVERS

BDD_NODE_LIMIT = 400_000


# ---------------------------------------------------------------------------

def experiment_scaling(report, max_width):
    report.section(
        "A. Scaling: multiplier width vs SAT, SAT sweeping, and BDD",
        "Behavioural `a * b` against a carry-save array multiplier. The two "
        "share almost no structure, so every row is a real proof obligation. "
        "`SAT` is the plain output miter; `SAT+sweep` first proves internal "
        "equivalences bottom-up; `BDD` builds the miter decision diagram under "
        "the best static order.")

    header = ("width", "AIG nodes", "CNF vars", "CNF clauses", "SAT (s)",
              "SAT+sweep (s)", "sweep merges", "BDD peak nodes", "BDD (s)")
    rows = []

    for width in range(3, max_width + 1):
        miter = build_miter("tests/mult_behav.v", "tests/mult_csa.v",
                            param_overrides={"WIDTH": width})
        plain = check_sat(miter, presimulate=False)

        swept_miter = build_miter("tests/mult_behav.v", "tests/mult_csa.v",
                                  param_overrides={"WIDTH": width})
        started = time.perf_counter()
        swept = check_sat(swept_miter, sweep=True, presimulate=False)
        swept_time = time.perf_counter() - started

        search = best_static_order(miter, node_limit=BDD_NODE_LIMIT)
        bdd = check_bdd(miter, order=search["order"], node_limit=BDD_NODE_LIMIT)

        if bdd["aborted"] or search["order"] is None:
            bdd_nodes, bdd_time = "> %d" % BDD_NODE_LIMIT, "-"
        else:
            bdd_nodes = "%d" % bdd["peak_nodes"]
            bdd_time = "%.3f" % bdd["build_time"]

        sweep_stats = swept.get("sweep") or {}
        rows.append((str(width), str(miter.aig.num_ands), str(plain["variables"]),
                     str(plain["clauses"]), "%.3f" % plain["solve_time"],
                     "%.3f" % swept_time, str(sweep_stats.get("merges", 0)),
                     bdd_nodes, bdd_time))
        print("   width %d: SAT %.2fs, sweep %.2fs, BDD %s"
              % (width, plain["solve_time"], swept_time, bdd_nodes))
        assert plain["equivalent"] and swept["equivalent"]

    report.table(header, rows)


def experiment_sweeping(report):
    report.section(
        "B. SAT sweeping: how much internal equivalence is there?",
        "Sweeping pays off exactly when the two designs compute the same "
        "intermediate values. `cone after` is the miter size once merges have "
        "been applied - zero means sweeping alone finished the proof, with no "
        "output-level SAT call at all.")

    cases = [
        ("16-bit RCA vs CLA", "tests/adder16_rca.v", "tests/adder16_cla.v", None),
        ("16-bit RCA vs Kogge-Stone", "tests/adder16_rca.v",
         "tests/adder16_ks.v", None),
        ("16-bit RCA vs carry-select", "tests/add_rca.v", "tests/add_csel.v",
         {"WIDTH": 16}),
        ("8-bit ALU behav vs struct", "tests/alu8_behav.v",
         "tests/alu8_struct.v", None),
        ("popcount seq vs tree", "tests/popcount8_seq.v",
         "tests/popcount8_tree.v", None),
        ("Gray roundtrip vs identity", "tests/gray8_roundtrip.v",
         "tests/gray8_identity.v", None),
        ("c17 gates vs truth table", "tests/c17_gates.v", "tests/c17_table.v", None),
        ("6x6 multiplier", "tests/mult_behav.v", "tests/mult_csa.v",
         {"WIDTH": 6}),
        ("16-bit RCA vs buggy CLA", "tests/adder16_rca.v",
         "tests/adder16_cla_buggy.v", None),
    ]

    header = ("design pair", "cone before", "cone after", "merges",
              "constants", "refuted", "SAT calls", "sweep (s)", "outcome")
    rows = []

    for name, spec, impl, params in cases:
        miter = build_miter(spec, impl, param_overrides=params or {})
        result = check_sat(miter, sweep=True, presimulate=False)
        stats = result.get("sweep") or {}
        rows.append((
            name, str(stats.get("cone_before", 0)), str(stats.get("cone_after", 0)),
            str(stats.get("merges", 0)), str(stats.get("constant_merges", 0)),
            str(stats.get("refuted_candidates", 0)), str(stats.get("sat_calls", 0)),
            "%.3f" % stats.get("time", 0.0), result["resolved_by"]))
        print("   %s done" % name)

    report.table(header, rows)


def experiment_ordering(report):
    report.section(
        "C. BDD variable ordering",
        "Peak node count for each static heuristic, plus what sifting adds on "
        "top of the best of them. `interleaved` alternates input port bits "
        "(a0, b0, a1, b1, ...); `dfs` follows a depth-first walk back from the "
        "miter output; `declaration` and `reverse` are the naive orders. "
        "`overflow` means the build passed %d nodes and was abandoned."
        % BDD_NODE_LIMIT)

    cases = [
        ("16-bit RCA vs CLA", "tests/adder16_rca.v", "tests/adder16_cla.v", None),
        ("16-bit RCA vs Kogge-Stone", "tests/adder16_rca.v",
         "tests/adder16_ks.v", None),
        ("8-bit ALU behav vs struct", "tests/alu8_behav.v",
         "tests/alu8_struct.v", None),
        ("8-bit shifter", "tests/barrel8_behav.v", "tests/barrel8_crossbar.v",
         None),
        ("popcount seq vs tree", "tests/popcount8_seq.v",
         "tests/popcount8_tree.v", None),
        ("6x6 multiplier", "tests/mult_behav.v", "tests/mult_csa.v",
         {"WIDTH": 6}),
    ]

    header = ("design pair",) + STATIC_ORDERS + ("best", "after sifting")
    rows = []

    for name, spec, impl, params in cases:
        miter = build_miter(spec, impl, param_overrides=params or {})
        search = best_static_order(miter, node_limit=BDD_NODE_LIMIT)
        cells = []
        for strategy in STATIC_ORDERS:
            value = search["per_strategy"].get(strategy)
            cells.append("overflow" if value is None else str(value))

        if search["order"] is None:
            sifted_cell = "-"
            best_cell = "all overflow"
        else:
            sifted = sift_order(miter, initial=search["order"],
                                node_limit=BDD_NODE_LIMIT, max_builds=120)
            best_cell = "%s (%d)" % (search["best"], search["peak_nodes"])
            sifted_cell = ("%d" % sifted["peak_nodes"]
                           if sifted["peak_nodes"] is not None else "-")
        rows.append((name,) + tuple(cells) + (best_cell, sifted_cell))
        print("   %s done" % name)

    report.table(header, rows)


def experiment_solvers(report):
    report.section(
        "D. SAT solver comparison",
        "The same 8x8 multiplier miter CNF handed to several of the solvers "
        "bundled with PySAT. The CNF is identical in every row; only the "
        "solver changes.")

    miter = build_miter("tests/mult_behav.v", "tests/mult_csa.v",
                        param_overrides={"WIDTH": 8})

    header = ("solver", "encode (s)", "solve (s)", "result")
    rows = []
    for solver in BENCHMARK_SOLVERS:
        try:
            result = check_sat(miter, solver_name=solver, presimulate=False)
        except Exception as exc:                    # noqa: BLE001
            rows.append((solver, "-", "-", "unavailable: %s" % exc))
            continue
        rows.append((solver, "%.4f" % result["encode_time"],
                     "%.3f" % result["solve_time"],
                     "UNSAT (equivalent)" if result["equivalent"]
                     else "SAT (differ)"))
        print("   %s: %.2fs" % (solver, result["solve_time"]))

    report.table(header, rows)


def experiment_width(report):
    report.section(
        "E. Adder width: where does the time actually go?",
        "Ripple-carry against carry-select, from 16 to 128 bits. Adders are "
        "easy for SAT, so this measures the whole pipeline - and shows that "
        "past a certain size the Verilog frontend, not the solver, dominates.")

    header = ("width", "inputs", "AIG nodes", "depth", "frontend (s)",
              "CNF clauses", "SAT (s)", "total (s)")
    rows = []

    for width in (16, 32, 64, 128):
        started = time.perf_counter()
        miter = build_miter("tests/add_rca.v", "tests/add_csel.v",
                            param_overrides={"WIDTH": width})
        frontend = time.perf_counter() - started

        result = check_sat(miter, presimulate=False)
        total = frontend + result["encode_time"] + result["solve_time"]
        rows.append((str(width), str(miter.aig.num_inputs),
                     str(miter.aig.num_ands),
                     str(miter.aig.depth([miter.root])),
                     "%.3f" % frontend, str(result["clauses"]),
                     "%.3f" % result["solve_time"], "%.3f" % total))
        print("   width %d: frontend %.2fs, SAT %.2fs" % (width, frontend,
                                                          result["solve_time"]))
        assert result["equivalent"]

    report.table(header, rows)


# ---------------------------------------------------------------------------

class Report:
    def __init__(self, path):
        self.path = path
        self.lines = []

    def section(self, title, blurb):
        print("")
        print(title)
        self.lines.append("")
        self.lines.append("## " + title)
        self.lines.append("")
        self.lines.append(blurb)
        self.lines.append("")

    def table(self, header, rows):
        widths = [len(h) for h in header]
        for row in rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], len(cell))

        def line(cells):
            return "| " + " | ".join(str(c).ljust(widths[i])
                                     for i, c in enumerate(cells)) + " |"

        self.lines.append(line(header))
        self.lines.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
        for row in rows:
            self.lines.append(line(row))
        self.lines.append("")

    def write(self):
        text = "\n".join(self.lines).rstrip() + "\n"
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("# Equivalence checker - experimental results\n")
            handle.write("\nGenerated by `benchmark.py`. "
                         "All timings on one machine, single-threaded.\n")
            handle.write(text)
        print("")
        print("wrote %s" % self.path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-mult-width", type=int, default=9)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    report = Report("results/benchmark.md")
    started = time.perf_counter()

    experiment_scaling(report, 7 if args.quick else args.max_mult_width)
    experiment_sweeping(report)
    experiment_ordering(report)
    if not args.quick:
        experiment_solvers(report)
    experiment_width(report)

    report.lines.append("")
    report.lines.append("Total benchmark runtime: %.1f s"
                        % (time.perf_counter() - started))
    report.write()
    return 0


def _entry():
    sys.setrecursionlimit(100000)
    box = {}

    def target():
        try:
            box["code"] = main()
        except BaseException as exc:                # noqa: BLE001
            box["error"] = exc

    for size in (128 * 1024 * 1024, 64 * 1024 * 1024, 32 * 1024 * 1024):
        try:
            threading.stack_size(size)
            break
        except (ValueError, RuntimeError):
            continue
    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("code", 0)


if __name__ == "__main__":
    sys.exit(_entry())
