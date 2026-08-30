"""Elaboration: Verilog AST -> bit-blasted AIG.

Responsibilities, in order:

  * evaluate parameters and unroll `generate` loops
  * create a bit vector for every declared signal
  * register a *driver* for every driven bit
  * resolve bits lazily, on demand, from the primary outputs backwards

Lazy resolution means declaration order does not matter and unused logic is
never built. A signal that is re-entered while it is still being resolved is a
combinational loop, which is reported rather than hung on.
"""

import copy

from . import vast
from .aig import AIG, FALSE, TRUE, neg


class ElaborationError(Exception):
    pass


class CombinationalLoop(ElaborationError):
    pass


class Signal:
    __slots__ = ("name", "msb", "lsb", "width", "direction", "is_reg", "signed", "bits")

    def __init__(self, name, msb, lsb, direction, is_reg, signed):
        self.name = name
        self.msb = msb
        self.lsb = lsb
        self.width = abs(msb - lsb) + 1
        self.direction = direction
        self.is_reg = is_reg
        self.signed = signed
        self.bits = [None] * self.width

    def pos_of_index(self, index):
        """Verilog bit index -> position in the vector (0 = LSB)."""
        return index - self.lsb if self.msb >= self.lsb else self.lsb - index

    def index_in_range(self, index):
        low, high = min(self.msb, self.lsb), max(self.msb, self.lsb)
        return low <= index <= high


class Driver:
    """Something that computes one or more signal bits."""

    __slots__ = ("kind", "node", "scope", "state")

    def __init__(self, kind, node, scope):
        self.kind = kind          # 'assign' | 'gate' | 'inst' | 'always'
        self.node = node
        self.scope = scope
        self.state = "idle"       # idle | running | done


class Scope:
    """One elaborated module instance."""

    def __init__(self, module_name, path, elaborator):
        self.module_name = module_name
        self.path = path
        self.elaborator = elaborator
        self.signals = {}
        self.params = {}
        self.bit_driver = {}      # (signal_name, pos) -> Driver
        self.drivers = []
        self.port_order = []

    def signal(self, name):
        return self.signals.get(name)


class Elaborator:
    def __init__(self, modules, warn=None):
        self.modules = {m.name: m for m in modules}
        self.aig = AIG()
        self.warnings = []
        self._warn_sink = warn
        # Every scope ever built, including instantiated children. Retained so
        # that an AIG literal can be traced back to the signal that produced
        # it - which is what lets fault localisation report a hierarchical
        # Verilog name instead of an anonymous node id.
        self.scopes = []

    def warn(self, message):
        self.warnings.append(message)
        if self._warn_sink:
            self._warn_sink(message)

    # ------------------------------------------------------------------ entry

    def elaborate_top(self, top_name, input_lits=None, param_overrides=None):
        """Elaborate `top_name` as the design root.

        `input_lits` optionally supplies pre-made AIG literals per input port
        name, which is how a miter feeds one shared set of primary inputs to
        both designs under comparison.
        """
        if top_name not in self.modules:
            raise ElaborationError("no module named %r (have: %s)"
                                   % (top_name, ", ".join(sorted(self.modules))))

        module = self.modules[top_name]
        scope = self._build_scope(module, "", param_overrides or {})

        # Primary inputs
        for name, signal in scope.signals.items():
            if signal.direction == "input":
                supplied = (input_lits or {}).get(name)
                for pos in range(signal.width):
                    if supplied is not None:
                        signal.bits[pos] = supplied[pos]
                    else:
                        signal.bits[pos] = self.aig.new_input("%s[%d]" % (name, pos))

        outputs = []
        for name in scope.port_order:
            signal = scope.signals.get(name)
            if signal is not None and signal.direction == "output":
                bits = [self.resolve_bit(scope, name, pos) for pos in range(signal.width)]
                outputs.append((name, signal, bits))
        return scope, outputs

    # -------------------------------------------------------- scope construction

    def _build_scope(self, module, path, param_overrides):
        scope = Scope(module.name, path, self)
        self.scopes.append(scope)

        # 1. parameters (declaration order; later ones may use earlier ones)
        items = self._expand_items(module.items, scope, param_overrides,
                                   collect_params=True)

        # 2. declarations
        for item in items:
            if isinstance(item, vast.Decl):
                self._declare(scope, item)

        # Ports named in a non-ANSI header but declared later are already
        # handled, because declarations were processed above.
        scope.port_order = list(module.ports)
        for name in module.ports:
            if name not in scope.signals:
                self.warn("port %r of module %r has no declaration; assuming 1-bit wire"
                          % (name, module.name))
                scope.signals[name] = Signal(name, 0, 0, None, False, False)

        # 3. drivers
        for item in items:
            self._register_driver(scope, item)

        return scope

    def _declare(self, scope, decl):
        msb = self.const_eval(decl.msb, scope) if decl.msb is not None else 0
        lsb = self.const_eval(decl.lsb, scope) if decl.lsb is not None else 0
        for name in decl.names:
            existing = scope.signals.get(name)
            if existing is not None:
                # e.g. `output x;` followed by `reg x;` - merge the attributes
                if decl.direction:
                    existing.direction = decl.direction
                if decl.kind == "reg":
                    existing.is_reg = True
                if decl.msb is not None and existing.width == 1:
                    merged = Signal(name, msb, lsb, existing.direction,
                                    existing.is_reg, decl.signed or existing.signed)
                    scope.signals[name] = merged
                continue
            scope.signals[name] = Signal(name, msb, lsb, decl.direction,
                                         decl.kind == "reg", bool(decl.signed))

    def _expand_items(self, items, scope, param_overrides, collect_params=False):
        """Flatten generate constructs and evaluate parameters."""
        out = []
        for item in items:
            if isinstance(item, vast.ParamDecl):
                if item.name in param_overrides and not item.local:
                    scope.params[item.name] = param_overrides[item.name]
                else:
                    scope.params[item.name] = self.const_eval(item.expr, scope)
                continue
            if isinstance(item, vast.GenvarDecl):
                continue
            if isinstance(item, vast.GenerateBlock):
                out.extend(self._expand_items(item.items, scope, param_overrides))
                continue
            if isinstance(item, vast.For):
                out.extend(self._unroll_generate_for(item, scope, param_overrides))
                continue
            if isinstance(item, vast.If) and not isinstance(item, vast.Always):
                # generate-if: pick the taken branch at elaboration time
                taken = item.then_body if self.const_eval(item.cond, scope) else item.else_body
                if taken is not None:
                    out.extend(self._expand_items([taken], scope, param_overrides))
                continue
            out.append(item)

        # Any parameter override naming a parameter the module does not declare
        # is almost certainly a typo, so surface it.
        for key in param_overrides:
            if key not in scope.params:
                self.warn("parameter override %r does not exist in module %r"
                          % (key, scope.module_name))
        return out

    def _unroll_generate_for(self, loop, scope, param_overrides):
        out = []
        index = self.const_eval(loop.start, scope)
        guard = 0
        while True:
            scope.params[loop.var] = index
            if not self.const_eval(loop.cond, scope):
                break
            guard += 1
            if guard > 100000:
                raise ElaborationError("generate-for loop did not terminate")

            local_names = set()
            for item in loop.body.items:
                if isinstance(item, vast.Decl):
                    local_names.update(item.names)
            body = _substitute(copy.deepcopy(loop.body), {loop.var: index},
                               local_names, "$%d" % index)
            out.extend(self._expand_items(body.items, scope, {}))

            index = self.const_eval(loop.step, scope)
        scope.params.pop(loop.var, None)
        return out

    def _register_driver(self, scope, item):
        if isinstance(item, vast.Decl):
            return
        if isinstance(item, vast.Assign):
            driver = Driver("assign", item, scope)
            self._claim_targets(scope, item.target, driver)
        elif isinstance(item, vast.GateInst):
            driver = Driver("gate", item, scope)
            self._claim_targets(scope, item.terminals[0], driver)
        elif isinstance(item, vast.ModuleInst):
            driver = Driver("inst", item, scope)
            child = self.modules.get(item.module_name)
            if child is None:
                raise ElaborationError("instantiated module %r is not defined"
                                       % item.module_name)
            child_ports = self._port_directions(child)
            for pname, expr in self._named_connections(item, child):
                if child_ports.get(pname) == "output" and expr is not None:
                    self._claim_targets(scope, expr, driver)
        elif isinstance(item, vast.Always):
            driver = Driver("always", item, scope)
            for name, positions in _always_targets(item.body, scope, self).items():
                for pos in positions:
                    scope.bit_driver[(name, pos)] = driver
        else:
            return
        scope.drivers.append(driver)

    def _claim_targets(self, scope, target, driver):
        for name, pos in self._target_bits(scope, target):
            scope.bit_driver[(name, pos)] = driver

    def _target_bits(self, scope, target):
        """Flatten an assignment target into (signal_name, position) pairs,
        least significant first."""
        if isinstance(target, vast.Ident):
            signal = scope.signal(target.name)
            if signal is None:
                raise ElaborationError("assignment to undeclared signal %r" % target.name)
            return [(target.name, pos) for pos in range(signal.width)]
        if isinstance(target, vast.BitSelect):
            signal = scope.signal(target.name)
            if signal is None:
                raise ElaborationError("assignment to undeclared signal %r" % target.name)
            index = self.const_eval(target.index, scope)
            return [(target.name, signal.pos_of_index(index))]
        if isinstance(target, vast.PartSelect):
            signal = scope.signal(target.name)
            if signal is None:
                raise ElaborationError("assignment to undeclared signal %r" % target.name)
            msb = self.const_eval(target.msb, scope)
            lsb = self.const_eval(target.lsb, scope)
            low, high = min(msb, lsb), max(msb, lsb)
            return [(target.name, signal.pos_of_index(i)) for i in range(low, high + 1)]
        if isinstance(target, vast.Concat):
            # Concatenation is written MSB-first; flatten to LSB-first order.
            bits = []
            for part in reversed(target.parts):
                bits.extend(self._target_bits(scope, part))
            return bits
        raise ElaborationError("unsupported assignment target: %r" % (target,))

    def _port_directions(self, module):
        directions = {}
        for item in module.items:
            if isinstance(item, vast.Decl) and item.direction:
                for name in item.names:
                    directions[name] = item.direction
        return directions

    def _named_connections(self, inst, child):
        connections = inst.connections
        if connections and connections[0][0] is None:
            # positional
            return list(zip(child.ports, [expr for _, expr in connections]))
        return connections

    # ------------------------------------------------------- constant evaluation

    def const_eval(self, expr, scope):
        """Evaluate a constant expression to a Python int."""
        if expr is None:
            return 0
        if isinstance(expr, vast.Const):
            return expr.number.value
        if isinstance(expr, vast.Ident):
            if expr.name in scope.params:
                return scope.params[expr.name]
            raise ElaborationError("%r is not a constant in this context" % expr.name)
        if isinstance(expr, vast.Unary):
            value = self.const_eval(expr.operand, scope)
            return {"-": lambda v: -v, "+": lambda v: v,
                    "!": lambda v: int(not v), "~": lambda v: ~v}[expr.op](value)
        if isinstance(expr, vast.Binary):
            left = self.const_eval(expr.left, scope)
            right = self.const_eval(expr.right, scope)
            ops = {
                "+": lambda: left + right, "-": lambda: left - right,
                "*": lambda: left * right,
                "/": lambda: left // right if right else 0,
                "%": lambda: left % right if right else 0,
                "<<": lambda: left << right, ">>": lambda: left >> right,
                "<<<": lambda: left << right, ">>>": lambda: left >> right,
                "<": lambda: int(left < right), ">": lambda: int(left > right),
                "<=": lambda: int(left <= right), ">=": lambda: int(left >= right),
                "==": lambda: int(left == right), "!=": lambda: int(left != right),
                "===": lambda: int(left == right), "!==": lambda: int(left != right),
                "&": lambda: left & right, "|": lambda: left | right,
                "^": lambda: left ^ right,
                "&&": lambda: int(bool(left) and bool(right)),
                "||": lambda: int(bool(left) or bool(right)),
            }
            if expr.op not in ops:
                raise ElaborationError("operator %r not allowed in a constant expression"
                                       % expr.op)
            return ops[expr.op]()
        if isinstance(expr, vast.Ternary):
            return (self.const_eval(expr.then_expr, scope)
                    if self.const_eval(expr.cond, scope)
                    else self.const_eval(expr.else_expr, scope))
        raise ElaborationError("expression is not constant: %r" % (expr,))

    # ---------------------------------------------------------------- resolution

    def resolve_bit(self, scope, name, pos):
        signal = scope.signal(name)
        if signal is None:
            raise ElaborationError("reference to undeclared signal %r" % name)
        if pos < 0 or pos >= signal.width:
            return FALSE
        if signal.bits[pos] is not None:
            return signal.bits[pos]

        driver = scope.bit_driver.get((name, pos))
        if driver is None:
            self.warn("%s%s[%d] has no driver; tied to 0"
                      % (scope.path, name, pos))
            signal.bits[pos] = FALSE
            return FALSE

        self._run_driver(driver)

        if signal.bits[pos] is None:
            signal.bits[pos] = FALSE
        return signal.bits[pos]

    def _run_driver(self, driver):
        if driver.state == "done":
            return
        if driver.state == "running":
            raise CombinationalLoop(
                "combinational loop detected while resolving a %s in module %r"
                % (driver.kind, driver.scope.module_name))
        driver.state = "running"
        try:
            if driver.kind == "assign":
                self._run_assign(driver)
            elif driver.kind == "gate":
                self._run_gate(driver)
            elif driver.kind == "inst":
                self._run_instance(driver)
            elif driver.kind == "always":
                self._run_always(driver)
        finally:
            driver.state = "done"

    def _write_target(self, scope, target, bits):
        targets = self._target_bits(scope, target)
        for i, (name, pos) in enumerate(targets):
            signal = scope.signal(name)
            if signal.bits[pos] is None:
                signal.bits[pos] = bits[i] if i < len(bits) else FALSE

    def _run_assign(self, driver):
        scope, node = driver.scope, driver.node
        width = len(self._target_bits(scope, node.target))
        bits = self.eval_expr(node.expr, scope, width)
        self._write_target(scope, node.target, _fit(bits, width))

    def _run_gate(self, driver):
        scope, node = driver.scope, driver.node
        gate = node.gate
        terminals = node.terminals
        if gate in ("buf", "not"):
            # buf/not may have multiple outputs and exactly one final input
            inputs = self.eval_expr(terminals[-1], scope, 1)
            value = inputs[0] if inputs else FALSE
            if gate == "not":
                value = neg(value)
            for target in terminals[:-1]:
                self._write_target(scope, target, [value])
            return

        operand_bits = [self.eval_expr(t, scope, 1)[0] for t in terminals[1:]]
        if gate == "and":
            value = self.aig.mk_and_list(operand_bits)
        elif gate == "nand":
            value = neg(self.aig.mk_and_list(operand_bits))
        elif gate == "or":
            value = self.aig.mk_or_list(operand_bits)
        elif gate == "nor":
            value = neg(self.aig.mk_or_list(operand_bits))
        elif gate == "xor":
            value = self.aig.mk_xor_list(operand_bits)
        elif gate == "xnor":
            value = neg(self.aig.mk_xor_list(operand_bits))
        else:
            raise ElaborationError("unsupported gate primitive %r" % gate)
        self._write_target(scope, terminals[0], [value])

    def _run_instance(self, driver):
        scope, node = driver.scope, driver.node
        child_module = self.modules[node.module_name]

        overrides = {}
        for i, (pname, expr) in enumerate(node.params):
            if pname is None:
                declared = [p.name for p in child_module.params]
                if i < len(declared):
                    overrides[declared[i]] = self.const_eval(expr, scope)
            else:
                overrides[pname] = self.const_eval(expr, scope)

        path = "%s%s." % (scope.path, node.inst_name)
        child_scope = self._build_scope(child_module, path, overrides)
        directions = self._port_directions(child_module)
        connections = self._named_connections(node, child_module)

        # Bind inputs: evaluate in the parent, write into the child.
        for pname, expr in connections:
            if directions.get(pname) != "input" or expr is None:
                continue
            child_signal = child_scope.signal(pname)
            if child_signal is None:
                self.warn("module %r has no port %r" % (node.module_name, pname))
                continue
            bits = _fit(self.eval_expr(expr, scope, child_signal.width),
                        child_signal.width)
            for pos in range(child_signal.width):
                child_signal.bits[pos] = bits[pos]

        # Pull outputs: resolve inside the child, write into the parent.
        for pname, expr in connections:
            if directions.get(pname) != "output" or expr is None:
                continue
            child_signal = child_scope.signal(pname)
            if child_signal is None:
                self.warn("module %r has no port %r" % (node.module_name, pname))
                continue
            bits = [self.resolve_bit(child_scope, pname, pos)
                    for pos in range(child_signal.width)]
            target_width = len(self._target_bits(scope, expr))
            self._write_target(scope, expr, _fit(bits, target_width))

    def _run_always(self, driver):
        scope, node = driver.scope, driver.node
        env = Env(scope, self)
        exec_statement(node.body, env, scope, self)

        for name, (bits, assigned) in env.values.items():
            signal = scope.signal(name)
            if signal is None:
                continue
            if not all(assigned):
                self.warn("%s%s is not assigned on every path through the always "
                          "block; unassigned bits default to 0 (a real synthesiser "
                          "would infer a latch here)" % (scope.path, name))
            for pos in range(signal.width):
                if signal.bits[pos] is None:
                    signal.bits[pos] = bits[pos]

    # --------------------------------------------------------- expression width

    def expr_width(self, expr, scope):
        if isinstance(expr, vast.Const):
            number = expr.number
            if number.width:
                return number.width
            return max(number.value.bit_length(), 1) if number.value >= 0 else 32
        if isinstance(expr, vast.Ident):
            if expr.name in scope.params:
                value = scope.params[expr.name]
                return max(int(value).bit_length(), 1)
            signal = scope.signal(expr.name)
            if signal is None:
                raise ElaborationError("reference to undeclared signal %r" % expr.name)
            return signal.width
        if isinstance(expr, vast.BitSelect):
            return 1
        if isinstance(expr, vast.PartSelect):
            return abs(self.const_eval(expr.msb, scope)
                       - self.const_eval(expr.lsb, scope)) + 1
        if isinstance(expr, vast.Concat):
            return sum(self.expr_width(p, scope) for p in expr.parts)
        if isinstance(expr, vast.Replicate):
            return (self.const_eval(expr.count, scope)
                    * self.expr_width(expr.value, scope))
        if isinstance(expr, vast.Unary):
            if expr.op in ("!", "&", "|", "^", "~&", "~|", "~^", "^~"):
                return 1
            return self.expr_width(expr.operand, scope)
        if isinstance(expr, vast.Binary):
            op = expr.op
            if op in ("==", "!=", "===", "!==", "<", ">", "<=", ">=", "&&", "||"):
                return 1
            if op in ("<<", ">>", "<<<", ">>>"):
                return self.expr_width(expr.left, scope)
            return max(self.expr_width(expr.left, scope),
                       self.expr_width(expr.right, scope))
        if isinstance(expr, vast.Ternary):
            return max(self.expr_width(expr.then_expr, scope),
                       self.expr_width(expr.else_expr, scope))
        raise ElaborationError("cannot determine width of %r" % (expr,))

    # ---------------------------------------------------------- expression eval

    def eval_expr(self, expr, scope, width=None, env=None):
        """Evaluate `expr` to a list of AIG literals, LSB first."""
        natural = self.expr_width(expr, scope)
        target = max(natural, width or 1)
        bits = self._eval(expr, scope, target, env)
        return _fit(bits, width if width is not None else natural)

    def _eval(self, expr, scope, width, env):
        aig = self.aig

        if isinstance(expr, vast.Const):
            value = expr.number.value
            return [TRUE if (value >> i) & 1 else FALSE for i in range(width)]

        if isinstance(expr, vast.Ident):
            if expr.name in scope.params:
                value = scope.params[expr.name]
                return [TRUE if (value >> i) & 1 else FALSE for i in range(width)]
            signal = scope.signal(expr.name)
            if signal is None:
                raise ElaborationError("reference to undeclared signal %r" % expr.name)
            bits = [self._read_bit(scope, expr.name, pos, env)
                    for pos in range(signal.width)]
            return _fit(bits, width, sign=signal.signed)

        if isinstance(expr, vast.BitSelect):
            signal = scope.signal(expr.name)
            if signal is None:
                raise ElaborationError("reference to undeclared signal %r" % expr.name)
            try:
                index = self.const_eval(expr.index, scope)
            except ElaborationError:
                # Variable index: build a multiplexer over every bit.
                selector = self.eval_expr(expr.index, scope, env=env)
                source = [self._read_bit(scope, expr.name, pos, env)
                          for pos in range(signal.width)]
                return _fit([_mux_tree(aig, selector, source)], width)
            if not signal.index_in_range(index):
                self.warn("index %d is outside the range of %r; reading 0"
                          % (index, expr.name))
                return _fit([FALSE], width)
            return _fit([self._read_bit(scope, expr.name,
                                        signal.pos_of_index(index), env)], width)

        if isinstance(expr, vast.PartSelect):
            signal = scope.signal(expr.name)
            msb = self.const_eval(expr.msb, scope)
            lsb = self.const_eval(expr.lsb, scope)
            low, high = min(msb, lsb), max(msb, lsb)
            bits = []
            for index in range(low, high + 1):
                if signal.index_in_range(index):
                    bits.append(self._read_bit(scope, expr.name,
                                               signal.pos_of_index(index), env))
                else:
                    bits.append(FALSE)
            return _fit(bits, width)

        if isinstance(expr, vast.Concat):
            bits = []
            for part in reversed(expr.parts):          # written MSB-first
                part_width = self.expr_width(part, scope)
                bits.extend(_fit(self._eval(part, scope, part_width, env), part_width))
            return _fit(bits, width)

        if isinstance(expr, vast.Replicate):
            count = self.const_eval(expr.count, scope)
            inner_width = self.expr_width(expr.value, scope)
            inner = _fit(self._eval(expr.value, scope, inner_width, env), inner_width)
            return _fit(inner * count, width)

        if isinstance(expr, vast.Unary):
            return self._eval_unary(expr, scope, width, env)

        if isinstance(expr, vast.Binary):
            return self._eval_binary(expr, scope, width, env)

        if isinstance(expr, vast.Ternary):
            cond = _reduce_or(aig, self._eval_self(expr.cond, scope, env))
            then_bits = _fit(self._eval(expr.then_expr, scope, width, env), width)
            else_bits = _fit(self._eval(expr.else_expr, scope, width, env), width)
            return [aig.mk_mux(cond, t, e) for t, e in zip(then_bits, else_bits)]

        raise ElaborationError("unsupported expression %r" % (expr,))

    def _eval_self(self, expr, scope, env):
        """Evaluate at the expression's own natural width."""
        natural = self.expr_width(expr, scope)
        return _fit(self._eval(expr, scope, natural, env), natural)

    def _read_bit(self, scope, name, pos, env):
        # Inside an always block, a signal already assigned earlier in the block
        # must read back the *new* value (blocking-assignment semantics).
        if env is not None and name in env.values:
            return env.values[name][0][pos]
        return self.resolve_bit(scope, name, pos)

    def _eval_unary(self, expr, scope, width, env):
        aig = self.aig
        op = expr.op
        operand = self._eval_self(expr.operand, scope, env)

        if op == "~":
            return _fit([neg(b) for b in operand], width)
        if op == "+":
            return _fit(operand, width)
        if op == "-":
            negated = _negate(aig, operand)
            return _fit(negated, width)
        if op == "!":
            return _fit([neg(_reduce_or(aig, operand))], width)
        if op == "&":
            return _fit([aig.mk_and_list(operand)], width)
        if op == "~&":
            return _fit([neg(aig.mk_and_list(operand))], width)
        if op == "|":
            return _fit([_reduce_or(aig, operand)], width)
        if op == "~|":
            return _fit([neg(_reduce_or(aig, operand))], width)
        if op == "^":
            return _fit([aig.mk_xor_list(operand)], width)
        if op in ("~^", "^~"):
            return _fit([neg(aig.mk_xor_list(operand))], width)
        raise ElaborationError("unsupported unary operator %r" % op)

    def _eval_binary(self, expr, scope, width, env):
        aig = self.aig
        op = expr.op

        # Logical connectives reduce both sides to one bit.
        if op in ("&&", "||"):
            left = _reduce_or(aig, self._eval_self(expr.left, scope, env))
            right = _reduce_or(aig, self._eval_self(expr.right, scope, env))
            value = aig.mk_and(left, right) if op == "&&" else aig.mk_or(left, right)
            return _fit([value], width)

        # Comparisons: operands are sized against each other, result is 1 bit.
        if op in ("==", "!=", "===", "!==", "<", ">", "<=", ">="):
            left_raw = self._eval_self(expr.left, scope, env)
            right_raw = self._eval_self(expr.right, scope, env)
            common = max(len(left_raw), len(right_raw))
            left = _fit(left_raw, common)
            right = _fit(right_raw, common)
            if op in ("==", "==="):
                value = aig.mk_and_list([aig.mk_xnor(a, b)
                                         for a, b in zip(left, right)])
            elif op in ("!=", "!=="):
                value = neg(aig.mk_and_list([aig.mk_xnor(a, b)
                                             for a, b in zip(left, right)]))
            elif op == "<":
                value = _less_than(aig, left, right)
            elif op == ">":
                value = _less_than(aig, right, left)
            elif op == "<=":
                value = neg(_less_than(aig, right, left))
            else:                                       # >=
                value = neg(_less_than(aig, left, right))
            return _fit([value], width)

        # Shifts: the result keeps the left operand's width.
        if op in ("<<", ">>", "<<<", ">>>"):
            left = _fit(self._eval(expr.left, scope, width, env), width)
            try:
                amount = self.const_eval(expr.right, scope)
                return _shift_const(left, amount, right=op in (">>", ">>>"))
            except ElaborationError:
                shift_bits = self._eval_self(expr.right, scope, env)
                return _barrel_shift(aig, left, shift_bits, right=op in (">>", ">>>"))

        # Everything else is width-matched arithmetic / bitwise.
        left = _fit(self._eval(expr.left, scope, width, env), width)
        right = _fit(self._eval(expr.right, scope, width, env), width)

        if op == "&":
            return [aig.mk_and(a, b) for a, b in zip(left, right)]
        if op == "|":
            return [aig.mk_or(a, b) for a, b in zip(left, right)]
        if op == "^":
            return [aig.mk_xor(a, b) for a, b in zip(left, right)]
        if op in ("~^", "^~"):
            return [aig.mk_xnor(a, b) for a, b in zip(left, right)]
        if op == "+":
            return _adder(aig, left, right, FALSE)[0]
        if op == "-":
            return _adder(aig, left, [neg(b) for b in right], TRUE)[0]
        if op == "*":
            return _multiplier(aig, left, right, width)
        if op in ("/", "%"):
            raise ElaborationError(
                "division and modulo are not supported; rewrite the design using "
                "shifts or an explicit divider circuit")
        raise ElaborationError("unsupported binary operator %r" % op)


def signal_names_by_node(scopes):
    """Map AIG node id -> a readable hierarchical signal name.

    A node is often driven by several equivalent names (structural hashing
    merges them); the shortest, shallowest one is kept as the label because it
    is the one a designer is most likely to recognise.
    """
    names = {}
    for scope in scopes:
        for signal in scope.signals.values():
            for pos, lit in enumerate(signal.bits):
                if lit is None or lit <= 1:
                    continue
                node = lit >> 1
                label = "%s%s" % (scope.path, signal.name)
                if signal.width > 1:
                    label += "[%d]" % (signal.lsb + pos
                                       if signal.msb >= signal.lsb
                                       else signal.lsb - pos)
                existing = names.get(node)
                if existing is None or (len(label), label) < (len(existing), existing):
                    names[node] = label
    return names


# ---------------------------------------------------------------------------
# always-block execution
# ---------------------------------------------------------------------------

class Env:
    """Signal values inside an always block, plus which bits were assigned."""

    def __init__(self, scope, elaborator):
        self.values = {}                # name -> (bits, assigned_flags)
        self.scope = scope
        self.elaborator = elaborator

    def ensure(self, name):
        if name in self.values:
            return self.values[name]
        signal = self.scope.signal(name)
        if signal is None:
            raise ElaborationError("assignment to undeclared signal %r" % name)
        entry = ([FALSE] * signal.width, [False] * signal.width)
        self.values[name] = entry
        return entry

    def clone(self):
        copy_env = Env(self.scope, self.elaborator)
        copy_env.values = {k: (list(v[0]), list(v[1])) for k, v in self.values.items()}
        return copy_env


def exec_statement(stmt, env, scope, elaborator):
    if stmt is None:
        return

    if isinstance(stmt, vast.Block):
        for inner in stmt.statements:
            exec_statement(inner, env, scope, elaborator)
        return

    if isinstance(stmt, vast.BlockingAssign):
        targets = elaborator._target_bits(scope, stmt.target)
        bits = _fit(elaborator.eval_expr(stmt.expr, scope, len(targets), env=env),
                    len(targets))
        for i, (name, pos) in enumerate(targets):
            values, assigned = env.ensure(name)
            values[pos] = bits[i]
            assigned[pos] = True
        return

    if isinstance(stmt, vast.If):
        aig = elaborator.aig
        cond = _reduce_or(aig, elaborator._eval_self(stmt.cond, scope, env))
        then_env = env.clone()
        exec_statement(stmt.then_body, then_env, scope, elaborator)
        else_env = env.clone()
        exec_statement(stmt.else_body, else_env, scope, elaborator)
        _merge(env, cond, then_env, else_env, aig)
        return

    if isinstance(stmt, vast.Case):
        aig = elaborator.aig
        selector = elaborator._eval_self(stmt.expr, scope, env)

        # Start from the default branch, then fold the items in reverse so the
        # first matching item wins - Verilog case semantics.
        result_env = env.clone()
        exec_statement(stmt.default, result_env, scope, elaborator)

        wildcards_allowed = stmt.kind in ("casez", "casex")

        for labels, body in reversed(stmt.items):
            match_terms = []
            for label in labels:
                label_bits = elaborator._eval_self(label, scope, env)
                common = max(len(selector), len(label_bits))
                left = _fit(selector, common)
                right = _fit(label_bits, common)

                # In a casez/casex label, bits written as ?/z/x are don't-cares
                # and must be dropped from the comparison entirely.
                care = _label_care_mask(label, common) if wildcards_allowed else None
                comparisons = [aig.mk_xnor(a, b)
                               for index, (a, b) in enumerate(zip(left, right))
                               if care is None or (care >> index) & 1]
                match_terms.append(aig.mk_and_list(comparisons))
            matched = aig.mk_or_list(match_terms)

            branch_env = env.clone()
            exec_statement(body, branch_env, scope, elaborator)

            merged = env.clone()
            _merge(merged, matched, branch_env, result_env, aig)
            result_env = merged

        env.values = result_env.values
        return

    if isinstance(stmt, vast.For):
        index = elaborator.const_eval(stmt.start, scope)
        saved = scope.params.get(stmt.var)
        guard = 0
        while True:
            scope.params[stmt.var] = index
            if not elaborator.const_eval(stmt.cond, scope):
                break
            guard += 1
            if guard > 100000:
                raise ElaborationError("for loop in always block did not terminate")
            exec_statement(stmt.body, env, scope, elaborator)
            index = elaborator.const_eval(stmt.step, scope)
        if saved is None:
            scope.params.pop(stmt.var, None)
        else:
            scope.params[stmt.var] = saved
        return

    raise ElaborationError("unsupported statement %r" % (stmt,))


def _merge(env, cond, then_env, else_env, aig):
    """env := cond ? then_env : else_env"""
    names = set(then_env.values) | set(else_env.values)
    for name in names:
        signal = env.scope.signal(name)
        width = signal.width
        then_bits, then_assigned = then_env.values.get(
            name, ([FALSE] * width, [False] * width))
        else_bits, else_assigned = else_env.values.get(
            name, ([FALSE] * width, [False] * width))
        merged_bits = [aig.mk_mux(cond, t, e) for t, e in zip(then_bits, else_bits)]
        # A bit counts as assigned only if both arms assign it - that is exactly
        # the condition under which a synthesiser would not infer a latch.
        merged_assigned = [t and e for t, e in zip(then_assigned, else_assigned)]
        env.values[name] = (merged_bits, merged_assigned)


def _always_targets(stmt, scope, elaborator):
    """Every (signal, positions) an always block can assign."""
    targets = {}

    def walk(node):
        if node is None:
            return
        if isinstance(node, vast.Block):
            for inner in node.statements:
                walk(inner)
        elif isinstance(node, vast.BlockingAssign):
            for name, pos in elaborator._target_bits(scope, node.target):
                targets.setdefault(name, set()).add(pos)
        elif isinstance(node, vast.If):
            walk(node.then_body)
            walk(node.else_body)
        elif isinstance(node, vast.Case):
            for _, body in node.items:
                walk(body)
            walk(node.default)
        elif isinstance(node, vast.For):
            walk(node.body)

    walk(stmt)
    return targets


# ---------------------------------------------------------------------------
# bit-vector helpers - all operate on lists of AIG literals, LSB first
# ---------------------------------------------------------------------------

def _label_care_mask(label, width):
    """Bits of a case label that participate in the comparison.

    Returns a bitmask with 1 for every position that must match. Only literal
    labels can carry wildcards; anything else is compared in full.
    """
    if isinstance(label, vast.Const):
        return (~label.number.xz_mask) & ((1 << width) - 1)
    return (1 << width) - 1


def _fit(bits, width, sign=False):
    """Truncate or extend a bit vector to `width`."""
    bits = list(bits)
    if len(bits) == width:
        return bits
    if len(bits) > width:
        return bits[:width]
    pad = bits[-1] if (sign and bits) else FALSE
    return bits + [pad] * (width - len(bits))


def _reduce_or(aig, bits):
    return aig.mk_or_list(bits)


def _adder(aig, a, b, carry_in):
    """Ripple-carry adder. Returns (sum_bits, carry_out).

    Structural hashing collapses the redundancy, and every other arithmetic
    operator is built on top of this one.
    """
    width = max(len(a), len(b))
    a = _fit(a, width)
    b = _fit(b, width)
    carry = carry_in
    result = []
    for i in range(width):
        axb = aig.mk_xor(a[i], b[i])
        result.append(aig.mk_xor(axb, carry))
        # carry_out = majority(a, b, carry)
        carry = aig.mk_or(aig.mk_and(a[i], b[i]), aig.mk_and(axb, carry))
    return result, carry


def _negate(aig, bits):
    """Two's complement negation."""
    inverted = [neg(b) for b in bits]
    return _adder(aig, inverted, [FALSE] * len(bits), TRUE)[0]


def _less_than(aig, a, b):
    """Unsigned a < b.

    Computed as the borrow out of a - b: subtracting via a + ~b + 1 produces a
    carry-out of 1 exactly when a >= b, so the complement is the answer.
    """
    width = max(len(a), len(b))
    a = _fit(a, width)
    b = _fit(b, width)
    _, carry_out = _adder(aig, a, [neg(x) for x in b], TRUE)
    return neg(carry_out)


def _multiplier(aig, a, b, width):
    """Unsigned shift-and-add multiplier, truncated to `width`."""
    a = _fit(a, width)
    b = _fit(b, width)
    acc = [FALSE] * width
    for i in range(width):
        # partial product = (a << i) masked by b[i]
        partial = [FALSE] * i + [aig.mk_and(a[j], b[i]) for j in range(width - i)]
        acc = _adder(aig, acc, _fit(partial, width), FALSE)[0]
    return acc


def _shift_const(bits, amount, right):
    width = len(bits)
    if amount <= 0:
        return list(bits)
    if amount >= width:
        return [FALSE] * width
    if right:
        return bits[amount:] + [FALSE] * amount
    return [FALSE] * amount + bits[:width - amount]


def _barrel_shift(aig, bits, shift_bits, right):
    """Logarithmic barrel shifter: one mux layer per shift-amount bit."""
    width = len(bits)
    current = list(bits)
    for level, control in enumerate(shift_bits):
        amount = 1 << level
        if amount >= width:
            # Any set bit at or above this weight shifts everything out.
            current = [aig.mk_and(neg(control), b) for b in current]
            continue
        shifted = _shift_const(current, amount, right)
        current = [aig.mk_mux(control, s, c) for s, c in zip(shifted, current)]
    return current


def _mux_tree(aig, selector, source):
    """Select source[i] where i is the value of `selector`."""
    current = list(source)
    for control in selector:
        if len(current) <= 1:
            break
        nxt = []
        for i in range(0, len(current), 2):
            low = current[i]
            high = current[i + 1] if i + 1 < len(current) else FALSE
            nxt.append(aig.mk_mux(control, high, low))
        current = nxt
    return current[0] if current else FALSE


# ---------------------------------------------------------------------------
# generate-loop substitution
# ---------------------------------------------------------------------------

def _substitute(node, genvars, local_names, suffix):
    """Rewrite one unrolled generate iteration.

    Genvar references become constants, and signals declared inside the loop
    body are renamed per iteration so each iteration gets its own wires.

    Written as a returning transform rather than an in-place mutation because
    the AST holds values inside tuples (case items, port connections) that
    cannot be patched in place.
    """
    from .lexer import Number

    def rename(name):
        return name + suffix if name in local_names else name

    def walk(value):
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, tuple):
            return tuple(walk(item) for item in value)
        if not isinstance(value, vast.Node):
            return value

        if isinstance(value, vast.Ident):
            if value.name in genvars:
                return vast.Const(number=Number(genvars[value.name]))
            return vast.Ident(name=rename(value.name))

        rebuilt = type(value)()
        for field in value._fields:
            setattr(rebuilt, field, walk(getattr(value, field, None)))

        # Fields holding bare name strings need the rename applied directly.
        if isinstance(rebuilt, vast.Decl):
            rebuilt.names = [rename(n) for n in value.names]
        elif isinstance(rebuilt, (vast.BitSelect, vast.PartSelect)):
            rebuilt.name = rename(value.name)
        elif isinstance(rebuilt, vast.ModuleInst):
            rebuilt.module_name = value.module_name
            rebuilt.inst_name = value.inst_name + suffix
        elif isinstance(rebuilt, vast.GateInst):
            rebuilt.gate = value.gate
            rebuilt.name = value.name
        elif isinstance(rebuilt, vast.ParamDecl):
            rebuilt.name = value.name
        elif isinstance(rebuilt, vast.For):
            rebuilt.var = value.var
        elif isinstance(rebuilt, vast.Unary):
            rebuilt.op = value.op
        elif isinstance(rebuilt, vast.Binary):
            rebuilt.op = value.op
        elif isinstance(rebuilt, vast.Const):
            rebuilt.number = value.number

        return rebuilt

    return walk(node)
