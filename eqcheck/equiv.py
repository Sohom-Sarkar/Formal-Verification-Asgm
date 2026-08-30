"""Miter construction and the SAT/BDD decision procedures.

The miter (Brand, ICCAD 1993): elaborate both designs onto shared primary
inputs, XOR corresponding outputs and OR the results. The miter is satisfiable
exactly when some input makes the designs disagree, so

    UNSAT  ->  equivalent
    SAT    ->  the model is a counterexample

The same root feeds the BDD backend, where the question becomes whether the
miter BDD is the FALSE terminal.
"""

import time

from .aig import AIG, FALSE, node_of, is_inverted, tseitin
from .bdd import build_from_aig, BDDSizeLimit
from .elaborate import Elaborator
from .simulate import ParallelSim
from .solvers import DEFAULT_SOLVER
from .sweep import sat_sweep, IncrementalEncoder
from .vparser import parse_file, parse_text


class PortMismatch(Exception):
    pass


class Design:
    """One elaborated design, ready to be compared."""

    def __init__(self, label, top, scope, outputs):
        self.label = label
        self.top = top
        self.scope = scope
        self.outputs = outputs          # list of (name, Signal, bits)

    @property
    def output_map(self):
        return {name: bits for name, _, bits in self.outputs}

    @property
    def output_widths(self):
        return {name: len(bits) for name, _, bits in self.outputs}


class Miter:
    def __init__(self):
        self.aig = AIG()
        self.input_order = []           # (name, width)
        self.input_bits = {}            # name -> list of literals
        self.designs = []
        self.output_xors = []           # (output_name, list of per-bit xor lits)
        self.root = FALSE
        self.warnings = []


def _collect_ports(modules, top_name, param_overrides=None):
    """Input and output port widths of `top_name`, without elaborating.

    Parameter overrides must be applied here too: on a parameterised design the
    port widths themselves depend on the parameters.
    """
    elaborator = Elaborator(modules)
    if top_name not in elaborator.modules:
        raise PortMismatch("no module named %r in this file (have: %s)"
                           % (top_name, ", ".join(sorted(elaborator.modules))))
    module = elaborator.modules[top_name]
    scope = elaborator._build_scope(module, "", param_overrides or {})

    inputs, outputs = [], []
    for name in scope.port_order:
        signal = scope.signals.get(name)
        if signal is None:
            continue
        if signal.direction == "input":
            inputs.append((name, signal.width))
        elif signal.direction == "output":
            outputs.append((name, signal.width))
    return inputs, outputs


def build_miter(spec_path, impl_path, spec_top=None, impl_top=None,
                spec_text=None, impl_text=None, param_overrides=None):
    """Elaborate both designs onto shared primary inputs and XOR the outputs."""
    spec_modules = parse_text(spec_text) if spec_text else parse_file(spec_path)
    impl_modules = parse_text(impl_text) if impl_text else parse_file(impl_path)

    spec_top = spec_top or _infer_top(spec_modules)
    impl_top = impl_top or _infer_top(impl_modules)

    spec_inputs, spec_outputs = _collect_ports(spec_modules, spec_top,
                                               param_overrides)
    impl_inputs, impl_outputs = _collect_ports(impl_modules, impl_top,
                                               param_overrides)

    _check_ports(spec_inputs, impl_inputs, "input", spec_top, impl_top)
    _check_ports(spec_outputs, impl_outputs, "output", spec_top, impl_top)

    miter = Miter()
    miter.input_order = spec_inputs

    # One shared set of primary inputs feeds both designs - this is what makes
    # the XOR of their outputs meaningful.
    for name, width in spec_inputs:
        miter.input_bits[name] = [miter.aig.new_input("%s[%d]" % (name, pos))
                                  for pos in range(width)]

    designs = []
    for label, modules, top in (("spec", spec_modules, spec_top),
                                ("impl", impl_modules, impl_top)):
        elaborator = Elaborator(modules)
        elaborator.aig = miter.aig          # both designs share one graph
        scope, outputs = elaborator.elaborate_top(
            top, input_lits=miter.input_bits,
            param_overrides=(param_overrides or {}))
        designs.append(Design(label, top, scope, outputs))
        miter.warnings.extend("[%s] %s" % (label, w) for w in elaborator.warnings)

    miter.designs = designs
    spec_map, impl_map = designs[0].output_map, designs[1].output_map

    difference_terms = []
    for name, width in spec_outputs:
        spec_bits = spec_map[name]
        impl_bits = impl_map[name]
        xors = [miter.aig.mk_xor(a, b) for a, b in zip(spec_bits, impl_bits)]
        miter.output_xors.append((name, xors))
        difference_terms.extend(xors)

    miter.root = miter.aig.mk_or_list(difference_terms)
    return miter


def _infer_top(modules):
    """The top module is the one nothing else instantiates."""
    from . import vast
    names = {m.name for m in modules}
    instantiated = set()
    for module in modules:
        stack = list(module.items)
        while stack:
            item = stack.pop()
            if isinstance(item, vast.ModuleInst):
                instantiated.add(item.module_name)
            elif isinstance(item, vast.GenerateBlock):
                stack.extend(item.items)
            elif isinstance(item, vast.For):
                stack.append(item.body)
            elif isinstance(item, vast.If):
                stack.extend(x for x in (item.then_body, item.else_body) if x)
    candidates = [m.name for m in modules if m.name not in instantiated]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise PortMismatch("could not infer a top module (every module is "
                           "instantiated); pass one explicitly")
    raise PortMismatch("ambiguous top module, candidates: %s; pass one explicitly"
                       % ", ".join(sorted(candidates)))


def _check_ports(left, right, kind, spec_top, impl_top):
    left_map = dict(left)
    right_map = dict(right)
    if set(left_map) != set(right_map):
        only_left = sorted(set(left_map) - set(right_map))
        only_right = sorted(set(right_map) - set(left_map))
        raise PortMismatch(
            "%s ports differ between %r and %r; only in first: %s; only in second: %s"
            % (kind, spec_top, impl_top, only_left or "-", only_right or "-"))
    for name in left_map:
        if left_map[name] != right_map[name]:
            raise PortMismatch(
                "%s port %r has width %d in %r but %d in %r"
                % (kind, name, left_map[name], spec_top, right_map[name], impl_top))


# ---------------------------------------------------------------------------
# SAT backend
# ---------------------------------------------------------------------------

def random_simulation(miter, num_vectors=512, seed=0x5EED):
    """Try to refute equivalence by simulation before calling a solver.

    Shallow bugs fall out of almost any random vector.
    """
    started = time.perf_counter()
    sim = ParallelSim(miter.aig, num_vectors=num_vectors, seed=seed)
    sigs = sim.signatures(order=miter.aig.cone([miter.root]))
    index = sim.find_falsifying_vector(sigs, miter.root)
    elapsed = time.perf_counter() - started

    if index is None:
        return {"falsified": False, "vectors": num_vectors, "time": elapsed,
                "counterexample": None}

    assignment = sim.extract_vector(index)
    counterexample = _decode_counterexample(
        miter, lambda node: assignment.get(node, False))
    return {"falsified": True, "vectors": num_vectors, "time": elapsed,
            "counterexample": counterexample}


def check_sat(miter, solver_name=DEFAULT_SOLVER, dimacs_path=None,
              sweep=False, presimulate=True, sim_vectors=512,
              minimize=False, sweep_vectors=192):
    """Decide the miter with SAT, cheapest stage first.

        structural hashing -> random simulation -> SAT sweeping -> SAT

    Any stage can settle it, so the solver only sees what survives the rest.
    """
    from pysat.solvers import Solver

    result = {
        "method": "sat", "solver": solver_name, "equivalent": None,
        "variables": 0, "clauses": 0, "encode_time": 0.0, "solve_time": 0.0,
        "stats": {}, "counterexample": None, "resolved_by": None,
        "simulation": None, "sweep": None, "minimized": None,
    }

    # Stage 1: structural hashing may already have folded the miter away.
    if miter.root == FALSE:
        result.update(equivalent=True, resolved_by="structural-hashing",
                      trivial="structural-hashing")
        return result

    # Stage 2: cheap random simulation.
    if presimulate:
        simulation = random_simulation(miter, num_vectors=sim_vectors)
        result["simulation"] = {k: v for k, v in simulation.items()
                                if k != "counterexample"}
        if simulation["falsified"]:
            result.update(equivalent=False, resolved_by="random-simulation",
                          counterexample=simulation["counterexample"])
            if minimize:
                result["minimized"] = minimize_counterexample(
                    miter, result["counterexample"], solver_name=solver_name)
            return result

    # Stage 3: SAT sweeping merges provable internal equivalences.
    target_aig, target_root = miter.aig, miter.root
    input_map = None
    if sweep:
        swept = sat_sweep(miter.aig, miter.root, num_vectors=sweep_vectors,
                          solver_name=solver_name)
        result["sweep"] = dict(swept.stats)
        if swept.root == FALSE:
            result.update(equivalent=True, resolved_by="sat-sweeping")
            return result
        target_aig, target_root = swept.aig, swept.root
        input_map = swept.input_map     # counterexamples now speak about the
                                        # rebuilt graph, so map back below

    # Stage 4: whatever is left goes to the solver.
    encode_start = time.perf_counter()
    cnf, var_of_node, dimacs = tseitin(target_aig, [target_root])
    cnf.add(dimacs(target_root))
    result["encode_time"] = time.perf_counter() - encode_start

    if dimacs_path:
        cnf.to_dimacs(dimacs_path)

    solve_start = time.perf_counter()
    with Solver(name=solver_name, bootstrap_with=cnf.clauses) as solver:
        satisfiable = solver.solve()
        model = solver.get_model() if satisfiable else None
        try:
            stats = dict(solver.accum_stats())
        except Exception:
            stats = {}
    result["solve_time"] = time.perf_counter() - solve_start

    result.update(variables=cnf.num_vars, clauses=len(cnf.clauses), stats=stats,
                  equivalent=not satisfiable,
                  resolved_by="sat-sweeping+sat" if sweep else "sat")

    if satisfiable:
        assignment = {abs(lit): (lit > 0) for lit in model}

        if input_map is None:
            def value_of(node):
                return assignment.get(var_of_node.get(node), False)
        else:
            def value_of(node):
                lit = input_map.get(node)
                if lit is None:
                    return False
                raw = assignment.get(var_of_node.get(node_of(lit)), False)
                return raw ^ bool(is_inverted(lit))

        result["counterexample"] = _decode_counterexample(miter, value_of)

        if minimize:
            result["minimized"] = minimize_counterexample(
                miter, result["counterexample"], solver_name=solver_name)

    return result


def minimize_counterexample(miter, counterexample, solver_name=DEFAULT_SOLVER):
    """Shrink a counterexample to the input bits that matter.

    A care set is valid when fixing it *forces* the miter to 1, so the test for
    dropping a bit is

        solve( [miter = 0] + remaining fixed bits )   must be UNSAT

    not "is the miter still satisfiable" - it always is. Greedy, one pass, so
    the result is locally minimal. All solves are incremental under
    assumptions.
    """
    started = time.perf_counter()
    encoder = IncrementalEncoder(miter.aig, solver_name=solver_name)
    encoder.encode_upto(miter.root)
    root_lit = encoder.dimacs(miter.root)

    fixed = {}
    for name, width in miter.input_order:
        entry = counterexample["inputs"][name]
        for pos, lit in enumerate(miter.input_bits[name]):
            literal = encoder.dimacs(lit)
            fixed[(name, pos)] = literal if entry["bits"][pos] else -literal

    free = set()
    for key in list(fixed):
        candidate = free | {key}
        trial = [-root_lit] + [v for k, v in fixed.items() if k not in candidate]
        encoder.calls += 1
        if not encoder.solver.solve(assumptions=trial):
            free.add(key)          # miter still forced to 1 without this bit

    care = sorted(k for k in fixed if k not in free)
    encoder.close()

    by_port = {}
    for name, pos in care:
        by_port.setdefault(name, []).append(pos)

    total_bits = sum(width for _, width in miter.input_order)
    return {
        "care_bits": len(care),
        "total_bits": total_bits,
        "free_bits": total_bits - len(care),
        "by_port": {k: sorted(v) for k, v in by_port.items()},
        "solver_calls": encoder.calls,
        "time": time.perf_counter() - started,
    }


def _decode_counterexample(miter, value_of_node):
    """Turn a solver model into per-port input values and the resulting outputs."""
    bit_value = {}
    for name, bits in miter.input_bits.items():
        for pos, lit in enumerate(bits):
            raw = value_of_node(node_of(lit))
            bit_value[lit] = raw ^ bool(is_inverted(lit))

    inputs = {}
    for name, width in miter.input_order:
        bits = miter.input_bits[name]
        value = 0
        for pos in range(width):
            if bit_value.get(bits[pos]):
                value |= 1 << pos
        inputs[name] = {"width": width, "value": value,
                        "bits": [int(bool(bit_value.get(b))) for b in bits]}

    # Re-simulate both designs on that input vector to report actual outputs.
    outputs = simulate(miter, {k: v["value"] for k, v in inputs.items()})
    return {"inputs": inputs, "outputs": outputs}


def simulate(miter, input_values):
    """Evaluate every AIG node under a concrete input assignment.

    Used to report what each design actually produced for a counterexample, and
    as an independent check that the counterexample is real.
    """
    values = {0: False}
    for name, width in miter.input_order:
        value = input_values.get(name, 0)
        for pos, lit in enumerate(miter.input_bits[name]):
            values[node_of(lit)] = bool((value >> pos) & 1)

    def lit_value(lit):
        return values[node_of(lit)] ^ bool(is_inverted(lit))

    roots = [miter.root]
    for design in miter.designs:
        for _, _, bits in design.outputs:
            roots.extend(bits)

    for node in miter.aig.cone(roots):
        if node in values:
            continue
        gate = miter.aig.and_gates.get(node)
        values[node] = lit_value(gate[0]) and lit_value(gate[1]) if gate else False

    result = {}
    for design in miter.designs:
        per_design = {}
        for name, _, bits in design.outputs:
            value = 0
            for pos, lit in enumerate(bits):
                if lit_value(lit):
                    value |= 1 << pos
            per_design[name] = {"width": len(bits), "value": value}
        result[design.label] = per_design
    result["differs"] = lit_value(miter.root)
    return result


# ---------------------------------------------------------------------------
# BDD backend
# ---------------------------------------------------------------------------

def check_bdd(miter, order=None, node_limit=2_000_000):
    """Decide equivalence by building the miter's ROBDD.

    Because an ROBDD is canonical, the miter is unsatisfiable exactly when its
    BDD is the FALSE terminal - no search required.
    """
    if miter.root == FALSE:
        return {
            "method": "bdd", "equivalent": True, "aborted": False,
            "build_time": 0.0, "peak_nodes": 0, "live_nodes": 0,
            "counterexample": None, "trivial": "structural-hashing",
        }

    start = time.perf_counter()
    try:
        manager, roots, level_of_input = build_from_aig(
            miter.aig, [miter.root], order=order, node_limit=node_limit)
    except BDDSizeLimit as exc:
        return {
            "method": "bdd",
            "equivalent": None,
            "aborted": True,
            "reason": str(exc),
            "build_time": time.perf_counter() - start,
        }
    build_time = time.perf_counter() - start

    root = roots[0]
    result = {
        "method": "bdd",
        "equivalent": root == 0,
        "aborted": False,
        "build_time": build_time,
        "peak_nodes": manager.peak_nodes,
        "live_nodes": manager.count_nodes([root]),
        "counterexample": None,
    }

    if root != 0:
        assignment = manager.satisfying_assignment(root)
        level_to_node = {level: node for node, level in level_of_input.items()}
        node_true = {}
        for level, value in assignment.items():
            node = level_to_node.get(level)
            if node is not None:
                node_true[node] = value
        result["counterexample"] = _decode_counterexample(
            miter, lambda node: node_true.get(node, False))
    return result


def interleaved_order(miter):
    """Interleave the bits of equally wide input ports.

    Keeps bits related by a carry adjacent, which is the difference between a
    linear and an exponential BDD on adders.
    """
    groups = [miter.input_bits[name] for name, _ in miter.input_order]
    order = []
    for pos in range(max((len(g) for g in groups), default=0)):
        for group in groups:
            if pos < len(group):
                order.append(node_of(group[pos]))
    return order


def dfs_order(miter):
    """Inputs in the order a depth-first walk from the miter output reaches them."""
    seen = []
    visited = set()
    stack = [node_of(miter.root)]
    inputs = set(miter.aig.inputs)
    while stack:
        node = stack.pop()
        if node in visited or node == 0:
            continue
        visited.add(node)
        if node in inputs:
            seen.append(node)
            continue
        gate = miter.aig.and_gates.get(node)
        if gate:
            # Push right then left so the left child is explored first.
            stack.append(node_of(gate[1]))
            stack.append(node_of(gate[0]))
    for node in miter.aig.inputs:
        if node not in visited:
            seen.append(node)
    return seen


def declaration_order(miter):
    return [node_of(lit) for name, _ in miter.input_order
            for lit in miter.input_bits[name]]


def variable_order(miter, strategy):
    if strategy == "interleaved":
        return interleaved_order(miter)
    if strategy == "declaration":
        return None
    if strategy == "dfs":
        return dfs_order(miter)
    if strategy == "reverse":
        return list(reversed(declaration_order(miter)))
    raise ValueError("unknown variable order strategy %r" % strategy)


STATIC_ORDERS = ("interleaved", "dfs", "declaration", "reverse")


def best_static_order(miter, strategies=STATIC_ORDERS, node_limit=200_000):
    """Try each static heuristic under a shrinking budget, keep the smallest.

    Each attempt is capped at the best size so far, so a hopeless order aborts
    early. Note this makes a per-strategy 'overflow' mean 'worse than the
    incumbent', not 'past the budget'.
    """
    started = time.perf_counter()
    results = {}
    best_name, best_cost, best_order = None, None, None

    for name in strategies:
        order = variable_order(miter, name)
        budget = best_cost if best_cost is not None else node_limit
        try:
            manager, roots, _ = build_from_aig(miter.aig, [miter.root],
                                               order=order, node_limit=budget)
            peak = manager.peak_nodes
        except BDDSizeLimit:
            results[name] = None
            continue
        results[name] = peak
        if best_cost is None or peak < best_cost:
            best_name, best_cost, best_order = name, peak, order

    return {
        "per_strategy": results,
        "best": best_name,
        "peak_nodes": best_cost,
        "order": best_order,
        "time": time.perf_counter() - started,
    }


def sift_order(miter, initial=None, max_builds=160, node_limit=200_000,
               trial_cap=40_000, verbose=False):
    """Improve a variable order by sifting (Rudell, ICCAD 1993).

    Each variable is moved through the order and kept where the BDD was
    smallest. Trial builds are capped at the incumbent size so a bad position
    aborts almost immediately.

    Rebuilds per trial instead of swapping adjacent levels in place, so it is
    much more expensive than a real implementation and gains correspondingly
    little.
    """
    started = time.perf_counter()
    order = list(initial if initial is not None else declaration_order(miter))

    def cost(candidate, budget):
        try:
            manager, roots, _ = build_from_aig(miter.aig, [miter.root],
                                               order=candidate,
                                               node_limit=max(budget, 16))
        except BDDSizeLimit:
            return None
        return manager.peak_nodes

    initial_best = cost(order, node_limit)
    overflowed = initial_best is None
    # If the starting order overflows, search under a deliberately small budget
    # rather than the full limit, so that trials stay cheap.
    best = initial_best if initial_best is not None else trial_cap
    builds = 1

    for index in range(len(order)):
        if builds >= max_builds:
            break
        variable = order[index]
        without = list(order)
        without.pop(index)

        for position in range(len(order)):
            if position == index or builds >= max_builds:
                continue
            trial = without[:position] + [variable] + without[position:]
            builds += 1
            trial_cost = cost(trial, min(best, trial_cap) if overflowed else best)
            if trial_cost is not None and trial_cost < best:
                best = trial_cost
                order = trial
                overflowed = False
                if verbose:
                    print("   sift: peak now %d" % best)
                break

    baseline = initial_best if initial_best is not None else node_limit
    return {
        "order": order,
        "peak_nodes": best if not overflowed else None,
        "initial_peak_nodes": initial_best,
        "initial_overflowed": initial_best is None,
        "builds": builds,
        "improved": (not overflowed) and best < baseline,
        "reduction": (1.0 - best / baseline) if (baseline and not overflowed) else 0.0,
        "time": time.perf_counter() - started,
        "aborted": overflowed,
    }


# ---------------------------------------------------------------------------
# per-output diagnosis
# ---------------------------------------------------------------------------

def analyze_outputs(miter, solver_name=DEFAULT_SOLVER):
    """Per-output-bit verdict, cone size and depth.

    One incremental solver for all bits, queried by assumption. Bits whose XOR
    folded to FALSE were settled by structural hashing and skip the solver.
    """
    started = time.perf_counter()
    encoder = IncrementalEncoder(miter.aig, solver_name=solver_name)

    rows = []
    for name, xors in miter.output_xors:
        for pos, xor_lit in enumerate(xors):
            spec_bits = miter.designs[0].output_map[name]
            impl_bits = miter.designs[1].output_map[name]
            cone = miter.aig.cone([spec_bits[pos], impl_bits[pos]])
            entry = {
                "output": name,
                "bit": pos,
                "cone_nodes": sum(1 for n in cone if n in miter.aig.and_gates),
                "depth": miter.aig.depth([spec_bits[pos], impl_bits[pos]]),
            }
            if xor_lit == FALSE:
                entry.update(differs=False, proved_by="structural-hashing")
            else:
                sat, _model = encoder.satisfiable(xor_lit)
                entry.update(differs=bool(sat),
                             proved_by="sat" if not sat else None)
            rows.append(entry)

    calls = encoder.calls
    encoder.close()
    return {
        "outputs": rows,
        "differing": [(r["output"], r["bit"]) for r in rows if r["differs"]],
        "solver_calls": calls,
        "time": time.perf_counter() - started,
    }


def failing_outputs(miter, solver_name=DEFAULT_SOLVER):
    """Output bits that can differ. Thin wrapper kept for compatibility."""
    return analyze_outputs(miter, solver_name=solver_name)["differing"]
