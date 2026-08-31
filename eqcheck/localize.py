"""SAT-based fault localisation (Smith & Veneris, IEEE TCAD 2005).

A node n is a single-fix location if some replacement function at n would make
the designs equivalent, i.e. if for every input there is a value at n that
repairs the outputs. Since that value is 0 or 1, the negation is propositional:

    SAT( miter(n := 0) AND miter(n := 1) )  =>  n is NOT a fix location
    UNSAT                                   =>  n IS a valid fix location

Build two copies of the revised design sharing the primary inputs, one with n
tied low and one high, miter each against the reference, and look for an input
that defeats both. Only nodes feeding a failing output are considered.

Several nodes are usually valid at once, since a fault can often be compensated
downstream. The true culprit is among them, but the set is not a singleton.
`diagnose` handles the multi-fault case, which is common because a bug inside a
module instantiated four times is four faults.
"""

import time

from .aig import AIG, FALSE, TRUE, node_of, is_inverted, tseitin
from .elaborate import Elaborator, signal_names_by_node
from .solvers import DEFAULT_SOLVER
from .vparser import parse_file, parse_text


class DesignInstance:
    """One design elaborated into a private AIG, with its own primary inputs."""

    def __init__(self, path=None, text=None, top=None, params=None):
        from .equiv import _infer_top

        modules = parse_text(text) if text is not None else parse_file(path)
        self.top = top or _infer_top(modules)

        elaborator = Elaborator(modules)
        scope, outputs = elaborator.elaborate_top(
            self.top, param_overrides=params or {})

        self.aig = elaborator.aig
        self.outputs = outputs                       # (name, Signal, bits)
        self.names = signal_names_by_node(elaborator.scopes)

        self.inputs = {}                             # port name -> [literals]
        self.input_order = []
        for name in scope.port_order:
            signal = scope.signals.get(name)
            if signal is not None and signal.direction == "input":
                self.inputs[name] = list(signal.bits)
                self.input_order.append((name, signal.width))

    @property
    def output_map(self):
        return {name: bits for name, _, bits in self.outputs}


def _remap(mapping, lit):
    return mapping[lit >> 1] ^ (lit & 1)


def _copy_cone(src, roots, dst, input_node_to_lit,
               substitute_node=None, substitute_lit=FALSE):
    """Copy the cone of `roots` from `src` into `dst`.

    `substitute_node` is forced to `substitute_lit`. Structural hashing in `dst`
    shares the untouched part between copies.
    """
    mapping = dict(input_node_to_lit)
    mapping[0] = FALSE

    for node in src.cone(roots):
        if node in mapping:
            continue
        gate = src.and_gates.get(node)
        if gate is None:
            mapping[node] = FALSE               # undriven input, tied low
            continue
        a, b = gate
        lit = dst.mk_and(_remap(mapping, a), _remap(mapping, b))
        if node == substitute_node:
            lit = substitute_lit
        mapping[node] = lit

    return [_remap(mapping, r) for r in roots], mapping


def _miter_of(dst, spec_bits, impl_bits):
    terms = [dst.mk_xor(a, b) for a, b in zip(spec_bits, impl_bits)]
    return dst.mk_or_list(terms)


def localize(spec_path, impl_path, spec_top=None, impl_top=None,
             spec_text=None, impl_text=None, params=None,
             solver_name=DEFAULT_SOLVER, max_candidates=None,
             progress=None):
    """Find single-fix locations in the revised design.

    Returns a dict with the candidate list (hierarchical names where known),
    plus the statistics needed to judge how sharp the localisation is.
    """
    from pysat.solvers import Solver

    started = time.perf_counter()

    spec = DesignInstance(spec_path, spec_text, spec_top, params)
    impl = DesignInstance(impl_path, impl_text, impl_top, params)

    spec_outputs = spec.output_map
    impl_outputs = impl.output_map
    shared_names = [name for name, _, _ in spec.outputs if name in impl_outputs]

    # Which output bits actually differ? Only their fan-in cones can host a fix,
    # so this prunes the candidate set before any localisation work starts.
    combined = AIG()
    shared_inputs = {}
    for name, width in spec.input_order:
        shared_inputs[name] = [combined.new_input("%s[%d]" % (name, pos))
                               for pos in range(width)]

    spec_map = _input_map(spec, shared_inputs)
    impl_map = _input_map(impl, shared_inputs)

    spec_roots = [lit for name in shared_names for lit in spec_outputs[name]]
    impl_roots = [lit for name in shared_names for lit in impl_outputs[name]]

    spec_copy, _ = _copy_cone(spec.aig, spec_roots, combined, spec_map)
    impl_copy, _ = _copy_cone(impl.aig, impl_roots, combined, impl_map)

    failing = []
    index = 0
    for name in shared_names:
        for pos in range(len(spec_outputs[name])):
            xor_lit = combined.mk_xor(spec_copy[index], impl_copy[index])
            if xor_lit != FALSE:
                cnf, _v, dimacs = tseitin(combined, [xor_lit])
                cnf.add(dimacs(xor_lit))
                with Solver(name=solver_name, bootstrap_with=cnf.clauses) as s:
                    if s.solve():
                        failing.append((name, pos))
            index += 1

    if not failing:
        return {"equivalent": True, "candidates": [], "considered": 0,
                "failing_outputs": [], "time": time.perf_counter() - started}

    # Candidate gates: nodes feeding at least one failing output bit.
    failing_impl_roots = [impl_outputs[name][pos] for name, pos in failing]
    candidate_nodes = [n for n in impl.aig.cone(failing_impl_roots)
                       if n in impl.aig.and_gates]
    if max_candidates is not None:
        candidate_nodes = candidate_nodes[:max_candidates]

    candidates = []
    solver_calls = 0

    for count, node in enumerate(candidate_nodes):
        if progress and count % 25 == 0:
            progress(count, len(candidate_nodes))

        work = AIG()
        work_inputs = {}
        for name, width in spec.input_order:
            work_inputs[name] = [work.new_input("%s[%d]" % (name, pos))
                                 for pos in range(width)]

        s_map = _input_map(spec, work_inputs)
        i_map = _input_map(impl, work_inputs)

        reference, _ = _copy_cone(spec.aig, spec_roots, work, s_map)
        forced_low, _ = _copy_cone(impl.aig, impl_roots, work, i_map,
                                   substitute_node=node, substitute_lit=FALSE)
        forced_high, _ = _copy_cone(impl.aig, impl_roots, work, i_map,
                                    substitute_node=node, substitute_lit=TRUE)

        miter_low = _miter_of(work, reference, forced_low)
        miter_high = _miter_of(work, reference, forced_high)
        probe = work.mk_and(miter_low, miter_high)

        if probe == FALSE:
            satisfiable = False                  # folded away structurally
        else:
            cnf, _v, dimacs = tseitin(work, [probe])
            cnf.add(dimacs(probe))
            with Solver(name=solver_name, bootstrap_with=cnf.clauses) as s:
                satisfiable = s.solve()
            solver_calls += 1

        if not satisfiable:
            candidates.append({
                "node": node,
                "name": impl.names.get(node),
                "depth": impl.aig.depth([node << 1]),
            })

    named = [c for c in candidates if c["name"]]
    return {
        "equivalent": False,
        "failing_outputs": failing,
        "candidates": candidates,
        "named_candidates": named,
        "considered": len(candidate_nodes),
        "impl_cone_nodes": sum(1 for n in impl.aig.cone(impl_roots)
                               if n in impl.aig.and_gates),
        "solver_calls": solver_calls,
        "time": time.perf_counter() - started,
    }


def _input_map(design, shared_inputs):
    """Map a design's own input nodes onto shared literals, matched by name."""
    mapping = {}
    for name, bits in design.inputs.items():
        target = shared_inputs.get(name)
        if target is None:
            continue
        for pos, lit in enumerate(bits):
            if lit is not None and lit > 1 and pos < len(target):
                mapping[node_of(lit)] = target[pos]
    return mapping


# N-fault diagnosis
# Single-fix search fails when several gates are wrong at once, which is the
# usual case: a bug inside a module instantiated four times is four faults.
# Counterexample-driven diagnosis instead (Smith & Veneris): k replicas sharing
# one selector variable per gate, each gate cut and free when its selector is
# set, inputs pinned to a failing vector, outputs pinned to the reference, and
# sum(s_n) <= N. Only accounts for the k vectors supplied, so verify_fix_set
# checks the result exactly.

def _counterexamples(spec, impl, limit=12, seed=0xC0FFEE):
    """Input vectors on which the two designs disagree."""
    import random

    rng = random.Random(seed)
    widths = dict(spec.input_order)

    shared = [n for n in spec.output_map if n in impl.output_map]
    spec_eval = _make_evaluator(spec, shared)
    impl_eval = _make_evaluator(impl, shared)

    vectors = []
    attempts = 0
    while len(vectors) < limit and attempts < 20000:
        attempts += 1
        values = {name: rng.getrandbits(width) for name, width in widths.items()}
        if spec_eval(values) != impl_eval(values):
            vectors.append(values)
    return vectors


def _make_evaluator(design, shared_names):
    """Evaluate a DesignInstance on concrete input values."""
    roots = [lit for name in shared_names for lit in design.output_map[name]]
    order = design.aig.cone(roots)

    def evaluate(values):
        node_value = {0: False}
        for name, bits in design.inputs.items():
            value = values.get(name, 0)
            for pos, lit in enumerate(bits):
                if lit is not None and lit > 1:
                    node_value[node_of(lit)] = bool((value >> pos) & 1)

        def lit_value(lit):
            return node_value.get(node_of(lit), False) ^ bool(is_inverted(lit))

        for node in order:
            if node in node_value:
                continue
            gate = design.aig.and_gates.get(node)
            node_value[node] = (lit_value(gate[0]) and lit_value(gate[1])
                                if gate else False)

        out = {}
        for name in shared_names:
            value = 0
            for pos, lit in enumerate(design.output_map[name]):
                if lit_value(lit):
                    value |= 1 << pos
            out[name] = value
        return out

    return evaluate


def diagnose(spec_path, impl_path, spec_top=None, impl_top=None,
             spec_text=None, impl_text=None, params=None,
             solver_name=DEFAULT_SOLVER, max_faults=4, vectors=12,
             max_sets=5, verify=True):
    """Find small sets of gates whose replacement explains every failure.

    Searches N = 1, 2, ... up to `max_faults` and stops at the first N that
    admits an explanation, so the answer is a minimum-cardinality diagnosis.
    """
    from pysat.card import CardEnc, EncType
    from pysat.formula import IDPool
    from pysat.solvers import Solver

    started = time.perf_counter()

    spec = DesignInstance(spec_path, spec_text, spec_top, params)
    impl = DesignInstance(impl_path, impl_text, impl_top, params)

    impl_out = impl.output_map
    shared = [name for name, _, _ in spec.outputs if name in impl_out]

    witnesses = _counterexamples(spec, impl, limit=vectors)
    if not witnesses:
        return {"equivalent": True, "sets": [], "faults": 0,
                "time": time.perf_counter() - started}

    spec_eval = _make_evaluator(spec, shared)

    impl_roots = [lit for name in shared for lit in impl_out[name]]
    cone_order = impl.aig.cone(impl_roots)
    candidates = [n for n in cone_order if n in impl.aig.and_gates]

    pool = IDPool()
    clauses = []

    const_true = pool.id(("const", "true"))
    clauses.append([const_true])

    selector = {n: pool.id(("sel", n)) for n in candidates}

    def value_var(replica, node):
        return pool.id(("v", replica, node))

    def lit_of(replica, lit):
        node = node_of(lit)
        if node == 0:
            # AIG literal 0 is FALSE, literal 1 is TRUE
            return -const_true if not is_inverted(lit) else const_true
        var = value_var(replica, node)
        return -var if is_inverted(lit) else var

    for index, vector in enumerate(witnesses):
        for name, bits in impl.inputs.items():
            value = vector.get(name, 0)
            for pos, lit in enumerate(bits):
                if lit is None or lit <= 1:
                    continue
                var = value_var(index, node_of(lit))
                clauses.append([var] if (value >> pos) & 1 else [-var])

        for node in cone_order:
            gate = impl.aig.and_gates.get(node)
            if gate is None:
                continue
            a, b = gate
            v = value_var(index, node)
            la, lb = lit_of(index, a), lit_of(index, b)

            if node in selector:
                s = selector[node]
                w = pool.id(("fix", index, node))
                # s = 0  =>  v <-> (a & b)
                clauses.append([s, -v, la])
                clauses.append([s, -v, lb])
                clauses.append([s, -la, -lb, v])
                # s = 1  =>  v <-> w, the gate is cut and free
                clauses.append([-s, -w, v])
                clauses.append([-s, w, -v])
            else:
                clauses.append([-v, la])
                clauses.append([-v, lb])
                clauses.append([v, -la, -lb])

        expected = spec_eval(vector)
        for name in shared:
            want = expected[name]
            for pos, lit in enumerate(impl_out[name]):
                target = lit_of(index, lit)
                clauses.append([target] if (want >> pos) & 1 else [-target])

    selector_lits = list(selector.values())
    found_sets = []
    faults = 0
    solver_calls = 0

    for bound in range(1, max_faults + 1):
        card = CardEnc.atmost(lits=selector_lits, bound=bound,
                              vpool=pool, encoding=EncType.seqcounter)
        with Solver(name=solver_name,
                    bootstrap_with=clauses + card.clauses) as solver:
            while len(found_sets) < max_sets:
                solver_calls += 1
                if not solver.solve():
                    break
                model = set(x for x in solver.get_model() if x > 0)
                chosen = sorted(n for n, s in selector.items() if s in model)
                found_sets.append(chosen)
                solver.add_clause([-selector[n] for n in chosen])
        if found_sets:
            faults = bound
            break
    else:
        # Exhausted the bound without an explanation: the design needs more
        # than `max_faults` simultaneous changes, which is itself informative.
        faults = None

    # Replacing the primary outputs "repairs" any design, so an all-output
    # diagnosis is valid and useless. Rank those last, deeper gates first.
    output_nodes = {node_of(lit) for name in shared for lit in impl_out[name]
                    if lit > 1}

    def rank(chosen):
        degenerate = sum(1 for n in chosen if n in output_nodes)
        depth = sum(impl.aig.depth([n << 1]) for n in chosen) / max(len(chosen), 1)
        return (degenerate, -depth)

    found_sets.sort(key=rank)

    results = []
    for chosen in found_sets:
        diagnosis = {
            "nodes": chosen,
            "names": [impl.names.get(n) for n in chosen],
            "size": len(chosen),
            "at_outputs": sum(1 for n in chosen if n in output_nodes),
        }
        if verify and len(chosen) <= 4:
            diagnosis["verified"] = verify_fix_set(spec, impl, shared, chosen,
                                               solver_name=solver_name)
        results.append(diagnosis)

    return {
        "equivalent": False,
        "faults": faults,
        "found": bool(found_sets),
        "max_faults_searched": max_faults,
        "sets": results,
        "candidates": len(candidates),
        "witnesses": len(witnesses),
        "solver_calls": solver_calls,
        "time": time.perf_counter() - started,
    }


def verify_fix_set(spec, impl, shared, nodes, solver_name=DEFAULT_SOLVER):
    """Exactly verify that `nodes` is a valid fix set.

    S repairs the design iff every input admits some assignment of constants to
    S that makes the outputs match. Negated: S fails iff one input defeats all
    2^|S| forcings, so build a copy per forcing and ask whether every miter can
    be 1 at once. Exponential in |S|, hence small sets only.
    """
    import itertools

    from pysat.solvers import Solver

    work = AIG()
    work_inputs = {}
    for name, width in spec.input_order:
        work_inputs[name] = [work.new_input("%s[%d]" % (name, pos))
                             for pos in range(width)]

    spec_roots = [lit for name in shared for lit in spec.output_map[name]]
    impl_roots = [lit for name in shared for lit in impl.output_map[name]]

    reference, _ = _copy_cone(spec.aig, spec_roots, work,
                              _input_map(spec, work_inputs))

    miters = []
    for pattern in itertools.product((FALSE, TRUE), repeat=len(nodes)):
        forced, _ = _copy_cone_multi(impl.aig, impl_roots, work,
                                     _input_map(impl, work_inputs),
                                     dict(zip(nodes, pattern)))
        miters.append(_miter_of(work, reference, forced))

    probe = work.mk_and_list(miters)
    if probe == FALSE:
        return True
    cnf, _v, dimacs = tseitin(work, [probe])
    cnf.add(dimacs(probe))
    with Solver(name=solver_name, bootstrap_with=cnf.clauses) as solver:
        return not solver.solve()


def _copy_cone_multi(src, roots, dst, input_node_to_lit, substitutions):
    """Like `_copy_cone`, but forces several nodes to constants at once."""
    mapping = dict(input_node_to_lit)
    mapping[0] = FALSE

    for node in src.cone(roots):
        if node in mapping:
            continue
        gate = src.and_gates.get(node)
        if gate is None:
            mapping[node] = FALSE
            continue
        a, b = gate
        lit = dst.mk_and(_remap(mapping, a), _remap(mapping, b))
        if node in substitutions:
            lit = substitutions[node]
        mapping[node] = lit

    return [_remap(mapping, r) for r in roots], mapping
