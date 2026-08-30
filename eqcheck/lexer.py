"""Tokenizer for the supported Verilog subset."""

import re

KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg",
    "parameter", "localparam", "assign", "always", "begin", "end",
    "if", "else", "case", "casex", "casez", "endcase", "default",
    "for", "generate", "endgenerate", "genvar", "integer", "signed",
    "and", "or", "nand", "nor", "xor", "xnor", "buf", "not",
    "function", "endfunction",
}

# Longest operators first so that e.g. "<<<" wins over "<<" wins over "<".
OPERATORS = [
    "<<<", ">>>", "===", "!==",
    "<<", ">>", "<=", ">=", "==", "!=", "&&", "||", "~&", "~|", "~^", "^~",
    "+", "-", "*", "/", "%", "!", "~", "&", "|", "^", "<", ">", "?", ":",
    "=", "(", ")", "[", "]", "{", "}", ",", ";", ".", "#", "@",
]


class Token:
    __slots__ = ("kind", "value", "line")

    def __init__(self, kind, value, line):
        self.kind = kind        # 'id' | 'kw' | 'num' | 'op' | 'eof'
        self.value = value
        self.line = line

    def __repr__(self):
        return "Token(%s,%r,line=%d)" % (self.kind, self.value, self.line)


class VerilogSyntaxError(Exception):
    pass


# 8'hFF / 4'b10x1 / 'd12 / 16'sh7F  and plain decimals
_SIZED = re.compile(r"(\d+)?\s*'\s*([sS])?([bBoOdDhH])\s*([0-9a-fA-FxXzZ_?]+)")
_PLAIN = re.compile(r"[0-9][0-9_]*")
_IDENT = re.compile(r"[A-Za-z_\\][A-Za-z0-9_$]*")

_BASE_BITS = {"b": 1, "o": 3, "d": 0, "h": 4}


class Number:
    """A Verilog literal.

    `xz_mask` marks bit positions written x, z or ? - don't-cares in a
    casez/casex label. Dropping it turns a wildcard match into an exact one.
    """

    __slots__ = ("width", "value", "signed", "xz_mask")

    def __init__(self, value, width=None, signed=False, xz_mask=0):
        self.value = value
        self.width = width
        self.signed = signed
        self.xz_mask = xz_mask

    def __repr__(self):
        return "Number(%d,width=%s,xz=%d)" % (self.value, self.width, self.xz_mask)


def _parse_sized(match):
    width_text, signed, base_char, digits = match.groups()
    base_char = base_char.lower()
    digits = digits.replace("_", "")
    if digits == "":
        digits = "0"

    base = {"b": 2, "o": 8, "d": 10, "h": 16}[base_char]
    bits_per_digit = _BASE_BITS[base_char]

    # Build the don't-care mask digit by digit, from the least significant end,
    # then substitute 0 for the wildcard digits to get the numeric value.
    xz_mask = 0
    if bits_per_digit:
        for position, digit in enumerate(reversed(digits)):
            if digit in "xXzZ?":
                span = (1 << bits_per_digit) - 1
                xz_mask |= span << (position * bits_per_digit)

    clean = re.sub(r"[xXzZ?]", "0", digits)
    value = int(clean, base)
    width = int(width_text) if width_text else None
    return Number(value, width, signed=bool(signed), xz_mask=xz_mask)


def tokenize(text):
    tokens = []
    i = 0
    line = 1
    length = len(text)

    while i < length:
        char = text[i]

        if char == "\n":
            line += 1
            i += 1
            continue
        if char in " \t\r":
            i += 1
            continue

        # comments
        if text.startswith("//", i):
            end = text.find("\n", i)
            i = length if end == -1 else end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end == -1:
                raise VerilogSyntaxError("unterminated block comment at line %d" % line)
            line += text.count("\n", i, end)
            i = end + 2
            continue

        # `timescale, `define etc. - skipped wholesale
        if char == "`":
            end = text.find("\n", i)
            i = length if end == -1 else end
            continue

        # numbers (sized form must be tried before plain decimal, because the
        # width digits of 8'hFF would otherwise tokenize as a decimal 8)
        match = _SIZED.match(text, i)
        if match:
            tokens.append(Token("num", _parse_sized(match), line))
            i = match.end()
            continue
        if char.isdigit():
            match = _PLAIN.match(text, i)
            tokens.append(Token("num", Number(int(match.group(0).replace("_", ""))), line))
            i = match.end()
            continue

        # identifiers and keywords
        match = _IDENT.match(text, i)
        if match:
            word = match.group(0)
            kind = "kw" if word in KEYWORDS else "id"
            tokens.append(Token(kind, word, line))
            i = match.end()
            continue

        # operators and punctuation
        for op in OPERATORS:
            if text.startswith(op, i):
                tokens.append(Token("op", op, line))
                i += len(op)
                break
        else:
            raise VerilogSyntaxError(
                "unexpected character %r at line %d" % (char, line))

    tokens.append(Token("eof", None, line))
    return tokens
