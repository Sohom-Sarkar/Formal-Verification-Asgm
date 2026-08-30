# Combinational Equivalence Checking — Project Report

**Formal Verification, Assignment 1 — Problem C**
*Develop an equivalence checker (using SAT or BDD) to verify the equivalence
between two different versions of a combinational design test-case.*

---

## Contents

1. [The problem](#1-the-problem)
2. [Why this cannot be done by testing](#2-why-this-cannot-be-done-by-testing)
3. [Scope and design decisions](#3-scope-and-design-decisions)
4. [System architecture](#4-system-architecture)
5. [The frontend: Verilog to gates](#5-the-frontend-verilog-to-gates)
6. [The intermediate representation: AIG](#6-the-intermediate-representation-aig)
7. [The miter construction](#7-the-miter-construction)
8. [Decision procedure 1: random simulation](#8-decision-procedure-1-random-simulation)
9. [Decision procedure 2: SAT sweeping](#9-decision-procedure-2-sat-sweeping)
10. [Decision procedure 3: Tseitin encoding and CDCL](#10-decision-procedure-3-tseitin-encoding-and-cdcl)
11. [Decision procedure 4: ROBDD](#11-decision-procedure-4-robdd)
12. [Decision procedure 5: algebraic reduction](#12-decision-procedure-5-algebraic-reduction)
13. [Diagnosis: from "wrong" to "wrong here"](#13-diagnosis-from-wrong-to-wrong-here)
14. [Test cases](#14-test-cases)
15. [Validation methodology](#15-validation-methodology)
16. [Experimental results](#16-experimental-results)
17. [Findings and discussion](#17-findings-and-discussion)
18. [Limitations](#18-limitations)
19. [Conclusion](#19-conclusion)
20. [References](#20-references)
21. [Appendix: running the tool](#21-appendix-running-the-tool)

---

## 1. The problem

### 1.1 What a combinational circuit is

A digital circuit is built from logic gates — physical switches implementing
AND, OR and NOT. Circuits divide into two families:

- **Sequential** circuits contain memory. Their output depends on the history
  of past inputs as well as the present ones (a counter, a state machine).
- **Combinational** circuits have no memory whatsoever. The outputs are a pure
  mathematical function of the current inputs. Apply the same inputs and you
  get the same outputs, always. An adder, a multiplier, a multiplexer.

This project concerns combinational circuits only, as the assignment specifies.

Circuits are described in a hardware description language — here, **Verilog**.
The line `assign sum = a + b;` is a request for an adder circuit; a synthesis
tool later turns that request into gates.

### 1.2 The equivalence checking problem

The problem statement asks for a checker comparing **two versions of a
combinational design**. This is not artificial: it is one of the most
commercially important problems in chip design, and it arises constantly:

- An engineer rewrites a slow but obviously correct circuit into a fast but
  obscure one.
- A synthesis tool restructures a netlist for area or timing.
- A design is hand-optimised after synthesis.
- A block is replaced by a supposedly drop-in equivalent.

In every case the same question arises:

> **Do these two circuits compute exactly the same function, for every possible
> combination of inputs?**

Not "for the tests we ran." Not "as far as we can tell." For **every** input,
without exception.

The canonical example used throughout this project: a 16-bit **ripple-carry
adder**, where each bit waits for the carry from the bit below it exactly as in
schoolbook addition, versus a 16-bit **Kogge-Stone adder**, which computes all
carries simultaneously through a logarithmic-depth prefix network. The two
share no structural resemblance at all. The Kogge-Stone version is far faster.
Are they the same function?

### 1.3 Why it matters

Software defects are patched. Silicon defects are not: a bug discovered after
fabrication means recalling physical hardware. Intel's 1994 Pentium FDIV bug
cost roughly **$475 million**, and it affected only a tiny fraction of input
values, which is precisely why conventional testing did not catch it.

That economics is what motivates *formal* verification: mathematical proof over
the entire input space, rather than sampling it.

---

## 2. Why this cannot be done by testing

The naive approach is to enumerate every input combination and compare. This
fails immediately, and the numbers are worth stating precisely because they
justify the entire project.

A 16-bit adder has 33 input bits (16 + 16 + carry-in):

```
2^33  =  8,589,934,592 combinations
```

Large, but tractable. Now consider a 64-bit adder, the kind in a laptop
processor. It has 129 input bits:

```
2^129  ≈  6.8 × 10^38 combinations
```

At a billion tests per second, beginning at the Big Bang, one would today have
completed a vanishingly small fraction of one percent. And the 128-bit adder
this project actually verifies has **257 input bits**:

```
2^257  ≈  2.3 × 10^77
```

which is roughly the number of atoms in the observable universe.

Exhaustive testing is therefore permanently impossible for real designs.
Random testing can find bugs but can never establish their absence. The only
route to "these are equivalent" is proof, and that is what this tool produces.

---

## 3. Scope and design decisions

### 3.1 What was built versus what was used

Everything in the verification pipeline is implemented in this project **except
the SAT solver itself**:

| Component | Source |
|---|---|
| Verilog lexer, parser | **Written here** |
| Elaborator / bit-blaster | **Written here** |
| AIG with structural hashing | **Written here** |
| Bit-parallel simulator | **Written here** |
| Tseitin CNF encoder | **Written here** |
| SAT sweeping engine | **Written here** |
| ROBDD package | **Written here** |
| Algebraic reduction engine | **Written here** |
| Fault localisation / diagnosis | **Written here** |
| AIGER / DIMACS / Graphviz export | **Written here** |
| **SAT solving** | **PySAT** (external) |

The single external dependency is [PySAT](https://pysathq.github.io/), used
purely as the constraint-solving engine.

### 3.2 Decision: own parser rather than Yosys

The obvious shortcut is to let **Yosys** — a mature open-source synthesis tool
— read the Verilog and emit a flat netlist. This was deliberately rejected.

Yosys can perform the *entire assignment* by itself: `miter -equiv` followed by
`sat -verify`, or the `equiv_make`/`equiv_simple`/`equiv_status` flow. Using it
would have reduced the submission to a wrapper around someone else's checker.
Writing the frontend keeps the work genuinely ours, removes any external
download, and (as it turned out) enabled two later features (hierarchical
signal naming for fault localisation, and direct access to circuit structure
for the algebraic backend) that a black-box netlist would have made awkward.

The cost is a restricted Verilog subset, documented in §5.4.

### 3.3 Decision: AIG as the intermediate representation

All engines consume a single intermediate representation: an **And-Inverter
Graph**. This means the frontend is written once and five different decision
procedures share it. It is also the representation used by Berkeley ABC, the
reference open-source tool in this domain, which made the AIGER export
straightforward.

---

## 4. System architecture

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
  hashing        simulation      (internal equiv.)        → PySAT      construction
      │               │                │                     │              │
  miter folds     any vector       miter folds to        UNSAT = equiv   FALSE terminal
  to constant     that differs     FALSE bottom-up       SAT = witness    = equivalent

                    ── or, for arithmetic circuits ──
                  algebraic reduction against the spec polynomial
                            (bypasses the miter entirely)

                                   on failure
                                       │
                    ┌──────────────────┼──────────────────┐
                    ▼                  ▼                  ▼
            counterexample      care-set minimisation   fault localisation
```

### 4.1 Module map

| Module | Lines | Responsibility |
|---|---:|---|
| `lexer.py` | 162 | Tokenisation, including x/z wildcard masks |
| `vast.py` | 115 | AST node definitions |
| `vparser.py` | 585 | Recursive-descent parser |
| `elaborate.py` | 1115 | Parameters, generate, hierarchy, always blocks, bit-blasting |
| `aig.py` | 308 | AIG, structural hashing, Tseitin encoder, depth statistics |
| `simulate.py` | 122 | Bit-parallel random simulation and signatures |
| `sweep.py` | 305 | SAT sweeping with an incremental solver |
| `bdd.py` | 197 | ROBDD package |
| `equiv.py` | 704 | Miter, staged SAT pipeline, BDD driver, ordering search, minimisation |
| `algebraic.py` | 288 | Polynomial reduction against an arithmetic specification |
| `localize.py` | 550 | Fault localisation and N-fault diagnosis |
| `export.py` | 164 | AIGER and Graphviz export |
| `sim.py` | 75 | Standalone simulation of a single design |
| `solvers.py` | 92 | Solver screening |
| `cli.py` | 597 | Command-line interface |
| **Total** | **5,411** | 17 modules (the table omits `__init__.py`, `__main__.py`) |

Plus 1,406 lines of test and experiment drivers (`run_tests.py`, `fuzz.py`,
`benchmark.py`, `make_figures.py`) and 22 Verilog test files.

---

## 5. The frontend: Verilog to gates

### 5.1 Intuition

Verilog is a programming language for hardware. It contains loops, conditional
statements, arithmetic on multi-bit numbers, and module hierarchy. None of that
exists in silicon, silicon has only gates. The frontend's job is to translate
one into the other, a process called **elaboration** followed by
**bit-blasting**.

The analogy: taking a recipe that says "make the sauce" and rewriting it as
every individual knife stroke and stir. Vastly longer, but every step is now
the same primitive kind of operation.

### 5.2 Lexing and parsing

A hand-written tokeniser splits the source into keywords, identifiers, numbers
and operators, handling comments, compiler directives, and Verilog's sized
literal syntax (`8'hFF`, `4'b10x1`, `16'sd120`).

One subtlety worth recording: literals may contain `x`, `z` or `?` digits.
These are **don't-care** positions in a `casez`/`casex` label. Naively
substituting zero for them (the obvious implementation) silently
mis-compiles every priority encoder ever written, because `8'b1???????` would
become `8'b10000000` and match only one value instead of 128. The lexer
therefore computes an **`xz_mask`** alongside the numeric value, recording
which bit positions were wildcards, and this mask is carried all the way
through to elaboration.

Parsing is recursive descent, with a precedence-climbing expression parser
implementing Verilog's full binary operator precedence table plus the
right-associative ternary `?:`.

### 5.3 Elaboration and bit-blasting

Elaboration performs, in order:

1. **Parameter evaluation.** Constant expressions are folded, including
   parameter overrides supplied on the command line. Port *widths* may depend
   on parameters, so this must happen before anything else.
2. **Generate unrolling.** A `generate for` loop is unrolled into explicit
   copies. Signals declared inside the loop body are renamed per iteration
   (`pp` becomes `pp$0`, `pp$1`, …) so each iteration gets private wires.
   Generate-`if` is resolved at elaboration time and the untaken branch
   discarded. Nested generate loops are supported: the Kogge-Stone adder uses
   a doubly-nested loop with an inner generate-`if`.
3. **Declaration processing.** Every signal becomes a vector of single-bit
   slots.
4. **Driver registration.** Each `assign`, gate primitive, module instance and
   `always` block is recorded as the *driver* of specific signal bits.
5. **Lazy resolution.** Bits are resolved on demand, backwards from the primary
   outputs.

**Why lazy resolution.** Declaration order in Verilog is irrelevant: a wire
may be used before it is assigned. Resolving on demand makes order a
non-issue, and unused logic is never constructed at all. It also gives
combinational-loop detection for free: if resolving a signal re-enters that
same signal while it is still in progress, that *is* a combinational loop, and
it is reported rather than hung on.

**Latch detection.** Inside an `always` block the elaborator tracks, per bit,
whether the bit is assigned on *every* path. Merging an `if`/`else` takes the
logical AND of the two branches' assignment flags. A bit not assigned on all
paths triggers a warning, because that is exactly where a synthesiser infers a
latch — meaning the design is not truly combinational and the comparison may be
meaningless.

**Arithmetic bit-blasting.** Each operator is expanded into gates:

| Operator | Construction |
|---|---|
| `+` | Ripple-carry chain: `s_i = a_i ⊕ b_i ⊕ c_i`, `c_{i+1} = maj(a_i, b_i, c_i)` |
| `-` | `a + ~b + 1` (two's complement) |
| `*` | Shift-and-add: partial products masked by each multiplier bit, accumulated |
| `<` | Borrow out of `a + ~b + 1`; unsigned `a < b` iff no carry out |
| `<<`, `>>` | Constant amount: a wire permutation. Variable amount: logarithmic barrel shifter, one mux layer per shift-amount bit |
| `==` | AND of bitwise XNORs |
| `?:` | Multiplexer per bit |
| Variable bit-select | Balanced multiplexer tree |

`always` blocks are executed symbolically: statements are processed in order
with an environment mapping signals to bit vectors; `if`/`else` evaluates both
branches and multiplexes the results on the condition; `case` folds the branches
in reverse so the first matching item wins, matching Verilog semantics.
`casez`/`casex` labels drop the wildcard bit positions from the comparison
entirely, using the mask computed by the lexer.

### 5.4 Supported subset

**Supported:** ANSI and non-ANSI ports; vectors, bit-select and part-select;
`assign`; gate primitives (`and`, `or`, `nand`, `nor`, `xor`, `xnor`, `buf`,
`not`); module instantiation and hierarchy; parameters and overrides; nested
`generate`/`genvar` loops; generate-`if`; `always @(*)` containing `if`/`else`,
`case`, `casez`/`casex`, `for`; the operators `+ - *`, all bitwise and reduction
operators, comparisons, constant and variable shifts, concatenation,
replication, `?:`.

**Not supported:** sequential logic (out of scope by the assignment);
division and modulo; `x`/`z` as *values* rather than case wildcards; memory
arrays. Each raises an explicit error rather than silently producing wrong
logic: a decision made deliberately, since silently-wrong verification is
worse than no verification.

---

## 6. The intermediate representation: AIG

### 6.1 Structure

An **And-Inverter Graph** contains only two-input AND nodes. Inversion is not a
node; it is encoded in the low bit of each edge, following the AIGER
convention:

```
literal = (node_id << 1) | inverted
literal 0 = constant FALSE      literal 1 = constant TRUE
```

Consequently complementing a signal is a single XOR with 1 — free, and it never
grows the graph. Every other gate is derived: `OR(a,b) = ¬(¬a ∧ ¬b)` by De
Morgan, `XOR(a,b) = OR(AND(a,¬b), AND(¬a,b))`, `MUX(s,t,e) = OR(AND(s,t),
AND(¬s,e))`.

### 6.2 Structural hashing

Every AND node passes through a hash table keyed on its (canonically ordered)
operands. If that pair has been built before, the existing node is returned.
Constant folding and trivial identities (`a ∧ a = a`, `a ∧ ¬a = 0`) are applied
first.

**This is not merely an optimisation.** Both designs are elaborated into *one
shared graph*. Any logic the two designs construct identically therefore
collapses to the same nodes, and the XOR comparing them folds to constant
`FALSE` automatically. In effect, structural hashing performs a free
equivalence check on every internal signal the two designs happen to share.

Measured effect: several test pairs are discharged entirely by this, with no
solver invoked at all. The tool reports when a verdict came from structural
hashing (`resolved by: structural-hashing`) rather than silently claiming a
proof it did not have to work for.

This is the degenerate case of SAT sweeping (§9), and notably it is exactly
what the *clausal congruence closure* technique in Kissat reconstructs at the
CNF level, see §17.6.

---

## 7. The miter construction

### 7.1 Intuition

Two calculators wired to a single keypad, with a buzzer that sounds the instant
they disagree about anything. The verification question becomes: *can the
buzzer ever sound?*

### 7.2 Algorithm

Following Brand (ICCAD 1993):

1. Create **one** set of primary inputs.
2. Elaborate both designs onto those same inputs.
3. For each corresponding output bit pair `(A_i, B_i)`, build `X_i = A_i ⊕ B_i`.
   XOR is 1 exactly when its inputs differ.
4. Build `miter = OR_i X_i`.

The resulting single literal is 1 for exactly those inputs on which the designs
disagree on at least one output bit. Therefore:

- **miter unsatisfiable ⟹ the designs are equivalent** on all `2^n` inputs
- **miter satisfiable ⟹ the satisfying assignment is a counterexample**

Port names, directions and widths must correspond; a mismatch is reported as an
error rather than silently compared. The top module is inferred as the one no
other module instantiates, or named explicitly.

Sharing the primary inputs is the essential detail: it is what makes the XOR
meaningful. Two circuits on separate inputs could differ trivially.

---

## 8. Decision procedure 1: random simulation

### 8.1 Intuition

Most real bugs are shallow. A swapped operand or an inverted select is exposed
by almost any random input. Spending a solver call to discover that is
wasteful, so the pipeline tries brute force first, briefly.

### 8.2 Algorithm: bit-parallel evaluation

Rather than simulating one input vector at a time, each node carries a
**signature**: a Python integer used as a bit-vector, where bit *k* holds that
node's value under random vector *k*. Because Python integers are
arbitrary-precision:

```python
sigs[node] = lit_sig(a) & lit_sig(b)        # 512 vectors evaluated at once
```

One machine operation evaluates 512 input vectors simultaneously. Inversion is
`(~v) & mask`.

If any bit of the miter's signature is 1, that bit index identifies a random
vector on which the designs differ — a counterexample, obtained without a
solver.

### 8.3 The second use: sound candidate filtering

Signatures also underpin SAT sweeping. If two nodes are functionally
equivalent, their signatures **must** match on every vector. Contrapositively,
differing signatures are *proof* of non-equivalence.

Signatures are therefore a **sound filter**: they never hide a genuine
equivalence, they only prevent the solver being asked about hopeless pairs.
Matching signatures are necessary but not sufficient, so every surviving
candidate is still proved properly.

### 8.4 Result

Both deliberately buggy designs are refuted in **under one millisecond**. Across
500 fuzzing rounds, random simulation resolved **481 of the verdicts**.

---

## 9. Decision procedure 2: SAT sweeping

Based on Kuehlmann & Krohm (DAC 1997), the technique that made industrial
equivalence checking scale.

### 9.1 Intuition

A plain output miter discards everything the two designs have in common. Two
implementations of the same adder compute the *same internal carries*; proving
the outputs equal from scratch throws that away.

The analogy: two students hand in the same long multiplication. Rather than
comparing only the final answers, compare their intermediate lines. Matching
intermediate results turn one hard comparison into several easy ones.

### 9.2 Algorithm

The cone is rebuilt into a fresh AIG in topological order. The invariant
maintained throughout is:

> `mapping[node]` is a literal in the new graph computing the same function as
> `node` in the old graph.

For each node:

```
lit = new.mk_and(map_lit(a), map_lit(b))      # children already mapped
sig = signature(node)
key = canonical_signature(sig)                # keys sig and ~sig together

if sig is all-zero or all-one:                # candidate constant
    if UNSAT(lit == that constant):
        mapping[node] = constant;  continue

for (candidate, candidate_node) in classes[key][:24]:
    phase  = (sigs[candidate_node] != sig)    # equal or complementary
    target = ¬candidate if phase else candidate
    if prove_equivalent(lit, target):
        mapping[node] = target                # MERGE
        break
else:
    classes[key].append((lit, node))          # new class representative
```

Keying on `min(sig, ~sig)` groups a node with its complement, because AIG
literals carry inversion for free and a node equivalent to *the negation* of
another is just as useful.

### 9.3 The equivalence test: incremental solving under assumptions

To prove `a ≡ b`, ask for a counterexample in each direction:

```
solve(assumptions=[ a, ¬b])   UNSAT  ⟹  a → b
solve(assumptions=[¬a,  b])   UNSAT  ⟹  b → a
```

Both UNSAT means `a ≡ b`. **Both directions are required** — one alone proves
only implication.

Assumptions are used rather than permanently added clauses because they are
retracted after the call, allowing **one solver instance to serve the entire
sweep**. Every clause learned while proving one equivalence remains available
for the next. New AIG nodes are Tseitin-encoded into the live solver lazily as
they are built.

### 9.4 Why merges cascade

Setting `mapping[node] = target` means every *later* node builds on `target`.
Structural hashing then does the rest: two parents that become
`mk_and(x, y)` with identical arguments collapse into a single node
automatically, with no proof required.

This is why a mere 12 merges annihilate a 341-node cone. Each proved
equivalence propagates upward, and by the time the walk reaches the miter
output both sides are literally the same node, so the XOR folds to `FALSE` and
**the equivalence is proved without ever solving the output miter**.

### 9.5 Refinement from refuted candidates

When a candidate is refuted, the solver returns an input vector on which the
two nodes genuinely differ. That vector is fed back into the simulator:

```python
sim.add_vectors(pending)       # append counterexamples as new vectors
sigs = sim.signatures(order)   # recompute
rebuild_classes()              # re-key every registered representative
```

Those two nodes now have different signatures and are permanently separated —
as is every other pair the vector distinguishes. Representatives are therefore
stored as `(literal, original_node)` pairs, because the original node indexes
the signature table and allows re-keying after refinement.

**This step was documented but initially not implemented**: the counterexamples
were collected into a list nothing consumed. Enabling it reduced wasted solver
calls on the Kogge-Stone pair from **435 to 71**, and total solver calls from
**528 to 114**. The defect was found while writing the algorithmic explanation
in this report, which is itself an argument for writing documentation carefully.

### 9.6 Soundness and completeness

- **Sound.** Every merge is backed by an UNSAT proof. Signatures only choose
  *what to ask*; they never decide anything.
- **Complete.** If sweeping does not fold the root to `FALSE`, the pipeline
  falls through to a full SAT solve on the rebuilt (smaller) graph. Sweeping
  can only help or waste time, never produce a wrong answer.
- **Budgeted.** `max_class_size` caps candidates per node and `max_sat_calls`
  caps total work; both degrade into "fewer merges", never incorrectness.

---

## 10. Decision procedure 3: Tseitin encoding and CDCL

### 10.1 The encoding problem

SAT solvers consume CNF, a conjunction of disjunctions. The naive conversion
substitutes gate definitions until everything is expressed over the primary
inputs. This is catastrophic: a node feeding two parents duplicates its entire
sub-formula, so a chain of *n* XOR gates yields **2^n** terms. A 16-bit adder
would produce a formula larger than any machine could hold.

### 10.2 Tseitin transformation

Tseitin's insight (1968): do not eliminate the intermediate wires — **give each
one a variable**.

For an AND node, introduce fresh variable `c` and assert `c ↔ (a ∧ b)`. Split
into two implications:

```
c → (a ∧ b)      ≡   (¬c ∨ a) ∧ (¬c ∨ b)
(a ∧ b) → c      ≡   (¬a ∨ ¬b ∨ c)
```

Three clauses and one variable per gate. The encoding is **linear** in circuit
size. It is not logically equivalent to the original formula — it has extra
variables, but it is **equisatisfiable**, which is all that is required.

Implementation details:

- **Inverters cost nothing.** `dimacs(lit)` returns `-var` when the literal's
  inversion bit is set. No clauses, no variables.
- **The constant is one pinned variable.** DIMACS variable 1 represents AIG
  node 0, with a unit clause `(¬1)`. AIG literal 0 then maps to `+1` (false)
  and literal 1 to `-1` (true), removing all special-casing.
- **Only the cone is encoded.** Logic not feeding the miter costs nothing.

Measured: the 16-bit ripple-carry versus carry-lookahead miter is 335 AND nodes
→ **375 variables, 1,025 clauses**.

Finally the miter output is asserted with a unit clause — "find an input where
they disagree."

### 10.3 What the solver does

PySAT dispatches to CaDiCaL. The algorithm is **Conflict-Driven Clause
Learning**:

1. **Unit propagation.** If a clause has all-but-one literal false, the
   remaining literal is forced. Cascade to fixpoint. Roughly 90% of runtime.
2. **Decide.** Choose an unassigned variable, guess a value, open a new
   decision level.
3. **Conflict analysis.** When propagation falsifies a clause, traverse the
   implication graph backwards to the first unique implication point and derive
   a **learned clause** implied by the originals.
4. **Backjump.** Undo to the level at which the learned clause becomes unit —
   often many levels at once.
5. **Restart** periodically, retaining learned clauses.

**This is why SAT is not brute force.** Brute force learns nothing from a
failure and eliminates one assignment at a time. A learned clause eliminates an
*exponentially large family* of assignments and remains in force for the rest
of the search.

The decisive evidence: the 128-bit adder comparison has **257 primary inputs**
(`2^257 ≈ 10^77` input vectors) and is proved **equivalent in about 0.15
seconds** (0.06–0.15 s across runs). No enumeration of any kind is occurring.

### 10.4 Solver screening

PySAT bundles roughly twenty solvers. On this platform (Windows, CPython 3.14,
python-sat 1.9.dev15) three of them — `kissat404`, `cryptosat`, `minisatgh` —
**abort the process** on construction rather than raising a catchable
exception. This was discovered when a benchmark run died with a segmentation
fault.

`solvers.py` screens solver names before any solver is constructed, converting
what would be a silent crash into an ordinary error message listing the 17
working alternatives. Each solver was probed in a subprocess so that a crashing
one could not take the probe down with it.

---

## 11. Decision procedure 4: ROBDD

### 11.1 Intuition

A **Reduced Ordered Binary Decision Diagram** is a canonical compressed form of
a Boolean function. The analogy is reducing fractions to lowest terms: is 6/8
the same as 9/12? Reduce both and each becomes 3/4, at which point equality is
immediate.

For a fixed variable order, ROBDDs are **canonical** (Bryant, 1986): two
functions are equal *if and only if* their ROBDDs are the identical node. So
equivalence requires no search at all — the miter is unsatisfiable exactly when
its BDD is the `FALSE` terminal.

### 11.2 Implementation

Written from scratch rather than taken from a library, so that genuine node
counts could be reported and ordering experiments run.

Both reduction rules are enforced at construction time by `make_node`:

1. **Redundant test elimination** — a node whose two branches are identical is
   not created; the child is returned.
2. **Isomorphic node sharing**: a unique table returns the existing node for
   any `(level, low, high)` triple already present.

All operations are built on `ite(f, g, h)` with a computed-result cache.
Construction from an AIG walks the cone in topological order, so the recursion
depth of `ite` is bounded by the number of variables rather than circuit depth.

A **node budget** turns the expected multiplier blow-up into a clean "aborted"
result rather than an out-of-memory crash, which is what makes the
exponential behaviour *measurable* rather than merely fatal.

### 11.3 Variable ordering

BDD size is exquisitely sensitive to variable order, and finding the optimal
order is NP-hard. Four static heuristics are provided:

- `interleaved` — alternate bits of the input ports (`a0, b0, a1, b1, …`),
  keeping bits related by a carry adjacent
- `dfs` — order of first encounter in a depth-first walk back from the miter
  output
- `declaration` — port declaration order
- `reverse`: the inverse

Plus `auto`, which tries all four under a *shrinking* budget (each attempt
capped at the best size found so far, so hopeless orders abort almost
immediately) and keeps the winner; and `sift`, a rebuild-based implementation
of Rudell's sifting (ICCAD 1993) that moves each variable through the order and
keeps the best position, using the same abort-budget trick.

The sifting here rebuilds the BDD per trial rather than swapping adjacent
levels in place as a production package would. That is asymptotically worse,
and the measured payoff is correspondingly modest: a negative result
reported in §16.3.

---

## 12. Decision procedure 5: algebraic reduction

This backend was added after measurement showed multipliers to be the one case
where every search-based method fails. It follows the AMulet line of work
(Kaufmann, Biere & Kauers).

### 12.1 Intuition

Search is the wrong paradigm for arithmetic. A multiplier is not a puzzle to be
searched; it is an *algebraic identity* to be verified. So verify it
algebraically.

### 12.2 The formulation

Work in `Z[x₁, …, xₙ]` modulo the Boolean relations `x² = x`. Each AIG AND node
becomes a polynomial equation:

```
v − L(l₁)·L(l₂) = 0        where  L(x) = x      for a positive literal
                                  L(x) = 1 − x  for a negated literal
```

The property to prove becomes the **specification polynomial**:

```
SPEC  =  Σ 2^i·p_i  −  (Σ 2^i·a_i)·(Σ 2^j·b_j)
```

The circuit is a correct multiplier **exactly when `SPEC` reduces to zero**
modulo the gate relations.

### 12.3 Why this is cheap

Reduction modulo an arbitrary set of polynomials requires a **Gröbner basis**,
whose computation is doubly exponential, which would make this useless.

The saving grace, and the reason the method works at all: the gate polynomials
**already form a Gröbner basis**, provided variables are ordered so that every
gate is greater than its inputs. Reduction then degenerates into plain
**substitution in reverse topological order** — eliminate the variables nearest
the outputs first, walk back toward the inputs, and check that everything
cancels. No Buchberger algorithm, no basis computation.

### 12.4 Representation

Because `x² = x`, exponents never exceed one, so a **monomial is simply a set of
variables**. A polynomial is therefore a dictionary:

```
frozenset(variables)  →  integer coefficient
```

Multiplying two monomials is a **set union**. Substituting `v := P` splits the
polynomial as `S = v·Q + R` (monomials containing `v`, with `v` removed, versus
the rest) and rewrites it as `P·Q + R`.

### 12.5 Results

| Multiplier width | Gates | Peak polynomial terms | Algebraic | Plain SAT |
|---:|---:|---:|---:|---:|
| 8 | 612 | 221 | **0.015 s** | 8.5 s |
| 12 | 1,492 | 309 | **0.034 s** | **11,509 s** (3 h 12 m) |
| 16 | 2,756 | 429 | 0.069 s | not reachable |
| 24 | 6,436 | 765 | 0.225 s | not reachable |
| 32 | 11,652 | 1,229 | 0.578 s | not reachable |
| 48 | 26,692 | 2,541 | 2.467 s | not reachable |
| **64** | **47,876** | **4,365** | **7.639 s** | not reachable |

The 12×12 case is the headline: **11,509 seconds by SAT, 0.034 seconds
algebraically**: a factor of roughly **340,000**. A 64×64 multiplier with
47,876 gates and 128 primary inputs is proved in 7.6 seconds, a problem no
amount of CDCL would ever finish.

Peak polynomial size grows approximately quadratically in width (221 terms at
8 bits, 4,365 at 64), which is why the method scales where search does not.

### 12.6 A stronger statement than equivalence

This backend verifies each design against the **arithmetic specification
itself**, not against the other design. No miter is constructed. Equivalence
follows as a corollary: if both circuits compute `a × b`, they compute the same
function.

This is how arithmetic circuits are actually verified in industry, and it means
cost depends on each circuit separately rather than on how differently the two
are structured.

### 12.7 The catch: proving is cheap, refuting is not

The method is sharply asymmetric:

| 6×6 multiplier | Peak polynomial terms | Time |
|---|---:|---:|
| **Correct** | 186 | 0.01 s |
| **Broken** (one partial product mis-shifted) | **621,004** | 33 s |

When the circuit is right, everything cancels and the polynomials stay tiny.
When it is wrong, nothing cancels and the polynomial explodes.

This is the exact mirror image of random simulation, which refutes in
microseconds and proves nothing. The two are **complementary, not competing**:
simulate to find bugs, reduce polynomials to prove correctness.

---

## 13. Diagnosis: from "wrong" to "wrong here"

A checker that reports only "they differ, here is an input" leaves the designer
to find the bug. Three features close that gap.

### 13.1 Counterexample minimisation

A 33-bit counterexample is mostly noise. The question asked of each bit is:
*if this bit were free to be anything, would the bug still occur?*

A care set `C` is valid when fixing `C` **forces** the miter to 1. So the test
for dropping a bit is **not** "is the miter still satisfiable": it always is,
since the designs genuinely differ — but:

```
solve( [miter = 0] + remaining fixed bits )   must be UNSAT
```

UNSAT means every completion of the remaining assignment still exposes the bug,
so the dropped bit was irrelevant. Bits are tried greedily, once each, yielding
a locally minimal care set. All solves are incremental under assumptions, so
the entire scan costs one solver instance.

> **This criterion was wrong in the first implementation.** Testing for
> continued satisfiability freed *every* bit, reporting a care set of size zero.
> The bug was caught because the reported answer was nonsensical.

**Results:**

| Design | Care set | Total input bits |
|---|---|---|
| Buggy carry-lookahead adder | **8** (`a[3:6]`, `b[3:6]`) | 33 |
| Buggy ALU | **6** (`a[7]`, `b[0:1]`, `op[0:2]`) | 19 |

The adder's care set localises to exactly one carry-lookahead block's inputs
plus its carry source — pointing directly at the faulty block. Independently
verified: 200 out of 200 random completions of the care set still expose the
bug.

### 13.2 Fault localisation

Following Smith, Veneris et al. (IEEE TCAD 2005).

A gate `n` is a **single-fix location** if some replacement function at `n`
would make the designs equivalent, that is, if for every input there exists a
value at `n` repairing all outputs. Negating, and noting the value ranges over
only `{0, 1}`:

```
SAT( miter(n := 0)  ∧  miter(n := 1) )   ⟹  n is NOT a fix location
UNSAT                                     ⟹  n IS a valid fix location
```

Build two copies of the revised design sharing the primary inputs: one with
`n` tied low, one tied high — miter each against the reference, and ask whether
any input defeats both at once. If none can, that gate can always be made to
repair the design.

Structural hashing shares everything outside `n`'s fan-out cone between the two
copies, so forcing one gate two ways costs far less than two circuits. Only
gates feeding a *failing* output are considered.

**Result on a 16-bit ripple-carry adder with one deliberately broken gate:**

```
failing bits : sum[10..15], cout
cone         : 142 gates, 55 candidates examined
fix locations: 1  ->  c[10]
```

`c[10]` is exactly the gate that was broken. Candidates are reported by
**hierarchical Verilog name** rather than internal node id, which required the
elaborator to retain every scope so that an AIG node can be traced back to the
signal that produced it (`u0.c[3]`, not `node 61`).

### 13.3 N-fault diagnosis

Single-fix search fails outright when several gates are wrong at once — and
that is the common case, because a bug inside a module instantiated four times
**is** four faults.

Counterexample-driven diagnosis handles this:

- take *k* input vectors on which the designs disagree
- build *k* replicas of the revised design, all sharing one set of **selector
  variables** `s_n`, one per candidate gate
- in each replica, gate `n` produces its normal value when `s_n = 0`, and a
  **free variable** when `s_n = 1`: the gate is cut and may take any value
- pin each replica's inputs to its vector and its outputs to the reference's
  output on that vector
- constrain `Σ s_n ≤ N` using PySAT cardinality encoding

Searching `N = 1, 2, 3, …` and stopping at the first satisfiable bound yields a
**minimum-cardinality** diagnosis. Each candidate set is then verified
*exactly* by enumerating all `2^|S|` constant forcings and demanding that no
input defeat all of them.

**Result on the carry-lookahead bug (one fault × four instances):** no single
fix exists; minimum diagnosis size **4**; verified set
`{u0.g[0], u1.g[0], u2.g[0], u3.g[0]}`: one gate per instance, the correct
shape.

**Two honest caveats**, both documented in the tool's own output:

1. **Many gate sets can repair a design.** It was verified directly that the
   *true* fault set `{u0.c[3] … u3.c[3]}` is also a valid fix set. The true
   fault is **guaranteed to be among** the valid diagnoses, but is **not
   guaranteed to be ranked first**. This is inherent to fault diagnosis.
2. **A degenerate diagnosis always exists**: any design can be "repaired" by
   replacing its own primary outputs. This was initially ranked *first*. Such
   sets are now ranked last and deeper gates preferred.

---

## 14. Test cases

The assignment requires a minimum of two test cases of medium-to-large
complexity. **Fourteen design pairs** are provided across 22 Verilog files.
Every pair is a different topology rather than a cosmetic edit, so
each proof is real work.

| # | Reference | Revision | Expected |
|---|---|---|---|
| 1 | 16-bit ripple-carry adder (hierarchical, `generate`) | 16-bit carry-lookahead, four 4-bit CLA blocks | equivalent |
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

Cases 4 and 14 are parameterised (`-p WIDTH=n`) and drive the scaling studies.

### 14.1 Complexity of the test cases

The assignment requires a minimum of two test cases of medium-to-large
complexity. The table below gives the measured size of each miter, so the
claim can be checked rather than taken on trust. "Miter gates" is the number
of AND nodes in the cone actually driving the comparison, after structural
hashing; "depth" is the longest input-to-output path in AND levels.

| Test case | Inputs | Input space | Miter gates | Depth | CNF clauses |
|---|---:|---|---:|---:|---:|
| 128-bit ripple-carry vs carry-select | **257** | **2^257** | 3,097 | 268 | 9,292 |
| 64-bit ripple-carry vs carry-select | 129 | 2^129 | 1,545 | 139 | 4,636 |
| 32-bit ripple-carry vs carry-select | 65 | 2^65 | 769 | 74 | 2,308 |
| 12x12 multiplier behavioural vs carry-save | 24 | 2^24 | **2,645** | 91 | 7,936 |
| 8x8 multiplier behavioural vs carry-save | 16 | 2^16 | 1,085 | 59 | 3,256 |
| 16-bit ripple-carry vs Kogge-Stone | 33 | 2^33 | 416 | 41 | 1,249 |
| 16-bit carry-lookahead vs Kogge-Stone | 33 | 2^33 | 419 | 22 | 1,258 |
| 16-bit ripple-carry vs carry-lookahead | 33 | 2^33 | 341 | 41 | 1,024 |
| 8-bit ALU, 8 operations | 19 | 2^19 | 652 | 38 | 1,957 |
| 8-bit shifter vs crossbar | 11 | 2^11 | 165 | 15 | 496 |
| popcount sequential vs adder tree | 8 | 2^8 | 159 | 21 | 478 |
| priority encoder, casez vs one-hot mask | 8 | 2^8 | 96 | 21 | 289 |
| Gray encode-then-decode vs identity | 8 | 2^8 | 69 | 21 | 208 |
| ISCAS-85 c17, gates vs truth table | 5 | 2^5 | 163 | 54 | 490 |

Eight of the fourteen pairs are substantial by any measure. The clearest single
piece of evidence is not a size figure but a runtime: the 12x12 multiplier
takes **11,509 seconds of CDCL search** to prove by the plain miter (§16.5), so
it is emphatically not a toy. The 128-bit adder has an input space of `2^257`,
larger than the number of atoms in the observable universe, and the algebraic
backend proves a 64x64 multiplier of 47,876 gates (§12.5).

The last four rows are small, and are included deliberately for reasons other
than size: c17 is the standard published ISCAS-85 benchmark and carries
recognition value; the Gray roundtrip is interesting for its *property* rather
than its size (the composition of encode and decode is the identity, though
nothing in the circuit says so); the priority encoder is the only case
exercising `casez` wildcard semantics; and the shifter covers the barrel and
crossbar paths cheaply. They supplement the suite rather than carry it.


### 14.2 Design notes

- **Case 2/3 (Kogge-Stone)** exercises nested generate loops with an inner
  generate-`if`, the most demanding frontend construct in the suite.
- **Case 12 (Gray roundtrip)** is elegant: the composition of Gray encode and
  decode is the identity, but nothing in the circuit says so — proving it
  requires reasoning about the whole XOR-prefix chain.
- **Case 13 (c17)** is the standard ISCAS-85 smoke test. The truth-table
  variant was generated programmatically from the published c17 function, not
  from this tool's own simulator, so the comparison is independent.
- **Cases 5, 6, 8** carry realistic bugs — a dropped product term, a single
  broken gate, a swapped mux input — rather than obviously broken logic.
  Case 6 exists specifically because cases 5 and 8 are *multi*-fault and cannot
  exercise single-fix localisation.

---

## 15. Validation methodology

A checker that grades itself proves very little. A buggy checker that always
answered "equivalent" would pass naive testing. Seven layers plus a fuzzer.

### 15.1 The seven layers (`run_tests.py`, 54 checks)

18 frontend designs, 15 equivalence cases, 5 engine cross-checks, 3 localisation checks and 13 algebraic checks.

1. **Frontend validation.** Each of 18 designs is simulated *on its own*
   against an independent golden model written in Python — exhaustively where
   the input space permits, otherwise on 400 random vectors. This is the layer
   that catches parser and bit-blaster bugs which would otherwise **cancel out**
   between the two designs and yield a false "EQUIVALENT".
2. **Equivalence checking.** Each pair against its expected verdict, with the
   SAT and BDD backends required to agree with each other.
3. **Witness replay.** Every reported counterexample is re-simulated through
   both designs; one that does not reproduce is a bug.
4. **Care-set validation.** For each minimised counterexample, 200 random
   completions of the care set must *all* still expose the bug.
5. **Engine cross-checks.** SAT sweeping, the plain miter and per-output
   analysis must agree on every design.
6. **Localisation ground truth.** For designs with a deliberately planted
   fault, diagnosis must name the gate that was actually broken: the only
   tests where the correct answer is known exactly.
7. **Algebraic soundness, both directions.** The polynomial backend must prove
   genuine multipliers *and* refuse to prove four deliberately corrupted ones,
   each of which must be refuted or reported inconclusive, never proved.

### 15.2 Randomised differential testing (`fuzz.py`)

The hand-written suite covers designs that were thought of. A fuzzer covers
those that were not.

Each round:

1. Generate a random combinational circuit **A**.
2. Rewrite it into **B** using only semantics-preserving transformations — De
   Morgan, XOR expansion, commutation, double negation, two's-complement
   subtraction, redundancy insertion (`x&x`, `x|x`, `x^0`, `x+0`).
3. Mutate **A** into **C** by corrupting one operator or operand.
4. Establish ground truth by **exhaustive simulation** over the entire input
   space, completely independent of the checker.
5. Require the checker to agree with ground truth on both pairs, and require
   every counterexample to genuinely reproduce.

**More than 1,500 rounds across eight seeds and several circuit shapes have
been run during development, with zero disagreements at every stage; the final
version passes cleanly.** Roughly 4% of mutations turn out to be
behaviour-preserving by accident, and the checker correctly calls each of those
equivalent rather than inventing a difference.

The fuzzer justified itself immediately: its first run exposed a genuine bug —
in one of the **rewrite rules**, where `~{2'd0, (c == 0)}` was used to invert a
mux condition. A bitwise complement of a widened comparison is never zero, so
that "inverted" condition was always true. That was a defect in the test
generator rather than the checker, but it is exactly the class of mistake
hand-written tests never find.

### 15.3 External cross-checking

The miter is exportable in **AIGER** format for independent checking with
Berkeley ABC (`abc -c "read_aiger miter.aag; sat"`), and in **DIMACS** for any
external SAT solver. The AIGER writer was validated by writing an independent
reader and confirming agreement with the internal simulator on 400 out of 400
random vectors.

---

## 16. Experimental results

All timings single-threaded on one machine. Full tables in
`results/benchmark.md`, regenerated by `python benchmark.py`.

### 16.1 Scaling: multiplier width vs each engine

| Width | AIG nodes | CNF vars | CNF clauses | SAT (s) | SAT+sweep (s) | BDD peak nodes | BDD (s) |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 3 | 87 | 87 | 242 | 0.002 | 0.004 | 287 | 0.001 |
| 4 | 204 | 206 | 593 | 0.011 | 0.014 | 1,657 | 0.007 |
| 5 | 363 | 367 | 1,070 | 0.071 | 0.087 | 6,196 | 0.026 |
| 6 | 564 | 570 | 1,673 | 0.365 | 0.304 | 21,061 | 0.091 |
| 7 | 807 | 815 | 2,402 | 1.388 | 1.561 | 67,442 | 0.247 |
| 8 | 1,092 | 1,102 | 3,257 | 8.515 | 8.977 | 210,561 | 1.295 |
| 9 | 1,419 | 1,431 | 4,238 | 33.033 | 46.580 | **> 400,000** | — |

### 16.2 SAT sweeping: how much internal equivalence exists

| Design pair | Cone before | Cone after | Merges | Refuted | Refinements | SAT calls | Outcome |
|---|---:|---:|---:|---:|---:|---:|---|
| 16-bit RCA vs CLA | 341 | **0** | 12 | 0 | 0 | 24 | sat-sweeping |
| 16-bit RCA vs Kogge-Stone | 416 | **0** | 15 | 71 | 2 | 114 | sat-sweeping |
| 16-bit RCA vs carry-select | 381 | **0** | 24 | 0 | 0 | 48 | sat-sweeping |
| 8-bit ALU behav vs struct | 652 | **0** | 85 | 25 | 1 | 209 | sat-sweeping |
| popcount seq vs tree | 159 | **0** | 26 | 10 | 0 | 73 | sat-sweeping |
| Gray roundtrip vs identity | 69 | **0** | 14 | 0 | 0 | 28 | sat-sweeping |
| c17 gates vs truth table | 163 | **0** | 62 | 0 | 0 | 124 | sat-sweeping |
| 6×6 multiplier | 557 | **0** | 13 | 3 | 0 | 32 | sat-sweeping |
| 16-bit RCA vs buggy CLA | 329 | 161 | 12 | 0 | 0 | 24 | sat-sweeping + sat |

Eight of nine pairs are discharged entirely bottom-up, with no output-level
solve.

### 16.3 BDD variable ordering

| Design pair | interleaved | dfs | declaration | reverse | best | after sifting |
|---|---:|---:|---:|---:|---|---:|
| 16-bit RCA vs CLA | 4,557 | **843** | overflow | overflow | dfs (843) | 843 |
| 16-bit RCA vs Kogge-Stone | 5,240 | **1,935** | overflow | overflow | dfs (1,935) | 1,935 |
| 8-bit ALU behav vs struct | **7,498** | overflow | overflow | overflow | interleaved | 6,511 |
| 8-bit shifter | 424 | 290 | overflow | **289** | reverse (289) | 289 |
| popcount seq vs tree | 869 | **459** | overflow | overflow | dfs (459) | 459 |
| 6×6 multiplier | 34,891 | overflow | 25,423 | **21,061** | reverse (21,061) | 20,670 |

`overflow` = exceeded a 400,000-node budget and was abandoned.

### 16.4 SAT solver comparison (identical 8×8 multiplier CNF)

| Solver | Encode (s) | Solve (s) |
|---|---:|---:|
| minisat22 | 0.011 | 15.46 |
| glucose4 | 0.007 | 16.66 |
| maplesat | 0.004 | 17.47 |
| **cadical153** | 0.003 | **11.37** |
| cadical195 | 0.004 | 11.54 |
| lingeling | 0.007 | 26.98 |

### 16.5 The SAT-sweeping crossover

| Width | Plain SAT | SAT + sweeping | Speed-up |
|---:|---:|---:|---:|
| 8 | 8.5 s | 9.0 s | 0.94× |
| 9 | 33.0 s | 46.6 s | 0.71× |
| 10 | not measured | 96.8 s | — |
| **12** | **11,509 s** | **3,227 s** | **3.57×** |

### 16.6 Exhaustive simulation as a baseline

| Multiplier | Input vectors | Exhaustive | SAT |
|---|---:|---:|---:|
| 5×5 | 1,024 | 0.000 s | 0.032 s |
| 6×6 | 4,096 | 0.001 s | 0.143 s |
| 7×7 | 16,384 | 0.002 s | 0.495 s |
| 8×8 | 65,536 | **0.008 s** | 4.335 s |
| 12×12 | 16,777,216 | **5.4 s** | **11,509 s** |

### 16.7 Adder width: where the time actually goes

| Width | Inputs | AIG nodes | Depth | Frontend (s) | CNF clauses | SAT (s) |
|---:|---:|---:|---:|---:|---:|---:|
| 16 | 33 | 388 | 41 | 0.031 | 1,145 | 0.007 |
| 32 | 65 | 780 | 74 | 0.069 | 2,309 | 0.014 |
| 64 | 129 | 1,564 | 139 | 0.120 | 4,637 | 0.051 |
| 128 | 257 | 3,132 | 268 | **0.239** | 9,293 | **0.150** |

---

## 17. Findings and discussion

### 17.1 BDDs hit a wall; SAT bends

Multiplier BDD peak size grows roughly threefold per added bit — 287 nodes at
width 3, 67,442 at width 7, 210,561 at width 8 — and exceeds a 400,000-node
budget at width 9, where SAT still finishes in 33 seconds.

This is Bryant's 1991 result reproduced in miniature: multiplier BDDs are
exponential in **every** variable order, so no heuristic rescues them. SAT
degrades steeply but without a hard cliff. It is precisely why industrial
equivalence checking migrated from BDDs to SAT during the 1990s.

### 17.2 SAT sweeping has a measurable crossover

Sweeping carries a fixed overhead — simulate, classify, then run dozens of
small equivalence solves. At widths 8–9 that overhead exceeds what the merges
save, and sweeping **loses** (0.94× and 0.71×).

But the plain miter is a single monolithic UNSAT proof whose cost explodes: a
factor of **350 for three extra bits** (33 s at width 9 to 11,509 s at width
12). Sweeping decomposes the same obligation into ~73 small solves that grow
far more slowly. The curves cross between width 9 and width 12, and at width 12
sweeping wins by **3.57×**.

Strikingly, that win comes from only **21 merges in a 2,645-node cone**. This
is not bulk simplification: a few well-placed internal equivalences break one
intractable proof into many tractable ones. That is exactly the argument for
sweeping in industrial tools: not that it helps easy problems (it does not) but
that it changes the *shape* of the curve on hard ones.

### 17.3 No BDD ordering heuristic wins everywhere

On the 16-bit adder, a depth-first order peaks at **843** nodes against
interleaved's 4,557 (**5.4× better**) while declaration and reverse order
overflow entirely. On the 6×6 multiplier the ranking **inverts completely**:
DFS overflows and `reverse` wins at 21,061. On the ALU, only interleaved
survives at all.

Since optimal ordering is NP-hard, this is why `auto` (try all under a
shrinking budget, keep the winner) beats committing to any single rule. Sifting
on top of the winner adds 13% on the ALU and 2% on the multiplier, and nothing
at all on the four designs where the best static order was already good — a
poor return for O(n²) rebuilds, and a fair reflection of why real packages sift
in place rather than by rebuilding.

### 17.4 Random simulation resolves most real bugs

Both buggy designs are refuted in under a millisecond with no solver call, and
across 500 fuzzing rounds simulation resolved 481 verdicts. **Falsification is
cheap; only proofs are expensive.** This asymmetry is why the pipeline runs
simulation first.

### 17.5 SAT is not brute force, but that does not make it always faster

The 128-bit adder settles `2^257 ≈ 10^77` input vectors in 63 milliseconds.
No enumeration is occurring; clause learning eliminates exponentially large
families of assignments per conflict.

**However**, measuring exhaustive simulation properly produced an uncomfortable
result: on the 12×12 multiplier, evaluating **all 16.7 million inputs took 5.4
seconds** while SAT took 11,509, brute force was **2,100× faster**.

The two are measuring different axes:

- **Number of inputs** decides whether enumeration is *possible*. 24 inputs →
  trivial. 257 inputs → impossible forever.
- **Structural hardness** decides whether SAT is *fast*. Adders are easy
  regardless of width; multipliers are pathological regardless of how few
  inputs they have.

Multipliers occupy the awkward corner (few inputs, brutal structure) so
enumeration wins. Wide adders occupy the opposite corner, and SAT wins by an
unbounded margin. This is a genuine engineering conclusion: **choose the engine
by input count as well as by structure.**

### 17.6 Two implemented techniques match the current state of the art

The SAT Competition 2025 entry for CaDiCaL/Kissat lists among its new
techniques *"clausal congruence closure … and clausal equivalence sweeping."*
Clausal congruence closure (SAT'24) is structural hashing performed at the CNF
level; clausal equivalence sweeping (FMCAD'24) is SAT sweeping performed at the
CNF level using an embedded mini-solver. Kissat won three gold medals in the
2024 competition largely on the back of them.

Those are stages 1 and 3 of this project's pipeline. They were implemented here
at the *circuit* level because the circuit is available; Kissat must
reconstruct that structure from CNF. This is a useful independent validation of
the architecture chosen.

### 17.7 Past a certain size, the frontend dominates

Adders are easy for SAT, so the 128-bit comparison spends **0.239 s** in Verilog
elaboration and bit-blasting against **0.150 s** in the solver. Beyond a point,
optimising the prover is optimising the wrong thing.

### 17.8 Algebraic methods are categorically better for arithmetic

The single largest result in the project: **11,509 s → 0.034 s** on the 12×12
multiplier, and a 64×64 multiplier proved in 7.6 s. Not an incremental
improvement: a change of paradigm, from search to symbolic computation.

---

## 18. Limitations

Listed plainly, since a verification tool that overstates its guarantees is
worse than useless.

1. **Verilog subset.** No sequential logic (out of scope), no division/modulo,
   no `x`/`z` as values, no memory arrays. Each raises an explicit error.
2. **Multi-fault localisation is heuristic in its ranking.** The true fault is
   guaranteed to be among the valid diagnoses; it is *not* guaranteed to be
   listed first. Verified directly on the CLA case, where both the true fault
   set and the reported one are valid.
3. **The algebraic backend is the textbook core, not AMulet.** It has none of
   the adder detection, variable elimination or XOR rewriting that makes
   heavily restructured industrial multipliers tractable. A sufficiently
   optimised multiplier could still exhaust the term budget — which is reported
   as *inconclusive*, never guessed.
4. **Algebraic refutation is expensive.** Proving a correct circuit is fast;
   refuting a broken one can blow up (621,004 terms on a 6×6). Use simulation
   or SAT for refutation.
5. **Sifting is rebuild-based**, so it is far more expensive than in-place
   level swapping and delivers correspondingly small gains.
6. **Timing measurements at width ≥ 10** come from separate long runs and vary
   by up to roughly 2× under machine load. The width-12 gaps are far outside
   that; the width-8/9 ones are within it, which is why the sweeping crossover
   is stated as a range rather than a point.
7. **Three bundled SAT solvers crash this platform** and are screened out
   rather than fixed.
8. **No proof certificate.** An UNSAT verdict is trusted from the solver.
   Emitting DRUP certificates and checking them independently was investigated
   and confirmed feasible (PySAT produced a 317-line proof for the adder miter)
   but not implemented.

---

## 19. Conclusion

This project set out to build an equivalence checker for two versions of a
combinational Verilog design. What was produced is a complete verification
pipeline in which every component except the SAT solver — parser, elaborator,
bit-blaster, AIG, simulator, CNF encoder, sweeping engine, BDD package,
algebraic engine, fault localiser — is implemented from scratch, across 5,411
lines and 17 modules, validated by 54 checks in seven layers plus more than
1,500 rounds of randomised differential testing against exhaustive simulation,
with zero disagreements.

The central architectural decision — flatten everything to a single
And-Inverter Graph, then build a miter, proved to be the right one. It let one
frontend serve **five** distinct decision procedures, and it made structural
hashing do useful verification work for free.

The most valuable outcome was not any single feature but what measurement
repeatedly revealed. Every significant conclusion in this report began as a
different expectation:

- SAT sweeping was expected to help hard instances uniformly. It **loses** on
  small ones and wins 3.57× at width 12, with a measurable crossover between.
- The multiplier SAT times were being presented as "the cost of proof" until
  exhaustive simulation turned out to be **2,100× faster** on the same problem.
- A single ordering heuristic was expected to dominate. None does; the best
  choice **inverts** between adders and multipliers.
- SAT was assumed to be the right engine for the hard case. Algebraic reduction
  beat it by a factor of **340,000**.

Three real defects were found the same way: a counterexample-minimisation
criterion that asked the wrong question, a sweeping refinement loop that was
documented but never connected, and a fault-localisation ranking that put a
degenerate answer first. Each was found because a reported number looked wrong,
not because a test failed. That is a lesson about verification tools in general:
the ones worth trusting are the ones that report enough detail to be caught
lying.

The final tool answers rather more than the question that was asked. Given two
combinational designs it will prove them equivalent over input spaces of `10^77`
vectors, or produce a distinguishing input, reduce that input to the handful of
bits that actually matter, and name the specific gate in the designer's own
signal hierarchy that would have to change. For arithmetic circuits it bypasses
equivalence checking altogether and proves the design against its mathematical
specification directly.

---

## 20. References

- G. Tseitin, *On the complexity of derivation in propositional calculus*, 1968, linear-size CNF encoding.
- R. Bryant, *Graph-based algorithms for Boolean function manipulation*, IEEE ToC, 1986 — ROBDDs and canonicity.
- R. Bryant, *On the complexity of VLSI implementations and graph representations of Boolean functions with application to integer multiplication*, IEEE ToC, 1991, multiplier BDDs are exponential in every order.
- D. Brand, *Verification of large synthesized designs*, ICCAD 1993: the miter construction.
- R. Rudell, *Dynamic variable ordering for ordered binary decision diagrams*, ICCAD 1993, sifting.
- A. Kuehlmann, F. Krohm, *Equivalence checking using cuts and heaps*, DAC 1997 — AIG-based SAT sweeping.
- A. Smith, A. Veneris, M. Ali, A. Viglas, *Fault diagnosis and logic debugging using Boolean satisfiability*, IEEE TCAD, 2005 — SAT-based fault localisation.
- D. Kaufmann, A. Biere, M. Kauers, *Verifying large multipliers by combining SAT and computer algebra*, FMCAD 2019; and *Improving AMulet2*, STTT 2023, algebraic verification of arithmetic circuits.
- A. Biere et al., *Clausal congruence closure*, SAT 2024; *Clausal equivalence sweeping*, FMCAD 2024, the same two techniques at CNF level.
- Berkeley ABC (`cec`, `&cec`) and Yosys (`equiv_make`, `miter` + `sat`), open-source industrial references.
- PySAT: the SAT solving interface used here.

---

## 21. Appendix: running the tool

### Setup

```bash
python -m venv .venv
.venv/Scripts/python -m pip install python-sat      # Linux/macOS: .venv/bin/python
```

### Verification

```bash
# regression suite (54 checks, 7 layers)
.venv/Scripts/python run_tests.py

# randomised differential testing against exhaustive simulation
.venv/Scripts/python fuzz.py --rounds 500

# full experimental study, regenerates results/benchmark.md
.venv/Scripts/python benchmark.py

# regenerate Graphviz / AIGER artefacts
.venv/Scripts/python make_figures.py
```

### Checking a pair of designs

```bash
# ripple-carry vs Kogge-Stone, both engines
.venv/Scripts/python -m eqcheck tests/adder16_rca.v tests/adder16_ks.v --method both

# find a bug, minimise the witness, name the broken gate
.venv/Scripts/python -m eqcheck tests/adder16_rca.v tests/adder16_rca_buggy1.v \
    --minimize --localize

# discharge a proof entirely by sweeping, and export for ABC
.venv/Scripts/python -m eqcheck tests/adder16_rca.v tests/adder16_ks.v \
    --sweep --aiger miter.aag

# a 16x16 multiplier, algebraically
.venv/Scripts/python -m eqcheck tests/mult_behav.v tests/mult_csa.v \
    -p WIDTH=16 --method algebraic
```

### Key options

| Option | Meaning |
|---|---|
| `--method sat\|bdd\|both\|algebraic` | decision procedure (default `sat`) |
| `--solver NAME` | PySAT solver, e.g. `cadical153`, `glucose4` |
| `--sweep` | run SAT sweeping before the output miter |
| `--minimize` | shrink the counterexample to the bits that matter |
| `--localize [N]` | name the gates that would have to change |
| `--order auto\|sift\|interleaved\|dfs\|declaration\|reverse` | BDD variable order |
| `-p NAME=VALUE` | override a top-level parameter |
| `--outputs` | per-output-bit table with cone size and depth |
| `--aiger` / `--dimacs` / `--dot` / `--bdd-dot` | export formats |
| `--json PATH` | full machine-readable result |

Exit status: `0` equivalent, `1` not equivalent, `2` input error, `3`
inconclusive, `4` internal inconsistency between backends.
