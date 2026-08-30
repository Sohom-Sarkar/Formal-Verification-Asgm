"""Regression suite for the equivalence checker.

Five independent layers, because a checker that grades itself is worth little:

  1. FRONTEND    - simulate each design on its own against a golden model
                   written in Python. Catches parser and bit-blaster bugs that
                   would otherwise cancel out between the two designs.
  2. EQUIVALENCE - run the checker on each pair and compare against the
                   expected verdict, requiring SAT and BDD to agree.
  3. WITNESS     - re-simulate every reported counterexample through both
                   designs; a counterexample that does not reproduce is a bug.
  4. CARE SET    - for minimized counterexamples, check that random completions
                   of the care set still expose the bug.
  5. ENGINE      - check that SAT sweeping, per-output analysis and the
                   ordering search agree with the plain pipeline.
  6. LOCALISATION- for designs with a deliberately planted fault, check that
                   fault diagnosis actually names the gate that was broken.
  7. ALGEBRAIC   - the polynomial backend must prove genuine multipliers and
                   refuse to prove broken ones.

Run:  python run_tests.py [-v]
"""

import random
import sys
import time

from eqcheck.equiv import (build_miter, check_sat, check_bdd, analyze_outputs,
                           variable_order, best_static_order)
from eqcheck.sim import Simulator
from eqcheck.localize import localize, diagnose
from eqcheck.algebraic import verify_multiplier, prove_equivalent_algebraic
from eqcheck.bigstack import run as run_big_stack

PASS = "PASS"
FAIL = "FAIL"

random.seed(20260828)


# ---------------------------------------------------------------- golden models

def golden_adder16(v):
    total = v["a"] + v["b"] + v["cin"]
    return {"sum": total & 0xFFFF, "cout": (total >> 16) & 1}


def golden_addN(width):
    mask = (1 << width) - 1

    def model(v):
        total = v["a"] + v["b"] + v["cin"]
        return {"sum": total & mask, "cout": (total >> width) & 1}
    return model


def golden_mult(width):
    mask = (1 << (2 * width)) - 1

    def model(v):
        return {"p": (v["a"] * v["b"]) & mask}
    return model


def golden_alu8(v):
    a, b, op = v["a"], v["b"], v["op"]
    shift = b & 0x7
    table = {
        0: (a + b) & 0xFF, 1: (a - b) & 0xFF, 2: a & b, 3: a | b,
        4: a ^ b, 5: (a << shift) & 0xFF, 6: (a >> shift) & 0xFF,
        7: 1 if a < b else 0,
    }
    y = table[op]
    return {"y": y, "zero": 1 if y == 0 else 0}


def golden_barrel8(v):
    return {"y": (v["a"] << v["s"]) & 0xFF}


def golden_popcount8(v):
    return {"y": bin(v["a"]).count("1")}


def golden_prio8(v):
    r = v["r"]
    if r == 0:
        return {"idx": 0, "vld": 0}
    return {"idx": r.bit_length() - 1, "vld": 1}


def golden_gray8(v):
    return {"y": v["a"]}


def golden_c17(v):
    n10 = not (v["N1"] and v["N3"])
    n11 = not (v["N3"] and v["N6"])
    n16 = not (v["N2"] and n11)
    n19 = not (n11 and v["N7"])
    return {"N22": int(not (n10 and n16)), "N23": int(not (n16 and n19))}


# --------------------------------------------------------------------- the cases

FRONTEND_CASES = [
    ("adder16_rca.v",      None, golden_adder16),
    ("adder16_cla.v",      None, golden_adder16),
    ("adder16_ks.v",       None, golden_adder16),
    ("add_rca.v",          {"WIDTH": 16}, golden_addN(16)),
    ("add_csel.v",         {"WIDTH": 16}, golden_addN(16)),
    ("alu8_behav.v",       None, golden_alu8),
    ("alu8_struct.v",      None, golden_alu8),
    ("barrel8_behav.v",    None, golden_barrel8),
    ("barrel8_crossbar.v", None, golden_barrel8),
    ("popcount8_seq.v",    None, golden_popcount8),
    ("popcount8_tree.v",   None, golden_popcount8),
    ("prio8_casez.v",      None, golden_prio8),
    ("prio8_struct.v",     None, golden_prio8),
    ("gray8_roundtrip.v",  None, golden_gray8),
    ("c17_gates.v",        None, golden_c17),
    ("c17_table.v",        None, golden_c17),
    ("mult_behav.v",       {"WIDTH": 6}, golden_mult(6)),
    ("mult_csa.v",         {"WIDTH": 6}, golden_mult(6)),
]

EQUIV_CASES = [
    # (name, spec, impl, params, expected_equivalent, run_bdd)
    ("16-bit ripple-carry  vs  carry-lookahead",
     "adder16_rca.v", "adder16_cla.v", None, True, True),
    ("16-bit ripple-carry  vs  Kogge-Stone parallel prefix",
     "adder16_rca.v", "adder16_ks.v", None, True, True),
    ("16-bit carry-lookahead  vs  Kogge-Stone",
     "adder16_cla.v", "adder16_ks.v", None, True, True),
    ("16-bit ripple-carry  vs  carry-select",
     "add_rca.v", "add_csel.v", {"WIDTH": 16}, True, True),
    ("32-bit ripple-carry  vs  carry-select",
     "add_rca.v", "add_csel.v", {"WIDTH": 32}, True, False),
    ("16-bit ripple-carry  vs  CLA with a dropped carry term",
     "adder16_rca.v", "adder16_cla_buggy.v", None, False, True),

    ("8-bit ALU: behavioural case  vs  structural mux tree",
     "alu8_behav.v", "alu8_struct.v", None, True, True),
    ("8-bit ALU: behavioural case  vs  mux tree with swapped shifts",
     "alu8_behav.v", "alu8_struct_buggy.v", None, False, True),

    ("8-bit shifter: behavioural  vs  decoder crossbar",
     "barrel8_behav.v", "barrel8_crossbar.v", None, True, True),
    ("popcount: sequential accumulate  vs  adder tree",
     "popcount8_seq.v", "popcount8_tree.v", None, True, True),
    ("priority encoder: casez wildcards  vs  one-hot mask",
     "prio8_casez.v", "prio8_struct.v", None, True, True),
    ("Gray encode-then-decode  vs  the identity",
     "gray8_roundtrip.v", "gray8_identity.v", None, True, True),
    ("ISCAS-85 c17: six NAND gates  vs  32-entry truth table",
     "c17_gates.v", "c17_table.v", None, True, True),

    ("6x6 multiplier: behavioural  vs  carry-save array",
     "mult_behav.v", "mult_csa.v", {"WIDTH": 6}, True, True),
    ("8x8 multiplier: behavioural  vs  carry-save array",
     "mult_behav.v", "mult_csa.v", {"WIDTH": 8}, True, False),
]

RANDOM_VECTORS = 400


# ------------------------------------------------------------------------ layers

def run_frontend_checks(verbose):
    print("")
    print("1. FRONTEND VALIDATION  (each design vs an independent Python model)")
    print("   " + "-" * 68)
    failures = 0

    for filename, params, model in FRONTEND_CASES:
        sim = Simulator("tests/" + filename, params=params)
        widths = sim.input_widths
        total_bits = sum(widths.values())

        exhaustive = total_bits <= 13
        if exhaustive:
            vectors = []
            for code in range(1 << total_bits):
                values, shift = {}, 0
                for name, width in widths.items():
                    values[name] = (code >> shift) & ((1 << width) - 1)
                    shift += width
                vectors.append(values)
        else:
            vectors = [{name: random.getrandbits(width)
                        for name, width in widths.items()}
                       for _ in range(RANDOM_VECTORS)]

        bad = None
        for values in vectors:
            got, want = sim.eval(values), model(values)
            if got != want:
                bad = (values, got, want)
                break

        label = ("exhaustive, %d vectors" % len(vectors) if exhaustive
                 else "%d random vectors" % len(vectors))
        if bad is None:
            print("   %-4s %-22s %s" % (PASS, filename, label))
        else:
            failures += 1
            print("   %-4s %-22s %s" % (FAIL, filename, label))
            print("        input    %s" % bad[0])
            print("        got      %s" % bad[1])
            print("        expected %s" % bad[2])

    return failures


def run_equivalence_checks(verbose):
    print("")
    print("2. EQUIVALENCE CHECKING  +  3. WITNESS REPLAY  +  4. CARE-SET CHECK")
    print("   " + "-" * 68)
    failures = 0

    for name, spec, impl, params, expected, run_bdd in EQUIV_CASES:
        miter = build_miter("tests/" + spec, "tests/" + impl,
                            param_overrides=params or {})

        start = time.perf_counter()
        sat = check_sat(miter, minimize=not expected)
        sat_time = time.perf_counter() - start

        bdd = None
        if run_bdd:
            search = best_static_order(miter, node_limit=400_000)
            bdd = check_bdd(miter, order=search["order"], node_limit=1_500_000)
            bdd["strategy"] = search["best"]

        ok = sat["equivalent"] == expected
        detail = []

        if bdd is not None and not bdd["aborted"]:
            if bdd["equivalent"] != sat["equivalent"]:
                ok = False
                detail.append("SAT and BDD backends disagree")

        if not expected:
            counterexample = sat.get("counterexample")
            if counterexample is None:
                ok = False
                detail.append("no counterexample produced")
            else:
                values = {k: v["value"]
                          for k, v in counterexample["inputs"].items()}
                spec_sim = Simulator("tests/" + spec, params=params)
                impl_sim = Simulator("tests/" + impl, params=params)
                if spec_sim.eval(values) == impl_sim.eval(values):
                    ok = False
                    detail.append("counterexample does NOT reproduce")
                else:
                    detail.append("witness replays: %s vs %s"
                                  % (spec_sim.eval(values), impl_sim.eval(values)))

                # Layer 4: every completion of the care set must still fail.
                minimized = sat.get("minimized")
                if minimized:
                    escapes = _care_set_escapes(miter, counterexample, minimized,
                                                spec_sim, impl_sim)
                    if escapes:
                        ok = False
                        detail.append("care set is not sufficient: %d/200 "
                                      "completions agreed" % escapes)
                    else:
                        detail.append(
                            "care set: %d of %d bits, 200/200 completions still "
                            "expose the bug"
                            % (minimized["care_bits"], minimized["total_bits"]))

        verdict = "EQUIVALENT" if sat["equivalent"] else "NOT EQUIVALENT"
        status = PASS if ok else FAIL
        failures += 0 if ok else 1

        print("   %-4s %s" % (status, name))
        print("        verdict  %-16s via %s" % (verdict, sat["resolved_by"]))
        print("        AIG %d nodes, depth %d | CNF %d vars, %d clauses | %.3f s"
              % (miter.aig.num_ands, miter.aig.depth([miter.root]),
                 sat["variables"], sat["clauses"], sat_time))
        if bdd is not None:
            if bdd["aborted"]:
                print("        BDD aborted: %s" % bdd["reason"])
            else:
                print("        BDD %d peak nodes via %s order | %.3f s"
                      % (bdd["peak_nodes"], bdd["strategy"], bdd["build_time"]))
        for line in detail:
            print("        %s" % line)

    return failures


def _care_set_escapes(miter, counterexample, minimized, spec_sim, impl_sim,
                      trials=200):
    """How many random completions of the care set fail to expose the bug."""
    rng = random.Random(4242)
    care = minimized["by_port"]
    escapes = 0
    for _ in range(trials):
        values = {}
        for name, width in miter.input_order:
            needed = set(care.get(name, ()))
            value = 0
            for pos in range(width):
                bit = (counterexample["inputs"][name]["bits"][pos]
                       if pos in needed else rng.getrandbits(1))
                value |= bit << pos
            values[name] = value
        if spec_sim.eval(values) == impl_sim.eval(values):
            escapes += 1
    return escapes


def run_engine_checks(verbose):
    print("")
    print("5. ENGINE CROSS-CHECKS  (sweeping, per-output analysis, ordering)")
    print("   " + "-" * 68)
    failures = 0

    cases = [
        ("adder16_rca.v", "adder16_cla.v", None, True),
        ("adder16_rca.v", "adder16_cla_buggy.v", None, False),
        ("alu8_behav.v", "alu8_struct.v", None, True),
        ("mult_behav.v", "mult_csa.v", {"WIDTH": 6}, True),
        ("popcount8_seq.v", "popcount8_tree.v", None, True),
    ]

    for spec, impl, params, expected in cases:
        label = "%s vs %s" % (spec[:-2], impl[:-2])

        miter = build_miter("tests/" + spec, "tests/" + impl,
                            param_overrides=params or {})
        swept = check_sat(miter, sweep=True, presimulate=False)

        plain_miter = build_miter("tests/" + spec, "tests/" + impl,
                                  param_overrides=params or {})
        plain = check_sat(plain_miter, presimulate=False)

        analysis = analyze_outputs(plain_miter)
        any_differs = bool(analysis["differing"])

        ok = (swept["equivalent"] == expected
              and plain["equivalent"] == expected
              and any_differs == (not expected))

        failures += 0 if ok else 1
        print("   %-4s %s" % (PASS if ok else FAIL, label))
        sweep_stats = swept.get("sweep")
        if sweep_stats:
            print("        sweeping: cone %d -> %d, %d merges, %d SAT calls, "
                  "%.3f s, resolved by %s"
                  % (sweep_stats["cone_before"], sweep_stats["cone_after"],
                     sweep_stats["merges"], sweep_stats["sat_calls"],
                     sweep_stats["time"], swept["resolved_by"]))
        print("        plain miter: %s in %.3f s | per-output: %d differing bits "
              "in %d solver calls"
              % ("UNSAT" if plain["equivalent"] else "SAT",
                 plain["solve_time"], len(analysis["differing"]),
                 analysis["solver_calls"]))
        if not ok:
            print("        MISMATCH: sweep=%s plain=%s per-output-differs=%s "
                  "expected=%s" % (swept["equivalent"], plain["equivalent"],
                                   any_differs, expected))

    return failures


def run_localization_checks(verbose):
    """Layer 6: the planted gate must show up as a valid fix location.

    These are the only tests where ground truth is known exactly - the fault
    was injected on purpose - so they check localisation rather than merely
    exercising it.
    """
    print("")
    print("6. FAULT LOCALISATION  (does it name the gate we actually broke?)")
    print("   " + "-" * 68)
    failures = 0

    # --- single-fault design: the answer should be exactly one gate ---------
    result = localize("tests/adder16_rca.v", "tests/adder16_rca_buggy1.v")
    names = [c["name"] for c in result["candidates"]]
    ok = names == ["c[10]"]
    failures += 0 if ok else 1
    print("   %-4s single-gate fault in a 16-bit ripple-carry adder" % (PASS if ok else FAIL))
    print("        planted at c[10]; reported %s" % (names or "nothing"))
    print("        %d of %d candidate gates survived, %d solver calls, %.2f s"
          % (len(result["candidates"]), result["considered"],
             result["solver_calls"], result["time"]))

    # --- multi-fault design: no single fix, but a verified 4-gate one -------
    single = localize("tests/adder16_rca.v", "tests/adder16_cla_buggy.v")
    multi = diagnose("tests/adder16_rca.v", "tests/adder16_cla_buggy.v",
                     max_faults=4, vectors=12, max_sets=4)
    verified = [e for e in multi["sets"] if e.get("verified")]
    ok = (not single["candidates"]) and multi.get("found") and multi["faults"] == 4         and bool(verified)
    failures += 0 if ok else 1
    print("   %-4s module-level fault replicated across 4 instances"
          % (PASS if ok else FAIL))
    print("        no single-gate fix exists: %s" % (not single["candidates"]))
    print("        minimum diagnosis size %s, %d verified set(s) of %d reported"
          % (multi.get("faults"), len(verified), len(multi["sets"])))
    if verified:
        print("        e.g. {%s}"
              % ", ".join(n or "?" for n in verified[0]["names"]))

    # --- an equivalent pair must yield no fix locations at all --------------
    clean = localize("tests/adder16_rca.v", "tests/adder16_cla.v")
    ok = clean["equivalent"] and not clean["candidates"]
    failures += 0 if ok else 1
    print("   %-4s equivalent designs report no fault at all" % (PASS if ok else FAIL))

    return failures


BROKEN_MULTIPLIERS = {
    "dropped partial products": """module m(input [5:0] a, input [5:0] b, output [11:0] p);
  wire [11:0] pp0,pp1,pp2,pp3;
  assign pp0 = ({6'd0,a} & {12{b[0]}}) << 0;
  assign pp1 = ({6'd0,a} & {12{b[1]}}) << 1;
  assign pp2 = ({6'd0,a} & {12{b[2]}}) << 2;
  assign pp3 = ({6'd0,a} & {12{b[3]}}) << 3;
  assign p = pp0 + pp1 + pp2 + pp3;
endmodule""",
    "an adder, not a multiplier": """module m(input [5:0] a, input [5:0] b, output [11:0] p);
  assign p = a + b;
endmodule""",
    "product off by one": """module m(input [5:0] a, input [5:0] b, output [11:0] p);
  assign p = (a * b) + 12'd1;
endmodule""",
    "one output bit inverted": """module m(input [5:0] a, input [5:0] b, output [11:0] p);
  wire [11:0] q; assign q = a * b;
  assign p = {q[11:4], ~q[3], q[2:0]};
endmodule""",
}


def run_algebraic_checks(verbose):
    """Layer 7: the algebraic backend must prove real multipliers and reject fakes.

    This backend answers a different question from the rest of the tool - it
    checks a circuit against the arithmetic specification rather than against
    another circuit - so it needs its own soundness evidence in both
    directions.
    """
    print("")
    print("7. ALGEBRAIC BACKEND  (polynomial reduction against the arithmetic spec)")
    print("   " + "-" * 68)
    failures = 0

    for width in (4, 6, 8, 12):
        for filename in ("mult_behav.v", "mult_csa.v"):
            result = verify_multiplier(path="tests/" + filename,
                                       params={"WIDTH": width})
            ok = result["proved"] is True
            failures += 0 if ok else 1
            print("   %-4s %-14s W=%-3d proved | %d gates, peak %d terms, %.3f s"
                  % (PASS if ok else FAIL, filename, width, result["gates"],
                     result.get("peak_terms", 0), result["time"]))

    for label, source in BROKEN_MULTIPLIERS.items():
        result = verify_multiplier(text=source, max_terms=2_000_000)
        # The safe outcomes are "refuted" or "gave up"; claiming a proof would
        # be unsound, and that is what this asserts.
        ok = result["proved"] is not True
        failures += 0 if ok else 1
        verdict = ("refuted" if result["proved"] is False
                   else "inconclusive (budget)" if result["aborted"] else "PROVED")
        print("   %-4s rejects %-28s -> %s" % (PASS if ok else FAIL, label, verdict))

    # The algebraic verdict must match SAT wherever SAT can still cope.
    agree = True
    for width in (4, 6):
        algebraic = prove_equivalent_algebraic("tests/mult_behav.v",
                                               "tests/mult_csa.v",
                                               params={"WIDTH": width})
        miter = build_miter("tests/mult_behav.v", "tests/mult_csa.v",
                            param_overrides={"WIDTH": width})
        sat = check_sat(miter, presimulate=False)
        if algebraic["equivalent"] != sat["equivalent"]:
            agree = False
    failures += 0 if agree else 1
    print("   %-4s algebraic verdict agrees with SAT at widths 4 and 6"
          % (PASS if agree else FAIL))

    return failures


def main():
    verbose = "-v" in sys.argv
    print("=" * 72)
    print("  Combinational equivalence checker - regression suite")
    print("=" * 72)

    started = time.perf_counter()
    failures = run_frontend_checks(verbose)
    failures += run_equivalence_checks(verbose)
    failures += run_engine_checks(verbose)
    failures += run_localization_checks(verbose)
    failures += run_algebraic_checks(verbose)
    elapsed = time.perf_counter() - started

    print("")
    print("=" * 72)
    if failures:
        print("  %d CHECK(S) FAILED     (%.1f s)" % (failures, elapsed))
    else:
        print("  ALL CHECKS PASSED     (%.1f s)" % elapsed)
    print("=" * 72)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_big_stack(main))
