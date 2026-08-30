"""AST node definitions for the supported Verilog subset."""


class Node:
    _fields = ()

    def __init__(self, **kwargs):
        for field in self._fields:
            setattr(self, field, kwargs.get(field))

    def __repr__(self):
        inner = ", ".join("%s=%r" % (f, getattr(self, f)) for f in self._fields)
        return "%s(%s)" % (type(self).__name__, inner)


class Module(Node):
    _fields = ("name", "params", "ports", "items")


class Decl(Node):
    """input / output / wire / reg declaration."""
    _fields = ("direction", "kind", "msb", "lsb", "names", "signed")


class ParamDecl(Node):
    _fields = ("name", "expr", "local")


class GenvarDecl(Node):
    _fields = ("names",)


class Assign(Node):
    """Continuous assignment."""
    _fields = ("target", "expr")


class GateInst(Node):
    """Built-in primitive: and/or/nand/nor/xor/xnor/buf/not."""
    _fields = ("gate", "name", "terminals")


class ModuleInst(Node):
    _fields = ("module_name", "inst_name", "params", "connections")


class Always(Node):
    _fields = ("body",)


class GenerateBlock(Node):
    _fields = ("items",)


class Block(Node):
    _fields = ("statements",)


class BlockingAssign(Node):
    _fields = ("target", "expr")


class If(Node):
    _fields = ("cond", "then_body", "else_body")


class Case(Node):
    _fields = ("expr", "items", "default", "kind")


class For(Node):
    _fields = ("var", "start", "cond", "step", "body")


class Const(Node):
    _fields = ("number",)


class Ident(Node):
    _fields = ("name",)


class BitSelect(Node):
    _fields = ("name", "index")


class PartSelect(Node):
    """name[msb:lsb] with constant bounds."""
    _fields = ("name", "msb", "lsb")


class Unary(Node):
    _fields = ("op", "operand")


class Binary(Node):
    _fields = ("op", "left", "right")


class Ternary(Node):
    _fields = ("cond", "then_expr", "else_expr")


class Concat(Node):
    _fields = ("parts",)


class Replicate(Node):
    _fields = ("count", "value")
