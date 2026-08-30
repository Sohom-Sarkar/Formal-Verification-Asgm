"""Recursive-descent parser for the supported Verilog subset.

Handles both ANSI and non-ANSI port styles, continuous assignments, gate
primitives, module instantiation with parameter overrides, always @(*) blocks
containing if/else/case/for, and generate-for loops.
"""

from . import vast
from .lexer import tokenize, VerilogSyntaxError

# Binary operator precedence, loosest first. Verilog's real table; ?: is
# handled separately because it is right-associative and ternary.
PRECEDENCE = [
    ("||",),
    ("&&",),
    ("|", "~|"),
    ("^", "~^", "^~"),
    ("&", "~&"),
    ("==", "!=", "===", "!=="),
    ("<", "<=", ">", ">="),
    ("<<", ">>", "<<<", ">>>"),
    ("+", "-"),
    ("*", "/", "%"),
]

UNARY_OPS = {"!", "~", "-", "+", "&", "|", "^", "~&", "~|", "~^", "^~"}

GATE_TYPES = {"and", "or", "nand", "nor", "xor", "xnor", "buf", "not"}


class Parser:
    def __init__(self, text):
        self.tokens = tokenize(text)
        self.pos = 0

    # ------------------------------------------------------------ utilities

    @property
    def current(self):
        return self.tokens[self.pos]

    def peek(self, offset=0):
        index = min(self.pos + offset, len(self.tokens) - 1)
        return self.tokens[index]

    def advance(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def at(self, value, kind=None):
        token = self.current
        if kind and token.kind != kind:
            return False
        return token.value == value

    def at_any(self, values):
        return self.current.value in values and self.current.kind in ("op", "kw")

    def accept(self, value):
        if self.at(value):
            self.pos += 1
            return True
        return False

    def expect(self, value):
        if not self.at(value):
            raise VerilogSyntaxError(
                "line %d: expected %r but found %r"
                % (self.current.line, value, self.current.value))
        return self.advance()

    def expect_id(self):
        token = self.current
        if token.kind not in ("id", "kw"):
            raise VerilogSyntaxError(
                "line %d: expected identifier, found %r" % (token.line, token.value))
        self.pos += 1
        return token.value

    # -------------------------------------------------------------- top level

    def parse(self):
        modules = []
        while self.current.kind != "eof":
            if self.at("module"):
                modules.append(self.parse_module())
            else:
                # Tolerate stray tokens between modules.
                self.advance()
        return modules

    def parse_module(self):
        self.expect("module")
        name = self.expect_id()

        params = []
        if self.accept("#"):
            self.expect("(")
            while not self.at(")"):
                self.accept("parameter")
                pname = self.expect_id()
                self.expect("=")
                params.append(vast.ParamDecl(name=pname, expr=self.parse_expr(),
                                             local=False))
                if not self.accept(","):
                    break
            self.expect(")")

        ports = []
        ansi_decls = []
        if self.accept("("):
            while not self.at(")"):
                if self.current.value in ("input", "output", "inout"):
                    decl = self.parse_port_decl_ansi()
                    ansi_decls.append(decl)
                    ports.extend(decl.names)
                else:
                    ports.append(self.expect_id())
                if not self.accept(","):
                    break
            self.expect(")")
        self.expect(";")

        items = list(params) + list(ansi_decls)
        while not self.at("endmodule"):
            if self.current.kind == "eof":
                raise VerilogSyntaxError("unexpected end of file in module %s" % name)
            item = self.parse_item()
            if item is not None:
                if isinstance(item, list):
                    items.extend(item)
                else:
                    items.append(item)
        self.expect("endmodule")

        return vast.Module(name=name, params=params, ports=ports, items=items)

    def parse_port_decl_ansi(self):
        direction = self.advance().value           # input / output / inout
        kind = None
        if self.current.value in ("wire", "reg"):
            kind = self.advance().value
        signed = self.accept("signed")
        msb = lsb = None
        if self.accept("["):
            msb = self.parse_expr()
            self.expect(":")
            lsb = self.parse_expr()
            self.expect("]")
        names = [self.expect_id()]
        # In an ANSI port list a bare identifier after a comma continues this
        # declaration only if the next token is not a direction keyword.
        while self.at(",") and self.peek(1).kind == "id" and self.peek(2).value in (",", ")"):
            self.advance()
            names.append(self.expect_id())
        return vast.Decl(direction=direction, kind=kind, msb=msb, lsb=lsb,
                         names=names, signed=signed)

    # ------------------------------------------------------------ module items

    def parse_item(self):
        token = self.current

        if token.value in ("input", "output", "inout", "wire", "reg"):
            return self.parse_decl()
        if token.value in ("parameter", "localparam"):
            return self.parse_param_decl()
        if token.value == "genvar":
            self.advance()
            names = [self.expect_id()]
            while self.accept(","):
                names.append(self.expect_id())
            self.expect(";")
            return vast.GenvarDecl(names=names)
        if token.value == "integer":
            self.advance()
            names = [self.expect_id()]
            while self.accept(","):
                names.append(self.expect_id())
            self.expect(";")
            return vast.Decl(direction=None, kind="reg", msb=None, lsb=None,
                             names=names, signed=True)
        if token.value == "assign":
            self.advance()
            statements = []
            while True:
                target = self.parse_expr()
                self.expect("=")
                statements.append(vast.Assign(target=target, expr=self.parse_expr()))
                if not self.accept(","):
                    break
            self.expect(";")
            return statements
        if token.value == "always":
            return self.parse_always()
        if token.value == "generate":
            self.advance()
            items = []
            while not self.at("endgenerate"):
                item = self.parse_item()
                if item is not None:
                    if isinstance(item, list):
                        items.extend(item)
                    else:
                        items.append(item)
            self.expect("endgenerate")
            return vast.GenerateBlock(items=items)
        if token.value == "for":
            return self.parse_generate_for()
        if token.value == "if":
            # generate-if, outside any always block
            return self.parse_generate_if()
        if token.value == "begin":
            # named generate block: begin : label ... end
            self.advance()
            if self.accept(":"):
                self.expect_id()
            items = []
            while not self.at("end"):
                item = self.parse_item()
                if item is not None:
                    if isinstance(item, list):
                        items.extend(item)
                    else:
                        items.append(item)
            self.expect("end")
            return vast.GenerateBlock(items=items)
        if token.value in GATE_TYPES and self.peek(1).value in ("(", "#") or \
                (token.value in GATE_TYPES and self.peek(1).kind == "id"):
            return self.parse_gate_inst()
        if token.kind == "id":
            return self.parse_module_inst()
        if self.accept(";"):
            return None

        raise VerilogSyntaxError(
            "line %d: unexpected %r in module body" % (token.line, token.value))

    def parse_decl(self):
        direction = None
        kind = None
        if self.current.value in ("input", "output", "inout"):
            direction = self.advance().value
        if self.current.value in ("wire", "reg"):
            kind = self.advance().value
        signed = self.accept("signed")
        msb = lsb = None
        if self.accept("["):
            msb = self.parse_expr()
            self.expect(":")
            lsb = self.parse_expr()
            self.expect("]")
        names = [self.expect_id()]
        while self.accept(","):
            names.append(self.expect_id())
        self.expect(";")
        return vast.Decl(direction=direction, kind=kind, msb=msb, lsb=lsb,
                         names=names, signed=signed)

    def parse_param_decl(self):
        local = self.advance().value == "localparam"
        self.accept("signed")
        if self.accept("["):                       # ranged parameter: ignore range
            self.parse_expr()
            self.expect(":")
            self.parse_expr()
            self.expect("]")
        decls = []
        while True:
            name = self.expect_id()
            self.expect("=")
            decls.append(vast.ParamDecl(name=name, expr=self.parse_expr(), local=local))
            if not self.accept(","):
                break
        self.expect(";")
        return decls

    def parse_gate_inst(self):
        gate = self.advance().value
        name = None
        if self.current.kind == "id":
            name = self.advance().value
        self.expect("(")
        terminals = [self.parse_expr()]
        while self.accept(","):
            terminals.append(self.parse_expr())
        self.expect(")")
        self.expect(";")
        return vast.GateInst(gate=gate, name=name, terminals=terminals)

    def parse_module_inst(self):
        module_name = self.expect_id()

        params = []
        if self.accept("#"):
            self.expect("(")
            while not self.at(")"):
                if self.accept("."):
                    pname = self.expect_id()
                    self.expect("(")
                    params.append((pname, self.parse_expr()))
                    self.expect(")")
                else:
                    params.append((None, self.parse_expr()))
                if not self.accept(","):
                    break
            self.expect(")")

        instances = []
        while True:
            inst_name = self.expect_id()
            if self.accept("["):                   # instance array range: ignored
                self.parse_expr()
                self.expect(":")
                self.parse_expr()
                self.expect("]")
            self.expect("(")
            connections = []
            while not self.at(")"):
                if self.accept("."):
                    pname = self.expect_id()
                    self.expect("(")
                    expr = None if self.at(")") else self.parse_expr()
                    self.expect(")")
                    connections.append((pname, expr))
                else:
                    connections.append((None, self.parse_expr()))
                if not self.accept(","):
                    break
            self.expect(")")
            instances.append(vast.ModuleInst(module_name=module_name,
                                             inst_name=inst_name,
                                             params=params,
                                             connections=connections))
            if not self.accept(","):
                break
        self.expect(";")
        return instances

    def parse_generate_for(self):
        self.expect("for")
        self.expect("(")
        var = self.expect_id()
        self.expect("=")
        start = self.parse_expr()
        self.expect(";")
        cond = self.parse_expr()
        self.expect(";")
        step_var = self.expect_id()
        self.expect("=")
        step = self.parse_expr()
        self.expect(")")

        if self.at("begin"):
            self.advance()
            if self.accept(":"):
                self.expect_id()
            items = []
            while not self.at("end"):
                item = self.parse_item()
                if item is not None:
                    if isinstance(item, list):
                        items.extend(item)
                    else:
                        items.append(item)
            self.expect("end")
            body = vast.GenerateBlock(items=items)
        else:
            item = self.parse_item()
            body = vast.GenerateBlock(items=item if isinstance(item, list) else [item])

        return vast.For(var=var, start=start, cond=cond, step=step, body=body)

    def parse_generate_if(self):
        self.expect("if")
        self.expect("(")
        cond = self.parse_expr()
        self.expect(")")
        then_body = self.parse_generate_body()
        else_body = None
        if self.accept("else"):
            else_body = self.parse_generate_body()
        return vast.If(cond=cond, then_body=then_body, else_body=else_body)

    def parse_generate_body(self):
        if self.at("begin"):
            self.advance()
            if self.accept(":"):
                self.expect_id()
            items = []
            while not self.at("end"):
                item = self.parse_item()
                if item is not None:
                    if isinstance(item, list):
                        items.extend(item)
                    else:
                        items.append(item)
            self.expect("end")
            return vast.GenerateBlock(items=items)
        item = self.parse_item()
        return vast.GenerateBlock(items=item if isinstance(item, list) else [item])

    # ------------------------------------------------------------------ always

    def parse_always(self):
        self.expect("always")
        if self.accept("@"):
            # Sensitivity list parsed and thrown away. Combinational-ness is
            # enforced in the elaborator, which reports any signal that
            # re-enters its own resolution as a combinational loop.
            if self.accept("("):
                depth = 1
                while depth:
                    token = self.advance()
                    if token.value == "(":
                        depth += 1
                    elif token.value == ")":
                        depth -= 1
                    elif token.kind == "eof":
                        raise VerilogSyntaxError("unterminated sensitivity list")
            elif self.accept("*"):
                pass
        return vast.Always(body=self.parse_statement())

    def parse_statement(self):
        token = self.current

        if token.value == "begin":
            self.advance()
            if self.accept(":"):
                self.expect_id()
            statements = []
            while not self.at("end"):
                if self.current.kind == "eof":
                    raise VerilogSyntaxError("unterminated begin block")
                statements.append(self.parse_statement())
            self.expect("end")
            return vast.Block(statements=statements)

        if token.value == "if":
            self.advance()
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            then_body = self.parse_statement()
            else_body = None
            if self.accept("else"):
                else_body = self.parse_statement()
            return vast.If(cond=cond, then_body=then_body, else_body=else_body)

        if token.value in ("case", "casex", "casez"):
            case_kind = self.advance().value
            self.expect("(")
            expr = self.parse_expr()
            self.expect(")")
            items = []
            default = None
            while not self.at("endcase"):
                if self.accept("default"):
                    self.accept(":")
                    default = self.parse_statement()
                    continue
                labels = [self.parse_expr()]
                while self.accept(","):
                    labels.append(self.parse_expr())
                self.expect(":")
                items.append((labels, self.parse_statement()))
            self.expect("endcase")
            return vast.Case(expr=expr, items=items, default=default,
                             kind=case_kind)

        if token.value == "for":
            self.advance()
            self.expect("(")
            var = self.expect_id()
            self.expect("=")
            start = self.parse_expr()
            self.expect(";")
            cond = self.parse_expr()
            self.expect(";")
            self.expect_id()
            self.expect("=")
            step = self.parse_expr()
            self.expect(")")
            return vast.For(var=var, start=start, cond=cond, step=step,
                            body=self.parse_statement())

        if self.accept(";"):
            return vast.Block(statements=[])

        # assignment: blocking (=) and non-blocking (<=) are equivalent for a
        # purely combinational block, so both map to the same node.
        target = self.parse_expr()
        if not (self.accept("=") or self.accept("<=")):
            raise VerilogSyntaxError(
                "line %d: expected assignment operator" % self.current.line)
        expr = self.parse_expr()
        self.expect(";")
        return vast.BlockingAssign(target=target, expr=expr)

    # -------------------------------------------------------------- expressions

    def parse_expr(self):
        return self.parse_ternary()

    def parse_ternary(self):
        cond = self.parse_binary(0)
        if self.accept("?"):
            then_expr = self.parse_ternary()
            self.expect(":")
            else_expr = self.parse_ternary()
            return vast.Ternary(cond=cond, then_expr=then_expr, else_expr=else_expr)
        return cond

    def parse_binary(self, level):
        if level >= len(PRECEDENCE):
            return self.parse_unary()
        ops = PRECEDENCE[level]
        left = self.parse_binary(level + 1)
        while self.current.kind == "op" and self.current.value in ops:
            op = self.advance().value
            right = self.parse_binary(level + 1)
            left = vast.Binary(op=op, left=left, right=right)
        return left

    def parse_unary(self):
        token = self.current
        if token.kind == "op" and token.value in UNARY_OPS:
            self.advance()
            return vast.Unary(op=token.value, operand=self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        token = self.current

        if token.kind == "num":
            self.advance()
            return vast.Const(number=token.value)

        if self.accept("("):
            expr = self.parse_expr()
            self.expect(")")
            return expr

        if self.accept("{"):
            # Either a concatenation {a, b} or a replication {N{x}}.
            first = self.parse_expr()
            if self.at("{"):
                self.advance()
                value = self.parse_expr()
                self.expect("}")
                self.expect("}")
                return vast.Replicate(count=first, value=value)
            parts = [first]
            while self.accept(","):
                parts.append(self.parse_expr())
            self.expect("}")
            return vast.Concat(parts=parts)

        if token.kind in ("id", "kw"):
            name = self.expect_id()
            if self.at("["):
                self.advance()
                index = self.parse_expr()
                if self.accept(":"):
                    lsb = self.parse_expr()
                    self.expect("]")
                    return vast.PartSelect(name=name, msb=index, lsb=lsb)
                self.expect("]")
                return vast.BitSelect(name=name, index=index)
            return vast.Ident(name=name)

        raise VerilogSyntaxError(
            "line %d: unexpected token %r in expression" % (token.line, token.value))


def parse_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return Parser(handle.read()).parse()


def parse_text(text):
    return Parser(text).parse()
