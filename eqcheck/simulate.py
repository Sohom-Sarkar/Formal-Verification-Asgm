"""Bit-parallel random simulation.

Each node carries a signature: a Python int used as a bit-vector, where bit k is
the node value under random vector k. One `&` therefore evaluates the node under
hundreds of vectors at once.

Two uses: cheap falsification of the miter, and candidate filtering for SAT
sweeping. Nodes with different signatures cannot be equivalent, so signatures
are a sound filter, though matching signatures prove nothing on their own.
"""

import random

from .aig import node_of, is_inverted


class ParallelSim:
    def __init__(self, aig, num_vectors=256, seed=0x5EED):
        self.aig = aig
        self.num_vectors = num_vectors
        self.rng = random.Random(seed)
        self.mask = (1 << num_vectors) - 1
        # node id -> signature. Input patterns are drawn once and reused.
        self.patterns = {}
        for node in aig.inputs:
            self.patterns[node] = self.rng.getrandbits(num_vectors) & self.mask
        self._signatures = None

    def add_vectors(self, assignments):
        """Append specific input vectors, e.g. counterexamples from the solver.

        Feeding a failed candidate's counterexample back in splits the class
        that produced it, so the same wrong guess is not made twice.
        """
        added = len(assignments)
        if not added:
            return
        for node in self.aig.inputs:
            bits = 0
            for i, assignment in enumerate(assignments):
                if assignment.get(node, False):
                    bits |= 1 << i
            self.patterns[node] = (self.patterns[node] << added) | bits
        self.num_vectors += added
        self.mask = (1 << self.num_vectors) - 1
        self._signatures = None

    def signatures(self, order=None, roots=None):
        """Signature for every node in the cone, computed in topological order."""
        if self._signatures is not None and order is None:
            return self._signatures

        if order is None:
            order = self.aig.cone(roots if roots is not None
                                  else [lit for _, lit in self.aig.outputs])

        sigs = {0: 0}
        for node in self.aig.inputs:
            sigs[node] = self.patterns.get(node, 0) & self.mask

        for node in order:
            if node in sigs:
                continue
            gate = self.aig.and_gates.get(node)
            if gate is None:
                sigs[node] = self.patterns.get(node, 0) & self.mask
                continue
            a, b = gate
            sigs[node] = self._lit_sig(sigs, a) & self._lit_sig(sigs, b)

        if order is None:
            self._signatures = sigs
        return sigs

    def _lit_sig(self, sigs, lit):
        value = sigs[node_of(lit)]
        return (~value) & self.mask if is_inverted(lit) else value

    def lit_signature(self, sigs, lit):
        return self._lit_sig(sigs, lit)

    def extract_vector(self, index):
        """Input assignment for random vector `index`, as {input node -> bool}."""
        return {node: bool((pattern >> index) & 1)
                for node, pattern in self.patterns.items()}

    def find_falsifying_vector(self, sigs, lit):
        """Index of a random vector under which `lit` is 1, or None."""
        signature = self._lit_sig(sigs, lit)
        if signature == 0:
            return None
        return (signature & -signature).bit_length() - 1


def canonical_signature(signature, mask):
    """Key that groups a signature with its complement.

    AIG literals carry inversion for free, so a node matching the negation of
    another is just as useful; min(sig, ~sig) buckets both.
    """
    complement = (~signature) & mask
    if signature <= complement:
        return signature, False
    return complement, True
