"""SAT sweeping: proving internal equivalences before the output miter.

Based on Kuehlmann & Krohm, *Equivalence checking using cuts and heaps*, DAC
1997 - the technique that made equivalence checking scale to real designs.

The problem with a plain output miter is that it throws away everything the two
designs have in common. Two implementations of the same adder compute the same
internal carries; asking a solver to prove the outputs equal, from scratch,
ignores that completely.

Sweeping works bottom-up instead:

  1. Run bit-parallel random simulation and group nodes by signature. Nodes
     with different signatures are definitely inequivalent, so only nodes
     sharing a signature are ever considered - the filter is sound.
  2. Walk the graph in topological order, rebuilding it into a fresh AIG. For
     each node, ask the solver whether it is equivalent to an earlier
     representative of its signature class.
  3. If UNSAT, the two are provably equal: *merge* them, so the rebuilt graph
     uses one node for both. Every later node built on top of it now sees the
     merged version, and structural hashing collapses the duplicates
     automatically, so proofs cascade upward.
  4. If SAT, the signature match was a false positive. The counterexample is
     fed back into the simulator, which splits the class so the same wrong
     guess is not made again.

By the time the walk reaches the outputs, the miter has usually already folded
to constant FALSE and the equivalence is proved without ever solving the hard
output-level instance.

Every equivalence test is an *incremental* solve under assumptions. To ask
whether literals a and b are equal we ask for a counterexample twice:

    solve(assumptions=[ a, -b])      -> UNSAT means a implies b
    solve(assumptions=[-a,  b])      -> UNSAT means b implies a

Both UNSAT means a == b. One solver instance is reused for the whole sweep, so
every clause learned while proving one equivalence helps prove the next.
"""

import time

from .aig import AIG, FALSE, TRUE, neg, node_of, is_inverted
from .simulate import ParallelSim, canonical_signature
from .solvers import DEFAULT_SOLVER


class IncrementalEncoder:
    """Keeps a SAT solver in step with an AIG that is still being built."""

    def __init__(self, aig, solver_name=DEFAULT_SOLVER):
        from pysat.solvers import Solver

        self.aig = aig
        self.solver = Solver(name=solver_name)
        self.var_of_node = {}
        self.num_vars = 0
        self.calls = 0

        # DIMACS variable 1 represents the AIG constant node, pinned to FALSE,
        # so AIG literal 0/1 maps to +1/-1 with no special case.
        const_var = self._new_var()
        self.var_of_node[0] = const_var
        self.solver.add_clause([-const_var])

        self._encoded = set()

    def _new_var(self):
        self.num_vars += 1
        return self.num_vars

    def dimacs(self, lit):
        var = self.var_of_node[node_of(lit)]
        return -var if is_inverted(lit) else var

    def encode_upto(self, lit):
        """Ensure every node in the cone of `lit` has clauses in the solver."""
        for node in self.aig.cone([lit]):
            if node in self._encoded:
                continue
            self._encoded.add(node)
            if node not in self.var_of_node:
                self.var_of_node[node] = self._new_var()
            gate = self.aig.and_gates.get(node)
            if gate is None:
                continue                       # primary input: free variable
            a, b = gate
            # Children are encoded first because cone() is topological.
            for child in (a, b):
                child_node = node_of(child)
                if child_node not in self.var_of_node:
                    self.var_of_node[child_node] = self._new_var()
            c = self.var_of_node[node]
            la, lb = self.dimacs(a), self.dimacs(b)
            self.solver.add_clause([-c, la])
            self.solver.add_clause([-c, lb])
            self.solver.add_clause([c, -la, -lb])

    def equivalent(self, a, b):
        """Prove a == b. Returns (True, None) or (False, model)."""
        self.encode_upto(a)
        self.encode_upto(b)
        da, db = self.dimacs(a), self.dimacs(b)

        self.calls += 1
        if self.solver.solve(assumptions=[da, -db]):
            return False, self.solver.get_model()
        self.calls += 1
        if self.solver.solve(assumptions=[-da, db]):
            return False, self.solver.get_model()
        return True, None

    def satisfiable(self, lit, assumptions=()):
        self.encode_upto(lit)
        self.calls += 1
        assume = [self.dimacs(lit)] + list(assumptions)
        if self.solver.solve(assumptions=assume):
            return True, self.solver.get_model()
        return False, None

    def close(self):
        self.solver.delete()


def _model_to_inputs(model, encoder, old_of_new):
    """Turn a solver model into {original input node -> bool}.

    The solver reasons about the rebuilt graph, but the simulator lives on the
    original one, so the values have to be translated back before they can be
    fed in as new simulation vectors.
    """
    if model is None:
        return {}
    value = {abs(x): (x > 0) for x in model}
    assignment = {}
    for new_node, old_node in old_of_new.items():
        var = encoder.var_of_node.get(new_node)
        assignment[old_node] = value.get(var, False) if var else False
    return assignment


class SweepResult:
    def __init__(self):
        self.aig = None
        self.root = FALSE
        self.input_map = {}          # old input node -> new literal
        self.stats = {}


def sat_sweep(aig, root, num_vectors=192, solver_name=DEFAULT_SOLVER,
              max_sat_calls=200000, max_class_size=24, refine_batch=24,
              verbose=False):
    """Rebuild the cone of `root` with all provable internal equivalences merged.

    Returns a SweepResult whose `root` is FALSE if sweeping alone proved the
    two designs equivalent.
    """
    started = time.perf_counter()

    order = aig.cone([root])
    sim = ParallelSim(aig, num_vectors=num_vectors)
    sigs = sim.signatures(order=order)

    new = AIG()
    mapping = {0: FALSE}
    input_map = {}
    old_of_new = {}
    for node in aig.inputs:
        lit = new.new_input(aig.input_names.get(node, "i%d" % node))
        mapping[node] = lit
        input_map[node] = lit
        old_of_new[node_of(lit)] = node

    encoder = IncrementalEncoder(new, solver_name=solver_name)

    # Every literal registered as a class representative, kept as
    # (literal in the new graph, node in the original graph). The original
    # node is what indexes into the signature table, so refinement can re-key
    # the whole table after new vectors arrive.
    registered = []
    classes = {}

    def rebuild_classes():
        classes.clear()
        for entry in registered:
            key, _ = canonical_signature(sigs[entry[1]], sim.mask)
            classes.setdefault(key, []).append(entry)

    def map_lit(lit):
        base = mapping[node_of(lit)]
        return neg(base) if is_inverted(lit) else base

    merges = 0
    refutations = 0
    const_merges = 0
    refinements = 0
    pending = []

    def refine():
        """Fold refuted counterexamples back into the simulation.

        A refuted candidate proves the two nodes differ on some input; adding
        that input as a simulation vector splits their signature class, so no
        later node in the class wastes a solver call on the same wrong guess.
        This is what keeps the candidate lists honest as the sweep proceeds.
        """
        nonlocal sigs, refinements
        if not pending:
            return
        sim.add_vectors(pending)
        pending.clear()
        sigs = sim.signatures(order=order)
        rebuild_classes()
        refinements += 1

    for node in aig.inputs:
        registered.append((mapping[node], node))
    rebuild_classes()

    for node in order:
        gate = aig.and_gates.get(node)
        if gate is None:
            continue                            # inputs already mapped

        a, b = gate
        lit = new.mk_and(map_lit(a), map_lit(b))
        mapping[node] = lit

        if lit in (FALSE, TRUE):
            continue                            # already folded to a constant
        if encoder.calls >= max_sat_calls:
            continue

        if len(pending) >= refine_batch:
            refine()

        signature = sigs[node]
        key, _complemented = canonical_signature(signature, sim.mask)

        # A node whose signature is constant across every vector is a candidate
        # constant. Cheaper to test than a pairwise equivalence, and merging it
        # collapses everything built on top of it.
        if signature == 0 or signature == sim.mask:
            target = FALSE if signature == 0 else TRUE
            probe = lit if target == FALSE else neg(lit)
            sat, model = encoder.satisfiable(probe)
            if not sat:
                mapping[node] = target
                const_merges += 1
                continue
            refutations += 1
            pending.append(_model_to_inputs(model, encoder, old_of_new))

        matched = False
        for entry in list(classes.get(key, ()))[:max_class_size]:
            candidate, candidate_node = entry
            if candidate == lit or candidate in (FALSE, TRUE):
                continue
            # Same class means the signatures are equal or exactly
            # complementary; compare them directly to pick the phase, so the
            # decision stays correct after refinement re-keys the table.
            complemented = sigs[candidate_node] != signature
            target = neg(candidate) if complemented else candidate

            equal, model = encoder.equivalent(lit, target)
            if equal:
                mapping[node] = target
                merges += 1
                matched = True
                break
            refutations += 1
            pending.append(_model_to_inputs(model, encoder, old_of_new))
            if encoder.calls >= max_sat_calls:
                break

        if not matched:
            entry = (lit, node)
            registered.append(entry)
            classes.setdefault(key, []).append(entry)

    new_root = map_lit(root)
    proved_by_sweeping = new_root == FALSE

    encoder.close()

    result = SweepResult()
    result.aig = new
    result.root = new_root
    result.input_map = input_map
    result.stats = {
        "nodes_before": len(aig.and_gates),
        "cone_before": sum(1 for n in order if n in aig.and_gates),
        "nodes_after": new.num_ands,
        "cone_after": sum(1 for n in new.cone([new_root])
                          if n in new.and_gates),
        "merges": merges,
        "constant_merges": const_merges,
        "refuted_candidates": refutations,
        "refinements": refinements,
        "sat_calls": encoder.calls,
        "random_vectors": sim.num_vectors,
        "proved_by_sweeping": proved_by_sweeping,
        "time": time.perf_counter() - started,
    }
    return result
