"""Randomised differential testing of the equivalence checker itself.

The hand-written suite in `run_tests.py` covers designs I thought to write. A
fuzzer covers the ones I did not. Each round:

  1. Generate a random combinational circuit A.
  2. Rewrite it into B using only semantics-preserving transformations
     (De Morgan, XOR expansion, commutation, double negation, redundancy
     insertion). B is structurally different but must be functionally identical.
  3. Mutate A into C by corrupting one operator or operand. C is *usually*
     different, though a mutation can accidentally preserve behaviour.
  4. Establish ground truth by exhaustive simulation over the whole input
     space - independent of the checker.
  5. Require the checker to agree with ground truth on both A-vs-B and A-vs-C,
     and require every counterexample it reports to genuinely reproduce.

This is the strongest correctness evidence in the project: it tests the
checker against an oracle on inputs nobody designed by hand, and a single
disagreement is a real bug.

Run:  python fuzz.py [--rounds N] [--seed S] [--width W] [--verbose]
"""

import argparse
import itertools
import random
import sys
import time

from eqcheck.equiv import build_miter, check_sat, PortMismatch
from eqcheck.elaborate import ElaborationError
from eqcheck.lexer import VerilogSyntaxError
from eqcheck.sim import Simulator
from eqcheck.bigstack import run as run_big_stack

# ---------------------------------------------------------------------------
# expression trees
# ---------------------------------------------------------------------------

BIN_OPS = ["&", "|", "^", "+", "-"]
BIN_OPS_RARE = ["*"]
UN_OPS = ["~", "-"]


def gen_expr(rng, inputs, width, depth):
    """A random expression of `width` bits."""
    if depth <= 0:
        if rng.random() < 0.75:
            return ("var", rng.choice(inputs))
        return ("const", rng.randrange(1 << width))

    roll = rng.random()
    if roll < 0.50:
        op = rng.choice(BIN_OPS if rng.random() < 0.9 else BIN_OPS_RARE)
        return ("bin", op,
                gen_expr(rng, inputs, width, depth - 1),
                gen_expr(rng, inputs, width, depth - 1))
    if roll < 0.66:
        return ("un", rng.choice(UN_OPS), gen_expr(rng, inputs, width, depth - 1))
    if roll < 0.80:
        # Constant shift keeps the generated logic small but still exercises
        # the shifter path.
        return ("shift", rng.choice(["<<", ">>"]),
                gen_expr(rng, inputs, width, depth - 1),
                rng.randrange(width))
    if roll < 0.92:
        return ("mux",
                gen_expr(rng, inputs, width, depth - 1),
                gen_expr(rng, inputs, width, depth - 1),
                gen_expr(rng, inputs, width, depth - 1))
    return ("cmp", rng.choice(["<", "==", ">"]),
            gen_expr(rng, inputs, width, depth - 1),
            gen_expr(rng, inputs, width, depth - 1))


def emit(node, width):
    kind = node[0]
    if kind == "var":
        return node[1]
    if kind == "const":
        return "%d'd%d" % (width, node[1])
    if kind == "un":
        return "(%s%s)" % (node[1], emit(node[2], width))
    if kind == "bin":
        return "(%s %s %s)" % (emit(node[2], width), node[1], emit(node[3], width))
    if kind == "shift":
        return "(%s %s %d)" % (emit(node[2], width), node[1], node[3])
    if kind == "mux":
        return "(%s ? %s : %s)" % (emit(node[1], width), emit(node[2], width),
                                   emit(node[3], width))
    if kind == "cmp":
        # A comparison is one bit; widen it so every expression has one width.
        return "{%d'd0, (%s %s %s)}" % (width - 1, emit(node[2], width),
                                        node[1], emit(node[3], width))
    raise AssertionError(kind)


# ---------------------------------------------------------------------------
# semantics-preserving rewrites  (A -> B)
# ---------------------------------------------------------------------------

def rewrite(node, rng, probability=0.45):
    """Rewrite an expression without changing what it computes."""
    kind = node[0]

    if kind in ("var", "const"):
        node = _maybe_wrap(node, rng, probability)
        return node

    if kind == "un":
        inner = rewrite(node[2], rng, probability)
        rebuilt = ("un", node[1], inner)
        if node[1] == "~" and rng.random() < probability:
            # ~x  ==  ~(~(~x))
            return ("un", "~", ("un", "~", rebuilt))
        return _maybe_wrap(rebuilt, rng, probability)

    if kind == "bin":
        op, left, right = node[1], rewrite(node[2], rng, probability), \
            rewrite(node[3], rng, probability)

        if rng.random() < probability:
            if op == "&":
                # De Morgan
                return ("un", "~", ("bin", "|", ("un", "~", left),
                                    ("un", "~", right)))
            if op == "|":
                return ("un", "~", ("bin", "&", ("un", "~", left),
                                    ("un", "~", right)))
            if op == "^":
                # x ^ y  ==  (x & ~y) | (~x & y)
                return ("bin", "|",
                        ("bin", "&", left, ("un", "~", right)),
                        ("bin", "&", ("un", "~", left), right))
            if op == "-":
                # x - y  ==  x + ~y + 1   (two's complement, same width)
                return ("bin", "+", ("bin", "+", left, ("un", "~", right)),
                        ("const", 1))
            if op in ("&", "|", "^", "+", "*"):
                return ("bin", op, right, left)          # commutative

        if op in ("&", "|", "^", "+", "*") and rng.random() < probability * 0.5:
            return ("bin", op, right, left)

        return _maybe_wrap(("bin", op, left, right), rng, probability)

    if kind == "shift":
        return _maybe_wrap(("shift", node[1], rewrite(node[2], rng, probability),
                            node[3]), rng, probability)

    if kind == "mux":
        cond = rewrite(node[1], rng, probability)
        then_expr = rewrite(node[2], rng, probability)
        else_expr = rewrite(node[3], rng, probability)
        if rng.random() < probability:
            # c ? t : e  ==  (c == 0) ? e : t
            # The condition must be a genuine 0/1 value: bitwise ~ of a widened
            # comparison is never zero, so it cannot be used to negate here.
            is_zero = ("cmp", "==", cond, ("const", 0))
            return ("mux", is_zero, else_expr, then_expr)
        return ("mux", cond, then_expr, else_expr)

    if kind == "cmp":
        left = rewrite(node[2], rng, probability)
        right = rewrite(node[3], rng, probability)
        if node[1] == ">" and rng.random() < probability:
            return ("cmp", "<", right, left)              # x > y == y < x
        return ("cmp", node[1], left, right)

    return node


def _maybe_wrap(node, rng, probability):
    """Add logic that cannot change the value: x&x, x|x, x^0, x+0."""
    if rng.random() >= probability * 0.4:
        return node
    trick = rng.randrange(4)
    if trick == 0:
        return ("bin", "&", node, node)
    if trick == 1:
        return ("bin", "|", node, node)
    if trick == 2:
        return ("bin", "^", node, ("const", 0))
    return ("bin", "+", node, ("const", 0))


# ---------------------------------------------------------------------------
# bug injection  (A -> C)
# ---------------------------------------------------------------------------

SWAPS = {"&": "|", "|": "&", "^": "&", "+": "-", "-": "+", "*": "+"}
CMP_SWAPS = {"<": ">", ">": "<", "==": "<"}


def mutate(node, rng):
    """Corrupt exactly one place in the tree. Returns (new_node, changed)."""
    sites = []

    def collect(n, path):
        if n[0] in ("bin", "cmp", "shift", "un", "const"):
            sites.append(path)
        if n[0] in ("bin", "cmp"):
            collect(n[2], path + (2,))
            collect(n[3], path + (3,))
        elif n[0] == "un":
            collect(n[2], path + (2,))
        elif n[0] == "shift":
            collect(n[2], path + (2,))
        elif n[0] == "mux":
            for index in (1, 2, 3):
                collect(n[index], path + (index,))

    collect(node, ())
    if not sites:
        return node, False

    target = rng.choice(sites)

    def apply(n, path):
        if not path:
            return corrupt(n, rng)
        index = path[0]
        parts = list(n)
        parts[index] = apply(n[index], path[1:])
        return tuple(parts)

    return apply(node, target), True


def corrupt(n, rng):
    kind = n[0]
    if kind == "bin" and n[1] in SWAPS:
        return ("bin", SWAPS[n[1]], n[2], n[3])
    if kind == "cmp" and n[1] in CMP_SWAPS:
        return ("cmp", CMP_SWAPS[n[1]], n[2], n[3])
    if kind == "shift":
        return ("shift", "<<" if n[1] == ">>" else ">>", n[2], n[3])
    if kind == "un":
        return n[2]                       # drop the operator
    if kind == "const":
        return ("const", n[1] ^ 1)
    if kind == "bin":
        return ("bin", n[1], n[3], n[2])  # swap operands
    return n


# ---------------------------------------------------------------------------
# Verilog emission
# ---------------------------------------------------------------------------

def emit_module(name, inputs, width, exprs):
    lines = ["module %s(" % name]
    ports = ["    input [%d:0] %s" % (width - 1, i) for i in inputs]
    ports += ["    output [%d:0] y%d" % (width - 1, k) for k in range(len(exprs))]
    lines.append(",\n".join(ports))
    lines.append(");")
    for k, expr in enumerate(exprs):
        lines.append("  assign y%d = %s;" % (k, emit(expr, width)))
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# ground truth
# ---------------------------------------------------------------------------

def all_vectors(inputs, width):
    space = range(1 << width)
    for combo in itertools.product(space, repeat=len(inputs)):
        yield dict(zip(inputs, combo))


def designs_differ(sim_a, sim_b, inputs, width):
    """Exhaustive comparison - the oracle the checker is graded against."""
    for values in all_vectors(inputs, width):
        if sim_a.eval(values) != sim_b.eval(values):
            return True, values
    return False, None


# ---------------------------------------------------------------------------
# one fuzz round
# ---------------------------------------------------------------------------

class Failure(Exception):
    pass


def fuzz_round(rng, width, num_inputs, num_outputs, depth, verbose=False):
    inputs = ["i%d" % k for k in range(num_inputs)]
    exprs = [gen_expr(rng, inputs, width, depth) for _ in range(num_outputs)]

    src_a = emit_module("dut", inputs, width, exprs)
    src_b = emit_module("dut", inputs, width,
                        [rewrite(e, rng) for e in exprs])

    mutated = []
    changed_any = False
    for expr in exprs:
        new_expr, changed = mutate(expr, rng)
        mutated.append(new_expr)
        changed_any = changed_any or changed
    src_c = emit_module("dut", inputs, width, mutated)

    try:
        sim_a = Simulator(text=src_a)
        sim_b = Simulator(text=src_b)
        sim_c = Simulator(text=src_c)
    except (ElaborationError, VerilogSyntaxError) as exc:
        raise Failure("frontend rejected generated Verilog: %s\n%s" % (exc, src_a))

    findings = []

    # --- A vs B: rewrites must preserve behaviour --------------------------
    differ, witness = designs_differ(sim_a, sim_b, inputs, width)
    if differ:
        raise Failure(
            "REWRITE BUG: a semantics-preserving rewrite changed behaviour at "
            "%s\n--- A ---\n%s\n--- B ---\n%s" % (witness, src_a, src_b))

    miter = build_miter(None, None, spec_text=src_a, impl_text=src_b)
    result = check_sat(miter, presimulate=True)
    if not result["equivalent"]:
        raise Failure(
            "CHECKER BUG: reported NOT EQUIVALENT for a pair that exhaustive "
            "simulation proves equal\n--- A ---\n%s\n--- B ---\n%s"
            % (src_a, src_b))
    findings.append(("A-vs-B", "equivalent", result["resolved_by"]))

    # --- A vs C: the mutant, whatever ground truth says --------------------
    if changed_any:
        differ_c, witness_c = designs_differ(sim_a, sim_c, inputs, width)
        miter_c = build_miter(None, None, spec_text=src_a, impl_text=src_c)
        result_c = check_sat(miter_c, presimulate=True)

        if differ_c and result_c["equivalent"]:
            raise Failure(
                "CHECKER BUG: reported EQUIVALENT but simulation differs at %s"
                "\n--- A ---\n%s\n--- C ---\n%s" % (witness_c, src_a, src_c))
        if not differ_c and not result_c["equivalent"]:
            raise Failure(
                "CHECKER BUG: reported NOT EQUIVALENT but the mutant is "
                "functionally identical\n--- A ---\n%s\n--- C ---\n%s"
                % (src_a, src_c))

        # Any counterexample must actually reproduce.
        if not result_c["equivalent"]:
            cex = result_c["counterexample"]
            values = {k: v["value"] for k, v in cex["inputs"].items()}
            if sim_a.eval(values) == sim_c.eval(values):
                raise Failure(
                    "CHECKER BUG: counterexample %s does not reproduce"
                    "\n--- A ---\n%s\n--- C ---\n%s" % (values, src_a, src_c))

        findings.append(("A-vs-C",
                         "differs" if differ_c else "equivalent (benign mutation)",
                         result_c["resolved_by"]))

    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rounds", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20260828)
    parser.add_argument("--width", type=int, default=3,
                        help="bit width of every signal (default 3)")
    parser.add_argument("--inputs", type=int, default=3)
    parser.add_argument("--outputs", type=int, default=2)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    started = time.perf_counter()

    space = (1 << args.width) ** args.inputs
    print("=" * 72)
    print("  Randomised differential testing")
    print("=" * 72)
    print("  %d rounds, seed %d" % (args.rounds, args.seed))
    print("  circuits: %d inputs x %d bits, %d outputs, expression depth %d"
          % (args.inputs, args.width, args.outputs, args.depth))
    print("  ground truth: exhaustive simulation over all %d input vectors"
          % space)
    print("")

    benign = 0
    resolved = {}
    for index in range(args.rounds):
        try:
            findings = fuzz_round(rng, args.width, args.inputs, args.outputs,
                                  args.depth, verbose=args.verbose)
        except Failure as exc:
            print("\nROUND %d FAILED\n" % index)
            print(exc)
            return 1
        for tag, verdict, how in findings:
            resolved[how] = resolved.get(how, 0) + 1
            if "benign" in verdict:
                benign += 1
        if args.verbose:
            print("  round %3d ok" % index)
        elif (index + 1) % 20 == 0:
            print("  %d/%d rounds passed" % (index + 1, args.rounds))

    elapsed = time.perf_counter() - started
    print("")
    print("=" * 72)
    print("  ALL %d ROUNDS PASSED   (%.1f s)" % (args.rounds, elapsed))
    print("  checker agreed with exhaustive simulation on every pair.")
    print("  %d mutations turned out to be behaviour-preserving and were "
          "correctly reported equivalent." % benign)
    print("  verdicts by stage: %s"
          % ", ".join("%s=%d" % kv for kv in sorted(resolved.items())))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(run_big_stack(main))
