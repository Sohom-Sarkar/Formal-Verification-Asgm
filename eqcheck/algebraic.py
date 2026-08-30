"""Algebraic verification of arithmetic circuits (the AMulet approach).

Multipliers are where CDCL falls apart: a 12x12 multiplier miter takes over
three hours here. Computer algebra handles them in milliseconds.

Work in Z[x_1..x_n] modulo x^2 = x. An AIG AND node gives the relation

    v - L(l1) * L(l2) = 0        L(x) = x, or 1 - x for a negated literal

and the property to prove is the specification polynomial

    SPEC = sum 2^i p_i - (sum 2^i a_i) * (sum 2^j b_j)

The circuit is correct exactly when SPEC reduces to zero modulo the gate
relations. Reduction normally needs a Groebner basis, but the gate polynomials
already form one when every gate is ordered above its inputs, so it reduces to
substitution in reverse topological order.

Since x^2 = x, a monomial is a set of variables, so a polynomial is a dict from
frozenset to coefficient and monomial multiplication is set union.

This is the core method, not AMulet: no adder detection, variable elimination or
XOR rewriting, so a heavily restructured multiplier can still blow up. A term
budget bounds that instead of exhausting memory.
"""

import time

from .aig import node_of, is_inverted


class PolynomialBlowup(Exception):
    """Raised when an intermediate polynomial exceeds the term budget."""

    def __init__(self, terms, limit):
        super().__init__("intermediate polynomial reached %d terms (limit %d)"
                         % (terms, limit))
        self.terms = terms
        self.limit = limit


ONE = frozenset()


def poly_add(target, source, scale=1):
    """target += scale * source, dropping cancelled terms."""
    for monomial, coeff in source.items():
        total = target.get(monomial, 0) + coeff * scale
        if total:
            target[monomial] = total
        elif monomial in target:
            del target[monomial]
    return target


def poly_mul(left, right):
    """Multiply two polynomials. Monomial product is set union, since x^2 = x."""
    result = {}
    for lm, lc in left.items():
        for rm, rc in right.items():
            monomial = lm | rm
            total = result.get(monomial, 0) + lc * rc
            if total:
                result[monomial] = total
            elif monomial in result:
                del result[monomial]
    return result


def literal_poly(lit):
    """AIG literal as a polynomial: x, or 1 - x when the literal is negated."""
    if lit == 0:
        return {}
    if lit == 1:
        return {ONE: 1}
    var = node_of(lit)
    if is_inverted(lit):
        return {ONE: 1, frozenset((var,)): -1}
    return {frozenset((var,)): 1}


def substitute(poly, var, definition, max_terms):
    """Replace `var` by `definition`: splits poly = var*Q + R, returns definition*Q + R."""
    quotient = {}
    remainder = {}
    for monomial, coeff in poly.items():
        if var in monomial:
            quotient[monomial - {var}] = coeff
        else:
            remainder[monomial] = coeff

    if not quotient:
        return remainder

    product = poly_mul(definition, quotient)
    if len(product) + len(remainder) > max_terms:
        raise PolynomialBlowup(len(product) + len(remainder), max_terms)

    return poly_add(remainder, product)


def multiplier_spec(output_lits, a_lits, b_lits, signed=False):
    """SPEC = sum 2^i p_i  -  (sum 2^i a_i)(sum 2^j b_j)."""
    spec = {}

    for i, lit in enumerate(output_lits):
        weight = 2 ** i
        if signed and i == len(output_lits) - 1:
            weight = -weight
        poly_add(spec, literal_poly(lit), weight)

    a_poly = {}
    for i, lit in enumerate(a_lits):
        weight = 2 ** i
        if signed and i == len(a_lits) - 1:
            weight = -weight
        poly_add(a_poly, literal_poly(lit), weight)

    b_poly = {}
    for j, lit in enumerate(b_lits):
        weight = 2 ** j
        if signed and j == len(b_lits) - 1:
            weight = -weight
        poly_add(b_poly, literal_poly(lit), weight)

    poly_add(spec, poly_mul(a_poly, b_poly), -1)
    return spec


def reduce_through_circuit(aig, spec, roots, max_terms=400_000, trace=None):
    """Eliminate every gate variable from `spec`, outputs first.

    Reverse topological order is what makes the gate polynomials a Groebner
    basis, so this loop is a complete reduction and not a heuristic.
    """
    order = aig.cone(roots)
    poly = dict(spec)
    peak = len(poly)

    for node in reversed(order):
        gate = aig.and_gates.get(node)
        if gate is None:
            continue                        # primary input: stays a variable
        left, right = gate
        definition = poly_mul(literal_poly(left), literal_poly(right))
        poly = substitute(poly, node, definition, max_terms)
        if len(poly) > peak:
            peak = len(poly)
        if trace is not None:
            trace.append((node, len(poly)))

    return poly, peak


def verify_multiplier(path=None, text=None, top=None, params=None,
                      a="a", b="b", p="p", signed=False,
                      max_terms=400_000):
    """Prove a design computes the product of two of its input ports.

    Checks against the arithmetic spec, so no reference design is needed.
    """
    from .localize import DesignInstance

    started = time.perf_counter()
    design = DesignInstance(path=path, text=text, top=top, params=params)

    for port in (a, b):
        if port not in design.inputs:
            raise ValueError("no input port %r (have: %s)"
                             % (port, ", ".join(sorted(design.inputs))))
    outputs = design.output_map
    if p not in outputs:
        raise ValueError("no output port %r (have: %s)"
                         % (p, ", ".join(sorted(outputs))))

    a_lits = design.inputs[a]
    b_lits = design.inputs[b]
    p_lits = outputs[p]

    spec = multiplier_spec(p_lits, a_lits, b_lits, signed=signed)
    spec_terms = len(spec)

    try:
        remainder, peak = reduce_through_circuit(
            design.aig, spec, list(p_lits), max_terms=max_terms)
    except PolynomialBlowup as exc:
        return {
            "proved": None,
            "aborted": True,
            "reason": str(exc),
            "spec_terms": spec_terms,
            "gates": design.aig.num_ands,
            "time": time.perf_counter() - started,
        }

    return {
        "proved": not remainder,
        "aborted": False,
        "remainder_terms": len(remainder),
        "spec_terms": spec_terms,
        "peak_terms": peak,
        "gates": design.aig.num_ands,
        "width": len(a_lits),
        "time": time.perf_counter() - started,
    }


def prove_equivalent_algebraic(spec_path, impl_path, spec_top=None, impl_top=None,
                               params=None, a="a", b="b", p="p", signed=False,
                               max_terms=400_000):
    """Equivalence via the spec: prove each design is a multiplier separately.

    Neither proof looks at the other design, and no miter is built, so cost
    depends on each circuit rather than on how differently they are built.
    """
    started = time.perf_counter()

    left = verify_multiplier(path=spec_path, top=spec_top, params=params,
                             a=a, b=b, p=p, signed=signed, max_terms=max_terms)
    right = verify_multiplier(path=impl_path, top=impl_top, params=params,
                              a=a, b=b, p=p, signed=signed, max_terms=max_terms)

    if left["aborted"] or right["aborted"]:
        verdict = None
    elif left["proved"] and right["proved"]:
        verdict = True
    else:
        # One is not a multiplier. They could still be wrong the same way, so
        # this is "not established", not "inequivalent". Leave it to SAT.
        verdict = None

    return {
        "method": "algebraic",
        "equivalent": verdict,
        "spec": left,
        "impl": right,
        "both_proved": bool(left.get("proved") and right.get("proved")),
        "time": time.perf_counter() - started,
    }
