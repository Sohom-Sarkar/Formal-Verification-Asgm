# Combinational Equivalence Checking

Formal Verification, Assignment 1, Problem C: build an equivalence checker
(SAT or BDD) that verifies two versions of a combinational design against each
other.

## The problem

Two Verilog files describing the same combinational circuit in different ways.
Do they compute the same function for every input?

This comes up constantly in practice: an engineer rewrites a slow circuit into
a fast one, a synthesis tool restructures a netlist, a block is swapped for a
supposedly drop-in replacement. The running example here is a 16-bit
ripple-carry adder against a Kogge-Stone adder. Same function, no structural
resemblance whatsoever.

Testing cannot answer this. A 16-bit adder has 33 input bits, so 8.6 billion
combinations, which is borderline feasible. The 128-bit adder in this test suite
has 257 input bits and 2^257 combinations, which is not feasible now or ever.
Random testing finds bugs but never establishes their absence, so the only route
to "equivalent" is proof.

## Scope

Everything except the SAT solver is implemented here: Verilog lexer and parser,
elaborator and bit-blaster, AIG, Tseitin encoder, SAT sweeping, ROBDD package,
algebraic engine, fault localiser, AIGER/Graphviz export. PySAT provides the
solver. 5,411 lines across 17 modules, plus 1,406 lines of test and experiment
drivers.

I deliberately did not use Yosys as a frontend. Yosys can do the whole
assignment on its own (`miter -equiv` then `sat -verify`), which would have made
this a wrapper. Writing the parser also turned out to enable two later features:
hierarchical signal names for fault localisation, and direct structural access
for the algebraic backend. The cost is a restricted Verilog subset.

All engines consume one intermediate representation, an And-Inverter Graph, so
the frontend is written once and five decision procedures share it.

## Architecture

```
   design A.v ──┐
                ├── lex ── parse ── elaborate ── bit-blast ──┐
   design B.v ──┘                                            │
                                                             ▼
                                       shared AIG + miter: OR(A_i XOR B_i)
                                                             │
      ┌───────────────┬────────────────┬─────────────────────┼──────────────┐
      ▼               ▼                ▼                     ▼              ▼
 structural       random           SAT sweeping         Tseitin → CNF    ROBDD
  hashing        simulation                               → PySAT

                  arithmetic circuits skip the miter and go through
                  algebraic reduction against a spec polynomial
```

| Module | Lines | |
|---|---:|---|
| `elaborate.py` | 1115 | parameters, generate, hierarchy, always blocks, bit-blasting |
| `equiv.py` | 704 | miter, SAT pipeline, BDD driver, ordering search, minimisation |
| `cli.py` | 597 | command line |
| `vparser.py` | 585 | recursive-descent parser |
| `localize.py` | 550 | fault localisation and N-fault diagnosis |
| `aig.py` | 308 | AIG, structural hashing, Tseitin encoder |
| `sweep.py` | 305 | SAT sweeping |
| `algebraic.py` | 288 | polynomial reduction |
| `bdd.py` | 197 | ROBDD package |
| `export.py`, `lexer.py`, `vast.py`, `simulate.py`, `solvers.py`, `sim.py` | 730 | |

## Frontend

Hand-written lexer and recursive-descent parser, then an elaborator that
bit-blasts everything to single-bit AIG nodes.

Elaboration evaluates parameters first (port widths can depend on them), unrolls
`generate` loops renaming locally declared signals per iteration, resolves
generate-`if` at elaboration time, creates a bit vector per signal, registers a
driver for every driven bit, and then resolves bits lazily backwards from the
outputs.

Lazy resolution means declaration order is irrelevant and unused logic is never
built. It also gives combinational-loop detection for free: re-entering a signal
that is still being resolved is exactly a loop.

Two details that mattered more than expected:

**`casez` wildcards.** Literals can contain `x`, `z` or `?` digits, which are
don't-cares in a `casez` label. Substituting zero for them — the obvious
implementation — turns `8'b1???????` into `8'b10000000` and silently
mis-compiles every priority encoder. The lexer computes an `xz_mask` alongside
the value and it is carried through to elaboration, where those bit positions
are dropped from the comparison.

**Latch detection.** Inside an `always` block the elaborator tracks per bit
whether it is assigned on every path, taking the AND of both arms when merging
an `if`/`else`. A bit that is not gets a warning, because that is where a
synthesiser infers a latch and the design is not really combinational.

Operator bit-blasting: `+` is a ripple-carry chain, `-` is `a + ~b + 1`, `*` is
shift-and-add, `<` is the borrow out of `a + ~b + 1`, variable shifts become a
logarithmic barrel shifter, `==` is an AND of XNORs. `always` blocks execute
symbolically, with `if`/`else` multiplexing the two branch environments and
`case` folding in reverse so the first match wins.

Supported: ANSI and non-ANSI ports, vectors and part-selects, `assign`, gate
primitives, hierarchy, parameters, nested generate loops, generate-`if`,
`always @(*)` with `if`/`else`/`case`/`casez`/`for`.

Not supported: sequential logic, division and modulo, `x`/`z` as values, memory
arrays. All raise explicit errors rather than producing wrong logic quietly.

## AIG and the miter

Two-input AND nodes with inversion in the low bit of each edge (AIGER
convention), so complementing is free. Every node passes a structural hash.

Both designs go into one shared graph, so anything they build identically
collapses to the same nodes and the comparison folds away by itself. Several
test pairs are discharged this way with no solver call, and the tool reports
when a verdict came from structural hashing rather than claiming a proof it did
not work for.

The miter is Brand's construction (ICCAD 1993): elaborate both designs onto the
same primary inputs, XOR corresponding outputs, OR the results. That single
literal is 1 exactly on inputs where the designs disagree, so UNSAT means
equivalent and SAT hands back a counterexample. Sharing the inputs is the part
that makes the XOR mean anything.

## The four stages

The pipeline tries the cheap things first and reports which one produced the
answer.

### Random simulation

Each node carries a signature: a Python int used as a bit-vector holding its
value under 512 random inputs, so one `&` evaluates all 512 at once.

Both buggy designs are refuted in under a millisecond, and across 500 fuzz
rounds simulation resolved 481 of the verdicts. Falsification is cheap; only
proofs are expensive.

Signatures also filter candidates for sweeping. If two nodes are equivalent
their signatures must match, so differing signatures are proof of
non-equivalence. That makes the filter sound: it never hides a real equivalence,
it only stops the solver being asked about hopeless pairs.

### SAT sweeping

Kuehlmann & Krohm, DAC 1997. A plain miter throws away everything the designs
have in common — two adders compute the same internal carries — so sweeping
proves internal equivalences bottom-up instead.

The cone is rebuilt into a fresh AIG in topological order, maintaining the
invariant that `mapping[node]` computes the same function as `node` did. For
each node, look for an earlier member of its signature class and try to prove
them equal:

```
lit = new.mk_and(map_lit(a), map_lit(b))
key = canonical_signature(sig)              # keys sig and ~sig together

for (candidate, candidate_node) in classes[key][:24]:
    phase  = (sigs[candidate_node] != sig)
    target = ¬candidate if phase else candidate
    if prove_equivalent(lit, target):
        mapping[node] = target              # merge
        break
```

Equivalence is tested incrementally under assumptions, in both directions:

```
solve([ a, ¬b])  UNSAT  ⟹  a → b
solve([¬a,  b])  UNSAT  ⟹  b → a
```

One direction alone only proves implication. Assumptions rather than added
clauses means one solver instance serves the whole sweep, so clauses learned
proving carry bit 3 still help at bit 4.

Merges cascade for free. Setting `mapping[node] = target` means later nodes
build on `target`, and structural hashing then collapses parents that become
identical, no proof needed. That is why 12 merges annihilate a 341-node cone:
by the time the walk reaches the output, both sides are the same node and the
XOR folds to `FALSE`.

Refuted candidates feed their counterexample back into the simulator, which
splits the signature class so the same wrong guess is not repeated. **I
documented this step and then never wired it up** — the counterexamples were
appended to a list nothing read. Fixing it cut wasted solver calls on the
Kogge-Stone pair from 435 to 71, and total calls from 528 to 114. I found it
while writing the algorithm description for this report.

Sweeping is sound (every merge has an UNSAT proof) and complete (if the root
does not fold, the full solve still runs on the smaller graph), so it can only
help or waste time.

### Tseitin encoding and SAT

The naive circuit-to-CNF conversion substitutes gate definitions until
everything is in terms of the inputs, which duplicates any node feeding two
parents and turns a chain of *n* XORs into 2^n terms.

Tseitin's fix is to give every internal wire a variable. For an AND node,
`c ↔ (a ∧ b)` splits into three clauses:

```
(¬c ∨ a)   (¬c ∨ b)   (c ∨ ¬a ∨ ¬b)
```

One variable and three clauses per gate, linear in circuit size. Not logically
equivalent to the original (it has extra variables) but equisatisfiable, which
is all that is needed. Inverters cost nothing because inversion is a sign flip
on the DIMACS literal. DIMACS variable 1 is pinned false and stands for the AIG
constant, which removes special-casing everywhere else. Only the miter cone is
encoded.

The 16-bit ripple-vs-CLA miter is 335 AND nodes, 375 variables, 1,025 clauses.

CaDiCaL then runs CDCL: propagate, decide, and on a conflict derive a learned
clause from the implication graph and backjump. Learning is what separates this
from brute force — a learned clause rules out an exponentially large family of
assignments, not one. The 128-bit adder settles 2^257 input vectors in about
0.15 s, which no enumeration could approach.

**Solver screening.** PySAT bundles about twenty solvers and three of them
(`kissat404`, `cryptosat`, `minisatgh`) abort the process on construction under
Windows/CPython 3.14, which try/except cannot catch. I found this when a
benchmark run died with a segfault. `solvers.py` screens names before
construction and lists the 17 that work. Each was probed in a subprocess so a
crashing one could not take the probe down.

## BDD backend

Written from scratch so node counts are real and ordering can be experimented
with. Unique table, `ite` with a computed table, both reduction rules enforced
at construction, which makes an ROBDD canonical for a fixed order (Bryant 1986).
Equivalence then needs no search at all: the miter is unsatisfiable exactly when
its BDD is the FALSE terminal.

Four static orderings (`interleaved`, `dfs`, `declaration`, `reverse`), plus
`auto`, which tries all four with each attempt capped at the best size found so
far so hopeless orders abort immediately, and `sift`, a rebuild-based version of
Rudell's sifting. Rebuilding per trial is asymptotically worse than in-place
level swapping, and the measured payoff is correspondingly small.

A node budget turns the multiplier blow-up into an "aborted" result instead of
an out-of-memory crash, which is what makes the exponential behaviour
measurable.

## Algebraic backend

Added after measurement showed multipliers were the one case where every
search-based method failed. Follows the AMulet work (Kaufmann, Biere & Kauers).

Work in `Z[x₁..xₙ]` modulo `x² = x`. Each AND node gives a relation, and the
property to prove is the specification polynomial:

```
v − L(l₁)·L(l₂) = 0        L(x) = x, or 1 − x for a negated literal
SPEC = Σ 2^i·p_i − (Σ 2^i·a_i)(Σ 2^j·b_j)
```

The circuit is a correct multiplier exactly when SPEC reduces to zero modulo the
gate relations.

Reduction modulo an arbitrary polynomial set needs a Gröbner basis, whose
computation is doubly exponential. The reason this works at all is that the gate
polynomials already are a Gröbner basis when every gate is ordered above its
inputs, so reduction degenerates into substitution in reverse topological order.
No Buchberger.

Since `x² = x`, exponents never exceed one, so a monomial is a set of variables
and a polynomial is a `frozenset → coefficient` dict. Multiplying monomials is a
set union. Substituting `v := P` splits `S = v·Q + R` and rewrites it as
`P·Q + R`.

| Width | Gates | Peak terms | Algebraic | Plain SAT |
|---:|---:|---:|---:|---:|
| 8 | 612 | 221 | 0.015 s | 8.5 s |
| 12 | 1,492 | 309 | **0.034 s** | **11,509 s** |
| 16 | 2,756 | 429 | 0.069 s | not reachable |
| 32 | 11,652 | 1,229 | 0.578 s | not reachable |
| 64 | 47,876 | 4,365 | **7.6 s** | not reachable |

The 12×12 case went from 3 hours 12 minutes to 34 milliseconds. Peak polynomial
size grows roughly quadratically in width, which is why this scales where search
does not.

It also proves something stronger than equivalence: each design is checked
against the arithmetic specification itself, so no miter is built and neither
design looks at the other.

The catch is that the method is sharply asymmetric. A correct 6×6 multiplier
peaks at 186 terms and finishes in 0.01 s; a broken one, with a single partial
product mis-shifted, peaks at **621,004 terms and takes 33 s**, because nothing
cancels. That is the opposite profile to random simulation, so the two
complement each other rather than compete.

This is the core method, not AMulet. There is no adder detection, variable
elimination or XOR rewriting, so a heavily restructured multiplier can still
exhaust the term budget, which is reported as inconclusive rather than guessed.

## Diagnosis

Three features turn "they differ" into something a designer can act on.

**Counterexample minimisation.** A care set is valid when fixing it *forces* the
miter to 1, so the test for dropping a bit is that `solve([miter = 0] +
remaining)` stays UNSAT — not "is the miter still satisfiable", which it always
is. I got this wrong first time round and the criterion freed every bit,
reporting a care set of size zero. On the buggy adder it cuts 33 input bits to
8, `a[3:6]` and `b[3:6]`, which is exactly one carry-lookahead block's inputs
plus its carry source. Verified independently: 200 of 200 random completions
still expose the bug.

**Fault localisation.** Smith & Veneris, IEEE TCAD 2005. A gate `n` is a
single-fix location if some replacement at `n` makes the designs equivalent.
Since the replacement value is only 0 or 1:

```
SAT( miter(n := 0) ∧ miter(n := 1) )  ⟹  n is not a fix location
UNSAT                                  ⟹  n is
```

Two copies of the revised design share the primary inputs, one with `n` tied low
and one high, each mitered against the reference. Structural hashing shares
everything outside `n`'s fan-out cone, so this costs far less than two circuits.
Only gates feeding a failing output are considered.

On a 16-bit ripple-carry adder with one broken gate: 142-gate cone, 55
candidates examined, **one survivor, `c[10]`** — the gate I broke. Candidates
are reported by hierarchical Verilog name (`u0.c[3]`, not `node 61`), which
needed the elaborator to retain every scope.

**N-fault diagnosis.** Single-fix search fails when several gates are wrong at
once, which is common: a bug inside a module instantiated four times is four
faults. So: *k* replicas of the design sharing selector variables `s_n`, each
gate cut and driven by a free variable when `s_n = 1`, inputs pinned to a
failing vector, outputs pinned to the reference, and `Σ s_n ≤ N` via PySAT
cardinality constraints. Searching N = 1, 2, 3 gives a minimum-cardinality
diagnosis, and each candidate set is verified exactly by enumerating all `2^|S|`
forcings.

On the carry-lookahead bug it reports no single fix, minimum size 4, and
verifies `{u0.g[0], u1.g[0], u2.g[0], u3.g[0]}` — one gate per instance.

Two caveats the tool prints itself. Many gate sets can repair a design: I
checked directly that the true fault set `{u0.c[3]…u3.c[3]}` is also valid, so
the true fault is guaranteed to be *among* the diagnoses but not to rank first.
And a diagnosis made entirely of primary outputs is always valid and always
useless — it was ranked first initially, so those are now ranked last.

## Test cases

Fourteen pairs across 22 Verilog files, each a different topology rather
than a cosmetic edit.

| # | Reference | Revision | Expected |
|---|---|---|---|
| 1 | 16-bit ripple-carry (hierarchical, `generate`) | carry-lookahead, four 4-bit blocks | equivalent |
| 2 | 16-bit ripple-carry | Kogge-Stone parallel prefix | equivalent |
| 3 | 16-bit carry-lookahead | Kogge-Stone | equivalent |
| 4 | *N*-bit ripple-carry (parameterised) | *N*-bit carry-select | equivalent |
| 5 | 16-bit ripple-carry | CLA with a dropped carry product term | **not** equivalent |
| 6 | 16-bit ripple-carry | ripple-carry with one broken gate | **not** equivalent |
| 7 | 8-bit ALU, behavioural `case` | structural mux tree | equivalent |
| 8 | 8-bit ALU, behavioural `case` | same tree, two shifts swapped | **not** equivalent |
| 9 | 8-bit shifter, `a << s` | decoder crossbar | equivalent |
| 10 | popcount by sequential `for` | balanced adder tree | equivalent |
| 11 | 8→3 priority encoder, `casez` | one-hot mask + OR plane | equivalent |
| 12 | Gray encode then decode | the identity | equivalent |
| 13 | ISCAS-85 c17, six NAND gates | 32-entry truth table | equivalent |
| 14 | *N*×*N* multiplier, `a * b` | carry-save array multiplier | equivalent |

Measured complexity, so the medium-to-large requirement can be checked rather
than taken on trust. "Miter gates" is the AND-node count in the cone actually
driving the comparison, after structural hashing.

| Test case | Inputs | Input space | Miter gates | Depth | CNF clauses |
|---|---:|---|---:|---:|---:|
| 128-bit ripple vs carry-select | 257 | 2^257 | 3,097 | 268 | 9,292 |
| 64-bit ripple vs carry-select | 129 | 2^129 | 1,545 | 139 | 4,636 |
| 32-bit ripple vs carry-select | 65 | 2^65 | 769 | 74 | 2,308 |
| 12×12 multiplier | 24 | 2^24 | 2,645 | 91 | 7,936 |
| 8×8 multiplier | 16 | 2^16 | 1,085 | 59 | 3,256 |
| 16-bit ripple vs Kogge-Stone | 33 | 2^33 | 416 | 41 | 1,249 |
| 16-bit CLA vs Kogge-Stone | 33 | 2^33 | 419 | 22 | 1,258 |
| 16-bit ripple vs CLA | 33 | 2^33 | 341 | 41 | 1,024 |
| 8-bit ALU, 8 operations | 19 | 2^19 | 652 | 38 | 1,957 |
| 8-bit shifter vs crossbar | 11 | 2^11 | 165 | 15 | 496 |
| popcount seq vs tree | 8 | 2^8 | 159 | 21 | 478 |
| priority encoder | 8 | 2^8 | 96 | 21 | 289 |
| Gray roundtrip vs identity | 8 | 2^8 | 69 | 21 | 208 |
| ISCAS-85 c17 | 5 | 2^5 | 163 | 54 | 490 |

Eight of the fourteen are substantial. The clearest evidence is not a size
figure but a runtime: the 12×12 multiplier takes 11,509 seconds of CDCL to prove
by the plain miter, so it is not a toy.

The last four rows are small and are there for reasons other than size. c17 is
the standard ISCAS-85 benchmark; the Gray roundtrip is interesting for its
property (encode composed with decode is the identity, though nothing in the
circuit says so); the priority encoder is the only `casez` coverage; the shifter
covers the barrel and crossbar paths cheaply. They supplement the suite rather
than carry it.

Cases 4 and 14 are parameterised and drive the scaling studies. Case 2 and 3
exercise nested generate loops with an inner generate-`if`, which is the most
demanding frontend construct here. The c17 truth table was generated from the
published c17 function, not from this tool's simulator, so the comparison is
independent. Case 6 exists because 5 and 8 are both multi-fault and cannot
exercise single-fix localisation.

## Validation

A checker that grades itself proves very little, so `run_tests.py` runs 54
checks in seven layers:

1. Each of 18 designs simulated alone against an independent Python model,
   exhaustively where the input space allows. This is the layer that catches
   frontend bugs which would otherwise cancel out between the two designs and
   produce a false EQUIVALENT.
2. Each pair against its expected verdict, SAT and BDD required to agree.
3. Every counterexample re-simulated through both designs.
4. 200 random completions of each minimised care set must still expose the bug.
5. Sweeping, plain miter and per-output analysis must agree.
6. For designs with a planted fault, diagnosis must name the gate that was
   broken. These are the only tests where the correct answer is known exactly.
7. The algebraic backend must prove genuine multipliers and refuse to prove four
   deliberately corrupted ones.

`fuzz.py` covers the designs I did not think to write. Each round generates a
random circuit A, rewrites it into B using only semantics-preserving
transformations (De Morgan, XOR expansion, commutation, double negation,
two's-complement subtraction, redundancy insertion), and mutates it into C by
corrupting one operator. Ground truth comes from exhaustive simulation, which is
independent of the checker, and the checker must agree on both pairs with every
counterexample reproducing.

More than 1,500 rounds across eight seeds during development, zero
disagreements. Around 4% of mutations turn out to be behaviour-preserving by
accident and are correctly called equivalent.

The fuzzer paid for itself on its first run by finding a bug in one of my
rewrite rules: `~{2'd0, (c == 0)}` was being used to invert a mux condition, but
a bitwise complement of a widened comparison is never zero, so the "inverted"
condition was always true. That was the test generator rather than the checker,
but it is exactly the sort of thing hand-written tests never catch.

The miter also exports to AIGER for independent checking with ABC, and to
DIMACS. I validated the AIGER writer by writing a separate reader and confirming
it agreed with the internal simulator on 400 of 400 random vectors.

## Results

Full tables in `results/benchmark.md`. Single-threaded, one machine.

**Multiplier scaling.**

| Width | Miter gates | CNF clauses | SAT | SAT+sweep | BDD peak nodes |
|---:|---:|---:|---:|---:|---:|
| 5 | 363 | 1,070 | 0.071 s | 0.087 s | 6,196 |
| 6 | 564 | 1,673 | 0.365 s | 0.304 s | 21,061 |
| 7 | 807 | 2,402 | 1.388 s | 1.561 s | 67,442 |
| 8 | 1,092 | 3,257 | 8.515 s | 8.977 s | 210,561 |
| 9 | 1,419 | 4,238 | 33.03 s | 46.58 s | > 400,000 |

**BDD variable ordering.** `overflow` means past a 400,000-node budget.

| Design pair | interleaved | dfs | declaration | reverse | after sifting |
|---|---:|---:|---:|---:|---:|
| 16-bit RCA vs CLA | 4,557 | **843** | overflow | overflow | 843 |
| 16-bit RCA vs Kogge-Stone | 5,240 | **1,935** | overflow | overflow | 1,935 |
| 8-bit ALU | **7,498** | overflow | overflow | overflow | 6,511 |
| 8-bit shifter | 424 | 290 | overflow | **289** | 289 |
| popcount | 869 | **459** | overflow | overflow | 459 |
| 6×6 multiplier | 34,891 | overflow | 25,423 | **21,061** | 20,670 |

**Sweeping.** Eight of nine pairs discharge entirely bottom-up, cone to zero.
The 16-bit RCA-vs-CLA cone collapses from 341 nodes after 12 merges.

**Solvers**, same 8×8 CNF: CaDiCaL 1.5.3 11.37 s, CaDiCaL 1.9.5 11.54 s,
MiniSat 15.46 s, Glucose 16.66 s, MapleSAT 17.47 s, Lingeling 26.98 s. Tseitin
encoding is 3–11 ms throughout.

**Adder width**, ripple vs carry-select: at 128 bits (257 inputs, 3,132 AIG
nodes, depth 268) the frontend takes 0.239 s and the solver 0.150 s.

## What the measurements changed

Almost every conclusion here started as a different expectation.

**SAT sweeping has a crossover, and loses on easy instances.** I expected it to
help uniformly. It carries a fixed overhead — simulate, classify, run dozens of
small solves — and at widths 8 and 9 that overhead exceeds what the merges save
(0.94× and 0.71×, so a loss). But the plain miter is one monolithic UNSAT proof
whose cost explodes: 33 s at width 9 to 11,509 s at width 12, a factor of 350
for three extra bits. Sweeping decomposes it into about 73 small solves, and at
width 12 wins 3.57× (3,227 s against 11,509 s). The curves cross somewhere
between 9 and 12.

The striking part is that the width-12 win comes from only **21 merges in a
2,645-node cone**. It is not bulk simplification. A few well-placed internal
equivalences break one intractable proof into many tractable ones, which is
exactly the argument for sweeping in industrial tools: it changes the shape of
the curve on hard instances rather than helping easy ones.

**Brute force beats SAT on multipliers.** I had been presenting the multiplier
SAT times as "the cost of proof" until I measured exhaustive simulation
properly. Evaluating all 16.7 million inputs of the 12×12 miter, bit-parallel,
takes **5.4 seconds** against SAT's 11,509 — brute force wins by 2,100×.

That is not a contradiction with the 128-bit adder result, because the two
measure different axes. Input count decides whether enumeration is possible at
all (24 inputs trivial, 257 inputs impossible forever). Structural hardness
decides whether SAT is fast (adders easy at any width, multipliers pathological
at any width). Multipliers sit in the awkward corner of few inputs and brutal
structure, so enumeration wins; wide adders sit in the opposite corner. The
practical conclusion is to pick the engine by input count as well as structure.

**No ordering heuristic dominates.** DFS is 5.4× better than interleaved on the
16-bit adder and overflows entirely on the multiplier, where `reverse` wins. On
the ALU only interleaved survives. Since optimal ordering is NP-hard this is
why `auto` beats committing to a rule, and why sifting adds 13% on the ALU, 2%
on the multiplier, and nothing on the four designs where the best static order
was already good.

**BDDs hit a wall while SAT bends.** Multiplier BDD size roughly triples per
added bit and blows the budget at width 9, where SAT still finishes in 33 s.
Bryant's 1991 result in miniature: multiplier BDDs are exponential in every
variable order, so no heuristic rescues them.

**Two of the techniques here are current state of the art.** CaDiCaL's SAT
Competition 2025 entry lists "clausal congruence closure" and "clausal
equivalence sweeping" among its new techniques, and Kissat won three golds in
2024 largely on them. Those are structural hashing and SAT sweeping performed at
the CNF level — stages 1 and 3 here, done at circuit level because the circuit
is available, where Kissat has to reconstruct it from clauses.

**Algebraic methods are categorically better for arithmetic**, by a factor of
340,000 on the 12×12 multiplier. Not an incremental improvement but a change of
paradigm, from search to symbolic computation.

## Limitations

- Verilog subset: no sequential logic, division/modulo, `x`/`z` as values, or
  memory arrays. All raise explicit errors.
- Multi-fault diagnosis ranking is heuristic. The true fault is guaranteed to be
  among the valid diagnoses, not to be listed first, and I verified this
  directly on the CLA case.
- The algebraic backend is the core method, not AMulet, so a heavily
  restructured multiplier can still exhaust the term budget. That is reported as
  inconclusive.
- Algebraic refutation is expensive: proving a correct circuit is fast, refuting
  a broken one can blow up to 621,004 terms.
- Sifting rebuilds per trial rather than swapping levels in place, so it is far
  more expensive than it should be and gains little.
- Timings at width ≥ 10 come from separate long runs and vary by up to about 2×
  under load. The width-12 gaps are well outside that; the width-8/9 ones are
  inside it, which is why the sweeping crossover is stated as a range.
- Three bundled SAT solvers crash this platform and are screened out rather than
  fixed.
- No proof certificate. An UNSAT verdict is trusted from the solver. Emitting
  DRUP certificates and checking them independently is feasible — PySAT produced
  a 317-line proof for the adder miter when I tested it — but not implemented.

## Conclusion

The tool proves or refutes equivalence for two combinational Verilog designs,
and on a refutation it returns a distinguishing input, reduces it to the bits
that actually matter, and names the gate in the designer's own signal hierarchy
that would have to change. For arithmetic circuits it skips equivalence checking
altogether and proves the design against its mathematical specification.

Flattening everything to one AIG and building a miter was the decision that
paid off most: one frontend serves five decision procedures, and structural
hashing ends up doing real verification work for free.

Three genuine defects turned up during the work — a minimisation criterion that
asked the wrong question, a sweeping refinement loop that was documented but
never connected, and a fault-localisation ranking that put a useless answer
first. All three were found because a reported number looked wrong, not because
a test failed. That is the argument for making a verification tool report enough
detail to be caught lying.

## References

- Tseitin, *On the complexity of derivation in propositional calculus*, 1968
- Bryant, *Graph-based algorithms for boolean function manipulation*, IEEE ToC 1986
- Bryant, *On the complexity of VLSI implementations and graph representations of boolean functions with application to integer multiplication*, IEEE ToC 1991
- Brand, *Verification of large synthesized designs*, ICCAD 1993
- Rudell, *Dynamic variable ordering for ordered binary decision diagrams*, ICCAD 1993
- Kuehlmann & Krohm, *Equivalence checking using cuts and heaps*, DAC 1997
- Smith, Veneris, Ali & Viglas, *Fault diagnosis and logic debugging using Boolean satisfiability*, IEEE TCAD 2005
- Kaufmann, Biere & Kauers, *Verifying large multipliers by combining SAT and computer algebra*, FMCAD 2019
- Biere et al., *Clausal congruence closure*, SAT 2024; *Clausal equivalence sweeping*, FMCAD 2024
- Berkeley ABC; Yosys; PySAT

## Running it

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

python run_tests.py                 # 54 checks
python fuzz.py --rounds 500
python benchmark.py                 # regenerates results/benchmark.md

python -m eqcheck tests/adder16_rca.v tests/adder16_ks.v --method both
python -m eqcheck tests/adder16_rca.v tests/adder16_rca_buggy1.v --minimize --localize
python -m eqcheck tests/mult_behav.v tests/mult_csa.v -p WIDTH=16 --method algebraic
```

Exit status: 0 equivalent, 1 not equivalent, 2 input error, 3 inconclusive,
4 backends disagree. Options are listed in the README.
