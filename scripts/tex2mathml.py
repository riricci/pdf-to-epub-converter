#!/usr/bin/env python3
"""Minimal LaTeX -> MathML (presentation) converter for the subset used in the
lecture notes: fractions, sub/superscripts, \\left...\\right, \\underbrace, \\tilde,
\\text, \\begin{aligned}, common symbols and Greek letters."""
import html
import re

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ϵ", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ", "sigma": "σ",
    "tau": "τ", "upsilon": "υ", "phi": "ϕ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π",
    "Sigma": "Σ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
OPS = {
    "approx": "≈", "simeq": "≃", "sim": "∼", "cong": "≅", "equiv": "≡", "ll": "≪", "gg": "≫",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "neq": "≠", "ne": "≠", "to": "→",
    "rightarrow": "→", "Rightarrow": "⇒", "leftarrow": "←", "Leftrightarrow": "⇔",
    "leftrightarrow": "↔", "uparrow": "↑", "downarrow": "↓",
    "cdot": "⋅", "times": "×", "div": "÷", "pm": "±", "mp": "∓", "infty": "∞",
    "partial": "∂", "nabla": "∇", "propto": "∝", "in": "∈", "cdots": "⋯", "ldots": "…",
    "dots": "…", "oplus": "⊕", "otimes": "⊗", "odot": "⊙", "wedge": "∧", "vee": "∨",
    "neg": "¬", "lnot": "¬", "circ": "∘", "deg": "°", "prime": "′", "sum": "∑", "int": "∫",
    "prod": "∏", "forall": "∀", "exists": "∃", "cap": "∩", "cup": "∪", "bullet": "∙",
    "mid": "|", "parallel": "∥", "perp": "⊥", "angle": "∠", "Re": "ℜ", "Im": "ℑ",
    "hbar": "ℏ", "ell": "ℓ", "lt": "<", "gt": ">",
}
FUNCS = {"sin", "cos", "tan", "exp", "log", "ln", "max", "min", "lim", "sinh", "cosh", "tanh", "arctan", "det"}
SPACES = {",": "0.17em", ":": "0.22em", ";": "0.28em", "quad": "1em", "qquad": "2em", " ": "0.25em", "!": "0em"}
FENCE = {"(": "(", ")": ")", "[": "[", "]": "]", "\\{": "{", "\\}": "}", "|": "|", "\\|": "‖",
         ".": "", "\\langle": "⟨", "\\rangle": "⟩", "\\lfloor": "⌊", "\\rfloor": "⌋"}
ACCENTS = {"tilde": "~", "hat": "^", "bar": "‾", "overline": "‾", "vec": "→", "dot": "˙", "ddot": "¨"}

TOK = re.compile(r"\\[A-Za-z]+|\\.|[0-9]+(?:[.,][0-9]+)*|\s+|.", re.S)


def esc(s):
    return html.escape(s, quote=False)


class P:
    def __init__(self, src):
        self.t = TOK.findall(src.replace("{,}", ","))
        self.i = 0

    def peek(self, skipws=True):
        j = self.i
        while skipws and j < len(self.t) and self.t[j].isspace():
            j += 1
        return self.t[j] if j < len(self.t) else None

    def next(self, skipws=True):
        while skipws and self.i < len(self.t) and self.t[self.i].isspace():
            self.i += 1
        if self.i >= len(self.t):
            return None
        x = self.t[self.i]
        self.i += 1
        return x

    def group_raw(self):
        """raw text of {...}"""
        assert self.next() == "{"
        depth, out = 1, []
        while True:
            x = self.next(False)
            if x is None:
                break
            if x == "{":
                depth += 1
            elif x == "}":
                depth -= 1
                if depth == 0:
                    break
            out.append(x)
        return "".join(out)

    def arg(self):
        x = self.peek()
        if x == "{":
            self.next()
            items = self.seq(stop={"}"})
            self.next()
            return row(items)
        return self.atom()

    def seq(self, stop=()):
        items = []
        while True:
            x = self.peek()
            if x is None or x in stop:
                return items
            if x == "\\right" and "\\right" in stop:
                return items
            a = self.atom()
            if a is None:
                continue
            items.append(self.scripts(a))

    def scripts(self, base):
        sub = sup = None
        while self.peek() in ("_", "^", "'"):
            x = self.next()
            if x == "'":
                p = "<mo>′</mo>"
                sup = p if sup is None else row([sup, p])
            elif x == "_":
                sub = upright_label(self.arg())
            else:
                sup = self.arg()
        if base.startswith("<munder") and (sub is not None):
            # \underbrace{...}_{text}
            return f"<munder>{base}{sub}</munder>"
        if sub is not None and sup is not None:
            return f"<msubsup>{base}{sub}{sup}</msubsup>"
        if sub is not None:
            return f"<msub>{base}{sub}</msub>"
        if sup is not None:
            return f"<msup>{base}{sup}</msup>"
        return base

    def atom(self):
        x = self.next()
        if x is None:
            return None
        if x == "{":
            items = self.seq(stop={"}"})
            self.next()
            return row(items)
        if x.isspace():
            return None
        if re.match(r"[0-9]", x):
            return f"<mn>{esc(x)}</mn>"
        if re.match(r"[A-Za-z]$", x):
            return f"<mi>{x}</mi>"
        if x.startswith("\\"):
            c = x[1:]
            if c in GREEK:
                v = GREEK[c]
                return f'<mi mathvariant="normal">{v}</mi>' if c[0].isupper() else f"<mi>{v}</mi>"
            if c in OPS:
                return f"<mo>{esc(OPS[c])}</mo>"
            if c in FUNCS:
                return f"<mi>{c}</mi>"
            if c in SPACES:
                return f'<mspace width="{SPACES[c]}"/>'
            if c in ("frac", "dfrac", "tfrac"):
                a = self.arg()
                b = self.arg()
                return f"<mfrac>{a}{b}</mfrac>"
            if c == "sqrt":
                return f"<msqrt>{self.arg()}</msqrt>"
            if c in ("text", "mathrm", "textrm", "mbox"):
                raw = self.group_raw()
                if c == "mathrm":
                    return f'<mi mathvariant="normal">{esc(raw)}</mi>'
                # spaces at the edges of \text{} would be dropped: use non-breaking spaces
                raw = re.sub(r"^ +| +$", lambda m: "\u00a0" * len(m.group(0)), raw)
                return f"<mtext>{esc(raw)}</mtext>"
            if c in ("mathcal", "mathscr"):
                raw = self.group_raw()
                return f'<mi mathvariant="script">{esc(raw)}</mi>'
            if c in ("mathbf", "boldsymbol"):
                return f'<mstyle mathvariant="bold">{self.arg()}</mstyle>'.replace("<mi>", '<mi mathvariant="bold-italic">' if c == "boldsymbol" else '<mi mathvariant="bold">')
            if c in ACCENTS:
                a = self.arg()
                return f'<mover accent="true">{a}<mo stretchy="true">{ACCENTS[c]}</mo></mover>'
            if c == "underbrace":
                a = self.arg()
                return f'<munder>{a}<mo stretchy="true">⏟</mo></munder>'
            if c == "left":
                f1 = self.next()
                inner = self.seq(stop={"\\right"})
                self.next()  # \right
                f2 = self.next()
                o = FENCE.get(f1, f1)
                cl = FENCE.get(f2, f2)
                parts = []
                if o:
                    parts.append(f'<mo fence="true" stretchy="true">{esc(o)}</mo>')
                parts += inner
                if cl:
                    parts.append(f'<mo fence="true" stretchy="true">{esc(cl)}</mo>')
                return "<mrow>" + "".join(parts) + "</mrow>"
            if c in ("big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr"):
                f = self.next()
                return f'<mo stretchy="false">{esc(FENCE.get(f, f))}</mo>'
            if c == "begin":
                env = self.group_raw()
                return self.env(env)
            if c in ("{", "}"):
                return f"<mo>{c}</mo>"
            if c == "|":
                return "<mo>‖</mo>"
            if c == "%":
                return "<mo>%</mo>"
            return f"<mi>{esc(c)}</mi>"
        if x in "+-=<>()[]|/!,;:.?*":
            v = {"-": "−", "*": "∗"}.get(x, x)
            attrs = ' stretchy="false"' if x in "()[]|" else ""
            return f"<mo{attrs}>{esc(v)}</mo>"
        if x == "&" or x == "}":
            return None
        return f"<mi>{esc(x)}</mi>" if x.isalpha() else f"<mo>{esc(x)}</mo>"

    def env(self, name):
        rows, cells, cur = [], [], []
        while True:
            x = self.peek()
            if x is None:
                break
            if x == "\\end":
                self.next()
                self.group_raw()
                break
            if x == "&":
                self.next()
                cells.append(row(cur))
                cur = []
                continue
            if x == "\\\\":
                self.next()
                cells.append(row(cur))
                rows.append(cells)
                cells, cur = [], []
                continue
            a = self.atom()
            if a is not None:
                cur.append(self.scripts(a))
        if cur or cells:
            cells.append(row(cur))
            rows.append(cells)
        if name in ("aligned", "align", "align*", "eqnarray"):
            out = []
            for r in rows:
                tds = []
                for k, c in enumerate(r):
                    al = "right" if k % 2 == 0 else "left"
                    tds.append(f'<mtd columnalign="{al}">{c}</mtd>')
                out.append("<mtr>" + "".join(tds) + "</mtr>")
            return '<mtable displaystyle="true" columnspacing="0em" rowspacing="0.6em">' + "".join(out) + "</mtable>"
        # cases / matrix / array
        out = ["<mtr>" + "".join(f'<mtd columnalign="left">{c}</mtd>' for c in r) + "</mtr>" for r in rows]
        tab = '<mtable columnspacing="1em">' + "".join(out) + "</mtable>"
        if name == "cases":
            return f'<mrow><mo stretchy="true">{{</mo>{tab}</mrow>'
        if name in ("pmatrix",):
            return f'<mrow><mo>(</mo>{tab}<mo>)</mo></mrow>'
        return tab


def upright_label(m):
    """subscripts made only of Latin letters (V_{CE}, I_B) -> upright label"""
    parts = re.findall(r"<m[in]>([A-Za-z]|[0-9]+)</m[in]>", m)
    if any(p.isalpha() for p in parts) and re.fullmatch(r"(?:<mrow>)?(?:<m[in]>(?:[A-Za-z]|[0-9]+)</m[in]>)+(?:</mrow>)?", m):
        return f'<mi mathvariant="normal">{"".join(parts)}</mi>'
    return m


def row(items):
    items = [x for x in items if x]
    if len(items) == 1:
        return items[0]
    return "<mrow>" + "".join(items) + "</mrow>"


def tex2mathml(src, display=False):
    p = P(src.strip())
    body = row(p.seq())
    alt = esc(src.strip()).replace('"', "&quot;").replace("\n", " ")
    d = ' display="block"' if display else ""
    return f'<math xmlns="http://www.w3.org/1998/Math/MathML"{d} alttext="{alt}"><mstyle displaystyle="{str(display).lower()}">{body}</mstyle></math>'


if __name__ == "__main__":
    import sys
    print(tex2mathml(sys.argv[1], display=True))
