# eqcheck

Combinational equivalence checker for Verilog. Takes two versions of a design
and either proves they compute the same function for every input, or returns an
input where they differ.

Everything is implemented here except the SAT solver: the Verilog parser,
elaborator and bit-blaster, the AIG, the Tseitin encoder, the SAT-sweeping
engine, the ROBDD package, the algebraic engine and the fault localiser.
[PySAT](https://pysathq.github.io/) supplies the solver.

Written for a formal verification assignment (Problem C). Full write-up in
[REPORT.md](REPORT.md).

## Install

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux/macOS: .venv/bin/python
```

## Use

```bash
# compare two designs
python -m eqcheck tests/adder16_rca.v tests/adder16_ks.v --method both

# find a bug, minimise the witness, name the broken gate
python -m eqcheck tests/adder16_rca.v tests/adder16_rca_buggy1.v --minimize --localize

# 16x16 multiplier, algebraically
python -m eqcheck tests/mult_behav.v tests/mult_csa.v -p WIDTH=16 --method algebraic

python run_tests.py        # 54 checks, 7 layers
python fuzz.py --rounds 500
python benchmark.py        # regenerates results/benchmark.md
```

Exit status: `0` equivalent, `1` not equivalent, `2` input error, `3`
inconclusive, `4` backends disagree.

## How it works

Both designs are parsed, bit-blasted to gates and elaborated into one shared
AIG. Corresponding outputs are XORed and OR-ed together into a *miter*: a single
wire that is 1 exactly when the designs disagree. Four stages then try to
decide it, cheapest first.

```
structural hashing → random simulation → SAT sweeping → Tseitin/CNF → SAT
```

Each can settle the question outright, and the tool reports which one did
(`resolved by:` in the output). Arithmetic circuits can skip the miter and go
through the algebraic backend instead.

### Frontend

Hand-written lexer and recursive-descent parser. Supports ANSI and non-ANSI
ports, vectors and part-selects, `assign`, gate primitives, module hierarchy,
parameters, nested `generate`/`genvar` loops, generate-`if`, and `always @(*)`
with `if`/`else`, `case`, `casez`/`casex` and `for`. Operators include `+ - *`,
bitwise and reduction operators, comparisons, constant and variable shifts,
concatenation, replication and `?:`.

Signal bits resolve lazily from the outputs backwards, so declaration order is
irrelevant, unused logic is never built, and combinational loops fall out as
re-entrancy during resolution.

`casez` wildcards are handled properly: `?`/`z`/`x` positions are tracked from
the lexer through to elaboration and dropped from the comparison. Treating them
as zeroes mis-compiles every priority encoder.

Inside `always` blocks the elaborator tracks which bits are assigned on every
path and warns when one is not, since that is where a synthesiser infers a latch
and the design is not really combinational.

Not supported: sequential logic, division/modulo, `x`/`z` as values, memory
arrays. All raise explicit errors.

### AIG

Two-input AND nodes, inversion in the low bit of each edge (AIGER convention),
so complementing is free. Every node goes through a structural hash.

Both designs share one graph, so logic they build identically collapses to the
same nodes and the XOR folds to constant `FALSE`. Several test pairs are
discharged this way with no solver call.

### Random simulation

Each node carries a signature: a Python int used as a bit-vector holding its
value under 512 random inputs, so one `&` evaluates all 512 at once.

Used for cheap falsification, and as a sound filter for sweeping, since nodes
whose signatures differ cannot be equivalent.

### SAT sweeping

Kuehlmann & Krohm, DAC 1997. Group nodes by signature, walk in topological order
rebuilding into a fresh AIG, and ask the solver whether each node matches an
earlier member of its class. Proved pairs are merged and structural hashing
cascades the result upward, so the miter often folds to `FALSE` before the walk
reaches the outputs.

Equivalence of `a` and `b` is tested incrementally under assumptions:

```
solve([ a, -b])  UNSAT  =>  a implies b
solve([-a,  b])  UNSAT  =>  b implies a
```

One solver instance serves the whole sweep. A refuted candidate yields a
counterexample which is fed back into the simulator, splitting the class so the
same wrong guess is not repeated.

### SAT backend

Tseitin encoding: three clauses and one variable per AND node, linear in circuit
size. Only the miter cone is encoded. DIMACS variable 1 is pinned false and
represents the AIG constant, so literals map without special-casing.

Default solver is CaDiCaL 1.5.3. `--solver` selects any of the 17 bundled
solvers that work here; three (`kissat404`, `cryptosat`, `minisatgh`) abort the
interpreter on construction on this platform and are screened out by
`solvers.py` with a message rather than a segfault.

`--minimize` shrinks a counterexample to the bits that provoke it. A care set is
valid when fixing it *forces* the miter to 1, so the test for dropping a bit is
that `solve([miter = 0] + remaining)` stays UNSAT. On the buggy adder this cuts
33 bits to 8, which localise to one carry-lookahead block.

`--outputs` gives a per-output-bit table with cone size and depth, using one
incremental solver across all bits.

### BDD backend

From-scratch ROBDD: unique table, `ite` with a computed table, both reduction
rules enforced at construction. ROBDDs are canonical for a fixed order, so
equivalence needs no search: the miter is unsatisfiable iff its BDD is the
`FALSE` terminal.

Orderings are `interleaved`, `dfs`, `declaration`, `reverse`, plus `auto` (try
all under a shrinking budget, keep the smallest) and `sift` (Rudell 1993,
rebuild-based). A node budget turns the multiplier blow-up into an "aborted"
result instead of an OOM.

### Algebraic backend

For arithmetic circuits, following the AMulet work (Kaufmann, Biere & Kauers).
Work in `Z[x1..xn]` modulo `x^2 = x`. Each AND node becomes `v - L(l1)*L(l2)`,
and the property becomes

```
SPEC = sum 2^i p_i - (sum 2^i a_i)(sum 2^j b_j)
```

The circuit is correct iff `SPEC` reduces to zero. Reduction would normally need
a Gröbner basis, but the gate polynomials already are one when each gate is
ordered above its inputs, so it degenerates to substitution in reverse
topological order. Since `x^2 = x`, a monomial is a set of variables and
multiplication is set union.

| Multiplier | Plain SAT | Algebraic |
|---|---|---|
| 12x12 | 11,509 s | **0.034 s** |
| 64x64 (47,876 gates) | not reachable | **7.6 s** |

This proves each design against the arithmetic specification rather than against
the other design, so no miter is built.

Proving is cheap, refuting is not: a correct 6x6 multiplier peaks at 186
polynomial terms, a broken one at 621,004 and 33 s. That is the opposite profile
to random simulation, so the two complement each other.

This is the textbook core, not AMulet. There is no adder detection, variable
elimination or XOR rewriting, so a heavily restructured multiplier can still
exhaust the term budget, which is reported as inconclusive.

### Fault localisation

Smith & Veneris, IEEE TCAD 2005. A gate `n` is a single-fix location if some
replacement at `n` would make the designs equivalent. The replacement value is
only 0 or 1, so:

```
SAT( miter(n := 0) AND miter(n := 1) )  =>  n is not a fix location
UNSAT                                   =>  n is
```

On a 16-bit ripple-carry adder with one broken gate this returns exactly one
candidate out of 55 examined, `c[10]`, which is the gate that was broken.
Candidates are reported by hierarchical Verilog name, since the elaborator
retains every scope.

When several gates are wrong at once, `--localize N` switches to
counterexample-driven N-fault diagnosis: *k* replicas sharing selector
variables, each gate cut and freed when its selector is set, `sum(s_n) <= N` via
cardinality constraints, searching N = 1, 2, … for a minimum-cardinality
diagnosis. Each candidate set is verified by enumerating all `2^|S|` forcings.

Two caveats, both reported by the tool. Many gate sets can repair a design, so
the true fault is guaranteed to be *among* the valid diagnoses but not
guaranteed to rank first. And a diagnosis made entirely of primary outputs is
always valid and always useless, so those are ranked last.

### Export

`--aiger` writes the miter for ABC (`abc -c "read_aiger miter.aag; sat"`),
`--dimacs` for any SAT solver, `--dot` and `--bdd-dot` for Graphviz.

## Validation

`run_tests.py` runs 54 checks in seven layers:

1. **Frontend.** Each of 18 designs simulated alone against an independent
   Python model, exhaustively where the input space allows. Catches frontend
   bugs that would otherwise cancel out between the two designs.
2. **Equivalence.** Each pair against its expected verdict, SAT and BDD required
   to agree.
3. **Witness replay.** Every counterexample re-simulated through both designs.
4. **Care set.** 200 random completions of each minimised counterexample must
   still expose the bug.
5. **Engine cross-checks.** Sweeping, plain miter and per-output analysis must
   agree.
6. **Localisation.** For designs with a planted fault, diagnosis must name the
   gate that was broken.
7. **Algebraic.** Must prove genuine multipliers and refuse to prove four
   deliberately corrupted ones.

`fuzz.py` generates random circuits, rewrites them with semantics-preserving
transformations, mutates them, and grades the checker against exhaustive
simulation. More than 1,500 rounds across eight seeds during development, zero
disagreements. Around 4% of mutations turn out to be behaviour-preserving by
accident and are correctly reported equivalent.

Its first run found a real bug, in one of the rewrite rules, where
`~{2'd0, (c == 0)}` was used to invert a mux condition. A bitwise complement of
a widened comparison is never zero, so the condition was always true.

## Test cases

Fourteen pairs, each a different topology rather than a cosmetic edit. Measured
complexity for all of them is in [REPORT.md §14.1](REPORT.md); the largest are a
128-bit adder comparison (257 inputs, 3,097 miter gates, `2^257` input space)
and a 12x12 multiplier that takes over three hours of SAT search.

| # | Reference | Revision | Expected |
|---|-----------|----------|----------|
| 1 | 16-bit ripple-carry (hierarchical, `generate`) | 16-bit carry-lookahead, four 4-bit blocks | equivalent |
| 2 | 16-bit ripple-carry | 16-bit Kogge-Stone parallel prefix | equivalent |
| 3 | 16-bit carry-lookahead | 16-bit Kogge-Stone | equivalent |
| 4 | *N*-bit ripple-carry (parameterised) | *N*-bit carry-select, 4-bit blocks | equivalent |
| 5 | 16-bit ripple-carry | CLA with one dropped carry product term | **not** equivalent |
| 6 | 16-bit ripple-carry | ripple-carry with one broken gate | **not** equivalent |
| 7 | 8-bit ALU, behavioural `case` | structural mux tree, open-coded subtract/compare | equivalent |
| 8 | 8-bit ALU, behavioural `case` | same mux tree, two shifts swapped | **not** equivalent |
| 9 | 8-bit shifter, `a << s` | decoder-driven crossbar | equivalent |
| 10 | popcount by sequential `for` | balanced adder tree | equivalent |
| 11 | 8→3 priority encoder, `casez` | one-hot mask + OR plane | equivalent |
| 12 | Gray encode then decode | the identity | equivalent |
| 13 | ISCAS-85 c17, six NAND gates | 32-entry truth table | equivalent |
| 14 | *N*×*N* multiplier, `a * b` | carry-save array multiplier | equivalent |

Cases 4 and 14 are parameterised with `-p WIDTH=n` and drive the scaling
studies.

## Results

Tables in [results/benchmark.md](results/benchmark.md), regenerated by
`benchmark.py`.

**BDDs hit a wall, SAT bends.** Multiplier BDD size roughly triples per added
bit and blows a 400,000-node budget at width 9, where SAT still finishes in
33 s. Bryant's 1991 result: multiplier BDDs are exponential in every order.

**SAT sweeping has a crossover.** It discharges eight of nine pairs entirely
bottom-up, but at widths 8–9 its overhead exceeds what the merges save (0.94x,
0.71x). At width 12 it wins 3.57x, 3,227 s against 11,509 s, from only 21 merges
in a 2,645-node cone. It changes the shape of the curve on hard instances rather
than helping easy ones.

**No BDD ordering heuristic wins everywhere.** DFS peaks at 843 nodes on the
16-bit adder against interleaved's 4,557, but overflows on the multiplier where
`reverse` wins at 21,061. On the ALU only interleaved survives. Sifting adds 13%
on the ALU, 2% on the multiplier and nothing elsewhere.

**Random simulation resolves most real bugs** in under a millisecond, and 481 of
500 fuzz verdicts. Falsification is cheap; proofs are expensive.

**SAT is not brute force, but that does not make it always faster.** The 128-bit
adder settles `2^257` inputs in about 0.15 s, which no enumeration could touch.
But exhaustive simulation of the 12x12 multiplier's 16.7M inputs takes 5.4 s
against SAT's 11,509, so brute force wins by 2,100x when the input space is
small enough to enumerate.

**Past a certain size the frontend dominates.** The 128-bit comparison spends
0.239 s in elaboration against 0.150 s in the solver.

## Options

| Option | Meaning |
|--------|---------|
| `--method sat\|bdd\|both\|algebraic` | decision procedure (default `sat`) |
| `--solver NAME` | PySAT solver |
| `--sweep` | SAT sweeping before the output miter |
| `--no-presim` / `--sim-vectors N` | control the simulation pass |
| `--minimize` | shrink the counterexample |
| `--localize [N]` | name the gates that would have to change |
| `--order auto\|sift\|interleaved\|dfs\|declaration\|reverse` | BDD variable order |
| `--bdd-limit N` | abort the BDD build past N nodes |
| `--mult-ports A,B,P` / `--max-terms N` | algebraic backend settings |
| `-p NAME=VALUE` | override a top-level parameter |
| `--outputs` / `--diagnose` / `--stats` | reporting |
| `--aiger` / `--dimacs` / `--dot` / `--bdd-dot` / `--json` | export |
| `--spec-top` / `--impl-top` | name the top module |

## Layout

```
eqcheck/
  lexer.py       tokenizer, x/z wildcard masks
  vast.py        AST nodes
  vparser.py     parser
  elaborate.py   parameters, generate, hierarchy, always blocks, bit-blasting
  aig.py         AIG, structural hashing, Tseitin encoder
  simulate.py    bit-parallel simulation and signatures
  sweep.py       SAT sweeping
  bdd.py         ROBDD package
  equiv.py       miter, SAT pipeline, BDD driver, ordering search, minimisation
  algebraic.py   polynomial reduction against an arithmetic spec
  localize.py    fault localisation and N-fault diagnosis
  sim.py         standalone simulation of one design
  export.py      AIGER and Graphviz export
  solvers.py     solver screening
  cli.py         command line
tests/           22 Verilog files, 14 design pairs
results/         benchmark tables, figures, AIGER exports
run_tests.py     regression suite
fuzz.py          randomised differential testing
benchmark.py     experiments
make_figures.py  regenerate results/figures/
REPORT.md        full write-up
```

## References

- Tseitin, *On the complexity of derivation in propositional calculus*, 1968
- Bryant, *Graph-based algorithms for boolean function manipulation*, IEEE ToC 1986
- Bryant, *On the complexity of VLSI implementations … integer multiplication*, IEEE ToC 1991
- Rudell, *Dynamic variable ordering for OBDDs*, ICCAD 1993
- Brand, *Verification of large synthesized designs*, ICCAD 1993
- Kuehlmann & Krohm, *Equivalence checking using cuts and heaps*, DAC 1997
- Smith, Veneris, Ali & Viglas, *Fault diagnosis and logic debugging using Boolean satisfiability*, IEEE TCAD 2005
- Kaufmann, Biere & Kauers, *Verifying large multipliers by combining SAT and computer algebra*, FMCAD 2019
- Biere et al., *Clausal congruence closure*, SAT 2024; *Clausal equivalence sweeping*, FMCAD 2024
- Berkeley ABC, Yosys

## Licence

MIT, see [LICENSE](LICENSE).
