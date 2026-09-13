"""A small Turtle reader, enough for LV2 plugin descriptions.

LV2 describes every plugin - its ports, ranges, units - in Turtle files. The
usual reader is lilv, which has no Python binding on a stock Fedora install, and
the engine promises zero dependencies. The Turtle that plugin authors write is a
narrow, regular subset of the language, so a strict reader of that subset is a
few hundred lines and fails loudly (`TurtleError`) on anything it does not know,
rather than guessing.

Terms come back as plain Python values: `IRI` and `BNode` are str subclasses so
they compare and hash cheaply, and literals are str, int, float or bool.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Iterator
from pathlib import Path

RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_FIRST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#first"
RDF_REST = "http://www.w3.org/1999/02/22-rdf-syntax-ns#rest"
RDF_NIL = "http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"


class TurtleError(ValueError):
    """The document is not Turtle this reader understands."""


class IRI(str):
    __slots__ = ()


class BNode(str):
    __slots__ = ()


Term = IRI | BNode | str | int | float | bool
Triple = tuple[IRI | BNode, IRI, Term]

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|\#[^\n]*)
  | (?P<long>\"\"\"(?:[^"\\]|\\.|"(?!""))*\"\"\"|'''(?:[^'\\]|\\.|'(?!''))*''')
  | (?P<string>"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')
  | (?P<iri><[^<>"{}|^`\\\s]*>)
  | (?P<directive>@prefix|@base)\b
  | (?P<sparql>(?i:PREFIX|BASE))(?=\s)
  | (?P<lang>@[a-zA-Z]+(?:-[a-zA-Z0-9]+)*)
  | (?P<number>[+-]?(?:\d*\.\d+(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+|\d+))
  | (?P<bnode>_:[A-Za-z0-9_][A-Za-z0-9_.-]*(?<!\.))
  | (?P<pname>(?:[A-Za-z][A-Za-z0-9_.-]*(?<!\.))?:(?:[A-Za-z0-9_:%](?:[A-Za-z0-9_.:%-]*[A-Za-z0-9_:%-])?)?)
  | (?P<dtype>\^\^)
  | (?P<punct>[\[\](),;.])
  | (?P<word>[A-Za-z]+)
    """,
    re.VERBOSE,
)

_ESCAPES = {"t": "\t", "b": "\b", "n": "\n", "r": "\r", "f": "\f", '"': '"', "'": "'", "\\": "\\"}
_ESCAPE_RE = re.compile(r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)", re.DOTALL)
XSD = "http://www.w3.org/2001/XMLSchema#"

#: Distinguishes blank nodes of separate parses. Not id(): ids are reused as soon
#: as a parser is freed, which silently merged the ports of different plugins.
_PARSES = itertools.count(1)


def _unescape(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        code = match.group(1)
        if code[0] in "uU":
            return chr(int(code[1:], 16))
        return _ESCAPES.get(code, code)

    return _ESCAPE_RE.sub(replace, text) if "\\" in text else text


def _tokens(text: str) -> Iterator[tuple[str, str, int]]:
    position = 0
    end = len(text)
    while position < end:
        match = _TOKEN.match(text, position)
        if match is None:
            line = text.count("\n", 0, position) + 1
            raise TurtleError(f"line {line}: cannot read {text[position:position + 30]!r}")
        kind = match.lastgroup or ""
        if kind != "ws":
            yield kind, match.group(), position
        position = match.end()


def _resolve(base: str, reference: str) -> str:
    """Resolve a relative IRI. LV2 only ever uses file names and fragments."""
    if not reference or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", reference):
        return reference or base
    if reference.startswith("#"):
        return base.split("#", 1)[0] + reference
    if reference.startswith("/"):
        scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*://[^/]*)", base)
        return (scheme.group(1) if scheme else "") + reference
    return base.split("#", 1)[0].rsplit("/", 1)[0] + "/" + reference


class _Parser:
    def __init__(self, text: str, base: str) -> None:
        self.tokens = list(_tokens(text))
        self.index = 0
        self.base = base
        self.prefixes: dict[str, str] = {}
        self.triples: list[Triple] = []
        self.blank = 0
        self.text = text

    # -- token helpers ------------------------------------------------------

    def peek(self) -> tuple[str, str, int] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> tuple[str, str, int]:
        token = self.peek()
        if token is None:
            raise TurtleError("unexpected end of document")
        self.index += 1
        return token

    def fail(self, token: tuple[str, str, int] | None, wanted: str) -> TurtleError:
        if token is None:
            return TurtleError(f"expected {wanted}, found end of document")
        line = self.text.count("\n", 0, token[2]) + 1
        return TurtleError(f"line {line}: expected {wanted}, found {token[1]!r}")

    def expect(self, value: str) -> None:
        token = self.next()
        if token[1] != value:
            raise self.fail(token, repr(value))

    def fresh(self) -> BNode:
        self.blank += 1
        return BNode(f"_:b{self.blank}")

    # -- grammar ------------------------------------------------------------

    def parse(self) -> list[Triple]:
        while self.peek() is not None:
            token = self.peek()
            assert token is not None
            if token[0] == "directive" or token[0] == "sparql":
                self.directive()
            else:
                self.statement()
        return self.triples

    def directive(self) -> None:
        kind, word, _ = self.next()
        sparql = kind == "sparql"
        if word.lower().lstrip("@") == "prefix":
            name = self.next()
            if name[0] != "pname" or not name[1].endswith(":"):
                raise self.fail(name, "a prefix name")
            iri = self.next()
            if iri[0] != "iri":
                raise self.fail(iri, "an IRI")
            self.prefixes[name[1][:-1]] = _resolve(self.base, _unescape(iri[1][1:-1]))
        else:
            iri = self.next()
            if iri[0] != "iri":
                raise self.fail(iri, "an IRI")
            self.base = _resolve(self.base, _unescape(iri[1][1:-1]))
        if not sparql:
            self.expect(".")

    def statement(self) -> None:
        token = self.peek()
        if token is not None and token[1] == "[":
            subject = self.blank_node_properties()
            following = self.peek()
            if following is not None and following[1] != ".":
                self.predicate_objects(subject)
        else:
            subject = self.subject()
            self.predicate_objects(subject)
        self.expect(".")

    def subject(self) -> IRI | BNode:
        token = self.next()
        if token[0] == "iri":
            return IRI(_resolve(self.base, _unescape(token[1][1:-1])))
        if token[0] == "pname":
            return self.expand(token)
        if token[0] == "bnode":
            return BNode(token[1])
        if token[1] == "(":
            return self.collection()
        raise self.fail(token, "a subject")

    def expand(self, token: tuple[str, str, int]) -> IRI:
        prefix, _, local = token[1].partition(":")
        if prefix not in self.prefixes:
            raise self.fail(token, f"a declared prefix ({prefix!r} is not)")
        local = re.sub(r"\\(.)", r"\1", local)
        return IRI(self.prefixes[prefix] + local)

    def predicate_objects(self, subject: IRI | BNode) -> None:
        while True:
            predicate = self.predicate()
            self.objects(subject, predicate)
            token = self.peek()
            if token is None or token[1] != ";":
                return
            while token is not None and token[1] == ";":
                self.next()
                token = self.peek()
            # A trailing ';' before '.' or ']' is legal and common in LV2 files.
            if token is None or token[1] in (".", "]"):
                return

    def predicate(self) -> IRI:
        token = self.next()
        if token[0] == "word" and token[1] == "a":
            return IRI(RDF_TYPE)
        if token[0] == "iri":
            return IRI(_resolve(self.base, _unescape(token[1][1:-1])))
        if token[0] == "pname":
            return self.expand(token)
        raise self.fail(token, "a predicate")

    def objects(self, subject: IRI | BNode, predicate: IRI) -> None:
        while True:
            self.triples.append((subject, predicate, self.object()))
            token = self.peek()
            if token is None or token[1] != ",":
                return
            self.next()

    def object(self) -> Term:
        token = self.next()
        kind, value, _ = token
        if kind == "iri":
            return IRI(_resolve(self.base, _unescape(value[1:-1])))
        if kind == "pname":
            return self.expand(token)
        if kind == "bnode":
            return BNode(value)
        if value == "[":
            self.index -= 1
            return self.blank_node_properties()
        if value == "(":
            return self.collection()
        if kind in ("string", "long"):
            quote = 3 if kind == "long" else 1
            return self.literal(_unescape(value[quote:-quote]))
        if kind == "number":
            if re.fullmatch(r"[+-]?\d+", value):
                return int(value)
            return float(value)
        if kind == "word" and value in ("true", "false"):
            return value == "true"
        raise self.fail(token, "an object")

    def literal(self, text: str) -> Term:
        token = self.peek()
        if token is not None and token[0] == "lang":
            self.next()
            return text
        if token is not None and token[0] == "dtype":
            self.next()
            datatype = self.next()
            if datatype[0] == "iri":
                iri = _resolve(self.base, _unescape(datatype[1][1:-1]))
            elif datatype[0] == "pname":
                iri = self.expand(datatype)
            else:
                raise self.fail(datatype, "a datatype")
            try:
                if iri in (XSD + "integer", XSD + "int", XSD + "long"):
                    return int(text)
                if iri in (XSD + "decimal", XSD + "double", XSD + "float"):
                    return float(text)
                if iri == XSD + "boolean":
                    return text.strip() in ("true", "1")
            except ValueError:
                return text
        return text

    def blank_node_properties(self) -> BNode:
        self.expect("[")
        node = self.fresh()
        token = self.peek()
        if token is not None and token[1] != "]":
            self.predicate_objects(node)
        self.expect("]")
        return node

    def collection(self) -> IRI | BNode:
        items: list[Term] = []
        while True:
            token = self.peek()
            if token is None:
                raise self.fail(token, "')'")
            if token[1] == ")":
                self.next()
                break
            items.append(self.object())
        head: IRI | BNode = IRI(RDF_NIL)
        for item in reversed(items):
            cell = self.fresh()
            self.triples.append((cell, IRI(RDF_FIRST), item))
            self.triples.append((cell, IRI(RDF_REST), head))
            head = cell
        return head


def parse(text: str, base: str = "file:///") -> list[Triple]:
    """Every triple in a Turtle document.

    Blank node labels are made unique per call, so triples from several files
    can be pooled without two files' `_:b1` colliding.
    """
    parser = _Parser(text, base)
    triples = parser.parse()
    tag = str(next(_PARSES))
    renamed: list[Triple] = []
    for s, p, o in triples:
        if isinstance(s, BNode):
            s = BNode(f"{s}.{tag}")
        if isinstance(o, BNode):
            o = BNode(f"{o}.{tag}")
        renamed.append((s, p, o))
    return renamed


def parse_file(path: Path) -> list[Triple]:
    return parse(path.read_text(encoding="utf-8", errors="replace"), path.resolve().as_uri())


class Store:
    """Triples indexed by subject, which is the only way LV2 data is walked."""

    def __init__(self) -> None:
        self._by_subject: dict[str, dict[str, list[Term]]] = {}

    def add(self, triples: list[Triple]) -> None:
        for s, p, o in triples:
            self._by_subject.setdefault(s, {}).setdefault(p, []).append(o)

    def objects(self, subject: str, predicate: str) -> list[Term]:
        return self._by_subject.get(subject, {}).get(predicate, [])

    def value(self, subject: str, predicate: str, default: Term | None = None) -> Term | None:
        found = self.objects(subject, predicate)
        return found[0] if found else default

    def types(self, subject: str) -> list[Term]:
        return self.objects(subject, RDF_TYPE)

    def subjects_of_type(self, type_iri: str) -> list[str]:
        return [s for s, props in self._by_subject.items() if type_iri in props.get(RDF_TYPE, ())]
