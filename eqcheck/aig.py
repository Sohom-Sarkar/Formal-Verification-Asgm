"""And-Inverter Graph: the common intermediate representation.

Both the SAT and the BDD backends consume an AIG, so the Verilog frontend is
written once. The representation follows the AIGER convention:

    literal = (node_id << 1) | inverted
    literal 0 = constant FALSE, literal 1 = constant TRUE

Node 0 is the constant node and owns literals 0 and 1. Inverters are encoded in
the literal's low bit rather than as nodes, so complementing is free and never
grows the graph.

Every AND node passes through a structural hash (`strash`), so a sub-circuit
that is built twice is stored once. That is the cheapest form of the
node-sharing that industrial checkers rely on.
"""

FALSE = 0
TRUE = 1


def neg(lit):
    """Complement a literal (flip the inverter bit)."""
    return lit ^ 1


def negate_if(lit, condition):
    return lit ^ 1 if condition else lit


def node_of(lit):
    return lit >> 1


def is_inverted(lit):
    return lit & 1


def is_const(lit):
    return lit <= 1


class AIG:
    """An and-inverter graph with structural hashing and constant folding."""

    def __init__(self):
        # node id -> (left_lit, right_lit); node 0 is the constant, so it has
        # no entry here.
        self.and_gates = {}
        self._strash = {}
        self.inputs = []            # node ids, in declaration order
        self.input_names = {}       # node id -> display name
        self._name_to_lit = {}      # display name -> literal
        self.outputs = []           # list of (name, literal)
        self._next_node = 1

    # ---------------------------------------------------------------- inputs

    def new_input(self, name):
        node = self._next_node
        self._next_node += 1
        self.inputs.append(node)
        self.input_names[node] = name
        lit = node << 1
        self._name_to_lit[name] = lit
        return lit

    def input_lit(self, name):
        return self._name_to_lit[name]

    def add_output(self, name, lit):
        self.outputs.append((name, lit))

    # ----------------------------------------------------------- gate builders

    def mk_and(self, a, b):
        # Constant folding and trivial identities. These fire constantly during
        # bit-blasting (carry chains produce a lot of AND(x, 0)), so doing them
        # here keeps the graph far smaller than the naive construction.
        if a == FALSE or b == FALSE:
            return FALSE
        if a == TRUE:
            return b
        if b == TRUE:
            return a
        if a == b:
            return a
        if a == neg(b):
            return FALSE

        # Canonical operand order makes the structural hash order-insensitive.
        if a > b:
            a, b = b, a

        key = (a, b)
        hit = self._strash.get(key)
        if hit is not None:
            return hit << 1

        node = self._next_node
        self._next_node += 1
        self.and_gates[node] = (a, b)
        self._strash[key] = node
        return node << 1

    def mk_nand(self, a, b):
        return neg(self.mk_and(a, b))

    def mk_or(self, a, b):
        # De Morgan: a | b == ~(~a & ~b)
        return neg(self.mk_and(neg(a), neg(b)))

    def mk_nor(self, a, b):
        return neg(self.mk_or(a, b))

    def mk_xor(self, a, b):
        if a == FALSE:
            return b
        if b == FALSE:
            return a
        if a == TRUE:
            return neg(b)
        if b == TRUE:
            return neg(a)
        if a == b:
            return FALSE
        if a == neg(b):
            return TRUE
        return self.mk_or(self.mk_and(a, neg(b)), self.mk_and(neg(a), b))

    def mk_xnor(self, a, b):
        return neg(self.mk_xor(a, b))

    def mk_mux(self, sel, then_lit, else_lit):
        """sel ? then_lit : else_lit"""
        if sel == TRUE:
            return then_lit
        if sel == FALSE:
            return else_lit
        if then_lit == else_lit:
            return then_lit
        return self.mk_or(self.mk_and(sel, then_lit),
                          self.mk_and(neg(sel), else_lit))

    def mk_and_list(self, lits):
        """Balanced AND reduction - shallower than a linear chain."""
        items = list(lits)
        if not items:
            return TRUE
        while len(items) > 1:
            nxt = []
            for i in range(0, len(items) - 1, 2):
                nxt.append(self.mk_and(items[i], items[i + 1]))
            if len(items) % 2:
                nxt.append(items[-1])
            items = nxt
        return items[0]

    def mk_or_list(self, lits):
        items = list(lits)
        if not items:
            return FALSE
        return neg(self.mk_and_list([neg(x) for x in items]))

    def mk_xor_list(self, lits):
        acc = FALSE
        for x in lits:
            acc = self.mk_xor(acc, x)
        return acc

    # ------------------------------------------------------------- statistics

    @property
    def num_ands(self):
        return len(self.and_gates)

    @property
    def num_inputs(self):
        return len(self.inputs)

    def cone(self, roots):
        """Node ids reachable from `roots`, in topological order (inputs first).

        Iterative so that deep carry chains cannot blow the Python stack.
        """
        seen = set()
        order = []
        for root in roots:
            if is_const(root):
                continue
            stack = [(node_of(root), False)]
            while stack:
                node, expanded = stack.pop()
                if expanded:
                    order.append(node)
                    continue
                if node in seen or node == 0:
                    continue
                seen.add(node)
                stack.append((node, True))
                gate = self.and_gates.get(node)
                if gate is not None:
                    for child in gate:
                        if not is_const(child) and node_of(child) not in seen:
                            stack.append((node_of(child), False))
        return order

    def depth(self, roots):
        """Longest path from any input to `roots`, in AND levels.

        Logic depth is the standard proxy for circuit delay, and it is what
        separates a ripple-carry adder from a parallel-prefix one even when
        both have a similar node count.
        """
        level = {0: 0}
        best = 0
        for node in self.cone(roots):
            gate = self.and_gates.get(node)
            if gate is None:
                level[node] = 0
                continue
            a, b = gate
            level[node] = 1 + max(level.get(node_of(a), 0), level.get(node_of(b), 0))
            best = max(best, level[node])
        return best

    def stats(self, roots=None):
        info = {
            "inputs": self.num_inputs,
            "and_nodes": self.num_ands,
            "outputs": len(self.outputs),
        }
        if roots is not None:
            cone = self.cone(roots)
            info["cone_nodes"] = sum(1 for n in cone if n in self.and_gates)
            info["depth"] = self.depth(roots)
        return info


# ---------------------------------------------------------------------------
# Tseitin transformation
# ---------------------------------------------------------------------------

class CNF:
    """A DIMACS CNF, plus the AIG-literal -> DIMACS-literal mapping."""

    def __init__(self):
        self.clauses = []
        self.num_vars = 0

    def add(self, *clause):
        self.clauses.append(list(clause))

    def new_var(self):
        self.num_vars += 1
        return self.num_vars

    def to_dimacs(self, path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("p cnf %d %d\n" % (self.num_vars, len(self.clauses)))
            for clause in self.clauses:
                handle.write(" ".join(str(x) for x in clause) + " 0\n")


def tseitin(aig, roots):
    """Encode the cone of `roots` into CNF.

    For an AND node c = a & b the standard three clauses are emitted:

        (~c | a)  (~c | b)  (c | ~a | ~b)

    which together assert c <-> (a & b). Only nodes inside the cone are
    encoded, so unused logic costs nothing.

    DIMACS variable 1 is pinned to FALSE and represents the AIG constant node,
    which lets AIG literal 0/1 map to DIMACS +1/-1 with no special casing.
    """
    cnf = CNF()
    const_var = cnf.new_var()          # var 1 == AIG node 0
    cnf.add(-const_var)                # pin it to FALSE

    var_of_node = {0: const_var}

    def dimacs(lit):
        var = var_of_node[node_of(lit)]
        return -var if is_inverted(lit) else var

    for node in aig.cone(roots):
        if node in var_of_node:
            continue
        var_of_node[node] = cnf.new_var()
        gate = aig.and_gates.get(node)
        if gate is None:
            continue                    # primary input: a free variable
        a, b = gate
        c = var_of_node[node]
        la, lb = dimacs(a), dimacs(b)
        cnf.add(-c, la)
        cnf.add(-c, lb)
        cnf.add(c, -la, -lb)

    # Primary inputs that never reached the cone still need variables so that a
    # returned model can report a value for every input.
    for node in aig.inputs:
        if node not in var_of_node:
            var_of_node[node] = cnf.new_var()

    return cnf, var_of_node, dimacs
