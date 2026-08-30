# Combinational Equivalence Checker (Problem C)

A self-contained equivalence checker for combinational Verilog, with **both** a
SAT backend and a BDD backend behind one interface.

Given two versions of a combinational design it either **proves** they compute
the same function for every possible input, or produces a concrete input vector
on which they disagree — minimised down to the bits that actually matter.

Everything except the SAT solver itself is implemented here: the Verilog lexer
and parser, the elaborator and bit-blaster, the AIG representation, the Tseitin
encoder, the SAT-sweeping engine, the ROBDD package, and the simulator. The
only external dependency is [PySAT](https://pysathq.github.io/), used purely as
the solving engine.

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python

.venv/Scripts/python run_tests.py                   # regression suite (54 checks)
.venv/Scripts/python fuzz.py --rounds 500           # randomised differential testing
.venv/Scripts/python benchmark.py                   # experimental study
```

Check a pair of designs:

```bash
.venv/Scripts/python -m eqcheck tests/adder16_rca.v tests/adder16_ks.v --method both
```

Find and localise a bug:

```bash
.venv/Scripts/python -m eqcheck tests/alu8_behav.v tests/alu8_struct_buggy.v \
    --minimize --outputs
```

Discharge a proof entirely by sweeping (no output-miter solve at all), and
export the miter for independent checking with ABC:

```bash
.venv/Scripts/python -m eqcheck tests/adder16_rca.v tests/adder16_ks.v \
    --sweep --aiger miter.aag
```

---

## The decision pipeline

The checker does not simply build a miter and call a solver. It runs four
stages, cheapest first, and each one can settle the question outright:

```
  design A.v ──┐
               ├── parse ── elaborate ── bit-blast ──┐
  design B.v ──┘  (own parser)                       │
                                                     ▼
                                    shared AIG, miter = OR(A_i XOR B_i)
                                                     │
        ┌──────────────┬─────────────────┬───────────┴──────────┐
        ▼              ▼                 ▼                      ▼
  1. structural   2. random        3. SAT sweeping        4. Tseitin -> CNF
     hashing         simulation       (internal              -> PySAT
                                       equivalences)
   miter folds     any vector       miter folds to        UNSAT = equivalent
   to constant     that differs     FALSE bottom-up       SAT   = counterexample
   = equivalent    = refuted        = equivalent
```

The tool reports which stage produced the answer (`resolved by:` in the
output), so it is always clear whether a proof cost a solver call at all.

### 1. Frontend (`lexer.py`, `vparser.py`, `elaborate.py`)

A hand-written lexer and recursive-descent parser for a practical Verilog
subset, then an elaborator that bit-blasts down to single-bit AIG nodes.

Supported: ANSI and non-ANSI ports, vectors and part-selects, `assign`, gate
primitives, module instantiation and hierarchy, parameters and overrides,
nested `generate`/`genvar` loops, generate-`if`, and `always @(*)` blocks with
`if`/`else`, `case`, `casez`/`casex` and `for`. Operators include `+ - *`, all
bitwise and reduction operators, comparisons, constant and variable shifts,
concatenation, replication, and `?:`.

Not supported: sequential logic, division and modulo, `x`/`z` as *values*, and
memory arrays. These raise explicit errors rather than silently producing wrong
logic.

Three details worth noting:

* **Lazy, demand-driven resolution.** Bits are resolved backwards from the
  primary outputs, so declaration order does not matter and unused logic is
  never built. Re-entering a signal still being resolved is exactly a
  combinational loop, and is reported as one.
* **`casez` wildcards are real.** Bits written `?`/`z`/`x` in a case label are
  tracked from the lexer through to elaboration and dropped from the
  comparison. Treating them as literal zeroes (the easy mistake) would
  silently mis-compile every priority encoder.
* **Latch detection.** Inside an `always` block the elaborator tracks which
  bits are assigned on *every* path. A bit that is not gets a warning, since
  that is exactly where a synthesiser infers a latch — meaning the design is
  not really combinational and the comparison may be meaningless.

### 2. AIG with structural hashing (`aig.py`)

Two-input AND nodes with inversion in the low bit of each edge literal (the
AIGER convention), so complementing is free. Every node passes a **structural
hash**: identical sub-circuits are built once.

This is not only an optimisation. Both designs share one AIG, so any logic they
build identically collapses to the same nodes and the XOR folds to constant
`FALSE`: the miter is discharged with no solver at all. It is the degenerate
case of SAT sweeping, and the tool says when a proof came from it.

### 3. Random simulation (`simulate.py`)

Every node carries a *signature*: a Python integer used as a bit-vector holding
its value under hundreds of random input vectors at once, so one `&` evaluates
512 vectors in a single operation. Two uses:

* **Falsification.** Shallow bugs (a swapped operand, an inverted select) are
  caught by almost any random vector, in microseconds rather than a solver call.
  In practice this resolves most real mismatches.
* **Candidate filtering.** Nodes with different signatures are definitely not
  equivalent, so signatures are a *sound filter* for the sweeper below.

### 4. SAT sweeping (`sweep.py`)

Based on Kuehlmann & Krohm (DAC 1997), the technique that made equivalence
checking scale to real designs.

A plain output miter throws away everything the two designs have in common. Two
implementations of the same adder compute the same internal carries; proving
the outputs equal from scratch ignores that. Sweeping works bottom-up instead:
group nodes by simulation signature, walk in topological order, and ask the
solver whether each node is equivalent to an earlier member of its class. A
proved pair is **merged**, so every later node sees the merged version and
structural hashing cascades the result upward. A refuted pair feeds its
counterexample back into the simulator, splitting the class so the same wrong
guess is not made twice.

Every test is an **incremental** solve under assumptions — to ask whether `a`
and `b` are equal, ask for a counterexample in each direction:

```
solve([ a, -b])  UNSAT  =>  a implies b
solve([-a,  b])  UNSAT  =>  b implies a
```

One solver instance serves the whole sweep, so clauses learned proving one
equivalence help prove the next.

### 5. SAT backend

The **Tseitin transformation** encodes the miter cone into CNF in linear size:
each AND node `c = a & b` contributes `(¬c ∨ a) (¬c ∨ b) (c ∨ ¬a ∨ ¬b)`. Only
nodes inside the cone are encoded. The default solver is CaDiCaL 1.5.3;
`--solver` selects any of the 17 bundled solvers that work in this environment
(three crash the interpreter on construction here, so `eqcheck/solvers.py`
screens them out with a clear message instead of a segfault).

**Counterexample minimisation** shrinks a witness to the input bits that
actually provoke the bug. A care set is valid when fixing it *forces* the miter
to 1, so the test for dropping a bit is that `solve([miter = 0] + remaining)`
stays UNSAT. On the buggy adder this cuts 33 input bits down to 8, and those 8
point straight at the faulty carry-lookahead block.

**Per-output analysis** (`--outputs`) reports each output bit's verdict, cone
size and logic depth, using one incremental solver across all bits and skipping
bits that structural hashing already proved equal.

### 6. Fault localisation (`localize.py`)

Reporting *"they differ, here is an input"* leaves the designer to find the bug.
This goes further and asks **which gate would have to change**, following Smith
& Veneris, "Fault diagnosis and logic debugging using Boolean satisfiability"
(IEEE TCAD, 2005).

A gate `n` is a **single-fix location** if some replacement function at `n`
would make the designs equivalent — that is, if for every input there exists a
value at `n` repairing all outputs. Negating that, and noting the value ranges
over just `{0, 1}`, gives one propositional test:

```
SAT( miter(n := 0)  AND  miter(n := 1) )   =>  n is NOT a fix location
UNSAT                                      =>  n IS a valid fix location
```

Build two copies of the revised design sharing the primary inputs, one with `n`
tied low and one tied high, miter each against the reference, and ask for an
input that defeats both. Structural hashing shares everything outside the
fan-out cone of `n`, so forcing one gate two ways costs far less than two
circuits. Only gates feeding a *failing* output are considered.

**When several gates are wrong at once** — the common case, since a bug inside a
module instantiated four times *is* four faults — no single fix exists, and
`diagnose()` switches to counterexample-driven N-fault diagnosis: *k* replicas
of the design sharing one set of selector variables `s_n`, each gate cut and
driven by a free variable when `s_n = 1`, inputs pinned to a failing vector,
outputs pinned to the reference, and `sum(s_n) <= N` via PySAT cardinality
constraints. It searches N = 1, 2, … so the result is a minimum-cardinality
diagnosis, and each candidate set is then verified *exactly* by enumerating all
2^|S| forcings.

Candidates are reported by **hierarchical Verilog name** (`u0.c[3]`, not
`node 61`), which is what makes the output usable: the elaborator retains
every scope so an AIG node can be traced back to the signal that produced it.

An honest caveat: many gate sets can repair a design, and a diagnosis made
entirely of primary outputs is always valid and always useless. Those are
ranked last, and deeper gates preferred, but **the true fault is guaranteed to
be among the valid sets rather than guaranteed to be ranked first.**

### 7. Algebraic backend (`algebraic.py`)

Multipliers are the one case where CDCL collapses — we measured 3 h 12 min on a
12x12. The literature is blunt about why: search is the wrong tool for
arithmetic. This backend abandons it entirely, following the AMulet line of
work (Kaufmann, Biere & Kauers).

Work in `Z[x1..xn]` modulo the Boolean relations `x^2 = x`. Each AIG AND node
becomes a polynomial equation, and the property to prove becomes the
**specification polynomial**:

```
v - L(l1)*L(l2) = 0            (gate,  L(x)=x  or  1-x  for a negated literal)
SPEC = sum 2^i p_i  -  (sum 2^i a_i)(sum 2^j b_j)
```

The circuit is correct exactly when `SPEC` reduces to zero modulo the gate
relations. Reduction modulo an arbitrary polynomial set needs a Gröbner basis,
which is doubly exponential, but the gate polynomials **already are** one,
provided every gate is ordered above its inputs. So reduction degenerates to
plain substitution in **reverse topological order**: eliminate variables from
the outputs backwards and see whether everything cancels. No Buchberger, no
basis computation.

Because `x^2 = x`, a monomial is just a *set* of variables, so a polynomial is
a `frozenset -> coefficient` dict and multiplying monomials is a set union.

The payoff is not incremental:

| Multiplier | Plain SAT | Algebraic |
|---|---|---|
| 12x12 | **11,509 s** (3 h 12 m) | **0.034 s** |
| 64x64 (47,876 gates) | not reachable | **7.6 s** |

Note this proves each design against the *arithmetic specification*, not
against the other design: a stronger statement, and no miter is built at all.
Equivalence follows because both compute the same function.

**The catch, and it is a real one: proving is cheap, refuting is not.** On a
correct 6x6 multiplier the polynomial peaks at 186 terms and finishes in
0.01 s. On a broken one nothing cancels and the same reduction peaks at
**621,004 terms and takes 33 s**. That is the exact opposite profile to random
simulation, which refutes in microseconds and proves nothing, so the two are
complementary, not competing.

This is the textbook core of the method, not AMulet: it has none of the adder
detection, variable elimination or XOR rewriting that makes heavily optimised
industrial multipliers tractable, so a restructured multiplier can still blow
up. A term budget bounds that instead of exhausting memory, and a budget
overrun reports *inconclusive* rather than guessing.

### 8. BDD backend (`bdd.py`)

A from-scratch ROBDD package: unique table, `ite`-based apply with a computed
table, both reduction rules enforced at construction. Because an ROBDD is
**canonical** for a fixed order, equivalence needs no search: the miter is
unsatisfiable exactly when its BDD is the `FALSE` terminal.

Four static ordering heuristics are provided (`interleaved`, `dfs`,
`declaration`, `reverse`), plus `auto`, which tries all of them under a
shrinking node budget and keeps the winner, and `sift`, a rebuild-based
implementation of Rudell's sifting (ICCAD 1993) that refines the winner
further. A node budget turns the expected multiplier blow-up into a clean
"aborted" result rather than an out-of-memory crash.

### 9. Export (`export.py`)

`--aiger` writes the miter in AIGER format for **independent** checking with
Berkeley ABC (`abc -c "read_aiger miter.aag; sat"`), `--dimacs` writes the CNF
for any external solver, and `--dot` / `--bdd-dot` draw the AIG and BDD with
Graphviz.

---

## Validation

Seven layers in `run_tests.py`, plus a fuzzer, because a checker that only
grades itself proves very little.

1. **Frontend validation.** Each of 18 designs is simulated on its own against
   an independent golden model written in Python, exhaustively where the input
   space allows. This catches parser and bit-blaster bugs that would otherwise
   *cancel out* between the two designs and yield a false "EQUIVALENT".
2. **Equivalence checking.** Every pair against its expected verdict, with the
   SAT and BDD backends required to agree.
3. **Witness replay.** Every counterexample re-simulated through both designs
   to confirm they really do disagree on it.
4. **Care-set validation.** For each minimised counterexample, 200 random
   completions of the care set must *all* still expose the bug.
5. **Engine cross-checks.** SAT sweeping, the plain miter, and per-output
   analysis must agree with each other on every design.
6. **Localisation ground truth.** For designs with a deliberately planted
   fault, diagnosis must name the gate that was actually broken: the only
   tests where the correct answer is known exactly.
7. **Algebraic soundness, both directions.** The polynomial backend must prove
   genuine multipliers *and* refuse to prove broken ones — four deliberately
   corrupted multipliers, each of which must be refuted or reported
   inconclusive, never proved.

### Randomised differential testing (`fuzz.py`)

The hand-written suite covers designs I thought to write; the fuzzer covers the
ones I did not. Each round generates a random circuit A, rewrites it into B
using only semantics-preserving transformations (De Morgan, XOR expansion,
commutation, double negation, two's-complement subtraction, redundancy
insertion), and mutates it into C by corrupting one operator or operand. Ground
truth comes from **exhaustive simulation**, independent of the checker; the
checker must agree on both pairs, and every counterexample must reproduce.

**More than 1,500 rounds across eight seeds have been run during development,
with zero disagreements at every stage.** Around 4% of mutations turn out to
be behaviour-preserving by accident, and the checker correctly calls each of
those equivalent rather than inventing a difference.

The fuzzer earned its keep immediately: its first run exposed a genuine bug —
in one of the rewrite rules, where `~{2'd0, (c == 0)}` was used to invert a mux
condition. A bitwise complement of a widened comparison is never zero, so that
"inverted" condition was always true. That was a bug in my test generator
rather than the checker, but it is exactly the class of mistake hand-written
tests never find.

---

## Test cases

Fourteen design pairs, every one a different topology rather than a
cosmetic edit, so each proof is real work. Measured complexity for every pair
is tabulated in [`REPORT.md` §14.1](REPORT.md); the largest are a 128-bit adder
comparison (257 inputs, 3,097 miter gates, an input space of `2^257`) and a
12x12 multiplier that takes over three hours of SAT search to discharge.

| # | Reference | Revision | Expected |
|---|-----------|----------|----------|
| 1 | 16-bit ripple-carry adder (hierarchical, `generate`) | 16-bit carry-lookahead (four 4-bit CLA blocks) | equivalent |
| 2 | 16-bit ripple-carry adder | 16-bit **Kogge-Stone** parallel-prefix adder | equivalent |
| 3 | 16-bit carry-lookahead | 16-bit Kogge-Stone | equivalent |
| 4 | *N*-bit ripple-carry (parameterised) | *N*-bit **carry-select**, 4-bit blocks | equivalent |
| 5 | 16-bit ripple-carry adder | CLA with one dropped carry product term | **not** equivalent |
| 6 | 16-bit ripple-carry adder | ripple-carry with **one** broken gate | **not** equivalent |
| 7 | 8-bit ALU, behavioural `case` | structural mux tree, open-coded subtract/compare | equivalent |
| 8 | 8-bit ALU, behavioural `case` | same mux tree with the two shifts swapped | **not** equivalent |
| 9 | 8-bit shifter, behavioural `a << s` | decoder-driven crossbar shifter | equivalent |
| 10 | popcount by sequential `for` accumulation | balanced adder tree | equivalent |
| 11 | 8→3 priority encoder, `casez` wildcards | one-hot mask + OR-plane encoder | equivalent |
| 12 | Gray encode **then** decode | the identity (`assign y = a`) | equivalent |
| 13 | ISCAS-85 **c17**, six NAND gates | 32-entry truth table (`case`) | equivalent |
| 14 | *N*×*N* multiplier, behavioural `a * b` | **carry-save** (Wallace-style) array multiplier | equivalent |

Cases 4 and 13 are parameterised (`-p WIDTH=n`) and drive the scaling studies.
The two buggy revisions carry realistic bugs — a dropped product term and a
swapped mux input — not obviously broken logic. Case 11 is a nice one: the
composition of Gray encode and decode is the identity, but nothing about the
circuit says so.

---

## Experimental findings

Full tables in [`results/benchmark.md`](results/benchmark.md), regenerated by
`python benchmark.py`. Timings are single-threaded on one machine. The
width-10 and width-12 multiplier rows were measured in separate long runs
rather than in the main sweep (a single row takes hours), and repeat runs of
the same configuration varied by up to about 2x under machine load — so treat
those as indicative. The width-12 gap is far larger than that variance; the
width-8/9 ones are within it, which is why the crossover point below is stated
as a range rather than a number.

**1. The BDD hits a wall; SAT bends.** On the multiplier, BDD peak size grows
roughly 3x per added bit — 287 nodes at width 3, 67,442 at width 7, 210,561 at
width 8, and blows a 400,000-node budget at width 9, where SAT still finishes
in 33 s. This is Bryant's 1991 result in miniature: multiplier BDDs are
exponential in *every* variable order, so no heuristic rescues them, whereas
SAT degrades steeply but without a hard cliff. It is precisely why industrial
equivalence checking moved from BDDs to SAT.

**2. SAT sweeping loses on easy instances and wins on hard ones — there is a
measurable crossover.** On eight of the nine pairs tested it discharges the
miter entirely bottom-up: the 16-bit RCA-vs-CLA cone collapses from 341 nodes
to **zero** after 20 merges, with no output-level solve at all. That is the
Kuehlmann-Krohm result reproduced.

But collapsing the cone is not the same as being faster, and the fuller picture
only emerged from pushing to larger instances:

| multiplier width | plain SAT | SAT + sweeping | speed-up |
|---|---|---|---|
| 8  | 8.5 s | 9.0 s | 0.94x |
| 9  | 33.0 s | 46.6 s | 0.71x |
| 12 | **11,509 s** (3 h 12 m) | **3,227 s** (54 m) | **3.57x** |

Sweeping carries a fixed overhead — simulate, classify, then run dozens of
small equivalence solves — and at widths 8–9 that overhead exceeds what the
merges save, so it *loses*. But the plain miter is one monolithic UNSAT proof
whose cost explodes with width (a factor of 350 for three extra bits), while
sweeping decomposes the same obligation into ~73 small solves that grow far
more slowly. The curves cross between width 9 and width 12.

This is exactly the argument for sweeping in industrial tools: not that it
helps on easy problems (it does not) but that it changes the *shape* of the
curve on hard ones. Strikingly, the width-12 win comes from only **21 merges**
in a 2,645-node cone, so it is not bulk simplification; a few well-placed
internal equivalences break one intractable proof into many tractable ones.

The 12x12 multiplier is the largest instance proved here. Sweeping also
depends on the two designs sharing intermediate values — the normal case for
pre- versus post-synthesis netlists, which Table B measures directly.

**3. No ordering heuristic wins everywhere.** On the 16-bit adder a depth-first
order peaks at **843** nodes against interleaved's 4,557 (5.4x better) while
declaration and reverse order overflow entirely. On the multiplier the ranking
inverts completely: DFS overflows and `reverse` wins at 21,061. On the ALU only
interleaved survives at all. Since optimal BDD ordering is NP-hard, this is why
`auto` (try them all under a shrinking budget, keep the winner) beats
committing to any single rule. Sifting on top of the winner adds 13% on the ALU
and 2% on the multiplier, and nothing at all on the four designs where the best
static order was already good: a poor return for O(n^2) rebuilds, which is a
fair reflection of why real packages sift in place rather than by rebuilding.

**4. Random simulation resolves most real bugs.** Both buggy designs are
refuted in under a millisecond with no solver call, and across 500 fuzz rounds
simulation resolved 481 of the verdicts. Falsification is cheap; only proofs
are expensive. This is why the pipeline runs it first.

**5. Solver choice matters about 2.4x; the encoding matters more.** On the
identical width-8 CNF, CaDiCaL 1.5.3 and 1.9.5 finish in ~11.4 s while
Lingeling takes 27.0 s. Tseitin encoding is a rounding error throughout
(3–11 ms), so effort spent shrinking the problem beats effort spent picking a
solver.

**6. Past a certain size the frontend, not the solver, dominates.** Adders are
easy for SAT, so the 128-bit ripple-vs-carry-select comparison spends 0.239 s
in Verilog elaboration and bit-blasting against 0.150 s in the solver.

## Command-line options

| Option | Meaning |
|--------|---------|
| `--method sat\|bdd\|both\|algebraic` | which decision procedure to run (default `sat`) |
| `--mult-ports A,B,P` | port names for the algebraic backend (default `a,b,p`) |
| `--max-terms N` | term budget for algebraic reduction |
| `--solver NAME` | PySAT solver, e.g. `cadical153`, `glucose4`, `minisat22` |
| `--sweep` | run SAT sweeping before the output miter |
| `--no-presim` / `--sim-vectors N` | control the random-simulation pass |
| `--minimize` | shrink the counterexample to the bits that matter |
| `--order auto\|sift\|interleaved\|dfs\|declaration\|reverse` | BDD variable order |
| `--bdd-limit N` | abort the BDD build past N nodes |
| `-p NAME=VALUE` | override a top-level parameter in both designs |
| `--outputs` | per-output-bit table with cone size and depth |
| `--diagnose` | list every output bit that can differ |
| `--localize [N]` | find which gates would have to change; searches up to N simultaneous faults |
| `--stats` | detailed solver statistics (conflicts, decisions, propagations) |
| `--aiger PATH` | export the miter as AIGER, for ABC |
| `--dimacs PATH` | export the miter CNF as DIMACS |
| `--dot` / `--bdd-dot PATH` | Graphviz drawings of the AIG and BDD |
| `--json PATH` | full machine-readable result |
| `--spec-top` / `--impl-top` | name the top module (otherwise inferred) |

Exit status: `0` equivalent, `1` not equivalent, `2` input error, `4` internal
inconsistency between backends.

---

## Layout

```
eqcheck/
  lexer.py       tokenizer, with x/z wildcard masks
  vast.py        AST node definitions
  vparser.py     recursive-descent parser
  elaborate.py   parameters, generate, hierarchy, always blocks, bit-blasting
  aig.py         AIG + structural hashing + Tseitin encoder + depth stats
  simulate.py    bit-parallel random simulation and signatures
  sweep.py       SAT sweeping with an incremental solver
  bdd.py         from-scratch ROBDD package
  equiv.py       miter, staged SAT pipeline, BDD, ordering search, minimisation
  localize.py    SAT-based fault localisation and N-fault diagnosis
  algebraic.py   polynomial reduction against an arithmetic specification
  sim.py         standalone simulation of a single design
  export.py      AIGER and Graphviz export
  solvers.py     solver screening (three bundled solvers crash this build)
  cli.py         command-line interface
tests/           22 Verilog files, 14 design pairs
results/         benchmark tables, Graphviz figures, AIGER exports
run_tests.py     regression suite (7 layers, 54 checks)
fuzz.py          randomised differential testing against exhaustive simulation
benchmark.py     experimental study
make_figures.py  regenerate the Graphviz / AIGER artefacts in results/figures/
REPORT.md        full write-up: problem, algorithms, results, conclusions
```

---

## References

- G. Tseitin, *On the complexity of derivation in propositional calculus*, 1968, linear-size CNF encoding.
- R. Bryant, *Graph-based algorithms for boolean function manipulation*, IEEE ToC, 1986, ROBDDs and canonicity.
- R. Bryant, *On the complexity of VLSI implementations and graph representations of boolean functions with application to integer multiplication*, IEEE ToC, 1991, multiplier BDDs are exponential in every order.
- R. Rudell, *Dynamic variable ordering for ordered binary decision diagrams*, ICCAD 1993, sifting.
- D. Brand, *Verification of large synthesized designs*, ICCAD 1993, the miter construction.
- A. Kuehlmann, F. Krohm, *Equivalence checking using cuts and heaps*, DAC 1997, AIG-based SAT sweeping.
- A. Biere et al., *CaDiCaL, Kissat, Paracooba*, SAT Competition, the solvers PySAT bundles.
- D. Kaufmann, A. Biere, M. Kauers, *Verifying large multipliers by combining SAT and computer algebra*, FMCAD 2019; and *Improving AMulet2*, STTT 2023 - algebraic verification of arithmetic circuits.
- A. Smith, A. Veneris, M. Ali, A. Viglas, *Fault diagnosis and logic debugging using Boolean satisfiability*, IEEE TCAD, 2005 - SAT-based fault localisation.
- Berkeley ABC (`cec`, `&cec`) and Yosys (`equiv_make`, `miter` + `sat`), open-source industrial references.
