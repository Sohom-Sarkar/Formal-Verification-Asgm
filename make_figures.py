"""Regenerate the Graphviz and AIGER artefacts in results/figures/.

Render the .dot files with Graphviz, e.g.

    dot -Tpng results/figures/c17_design_aig.dot -o c17.png

Run:  python make_figures.py
"""

import os
import sys
import threading

from eqcheck.equiv import build_miter, variable_order
from eqcheck.bdd import build_from_aig
from eqcheck.sim import Simulator
from eqcheck import export

OUT = "results/figures"


def main():
    os.makedirs(OUT, exist_ok=True)

    # 1. The c17 circuit itself: six NAND gates, small enough to read.
    sim = Simulator("tests/c17_gates.v")
    roots = [lit for _, _, bits in sim.outputs for lit in bits]
    count = export.write_aig_dot(sim.aig, roots, OUT + "/c17_design_aig.dot",
                                 root_labels=[name for name, _, _ in sim.outputs])
    print("c17 design AIG            %2d nodes -> %s/c17_design_aig.dot"
          % (count, OUT))

    # 2. A small miter that is NOT constant, so the BDD is worth drawing:
    #    a 3-bit ripple-carry adder against a deliberately broken one.
    good = """module a3(input [2:0] a, input [2:0] b, output [3:0] s);
  assign s = a + b;
endmodule"""
    bad = """module a3(input [2:0] a, input [2:0] b, output [3:0] s);
  wire [2:0] c;
  assign c[0] = 1'b0;
  assign s[0] = a[0] ^ b[0];
  assign c[1] = a[0] & b[0];
  assign s[1] = a[1] ^ b[1] ^ c[1];
  assign c[2] = a[1] & b[1];              // BUG: carry term dropped
  assign s[2] = a[2] ^ b[2] ^ c[2];
  assign s[3] = (a[2] & b[2]) | (c[2] & (a[2] ^ b[2]));
endmodule"""

    miter = build_miter(None, None, spec_text=good, impl_text=bad)
    count = export.write_aig_dot(miter.aig, [miter.root],
                                 OUT + "/small_miter_aig.dot",
                                 root_labels=["miter"])
    print("3-bit buggy miter AIG     %2d nodes -> %s/small_miter_aig.dot"
          % (count, OUT))

    order = variable_order(miter, "interleaved")
    manager, bdd_roots, level_of_input = build_from_aig(
        miter.aig, [miter.root], order=order)
    names = {level: miter.aig.input_names.get(node, "n%d" % node)
             for node, level in level_of_input.items()}
    count = export.write_bdd_dot(manager, bdd_roots[0],
                                 OUT + "/small_miter_bdd.dot", var_names=names)
    print("3-bit buggy miter BDD     %2d nodes -> %s/small_miter_bdd.dot"
          % (count, OUT))

    # 3. AIGER export of a real miter, for independent checking with ABC.
    big = build_miter("tests/adder16_rca.v", "tests/adder16_ks.v")
    info = export.write_aiger(big.aig, [big.root], OUT + "/adder16_miter.aag",
                              output_names=["miter"])
    print("16-bit RCA vs KS AIGER    %d inputs, %d ANDs -> %s/adder16_miter.aag"
          % (info["inputs"], info["ands"], OUT))
    print("")
    print('verify externally:  abc -c "read_aiger %s/adder16_miter.aag; sat"'
          % OUT)
    return 0


def _entry():
    sys.setrecursionlimit(100000)
    box = {}

    def target():
        try:
            box["code"] = main()
        except BaseException as exc:            # noqa: BLE001
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
