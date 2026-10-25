"""SPICE models of capacitors and series parts (#51), cut down to what KiPI solves with.

A vendor's model of a capacitor, resistor, ferrite bead or inductor is a ``.SUBCKT`` with
two pins, built of resistors, inductors and capacitors (often a ladder of them, to follow
the part's impedance over frequency). KiPI works out the impedance between the two pins
and fits the values its own models have, a capacitor's C, ESL and ESR
(:func:`capacitor_values`) and a series part's DC resistance and ESL (:func:`link_values`),
for the tables and reports.

A series diode (#191) is a ``.model NAME D(...)`` card (its anode the first pin, side A)
or a two-pin subcircuit with ``D`` elements in it. Its diodes follow the SPICE junction:
IS, N and RS, BV and IBV for reverse breakdown, and CJO, VJ, M, FC and TT for its
capacitance, at 27 °C. The DC solve takes the part's current at each voltage across it
(:meth:`SpiceModel.dc_current`); AC and Coupled Noise its small-signal impedance at a DC
operating current (:meth:`SpiceModel.impedance`).

The file is only read, never run: elements other than R, L, C, D, subcircuit calls and
0 V sources (current probes) are refused, values may be plain arithmetic of numbers and
parameters only, and ``.include``/``.lib`` lines are not followed.
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

MAX_FILE_BYTES = 64 << 20           # a vendor's whole series library is a few MB
MAX_DEPTH = 12                      # subcircuits calling subcircuits
MAX_ELEMENTS = 20000                # in one flattened model
MAX_NODES = 600                     # the impedance is solved densely
MAX_EXPR = 400                      # characters in one value expression

# Where the values are read. The capacitor's C and ESL are fitted a factor either side
# of its self-resonance, its ESR at it; a series part's ESL is its inductance at
# LINK_ESL_HZ, low on a ferrite bead's inductive slope, as the bead dialog reads it.
SWEEP_HZ = np.logspace(0, 11, 1101)         # 1 Hz to 100 GHz
FIT_FACTOR = 30.0
LINK_ESL_HZ = 100e3
# A diode's fitted values are read at this current: its slope resistance (the DCR in its
# editor) and its ESL.
LINK_BIAS_A = 1.0

VT = 1.380649e-23 * 300.15 / 1.602176634e-19    # thermal voltage at 27 °C
GMIN = 1e-12                                    # across every junction, as in SPICE

PORT_A, PORT_B = "\0a", "\0b"


class SpiceError(ValueError):
    """A SPICE file or model KiPI cannot use, with why."""


# ------------------------------------------------------------------ values
_SUFFIX = (("meg", 1e6), ("mil", 25.4e-6), ("t", 1e12), ("g", 1e9), ("k", 1e3),
           ("m", 1e-3), ("u", 1e-6), ("µ", 1e-6), ("μ", 1e-6), ("n", 1e-9), ("p", 1e-12),
           ("f", 1e-15))
_NUM = re.compile(r"(\d+\.?\d*|\.\d+)(?:[eE]([+-]?\d+))?([a-zA-Zµμ]*)")
_NAME = re.compile(r"[A-Za-z_][\w.]*")
_FUNCS = {"sqrt": math.sqrt, "abs": abs, "exp": math.exp, "log": math.log,
          "log10": math.log10, "sin": math.sin, "cos": math.cos}
_CONSTS = {"pi": math.pi}


def number(text: str) -> Optional[float]:
    """A SPICE number ("10u", "2.2nF", "1.5MEG", "1e-6"), or None if it is not one. As in
    SPICE, M is milli, MEG mega, and a unit after the scale ("uF", "Ohm") is ignored."""
    m = _NUM.fullmatch(text.strip())
    if m is None:
        return None
    return _scaled(m)


def _scaled(m) -> float:
    v = float(m.group(1)) * (10.0 ** int(m.group(2)) if m.group(2) else 1.0)
    rest = m.group(3).lower()
    for s, k in _SUFFIX:
        if rest.startswith(s):
            return v * k
    return v


class _Expr:
    """Plain arithmetic on numbers, parameters and a few functions: + - * / ^ ** ( )."""

    def __init__(self, text: str, env: Dict[str, float]):
        if len(text) > MAX_EXPR:
            raise SpiceError(f"value too long: {text[:40]}…")
        self.text, self.env, self.i, self.depth = text, env, 0, 0

    def run(self) -> float:
        v = self._sum()
        self._space()
        if self.i != len(self.text):
            raise SpiceError(f"cannot read the value {self.text!r}")
        return v

    def _space(self):
        while self.i < len(self.text) and self.text[self.i].isspace():
            self.i += 1

    def _peek(self, *ops) -> Optional[str]:
        self._space()
        for op in ops:
            if self.text.startswith(op, self.i):
                return op
        return None

    def _sum(self) -> float:
        v = self._product()
        while (op := self._peek("+", "-")) is not None:
            self.i += 1
            w = self._product()
            v = v + w if op == "+" else v - w
        return v

    def _product(self) -> float:
        v = self._power()
        while (op := self._peek("**", "*", "/")) is not None and op != "**":
            self.i += 1
            w = self._power()
            if op == "*":
                v *= w
            elif w == 0:
                raise SpiceError(f"division by zero in {self.text!r}")
            else:
                v /= w
        return v

    def _power(self) -> float:
        v = self._unary()
        if (op := self._peek("**", "^")) is not None:
            self.i += len(op)
            w = self._power()
            try:
                v = float(v ** w)
            except (OverflowError, ZeroDivisionError, TypeError) as e:
                raise SpiceError(f"cannot work out {self.text!r}") from e
        return v

    def _unary(self) -> float:
        if (op := self._peek("+", "-")) is not None:
            self.i += 1
            v = self._unary()
            return -v if op == "-" else v
        return self._atom()

    def _atom(self) -> float:
        self._space()
        t = self.text
        if self._peek("("):
            self.depth += 1
            if self.depth > 50:
                raise SpiceError(f"value nested too deep: {t[:40]}…")
            self.i += 1
            v = self._sum()
            if not self._peek(")"):
                raise SpiceError(f"missing ) in {t!r}")
            self.i += 1
            self.depth -= 1
            return v
        m = _NUM.match(t, self.i)
        if m:
            self.i = m.end()
            return _scaled(m)
        m = _NAME.match(t, self.i)
        if m:
            self.i = m.end()
            name = m.group(0).lower()
            if name in _FUNCS and self._peek("("):
                self.i += 1
                arg = self._sum()
                if not self._peek(")"):
                    raise SpiceError(f"missing ) in {t!r}")
                self.i += 1
                try:
                    return float(_FUNCS[name](arg))
                except (ValueError, OverflowError) as e:
                    raise SpiceError(f"cannot work out {t!r}") from e
            if name in self.env:
                return self.env[name]
            if name in _CONSTS:
                return _CONSTS[name]
            raise SpiceError(f"unknown parameter {m.group(0)!r}")
        raise SpiceError(f"cannot read the value {t!r}")


def evaluate(text: str, env: Optional[Dict[str, float]] = None) -> float:
    """A value field: a number, or ``{expression}`` / ``'expression'`` of parameters."""
    s = text.strip()
    if len(s) >= 2 and (s[0], s[-1]) in (("{", "}"), ("'", "'"), ('"', '"')):
        s = s[1:-1]
    v = number(s)
    return v if v is not None else _Expr(s, env or {}).run()


# ------------------------------------------------------------------ file
@dataclass
class Subckt:
    name: str
    ports: List[str]
    params: List[Tuple[str, str]]               # defaults, in order, as written
    body: List[List[str]] = field(default_factory=list)   # tokenized lines


def _logical_lines(text: str) -> List[str]:
    out: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        line = line.split(";", 1)[0]
        line = re.split(r"\s\$", line, maxsplit=1)[0].strip()      # HSPICE's inline comment
        if not line:
            continue
        if line.startswith("+"):
            if out:
                out[-1] += " " + line[1:]
            continue
        out.append(line)
    return out


def _tokens(line: str) -> List[str]:
    """Words of a line, an expression in braces or quotes kept whole, ``k = v`` as
    ``k=v``."""
    line = re.sub(r"\s*=\s*", "=", line)
    out, cur, close = [], "", None
    for ch in line:
        if close is not None:
            cur += ch
            if ch == close:
                close = None
        elif ch in "{'\"":
            cur += ch
            close = "}" if ch == "{" else ch
        elif ch.isspace() or ch in "()":
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _split_params(words: Sequence[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Plain words, and the ``name=value`` ones."""
    plain, params = [], []
    for w in words:
        if w.lower() in ("params:", "param:"):
            continue
        if "=" in w and not w.startswith("="):
            k, v = w.split("=", 1)
            params.append((k.lower(), v))
        else:
            plain.append(w)
    return plain, params


class Library:
    """The subcircuits of a SPICE file."""

    def __init__(self, text: str, name: str = ""):
        self.name = name
        self.subckts: Dict[str, Subckt] = {}
        self.params: List[Tuple[str, str]] = []     # file-level .param
        # Diode .model cards, anywhere in the file: lower-case name -> (name, params).
        self.models: Dict[str, Tuple[str, List[Tuple[str, str]]]] = {}
        stack: List[Subckt] = []
        for line in _logical_lines(text):
            words = _tokens(line)
            if not words:
                continue
            head = words[0].lower()
            if head == ".subckt":
                plain, params = _split_params(words[1:])
                if not plain:
                    raise SpiceError(".SUBCKT without a name")
                stack.append(Subckt(plain[0], plain[1:], params))
            elif head == ".ends":
                if stack:
                    s = stack.pop()
                    self.subckts.setdefault(s.name.lower(), s)
            elif head == ".model":
                if len(words) >= 3 and words[2].lower() == "d":
                    self.models.setdefault(words[1].lower(),
                                           (words[1], _split_params(words[3:])[1]))
            elif stack:
                stack[-1].body.append(words)
            elif head == ".param":
                self.params += _split_params(words[1:])[1]
        if stack:
            raise SpiceError(f".SUBCKT {stack[-1].name} has no .ENDS")

    @staticmethod
    def read(path) -> "Library":
        path = Path(path)
        if path.stat().st_size > MAX_FILE_BYTES:
            raise SpiceError(f"{path.name} is too large to be a SPICE model file")
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1")            # vendors' µ and Ω
        return Library(text, path.name)

    def two_pin(self) -> List[str]:
        """Names of the subcircuits with two pins (the ones a part can be), in file order."""
        return [s.name for s in self.subckts.values() if len(s.ports) == 2]

    def diodes(self) -> List[str]:
        """Names of the diode ``.model`` cards, which a series part can be too."""
        return [name for name, _p in self.models.values()]

    def model(self, name: str) -> "SpiceModel":
        """Subcircuit ``name`` flattened into its R, L, C and diodes between its two pins,
        or diode model ``name`` from its anode (pin A) to its cathode."""
        s = self.subckts.get(name.lower())
        if s is None and name.lower() in self.models:
            m = SpiceModel(self.models[name.lower()][0])
            self._diode(name, PORT_A, PORT_B, name, 1.0, self._env(self.params, {}), m, "")
            return m
        if s is None:
            raise SpiceError(f"no subcircuit {name}")
        if len(s.ports) != 2:
            raise SpiceError(f"{s.name} has {len(s.ports)} pins, not two")
        env = self._env(self.params, {})
        m = SpiceModel(s.name)
        self._flatten(s, [PORT_A, PORT_B], {}, env, "", m, 0)
        return m

    def netlist(self, name: str) -> str:
        """Subcircuit ``name`` on its own, as text: with the subcircuits it calls and the
        file's parameters, so it reads back alone (into a setup file, say)."""
        order: List[Subckt] = []
        models: List[str] = [name.lower()] if name.lower() in self.models else []
        todo = [name.lower()]
        while todo:
            s = self.subckts.get(todo.pop())
            if s is None or s in order:
                continue
            order.append(s)
            for words in s.body:
                if words[0][:1].upper() == "X":
                    plain = _split_params(words[1:])[0]
                    if plain:
                        todo.append(plain[-1].lower())
                elif words[0][:1].upper() == "D":
                    plain = _split_params(words[1:])[0]
                    if len(plain) > 2 and plain[2].lower() not in models:
                        models.append(plain[2].lower())
        lines = [".param " + " ".join(f"{k}={v}" for k, v in self.params)] \
            if self.params else []
        for key in models:
            if key in self.models:
                mname, params = self.models[key]
                lines.append(f".model {mname} D(" + " ".join(f"{k}={v}" for k, v in params)
                             + ")")
        for s in order:
            params = (" PARAMS: " + " ".join(f"{k}={v}" for k, v in s.params)
                      if s.params else "")
            lines.append(f".SUBCKT {s.name} {' '.join(s.ports)}{params}")
            lines += [" ".join(w) for w in s.body]
            lines.append(".ENDS")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _env(params, env: Dict[str, float]) -> Dict[str, float]:
        env = dict(env)
        for k, v in params:
            env[k] = evaluate(v, env)
        return env

    def _flatten(self, s: Subckt, nodes: List[str], given: Dict[str, float],
                 outer: Dict[str, float], path: str, m: "SpiceModel", depth: int):
        if depth > MAX_DEPTH:
            raise SpiceError(f"subcircuits nested more than {MAX_DEPTH} deep at {s.name}")
        env = self._env(s.params, outer)
        env.update(given)
        body = s.body
        for words in body:                          # .param inside the subcircuit
            if words[0].lower() == ".param":
                env = self._env(_split_params(words[1:])[1], env)
        ports = {p.lower(): n for p, n in zip(s.ports, nodes)}

        def node(n: str) -> str:
            n = n.lower()
            if n in ports:
                return ports[n]
            return "0" if n in ("0", "gnd") else f"{path}{n}"

        for words in body:
            name = words[0]
            kind = name[0].upper()
            if kind == ".":
                continue
            if kind in "RLC":
                self._passive(words, kind, node, env, m, path)
            elif kind == "D":
                plain, params = _split_params(words[1:])
                if len(plain) < 3:
                    raise SpiceError(f"{name}: needs two nodes and a model")
                area = dict(params).get("area", plain[3] if len(plain) > 3 else "1")
                self._diode(name, node(plain[0]), node(plain[1]), plain[2],
                            evaluate(area, env), env, m, path)
            elif kind == "V":
                self._probe(words, node, env, m)
            elif kind == "X":
                plain, params = _split_params(words[1:])
                if len(plain) < 2:
                    raise SpiceError(f"{name}: no subcircuit named")
                sub = self.subckts.get(plain[-1].lower())
                if sub is None:
                    raise SpiceError(f"{name} calls {plain[-1]}, which is not in the file")
                pins = plain[:-1]
                if len(pins) != len(sub.ports):
                    raise SpiceError(f"{name} gives {sub.name} {len(pins)} pins, not "
                                     f"{len(sub.ports)}")
                vals = {k: evaluate(v, env) for k, v in params}
                self._flatten(sub, [node(p) for p in pins], vals, env,
                              f"{path}{name.lower()}.", m, depth + 1)
            else:
                raise SpiceError(f"{name}: only resistors, inductors, capacitors and diodes "
                                 f"can be read, not "
                                 f"{_KINDS.get(kind, 'this kind of element')}")
            if len(m.elements) > MAX_ELEMENTS:
                raise SpiceError(f"more than {MAX_ELEMENTS} elements")

    @staticmethod
    def _passive(words, kind, node, env, m: "SpiceModel", path: str):
        name = words[0]
        if len(words) < 3:
            raise SpiceError(f"{name}: needs two nodes and a value")
        a, b = node(words[1]), node(words[2])
        plain, params = _split_params(words[3:])
        extra = dict(params)
        value = extra.pop(kind.lower(), None)
        if value is None:
            value = extra.pop("value", None)
        if value is None:
            if not plain:
                raise SpiceError(f"{name}: no value")
            value = plain[0]
        v = evaluate(value, env)
        # LTspice's parasitics on a capacitor or an inductor: Rser and Lser in series,
        # Rpar and Cpar across the whole.
        rser = evaluate(extra["rser"], env) if "rser" in extra and kind != "R" else 0.0
        lser = evaluate(extra["lser"], env) if "lser" in extra and kind == "C" else 0.0
        inner = f"{path}{name.lower()}#"
        x, y = a, b
        if rser:
            m.add("R", x, inner + "r", rser)
            x = inner + "r"
        if lser:
            m.add("L", x, inner + "l", lser)
            x = inner + "l"
        m.add(kind, x, y, v)
        if kind != "R":
            if "rpar" in extra:
                m.add("R", a, b, evaluate(extra["rpar"], env))
            if "cpar" in extra:
                m.add("C", a, b, evaluate(extra["cpar"], env))

    def _diode(self, name: str, a: str, b: str, model: str, area: float,
               env: Dict[str, float], m: "SpiceModel", path: str):
        """Diode ``name`` of ``model`` from anode ``a`` to cathode ``b``: a junction, with
        the model's RS in series."""
        got = self.models.get(model.lower())
        if got is None:
            raise SpiceError(f"{name}: its diode model {model} is not in the file")
        if not area > 0:
            raise SpiceError(f"{name}: its area must be positive")
        p = dict(got[1])
        if {"ron", "roff", "vfwd", "vrev"} & set(p):
            raise SpiceError(f"{model}: LTspice's piecewise-linear diode (Ron, Vfwd) is not "
                             "read; use a junction model (IS, N, RS)")

        def val(*keys, default=None):
            for k in keys:
                if k in p:
                    return evaluate(p[k], env)
            return default
        d = Diode(is_a=val("is", default=1e-14) * area, n=val("n", default=1.0),
                  bv=val("bv"), ibv=val("ibv", default=1e-3) * area,
                  cjo=val("cjo", "cj0", "cj", default=0.0) * area,
                  vj=val("vj", "pb", default=1.0), m=val("m", "mj", default=0.5),
                  fc=val("fc", default=0.5), tt=val("tt", default=0.0))
        if not (d.is_a > 0 and d.n > 0 and d.ibv > 0 and d.vj > 0 and 0 <= d.fc < 1):
            raise SpiceError(f"{model}: IS, N, IBV and VJ must be positive and FC below 1")
        if d.bv is not None and not d.bv > 0:
            d = dataclasses.replace(d, bv=None)     # BV=0 or below: no breakdown
        rs = val("rs", default=0.0) / area
        if rs:
            inner = f"{path}{name.lower()}#rs"
            m.add("R", a, inner, rs)
            a = inner
        m.add_diode(a, b, d)

    @staticmethod
    def _probe(words, node, env, m: "SpiceModel"):
        """A voltage source used as a current probe: 0 V, a short."""
        name = words[0]
        if len(words) < 3:
            raise SpiceError(f"{name}: needs two nodes")
        rest = [w for w in words[3:] if w.lower() not in ("dc", "ac")]
        for w in rest:
            try:
                v = evaluate(w.split("=", 1)[-1], env)
            except SpiceError:
                v = 1.0
            if v != 0:
                raise SpiceError(f"{name}: only 0 V sources (current probes) can be read")
        m.add("R", node(words[1]), node(words[2]), 0.0)


_KINDS = {"Q": "transistors", "M": "transistors", "J": "transistors",
          "E": "controlled sources", "F": "controlled sources", "G": "controlled sources",
          "H": "controlled sources", "B": "behavioral sources", "I": "current sources",
          "K": "coupled inductors", "S": "switches", "W": "switches",
          "T": "transmission lines", "O": "transmission lines", "U": "transmission lines"}


# ------------------------------------------------------------------ diodes
def _expo(x: float, x_max: float) -> Tuple[float, float]:
    """exp(x) and its slope, carried on linearly past ``x_max`` so it cannot overflow."""
    if x <= x_max:
        e = math.exp(x)
        return e, e
    e = math.exp(x_max)
    return e * (1 + x - x_max), e


@dataclass(frozen=True)
class Diode:
    """A SPICE junction (its RS is a resistor beside it): currents in amps from anode to
    cathode."""
    is_a: float = 1e-14
    n: float = 1.0
    bv: Optional[float] = None          # reverse breakdown voltage; None: none
    ibv: float = 1e-3                   # current at -BV
    cjo: float = 0.0
    vj: float = 1.0
    m: float = 0.5
    fc: float = 0.5
    tt: float = 0.0

    @property
    def nvt(self) -> float:
        return self.n * VT

    def current(self, v: float) -> Tuple[float, float]:
        """The current at junction voltage ``v``, and its slope (the conductance)."""
        nvt = self.nvt
        e, de = _expo(v / nvt, min(700.0, math.log(1e4 / self.is_a)))
        i, g = self.is_a * (e - 1), self.is_a * de / nvt
        if self.bv is not None:
            x_max = min(700.0, math.log(1e4 / self.ibv))
            e, de = _expo(-(v + self.bv) / nvt, x_max)
            i -= self.ibv * (e - math.exp(max(-self.bv / nvt, -700.0)))
            g += self.ibv * de / nvt
        return i + GMIN * v, g + GMIN

    def limit(self, new: float, old: float) -> float:
        """SPICE's step limit on a junction's voltage between Newton steps (pnjlim), on the
        forward side and, with BV, on the breakdown side."""
        nvt = self.nvt

        def lim(new, old, i_s):
            vcrit = nvt * math.log(nvt / (math.sqrt(2) * i_s))
            if new > vcrit and abs(new - old) > 2 * nvt:
                if old > 0:
                    arg = 1 + (new - old) / nvt
                    return old + nvt * math.log(arg) if arg > 0 else vcrit
                return nvt * math.log(new / nvt)
            return new
        new = lim(new, old, self.is_a)
        if self.bv is not None:
            new = -lim(-(new + self.bv), -(old + self.bv), self.ibv) - self.bv
        return new

    def capacitance(self, v: float, g: float) -> float:
        """Junction and diffusion capacitance at junction voltage ``v``, conductance ``g``."""
        c = self.tt * g
        if self.cjo:
            if v < self.fc * self.vj:
                c += self.cjo * (1 - v / self.vj) ** -self.m
            else:
                c += (self.cjo * (1 - self.fc) ** (-1 - self.m)
                      * (1 - self.fc * (1 + self.m) + self.m * v / self.vj))
        return c


# ------------------------------------------------------------------ model
@dataclass
class Element:
    kind: str           # "R", "L", "C" or "D"
    a: str              # a diode's anode
    b: str
    value: float
    diode: Optional[Diode] = None


@dataclass
class SpiceModel:
    """A two-pin network of resistors, inductors, capacitors and diodes."""
    name: str
    elements: List[Element] = field(default_factory=list)

    def add(self, kind: str, a: str, b: str, value: float):
        if a != b:
            self.elements.append(Element(kind, a, b, float(value)))

    def add_diode(self, a: str, b: str, d: Diode):
        if a != b:
            self.elements.append(Element("D", a, b, 0.0, d))

    @property
    def has_diodes(self) -> bool:
        return any(e.kind == "D" for e in self.elements)

    def _merged(self, dc: bool, elements: Optional[List[Element]] = None):
        """A node -> group map with shorts (and, at DC, inductors) merged, and the elements
        between groups: capacitors are dropped at DC."""
        elements = self.elements if elements is None else elements
        parent: Dict[str, str] = {}

        def find(n):
            parent.setdefault(n, n)
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        for e in elements:
            find(e.a), find(e.b)
            if (e.kind in "RL" and e.value == 0) or (dc and e.kind == "L"):
                parent[find(e.a)] = find(e.b)
        keep = []
        for e in elements:
            if e.kind == "C" and (dc or e.value == 0):
                continue
            if e.kind in "RL" and (e.value == 0 or (dc and e.kind == "L")):
                continue
            a, b = find(e.a), find(e.b)
            if a != b:
                keep.append(Element(e.kind, a, b, e.value, e.diode))
        return find(PORT_A), find(PORT_B), keep

    @staticmethod
    def _component(a: str, b: str, elements: List[Element]) -> Optional[List[str]]:
        """The nodes connected to ``a`` (``b`` first), or None if ``b`` is not one."""
        nbr: Dict[str, List[str]] = {}
        for e in elements:
            nbr.setdefault(e.a, []).append(e.b)
            nbr.setdefault(e.b, []).append(e.a)
        seen, todo = {a}, [a]
        while todo:
            for n in nbr.get(todo.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    todo.append(n)
        if b not in seen:
            return None
        seen.discard(b)
        if len(seen) > MAX_NODES:
            raise SpiceError(f"more than {MAX_NODES} nodes")
        return sorted(seen, key=lambda n: (n != a, n))

    def impedance(self, freqs, c_scale: float = 1.0, bias_a: float = 0.0) -> np.ndarray:
        """Impedance between the two pins at each of ``freqs`` (Hz, all > 0); inf where
        they are not connected. ``c_scale`` scales every capacitor (a derating). Diodes
        are their small-signal conductance and capacitance with ``bias_a`` amps through
        the part from pin A to pin B."""
        f = np.atleast_1d(np.asarray(freqs, float))
        a, b, els = self._merged(dc=False, elements=self._small_signal(bias_a))
        if a == b:
            return np.zeros(f.shape, complex)
        nodes = self._component(a, b, els)
        if nodes is None:
            return np.full(f.shape, np.inf, complex)
        idx = {n: i for i, n in enumerate(nodes)}
        n = len(nodes)
        g, c, k = (np.zeros((n, n)) for _ in range(3))
        for e in els:
            mat, v = {"R": (g, 1.0 / e.value), "C": (c, e.value * c_scale),
                      "L": (k, 1.0 / e.value)}[e.kind]
            i, j = idx.get(e.a), idx.get(e.b)
            for p, q, s in ((i, i, v), (j, j, v), (i, j, -v), (j, i, -v)):
                if p is not None and q is not None:
                    mat[p, q] += s
        w = 2 * np.pi * f
        out = np.empty(f.shape, complex)
        rhs = np.zeros(n, complex)
        rhs[0] = 1.0                                # 1 A into pin A, out of pin B
        step = max(1, int(4e6 // (n * n)))
        for s in range(0, len(f), step):
            ws = w[s:s + step, None, None]
            y = g + 1j * ws * c + k / (1j * ws)
            try:
                v = np.linalg.solve(y, np.broadcast_to(rhs, (len(ws), n))[..., None])
            except np.linalg.LinAlgError as e:
                raise SpiceError(f"{self.name}: the network cannot be solved") from e
            out[s:s + step] = v[:, 0, 0]
        return out

    def _small_signal(self, bias_a: float) -> List[Element]:
        """The elements with each diode as its conductance and capacitance at ``bias_a``."""
        if not self.has_diodes:
            return self.elements
        vds = self.operating_point(bias_a)[2]
        out = [e for e in self.elements if e.kind != "D"]
        for e, (vd, g) in zip((e for e in self.elements if e.kind == "D"), vds):
            out.append(Element("R", e.a, e.b, 1.0 / g))
            c = e.diode.capacitance(vd, g)
            if c > 0:
                out.append(Element("C", e.a, e.b, c))
        return out

    def _dc_network(self):
        """The network at DC, for :meth:`dc_current`: its node count (pin A is node 0, pin
        B is None), its resistors and its diodes, as (node, node, conductance or Diode),
        and the last operating point (node voltages, junction voltages)."""
        net = self.__dict__.get("_dc")
        if net is None:
            a, b, els = self._merged(dc=True)
            nodes = self._component(a, b, els) if a != b else None
            if nodes is None:
                raise SpiceError(f"{self.name} does not conduct at DC, so it cannot be a "
                                 "series part")
            idx = {n: i for i, n in enumerate(nodes)}
            res = [(idx.get(e.a), idx.get(e.b), 1.0 / e.value) for e in els if e.kind == "R"]
            dio = [(idx.get(e.a), idx.get(e.b), e.diode) for e in els if e.kind == "D"]
            net = self.__dict__["_dc"] = (len(nodes), res, dio, np.zeros(len(nodes)),
                                         [0.0] * len(dio))
        return net

    def dc_current(self, v: float) -> Tuple[float, float]:
        """The DC current from pin A to pin B with ``v`` volts from A to B, and its slope
        dI/dV. Newton's method with SPICE's junction step limits, starting from the last
        call's operating point."""
        return self._dc_solve(v)[:2]

    def _dc_solve(self, v: float):
        n, res, dio, x_last, vd_last = self._dc_network()
        x = x_last.copy()
        x[0] = v
        vd = list(vd_last)
        for _ in range(500):
            jac = np.zeros((n, n))
            r = np.zeros(n)
            for p, q, g in res:
                for s, t, w in ((p, p, g), (q, q, g), (p, q, -g), (q, p, -g)):
                    if s is not None and t is not None:
                        jac[s, t] += w
            gds = []
            for (p, q, d), vk in zip(dio, vd):
                i, g = d.current(vk)
                gds.append(g)
                ieq = i - g * vk
                for s, t, w in ((p, p, g), (q, q, g), (p, q, -g), (q, p, -g)):
                    if s is not None and t is not None:
                        jac[s, t] += w
                if p is not None:
                    r[p] -= ieq
                if q is not None:
                    r[q] += ieq
            if n > 1:
                try:
                    x[1:] = np.linalg.solve(jac[1:, 1:], r[1:] - jac[1:, 0] * v)
                except np.linalg.LinAlgError as e:
                    raise SpiceError(f"{self.name}: the network cannot be solved at DC") from e
            new = [(x[p] if p is not None else 0.0) - (x[q] if q is not None else 0.0)
                   for p, q, _d in dio]
            done = all(abs(a - b) <= 1e-9 + 1e-6 * abs(b) for a, b in zip(new, vd))
            if done:
                break
            vd = [d.limit(a, b) for (_p, _q, d), a, b in zip(dio, new, vd)]
        else:
            raise SpiceError(f"{self.name}: no DC operating point found at {v:g} V")
        i = float(jac[0] @ x - r[0])
        slope = float(jac[0, 0])
        if n > 1:
            slope -= float(jac[0, 1:] @ np.linalg.solve(jac[1:, 1:], jac[1:, 0]))
        x_last[:] = x
        vd_last[:] = vd
        return i, slope, list(zip(vd, gds))

    def operating_point(self, i: float) -> Tuple[float, float, List[Tuple[float, float]]]:
        """The voltage from pin A to pin B with ``i`` amps through the part at DC, its slope
        resistance dV/dI, and each diode's junction voltage and conductance there."""
        if not self.has_diodes:
            r = self.dc_resistance()
            if r is None:
                raise SpiceError(f"{self.name} does not conduct at DC")
            return i * r, r, []
        lo, hi = (0.0, 0.5) if i >= 0 else (-0.5, 0.0)
        while self.dc_current(hi)[0] < i:
            lo, hi = hi, 2 * hi
            if hi > 1e5:
                raise SpiceError(f"{self.name} cannot carry {i:g} A")
        while self.dc_current(lo)[0] > i:
            lo, hi = 2 * lo, lo
            if lo < -1e5:
                raise SpiceError(f"{self.name} cannot carry {i:g} A")
        v = hi if i > 0 else lo
        for _ in range(200):
            got, g, vds = self._dc_solve(v)
            if abs(got - i) <= 1e-12 + 1e-9 * abs(i) or hi - lo < 1e-12:
                return v, 1.0 / g, vds
            if got < i:
                lo = v
            else:
                hi = v
            step = v - (got - i) / g
            v = step if lo < step < hi else 0.5 * (lo + hi)
        raise SpiceError(f"{self.name}: no DC operating point found at {i:g} A")

    def dc_resistance(self) -> Optional[float]:
        """Resistance between the pins at DC; None if no resistor path joins them."""
        a, b, els = self._merged(dc=True)
        if a == b:
            return 0.0
        nodes = self._component(a, b, els)
        if nodes is None:
            return None
        idx = {n: i for i, n in enumerate(nodes)}
        g = np.zeros((len(nodes), len(nodes)))
        for e in els:
            v = 1.0 / e.value
            i, j = idx.get(e.a), idx.get(e.b)
            for p, q, s in ((i, i, v), (j, j, v), (i, j, -v), (j, i, -v)):
                if p is not None and q is not None:
                    g[p, q] += s
        rhs = np.zeros(len(nodes))
        rhs[0] = 1.0
        try:
            return float(np.linalg.solve(g, rhs)[0])
        except np.linalg.LinAlgError as e:
            raise SpiceError(f"{self.name}: the network cannot be solved at DC") from e


# ------------------------------------------------------------------ fits
@dataclass
class CapValues:
    capacitance_f: float
    esl_h: float
    esr_ohm: float
    srf_hz: float


@dataclass
class LinkValues:
    resistance_ohm: float
    esl_h: Optional[float]          # None: not inductive at LINK_ESL_HZ
    # A diode's: its voltage from side A to side B at some currents, [(amps, volts)].
    forward: Optional[List[Tuple[float, float]]] = None


def capacitor_values(m: SpiceModel) -> CapValues:
    """A capacitor model's series C, ESL and ESR: C and ESL fitted to its reactance a
    factor of FIT_FACTOR below and above its self-resonance, ESR its resistance there."""
    z = m.impedance(SWEEP_HZ)
    if not np.all(np.isfinite(z)):
        raise SpiceError(f"{m.name}: its two pins are not connected")
    x = z.imag
    if x[0] >= 0:
        raise SpiceError(f"{m.name} is not capacitive at low frequency: it is not a "
                         "capacitor's model")
    up = np.nonzero((x[:-1] < 0) & (x[1:] >= 0))[0]
    if not up.size:
        raise SpiceError(f"{m.name} does not resonate below 100 GHz, so it has no ESL to "
                         "read")
    i = int(up[0])
    lf = np.log(SWEEP_HZ[i:i + 2])
    srf = float(np.exp(lf[0] + (lf[1] - lf[0]) * -x[i] / (x[i + 1] - x[i])))
    f1 = max(srf / FIT_FACTOR, SWEEP_HZ[0])
    f2 = min(srf * FIT_FACTOR, SWEEP_HZ[-1])
    z1, z2, z0 = m.impedance([f1, f2, srf])
    w1, w2 = 2 * math.pi * f1, 2 * math.pi * f2
    # X = w L - (1/C) / w at both points.
    try:
        esl, inv_c = np.linalg.solve([[w1, -1 / w1], [w2, -1 / w2]], [z1.imag, z2.imag])
    except np.linalg.LinAlgError:
        esl, inv_c = 0.0, 0.0
    if not (esl > 0 and inv_c > 0):
        esl, inv_c = z2.imag / w2, -z1.imag * w1
    if not (esl > 0 and inv_c > 0):
        raise SpiceError(f"{m.name}: no series C and ESL fit its impedance")
    esr = float(z0.real)
    if esr <= 0:
        raise SpiceError(f"{m.name} has no resistance at resonance")
    return CapValues(float(1 / inv_c), float(esl), esr, srf)


def link_values(m: SpiceModel) -> LinkValues:
    """A series part's DC resistance and its inductance at LINK_ESL_HZ (None where it is
    not inductive there: a resistor with only some capacitance across it). A diode's are
    its slope resistance and inductance at LINK_BIAS_A, with its forward voltage."""
    if m.has_diodes:
        forward = [(i, m.operating_point(i)[0]) for i in (1e-3, 0.1, LINK_BIAS_A)]
        r = m.operating_point(LINK_BIAS_A)[1]
        z = m.impedance([LINK_ESL_HZ], bias_a=LINK_BIAS_A)[0]
        esl = z.imag / (2 * math.pi * LINK_ESL_HZ)
        return LinkValues(float(r), float(esl) if esl > 0 else None, forward)
    r = m.dc_resistance()
    if r is None:
        raise SpiceError(f"{m.name} does not conduct at DC (a capacitor's model?), so it "
                         "cannot be a series part")
    if r < 0:
        raise SpiceError(f"{m.name} has a negative resistance at DC")
    z = m.impedance([LINK_ESL_HZ])[0]
    if z.imag < 0 and abs(z) < r / 2:
        # Its impedance falls with frequency: a capacitor with a leakage resistance.
        raise SpiceError(f"{m.name} is capacitive (a capacitor's model?), so it cannot be "
                         "a series part")
    esl = z.imag / (2 * math.pi * LINK_ESL_HZ)
    return LinkValues(r, float(esl) if esl > 0 else None)


def best_match(names: Sequence[str], hints: Sequence[str]) -> Optional[str]:
    """The subcircuit named like one of ``hints`` (a model's name, a part's value): the
    same apart from case and punctuation, else one containing the other."""
    def key(s):
        return re.sub(r"[^0-9a-z]", "", s.lower())
    keys = [(n, key(n)) for n in names]
    for h in filter(None, map(key, hints)):
        for n, k in keys:
            if k == h:
                return n
    for h in filter(None, map(key, hints)):
        if len(h) < 4:
            continue
        for n, k in keys:
            if k and (h in k or (len(k) >= 4 and k in h)):
                return n
    return None


# ------------------------------------------------------------------ in a setup
@dataclass
class SpiceRef:
    """A part's SPICE model kept in the setup: its subcircuit as text (with what it
    calls), so the setup file carries it, and the values fitted from it.

    With ``use`` on, the AC and Coupled Noise solves take the part's impedance from the
    model at every frequency, and the RLC values in its editor are the fitted ones (the
    DC solve uses the fitted DC resistance, or a diode's current at each voltage). Off,
    the part is its RLC values again and the model is only kept."""
    subckt: str
    netlist: str
    file: str = ""                  # the file it came from, for the user to see
    use: bool = True
    # The fitted values, by the CapModel or DcLink field they fill: capacitance_f, esl_h
    # and esr_ohm, or resistance and esl_h.
    values: Dict[str, float] = field(default_factory=dict)

    def model(self) -> SpiceModel:
        return _model(self.netlist, self.subckt)

    @property
    def has_diodes(self) -> bool:
        return self.model().has_diodes

    def dc_current(self, v: float) -> Tuple[float, float]:
        """The part's DC current from side A to side B at ``v`` volts, and its slope."""
        return self.model().dc_current(v)

    def impedance(self, omega, c_scale: float = 1.0, bias_a: float = 0.0) -> np.ndarray:
        """The model's impedance at angular frequencies ``omega``, every capacitor in it
        scaled by ``c_scale``, its diodes biased by ``bias_a`` amps from side A to B."""
        w = np.atleast_1d(np.asarray(omega, float))
        z = _impedance(self.netlist, self.subckt, float(c_scale), float(bias_a),
                       w.tobytes())
        return z.reshape(np.shape(omega)) if np.ndim(omega) else z[0]

    @staticmethod
    def from_dict(d) -> Optional["SpiceRef"]:
        if not d:
            return None
        return SpiceRef(str(d["subckt"]), str(d["netlist"]), str(d.get("file", "")),
                        bool(d.get("use", True)),
                        {str(k): float(v) for k, v in (d.get("values") or {}).items()})


@lru_cache(maxsize=256)
def _model(netlist: str, subckt: str) -> SpiceModel:
    return Library(netlist).model(subckt)


@lru_cache(maxsize=128)
def _impedance(netlist: str, subckt: str, c_scale: float, bias_a: float,
               omega: bytes) -> np.ndarray:
    # A board's capacitors share a few models, each solved once per sweep.
    w = np.frombuffer(omega, float)
    z = _model(netlist, subckt).impedance(w / (2 * np.pi), c_scale, bias_a)
    z.setflags(write=False)
    return z


def capacitor_ref(lib: Library, name: str) -> Tuple[SpiceRef, CapValues]:
    """Capacitor subcircuit ``name`` of ``lib`` to keep in a setup, and its fit."""
    v = capacitor_values(lib.model(name))
    return SpiceRef(lib.subckts[name.lower()].name, lib.netlist(name), lib.name, True,
                    {"capacitance_f": v.capacitance_f, "esl_h": v.esl_h,
                     "esr_ohm": v.esr_ohm}), v


def link_ref(lib: Library, name: str) -> Tuple[SpiceRef, LinkValues]:
    """Series part subcircuit or diode model ``name`` of ``lib`` to keep in a setup, and
    its fit."""
    v = link_values(lib.model(name))
    vals = {"resistance": v.resistance_ohm}
    if v.esl_h is not None:
        vals["esl_h"] = v.esl_h
    s = lib.subckts.get(name.lower())
    return SpiceRef(s.name if s else lib.models[name.lower()][0], lib.netlist(name),
                    lib.name, True, vals), v


def constant_drop_ref(vf: float, rs: float) -> SpiceRef:
    """A diode model for an older setup's constant forward drop ``vf`` with series
    resistance ``rs``: a junction so steep that its drop is within a few percent of
    ``vf`` from 10 mA to 1 A."""
    n = max(0.1, vf / (200 * VT))
    i_s = 0.1 / math.expm1(vf / (n * VT))
    name = f"VF{round(vf * 1000)}MV"
    lib = Library(f".model {name} D(IS={i_s:.6g} N={n:.6g} RS={rs:.6g})\n")
    ref, _v = link_ref(lib, name)
    ref.file = f"the older setup's constant {vf:g} V drop"
    return ref
