"""Command-line interface.

    python -m eqcheck spec.v impl.v [options]
"""

import argparse
import json
import sys

from .equiv import (build_miter, check_sat, check_bdd, analyze_outputs,
                    variable_order, best_static_order, sift_order,
                    PortMismatch, STATIC_ORDERS)
from .elaborate import ElaborationError
from .lexer import VerilogSyntaxError
from .solvers import DEFAULT_SOLVER, verify_solver
from . import export
from .bigstack import run as run_big_stack
from .localize import localize, diagnose
from .algebraic import prove_equivalent_algebraic

BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
RESET = "\033[0m"


def _supports_colour(stream):
    return hasattr(stream, "isatty") and stream.isatty()


class Printer:
    def __init__(self, stream, colour):
        self.stream = stream
        self.colour = colour

    def paint(self, text, code):
        return "%s%s%s" % (code, text, RESET) if self.colour else text

    def line(self, text=""):
        self.stream.write(text + "\n")

    def header(self, text):
        self.line()
        self.line(self.paint(text, BOLD))
        self.line(self.paint("-" * len(text), DIM))

    def field(self, label, value):
        self.line("    %-18s %s" % (label, value))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="eqcheck",
        description="Prove or refute the equivalence of two combinational "
                    "Verilog designs using SAT and/or BDD.")
    parser.add_argument("spec", help="reference (golden) Verilog file")
    parser.add_argument("impl", help="revised Verilog file to compare against it")
    parser.add_argument("--spec-top", help="top module in the reference file")
    parser.add_argument("--impl-top", help="top module in the revised file")

    engine = parser.add_argument_group("engine")
    engine.add_argument("--method",
                        choices=("sat", "bdd", "both", "algebraic"),
                        default="sat",
                        help="which decision procedure to run (default: sat). "
                             "'algebraic' proves each design against an "
                             "arithmetic specification by polynomial reduction, "
                             "which is vastly faster on multipliers")
    engine.add_argument("--mult-ports", default="a,b,p", metavar="A,B,P",
                        help="port names for --method algebraic (default a,b,p)")
    engine.add_argument("--max-terms", type=int, default=400_000,
                        help="term budget for algebraic reduction")
    engine.add_argument("--solver", default=DEFAULT_SOLVER,
                        help="PySAT solver name (default: %s)" % DEFAULT_SOLVER)
    engine.add_argument("--sweep", action="store_true",
                        help="run SAT sweeping to merge internal equivalences "
                             "before solving the output miter")
    engine.add_argument("--no-presim", action="store_true",
                        help="skip the random-simulation falsification pass")
    engine.add_argument("--sim-vectors", type=int, default=512,
                        help="random vectors for the simulation pass (default 512)")
    engine.add_argument("--minimize", action="store_true",
                        help="shrink a counterexample to the input bits that "
                             "actually provoke the mismatch")

    bdd = parser.add_argument_group("BDD")
    bdd.add_argument("--order",
                     choices=STATIC_ORDERS + ("auto", "sift"),
                     default="auto",
                     help="BDD variable order; 'auto' tries every static "
                          "heuristic and keeps the smallest, 'sift' then "
                          "refines it (default: auto)")
    bdd.add_argument("--bdd-limit", type=int, default=2_000_000,
                     help="abort the BDD build past this many nodes")

    out = parser.add_argument_group("reporting and export")
    out.add_argument("-p", "--param", action="append", default=[],
                     metavar="NAME=VALUE",
                     help="override a top-level parameter in both designs")
    out.add_argument("--outputs", action="store_true",
                     help="per-output-bit table with cone size and depth")
    out.add_argument("--diagnose", action="store_true",
                     help="on a mismatch, list every output bit that can differ")
    out.add_argument("--localize", nargs="?", type=int, const=4, default=None,
                     metavar="MAXFAULTS",
                     help="on a mismatch, find which gates would have to change "
                          "(SAT-based fault diagnosis); optional argument caps "
                          "how many simultaneous faults to search for")
    out.add_argument("--stats", action="store_true",
                     help="show detailed solver statistics")
    out.add_argument("--dimacs", metavar="PATH",
                     help="write the miter CNF in DIMACS format")
    out.add_argument("--aiger", metavar="PATH",
                     help="write the miter in AIGER format (for ABC and friends)")
    out.add_argument("--dot", metavar="PATH",
                     help="write the miter AIG as a Graphviz file")
    out.add_argument("--bdd-dot", metavar="PATH",
                     help="write the miter BDD as a Graphviz file")
    out.add_argument("--json", metavar="PATH", help="write the full result as JSON")
    out.add_argument("-q", "--quiet", action="store_true",
                     help="print only the verdict line")
    out.add_argument("--no-colour", action="store_true")
    return parser


def format_value(port):
    width = port["width"]
    value = port["value"]
    digits = (width + 3) // 4
    return "%d'h%0*X (%d)" % (width, digits, value, value)


def report_counterexample(printer, miter, counterexample, minimized=None):
    printer.header("Counterexample")
    printer.line("Input assignment that makes the two designs disagree:")
    printer.line()

    care = (minimized or {}).get("by_port")
    for name, _ in miter.input_order:
        port = counterexample["inputs"][name]
        bits = "".join(str(b) for b in reversed(port["bits"]))
        if care is not None:
            # Mark the bits that actually matter; the rest are don't-cares.
            needed = set(care.get(name, ()))
            bits = "".join(
                (str(port["bits"][pos]) if pos in needed else "-")
                for pos in reversed(range(port["width"])))
        printer.line("    %-14s = %-18s %s" % (name, format_value(port),
                                               printer.paint("b" + bits, DIM)))

    if minimized is not None:
        printer.line()
        printer.line("    %s only %d of %d input bits are needed to expose the "
                     "bug (- = don't care)"
                     % (printer.paint("minimized:", CYAN),
                        minimized["care_bits"], minimized["total_bits"]))

    outputs = counterexample["outputs"]
    spec_out, impl_out = outputs["spec"], outputs["impl"]
    printer.line()
    printer.line("Resulting outputs:")
    printer.line()
    printer.line("    %-14s %-22s %-22s" % ("output", "reference", "revised"))
    for name in spec_out:
        same = spec_out[name]["value"] == impl_out[name]["value"]
        marker = "" if same else printer.paint("  <-- differs", RED)
        printer.line("    %-14s %-22s %-22s%s"
                     % (name, format_value(spec_out[name]),
                        format_value(impl_out[name]), marker))


def report_output_table(printer, analysis):
    printer.header("Per-output analysis")
    printer.line("    %-12s %-5s %-11s %-7s %s"
                 % ("output", "bit", "cone nodes", "depth", "verdict"))
    for row in analysis["outputs"]:
        if row["differs"]:
            verdict = printer.paint("DIFFERS", RED)
        else:
            verdict = "equal (%s)" % row["proved_by"]
        printer.line("    %-12s %-5d %-11d %-7d %s"
                     % (row["output"], row["bit"], row["cone_nodes"],
                        row["depth"], verdict))
    printer.line()
    printer.field("solver calls", "%d in %.3f s"
                  % (analysis["solver_calls"], analysis["time"]))


def resolve_bdd_order(miter, strategy, node_limit, printer, quiet):
    """Return (order, description, extra_info) for the chosen strategy."""
    if strategy == "auto":
        search = best_static_order(miter)
        if search["order"] is None:
            return None, "auto (all heuristics overflowed)", search
        return search["order"], "auto -> %s" % search["best"], search
    if strategy == "sift":
        search = best_static_order(miter)
        sifted = sift_order(miter, initial=search["order"], node_limit=node_limit)
        return sifted["order"], "sift (from %s)" % (search["best"] or "declaration"), \
            {"static": search, "sift": sifted}
    return variable_order(miter, strategy), strategy, None


def run(argv=None):
    args = build_parser().parse_args(argv)
    colour = _supports_colour(sys.stdout) and not args.no_colour
    printer = Printer(sys.stdout, colour)

    if args.method in ("sat", "both"):
        try:
            verify_solver(args.solver)
        except ValueError as exc:
            printer.line(printer.paint("ERROR: ", RED) + str(exc))
            return 2

    overrides = {}
    for item in args.param:
        if "=" not in item:
            printer.line("error: --param expects NAME=VALUE, got %r" % item)
            return 2
        key, value = item.split("=", 1)
        try:
            overrides[key.strip()] = int(value, 0)
        except ValueError:
            printer.line("error: parameter %r must be an integer" % key)
            return 2

    try:
        miter = build_miter(args.spec, args.impl,
                            spec_top=args.spec_top, impl_top=args.impl_top,
                            param_overrides=overrides)
    except (PortMismatch, ElaborationError, VerilogSyntaxError) as exc:
        printer.line(printer.paint("ERROR: ", RED) + str(exc))
        return 2
    except OSError as exc:
        printer.line(printer.paint("ERROR: ", RED) + "cannot read input: %s" % exc)
        return 2

    if not args.quiet:
        printer.header("Designs")
        for design in miter.designs:
            ports = ", ".join("%s[%d]" % (name, len(bits))
                              for name, _, bits in design.outputs)
            printer.line("    %-6s top module %-20s outputs: %s"
                         % (design.label, design.top, ports))
        stats = miter.aig.stats(roots=[miter.root])
        printer.line()
        printer.field("primary inputs", stats["inputs"])
        printer.field("miter AIG nodes", "%d total, %d in the miter cone"
                      % (stats["and_nodes"], stats.get("cone_nodes", 0)))
        printer.field("miter logic depth", stats.get("depth", 0))

        if miter.warnings:
            printer.header("Warnings")
            for warning in dict.fromkeys(miter.warnings):
                printer.line("    " + printer.paint(warning, YELLOW))

    results = {}
    verdicts = []

    if args.method == "algebraic":
        try:
            ports = [x.strip() for x in args.mult_ports.split(",")]
            if len(ports) != 3:
                raise ValueError("--mult-ports needs exactly three names")
        except ValueError as exc:
            printer.line(printer.paint("ERROR: ", RED) + str(exc))
            return 2

        try:
            result = prove_equivalent_algebraic(
                args.spec, args.impl, spec_top=args.spec_top,
                impl_top=args.impl_top, params=overrides,
                a=ports[0], b=ports[1], p=ports[2],
                max_terms=args.max_terms)
        except ValueError as exc:
            printer.line(printer.paint("ERROR: ", RED) + str(exc))
            return 2

        results["algebraic"] = result
        if not args.quiet:
            printer.header("Algebraic backend")
            printer.field("specification", "%s * %s == %s"
                          % (ports[0], ports[1], ports[2]))
            for label, side in (("reference", result["spec"]),
                                ("revised", result["impl"])):
                if side["aborted"]:
                    printer.field(label, printer.paint("aborted: " + side["reason"],
                                                       YELLOW))
                else:
                    printer.field(label,
                                  "%s | %d gates, spec %d terms, peak %d terms, "
                                  "%.3f s"
                                  % ("proved a multiplier" if side["proved"]
                                     else printer.paint("NOT a multiplier", RED),
                                     side["gates"], side["spec_terms"],
                                     side["peak_terms"], side["time"]))

        if result["equivalent"] is None:
            printer.line()
            printer.line(printer.paint("INCONCLUSIVE", YELLOW)
                         + " - the algebraic backend could not establish this; "
                           "re-run with --method sat.")
            return 3
        verdicts.append(result["equivalent"])

    if args.method in ("sat", "both"):
        result = check_sat(miter, solver_name=args.solver,
                           dimacs_path=args.dimacs,
                           sweep=args.sweep,
                           presimulate=not args.no_presim,
                           sim_vectors=args.sim_vectors,
                           minimize=args.minimize)
        results["sat"] = result
        verdicts.append(result["equivalent"])

        if not args.quiet:
            printer.header("SAT backend")
            printer.field("resolved by", printer.paint(result["resolved_by"], CYAN))

            simulation = result.get("simulation")
            if simulation:
                printer.field("random simulation",
                              "%d vectors in %.4f s -> %s"
                              % (simulation["vectors"], simulation["time"],
                                 "counterexample found" if simulation["falsified"]
                                 else "no counterexample"))

            sweep = result.get("sweep")
            if sweep:
                printer.field("SAT sweeping",
                              "cone %d -> %d nodes, %d merges (+%d constants), "
                              "%d refuted, %d solver calls, %.3f s"
                              % (sweep["cone_before"], sweep["cone_after"],
                                 sweep["merges"], sweep["constant_merges"],
                                 sweep["refuted_candidates"], sweep["sat_calls"],
                                 sweep["time"]))

            if result["variables"]:
                printer.field("solver", result["solver"])
                printer.field("CNF", "%d variables, %d clauses"
                              % (result["variables"], result["clauses"]))
                printer.field("Tseitin encode", "%.4f s" % result["encode_time"])
                printer.field("solve", "%.4f s" % result["solve_time"])

            if args.stats and result.get("stats"):
                printer.line()
                for key in ("conflicts", "decisions", "propagations", "restarts"):
                    if key in result["stats"]:
                        printer.field("  " + key, result["stats"][key])

    bdd_manager_info = None
    if args.method in ("bdd", "both"):
        order, description, extra = resolve_bdd_order(
            miter, args.order, args.bdd_limit, printer, args.quiet)
        result = check_bdd(miter, order=order, node_limit=args.bdd_limit)
        results["bdd"] = result
        results["bdd"]["order_strategy"] = description
        if extra is not None:
            results["bdd"]["order_search"] = _jsonable_order(extra)

        if not args.quiet:
            printer.header("BDD backend")
            printer.field("variable order", description)
            if extra and "per_strategy" in extra:
                printer.field("heuristics tried", ", ".join(
                    "%s=%s" % (k, v if v is not None else "overflow")
                    for k, v in extra["per_strategy"].items()))
            if result["aborted"]:
                printer.line("    " + printer.paint("aborted: " + result["reason"],
                                                    YELLOW))
                printer.field("build time", "%.4f s" % result["build_time"])
            else:
                printer.field("build time", "%.4f s" % result["build_time"])
                printer.field("peak nodes", result["peak_nodes"])
                printer.field("live nodes", result["live_nodes"])
                bdd_manager_info = (order, result)
        if not result["aborted"]:
            verdicts.append(result["equivalent"])

    if not verdicts:
        printer.line(printer.paint("\nINCONCLUSIVE", YELLOW)
                     + " - no backend reached a verdict.")
        return 3

    if len(set(verdicts)) > 1:
        printer.line(printer.paint("\nINTERNAL ERROR", RED)
                     + " - SAT and BDD backends disagree. Please report this.")
        return 4

    equivalent = verdicts[0]

    counterexample = None
    minimized = None
    for backend in ("sat", "bdd"):
        if backend in results and results[backend].get("counterexample"):
            counterexample = results[backend]["counterexample"]
            minimized = results[backend].get("minimized")
            break

    if not equivalent and counterexample and not args.quiet:
        report_counterexample(printer, miter, counterexample, minimized)

    if (args.outputs or args.diagnose) and not args.quiet:
        analysis = analyze_outputs(miter, solver_name=args.solver)
        results["output_analysis"] = analysis
        if args.outputs:
            report_output_table(printer, analysis)
        elif analysis["differing"]:
            printer.header("Failing output bits")
            by_output = {}
            for name, pos in analysis["differing"]:
                by_output.setdefault(name, []).append(pos)
            for name, positions in by_output.items():
                printer.line("    %-14s bits %s"
                             % (name, ", ".join(str(p) for p in sorted(positions))))

    if args.localize is not None and not equivalent and not args.quiet:
        printer.header("Fault localisation")
        single = localize(args.spec, args.impl, spec_top=args.spec_top,
                          impl_top=args.impl_top, params=overrides,
                          solver_name=args.solver)
        results["localization"] = {k: v for k, v in single.items()
                                   if k != "candidates"}

        if single["candidates"]:
            printer.field("single-fix gates",
                          "%d of %d candidates in the failing cone"
                          % (len(single["candidates"]), single["considered"]))
            printer.line()
            for entry in single["candidates"][:12]:
                label = entry["name"] or "node %d" % entry["node"]
                printer.line("      %s%s"
                             % (printer.paint(label, CYAN),
                                "   (depth %d)" % entry["depth"]))
        else:
            printer.line("    no single gate can repair this - searching for "
                         "multi-gate explanations")
            printer.line()
            multi = diagnose(args.spec, args.impl, spec_top=args.spec_top,
                             impl_top=args.impl_top, params=overrides,
                             solver_name=args.solver, max_faults=args.localize)
            results["diagnosis"] = multi
            if not multi.get("found"):
                printer.field("result", "no explanation with <= %d simultaneous "
                              "faults" % multi["max_faults_searched"])
            else:
                printer.field("minimum faults", multi["faults"])
                printer.field("candidate gates", multi["candidates"])
                printer.line()
                for entry in multi["sets"][:4]:
                    names = ", ".join(n or "?" for n in entry["names"])
                    status = {True: "verified", False: "spurious",
                              None: "unchecked"}[entry.get("verified")]
                    note = ("  (degenerate - sits on the outputs)"
                            if entry["at_outputs"] == entry["size"] else "")
                    printer.line("      {%s}" % printer.paint(names, CYAN))
                    printer.line("        %s%s" % (status, note))
                printer.line()
                printer.line(printer.paint(
                    "    note: many gate sets can repair a design; the true "
                    "fault is guaranteed to be among them, but the ranking "
                    "above is heuristic.", DIM))

    exported = {}
    if args.aiger:
        info = export.write_aiger(miter.aig, [miter.root], args.aiger,
                                  output_names=["miter"])
        exported["aiger"] = args.aiger
        if not args.quiet:
            printer.line()
            printer.line(printer.paint(
                "wrote %s (AIGER: %d inputs, %d AND gates) - verify externally "
                "with:  abc -c \"read_aiger %s; sat\""
                % (args.aiger, info["inputs"], info["ands"], args.aiger), DIM))
    if args.dot:
        try:
            count = export.write_aig_dot(miter.aig, [miter.root], args.dot,
                                         root_labels=["miter"])
            exported["dot"] = args.dot
            if not args.quiet:
                printer.line(printer.paint("wrote %s (%d nodes)"
                                           % (args.dot, count), DIM))
        except ValueError as exc:
            printer.line(printer.paint("skipped --dot: %s" % exc, YELLOW))
    if args.bdd_dot:
        order, _description, _extra = resolve_bdd_order(
            miter, args.order, args.bdd_limit, printer, True)
        try:
            from .bdd import build_from_aig
            manager, roots, level_of_input = build_from_aig(
                miter.aig, [miter.root], order=order, node_limit=args.bdd_limit)
            names = {}
            for node, level in level_of_input.items():
                names[level] = miter.aig.input_names.get(node, "n%d" % node)
            count = export.write_bdd_dot(manager, roots[0], args.bdd_dot,
                                         var_names=names)
            exported["bdd_dot"] = args.bdd_dot
            if not args.quiet:
                printer.line(printer.paint("wrote %s (%d BDD nodes)"
                                           % (args.bdd_dot, count), DIM))
        except ValueError as exc:
            printer.line(printer.paint("skipped --bdd-dot: %s" % exc, YELLOW))

    printer.line()
    if equivalent:
        count = miter.aig.num_inputs
        space = ("all %d possible input vectors" % (2 ** count) if count <= 20
                 else "all 2^%d input vectors" % count)
        printer.line(printer.paint("EQUIVALENT", GREEN)
                     + " - the designs agree on %s." % space)
    else:
        printer.line(printer.paint("NOT EQUIVALENT", RED)
                     + " - a distinguishing input vector was found.")

    if args.json:
        payload = {
            "spec": {"file": args.spec, "top": miter.designs[0].top},
            "impl": {"file": args.impl, "top": miter.designs[1].top},
            "aig": miter.aig.stats(roots=[miter.root]),
            "equivalent": equivalent,
            "results": {k: _jsonable(v) for k, v in results.items()},
            "exports": exported,
            "warnings": list(dict.fromkeys(miter.warnings)),
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)
        if not args.quiet:
            printer.line(printer.paint("wrote %s" % args.json, DIM))

    return 0 if equivalent else 1


def _jsonable_order(extra):
    clean = {}
    for key, value in extra.items():
        if isinstance(value, dict):
            clean[key] = {k: v for k, v in value.items() if k != "order"}
        elif key != "order":
            clean[key] = value
    return clean


def _jsonable(result):
    if not isinstance(result, dict):
        return result
    clean = {}
    for key, value in result.items():
        if key == "stats":
            clean[key] = {str(k): v for k, v in (value or {}).items()}
        elif key == "order":
            continue
        else:
            clean[key] = value
    return clean


def main(argv=None):
    return run_big_stack(run, argv)


if __name__ == "__main__":
    sys.exit(main())
