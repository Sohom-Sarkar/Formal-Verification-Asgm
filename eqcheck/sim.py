"""Simulation of a single elaborated design.

Comparing two designs cannot catch a systematic frontend bug: if the parser
misreads the same construct in both, they stay equal to each other and the
checker reports EQUIVALENT. Simulating one design against an independent Python
model closes that gap, which is what the regression suite uses it for.
"""

from .aig import node_of, is_inverted
from .elaborate import Elaborator
from .vparser import parse_file, parse_text


class Simulator:
    def __init__(self, path=None, top=None, params=None, text=None):
        from .equiv import _infer_top

        modules = parse_text(text) if text is not None else parse_file(path)
        self.top = top or _infer_top(modules)

        elaborator = Elaborator(modules)
        scope, outputs = elaborator.elaborate_top(
            self.top, param_overrides=params or {})

        self.aig = elaborator.aig
        self.warnings = elaborator.warnings
        self.outputs = outputs                      # (name, Signal, bits)

        self.inputs = []                            # (name, width, bits)
        for name in scope.port_order:
            signal = scope.signals.get(name)
            if signal is not None and signal.direction == "input":
                self.inputs.append((name, signal.width, list(signal.bits)))

        self._roots = [lit for _, _, bits in outputs for lit in bits]
        self._order = self.aig.cone(self._roots)

    @property
    def input_widths(self):
        return {name: width for name, width, _ in self.inputs}

    @property
    def output_widths(self):
        return {name: len(bits) for name, _, bits in self.outputs}

    def eval(self, values):
        """Evaluate the design. `values` maps port name -> integer."""
        node_value = {0: False}
        for name, width, bits in self.inputs:
            value = values.get(name, 0)
            for pos, lit in enumerate(bits):
                node_value[node_of(lit)] = bool((value >> pos) & 1)

        def lit_value(lit):
            return node_value[node_of(lit)] ^ bool(is_inverted(lit))

        for node in self._order:
            if node in node_value:
                continue
            gate = self.aig.and_gates.get(node)
            node_value[node] = (lit_value(gate[0]) and lit_value(gate[1])
                                if gate else False)

        result = {}
        for name, _, bits in self.outputs:
            out = 0
            for pos, lit in enumerate(bits):
                if lit_value(lit):
                    out |= 1 << pos
            result[name] = out
        return result
