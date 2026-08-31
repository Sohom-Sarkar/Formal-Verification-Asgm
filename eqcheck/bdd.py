"""Reduced ordered binary decision diagrams.

Written from scratch so node counts are real and variable ordering can be
experimented with.

Both reduction rules are enforced at construction by `make_node`: a node whose
branches are identical is not created, and the unique table returns the existing
node for a triple that already exists. That makes an ROBDD canonical for a fixed
variable order (Bryant, 1986), so two functions are equal exactly when they are
the same node index and equivalence checking needs no search.
"""

FALSE = 0
TRUE = 1


class BDDSizeLimit(Exception):
    """Raised when a BDD exceeds the configured node budget.

    Multipliers make this fire - which is the interesting experimental result,
    not a bug, so it is a distinct exception the caller can catch and report.
    """

    def __init__(self, limit):
        super().__init__("BDD exceeded %d nodes" % limit)
        self.limit = limit


class BDD:
    def __init__(self, num_vars, node_limit=2_000_000):
        self.num_vars = num_vars
        self.node_limit = node_limit
        # node index -> (level, low, high). Levels are positions in the
        # variable order, not variable ids, so reordering is just a remap.
        self.nodes = [None, None]      # indices 0 and 1 are the terminals
        self._unique = {}
        self._ite_cache = {}
        self.peak_nodes = 0

    def make_node(self, level, low, high):
        if low == high:
            return low                              # rule 1
        key = (level, low, high)
        hit = self._unique.get(key)
        if hit is not None:
            return hit                              # rule 2
        if len(self.nodes) >= self.node_limit:
            raise BDDSizeLimit(self.node_limit)
        index = len(self.nodes)
        self.nodes.append(key)
        self._unique[key] = index
        if index > self.peak_nodes:
            self.peak_nodes = index
        return index

    def var(self, level):
        return self.make_node(level, FALSE, TRUE)

    def level_of(self, node):
        if node <= 1:
            return self.num_vars                    # terminals sit below all
        return self.nodes[node][0]

    def ite(self, f, g, h):
        """if-then-else: the single primitive every other operation builds on."""
        # terminal cases
        if f == TRUE:
            return g
        if f == FALSE:
            return h
        if g == h:
            return g
        if g == TRUE and h == FALSE:
            return f

        key = (f, g, h)
        hit = self._ite_cache.get(key)
        if hit is not None:
            return hit

        top = min(self.level_of(f), self.level_of(g), self.level_of(h))
        fl, fh = self._cofactors(f, top)
        gl, gh = self._cofactors(g, top)
        hl, hh = self._cofactors(h, top)

        low = self.ite(fl, gl, hl)
        high = self.ite(fh, gh, hh)
        merged = self.make_node(top, low, high)

        self._ite_cache[key] = merged
        return merged

    def _cofactors(self, node, level):
        """Split `node` on `level`; if it does not test that level it is its own
        cofactor in both directions."""
        if node <= 1:
            return node, node
        nlevel, low, high = self.nodes[node]
        if nlevel != level:
            return node, node
        return low, high

    def apply_and(self, a, b):
        return self.ite(a, b, FALSE)

    def apply_or(self, a, b):
        return self.ite(a, TRUE, b)

    def apply_xor(self, a, b):
        return self.ite(a, self.apply_not(b), b)

    def apply_not(self, a):
        return self.ite(a, FALSE, TRUE)

    def count_nodes(self, roots):
        """Live node count reachable from `roots` (excludes terminals)."""
        seen = set()
        stack = [r for r in roots if r > 1]
        while stack:
            node = stack.pop()
            if node in seen or node <= 1:
                continue
            seen.add(node)
            _, low, high = self.nodes[node]
            stack.extend((low, high))
        return len(seen)

    def satisfying_assignment(self, root):
        """Any path from `root` to TRUE, as {level: bool}.

        Levels not on the path are genuine don't-cares and are simply absent.
        """
        if root == FALSE:
            return None
        assignment = {}
        node = root
        while node > 1:
            level, low, high = self.nodes[node]
            if low != FALSE:
                assignment[level] = False
                node = low
            else:
                assignment[level] = True
                node = high
        return assignment


def build_from_aig(aig, roots, order=None, node_limit=2_000_000):
    """Build BDDs for `roots` under a variable order.

    `order` is AIG input node ids, best first. Defaults to declaration order,
    which is usually poor for adders.
    """
    inputs = list(aig.inputs)
    if order is None:
        order = inputs
    level_of_input = {node: level for level, node in enumerate(order)}
    for node in inputs:
        if node not in level_of_input:
            level_of_input[node] = len(level_of_input)

    manager = BDD(num_vars=len(level_of_input), node_limit=node_limit)

    # node id -> BDD index for the non-inverted literal
    memo = {0: FALSE}
    for node in inputs:
        memo[node] = manager.var(level_of_input[node])

    def lit_to_bdd(lit):
        base = memo[lit >> 1]
        return manager.apply_not(base) if (lit & 1) else base

    for node in aig.cone(roots):
        if node in memo:
            continue
        gate = aig.and_gates.get(node)
        if gate is None:
            memo[node] = manager.var(level_of_input[node])
            continue
        a, b = gate
        memo[node] = manager.apply_and(lit_to_bdd(a), lit_to_bdd(b))

    return manager, [lit_to_bdd(r) for r in roots], level_of_input
