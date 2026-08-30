"""Algebraic verification of arithmetic circuits, by polynomial reduction.

Multipliers are the case where SAT falls apart. Our own measurements put a
12x12 multiplier miter at over three hours of CDCL search. The literature is
blunt about why: CDCL is simply the wrong tool for arithmetic, and the right
one is computer algebra (Kaufmann, Biere and Kauers; the AMulet line of work).

The idea
--------

Work in the ring Z[x_1, ..., x_n] modulo the Boolean relations x^2 = x. Every
gate becomes a polynomial equation. For an AIG AND node

    v = l1 AND l2        with  L(x) = x  for a positive literal
                               L(x) = 1 - x  for a negated one

the defining relation is simply

    v - L(l1) * L(l2) = 0

and the thing to prove - that the circuit really computes a product - is the
*specification polynomial*

    SPEC  =  sum_i 2^i p_i  -  (sum_i 2^i a_i) * (sum_j 2^j b_j)

The circuit is correct exactly when SPEC reduces to zero modulo the gate
relations. So the whole verification is: substitute every gate variable by its
definition, and see whether everything cancels.

Why this is cheap
-----------------

In general, reducing modulo a set of polynomials needs a Groebner basis, and
computing one is doubly exponential. The saving grace - and the reason this
approach works at all - is that the gate polynomials *already are* a Groebner
basis, provided variables are ordered so that every gate is greater than its
inputs. Reduction then degenerates into plain substitution in reverse
topological order: eliminate the variables nearest the outputs first and walk
back toward the inputs. No Buchberger, no basis computation.

Representation
--------------

Because x^2 = x, a monomial is just a *set* of variables - exponents never
exceed one. A polynomial is therefore a dict

    frozenset(variables)  ->  integer coefficient

and multiplying two monomials is a set union. Substituting v := P in a
polynomial S splits S = v*Q + R and rewrites it as P*Q + R.

Limits
------

This is the textbook core of the method, not AMulet. It has none of the
preprocessing that makes heavily optimised industrial multipliers tractable -
adder detection, variable elimination, XOR rewriting - so a restructured
multiplier can still blow up the intermediate polynomial. A term budget bounds
that rather than letting it exhaust memory.
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
    """Replace `var` by `definition` throughout `poly`.

    Splits poly = var*Q + R and returns definition*Q + R. Because monomials are
    sets, "contains var" is a membership test and removing it is a set
    difference.
    """
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
    """Prove that a design computes the product of two of its input ports.

    This is stronger than an equivalence check: it verifies the circuit against
    the *arithmetic specification* itself, so no reference implementation is
    needed at all.
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
    """Equivalence by way of a shared specification.

    Rather than comparing the two designs against each other, prove each one
    against the arithmetic specification independently. If both compute the
    product of the same two ports, they compute the same function, so they are
    equivalent - and neither proof ever looks at the other design.

    This is how arithmetic circuits are actually verified in practice, and it
    sidesteps the miter entirely: the cost depends on each circuit separately
    rather than on how differently the two are structured.
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
        # One of them is not a multiplier. That does not by itself make the two
        # designs inequivalent - they could be wrong in the same way - so the
        # honest answer is "not established here", and the SAT backend decides.
        verdict = None

    return {
        "method": "algebraic",
        "equivalent": verdict,
        "spec": left,
        "impl": right,
        "both_proved": bool(left.get("proved") and right.get("proved")),
        "time": time.perf_counter() - started,
    }
