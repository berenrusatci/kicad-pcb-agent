"""Reading a KiCad s-expression netlist as evidence.

The netlist is the one artifact that says what is CONNECTED, as opposed to
what was drawn or what was intended. Wires can cross a symbol without
joining it and a label can sit one grid step off its pin; both look correct
on a schematic sheet and neither survives into the netlist. So anything in
this repo that wants to claim a connection reads it from here.

Kept in ``common/`` rather than beside its first caller because a second
caller appeared within the week, and a line-oriented reimplementation is
the specific way this goes wrong: kicad-cli writes a node's ``ref`` and
``pin`` on separate lines, so a regex that pairs "the last ref seen" with
"the next pin seen" works on one file's formatting and silently mispairs
on another's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from .sexpr import parse_sexpr, unquote

Node = Tuple[str, str]          # (ref, pin)
NetMap = Dict[str, List[Node]]


def read_nets(path: Path | str) -> NetMap:
    """``{net_name: [(ref, pin), ...]}`` from a KiCad netlist file."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_nets(text)


def parse_nets(text: str) -> NetMap:
    """Same as :func:`read_nets` for text already in hand."""
    tree = parse_sexpr(text)
    if not tree:
        raise ValueError("not a parseable s-expression")

    nets: NetMap = {}

    def walk(node) -> None:
        if not isinstance(node, list):
            return
        if node and node[0] == "net":
            name = None
            nodes: List[Node] = []
            for child in node[1:]:
                if not isinstance(child, list) or not child:
                    continue
                if child[0] == "name" and len(child) > 1:
                    name = unquote(child[1])
                elif child[0] == "node":
                    ref = pin = None
                    for leaf in child[1:]:
                        if isinstance(leaf, list) and len(leaf) > 1:
                            if leaf[0] == "ref":
                                ref = unquote(leaf[1])
                            elif leaf[0] == "pin":
                                pin = unquote(leaf[1])
                    if ref is not None and pin is not None:
                        nodes.append((ref, pin))
            if name is not None:
                nets.setdefault(name, []).extend(nodes)
            return
        for child in node:
            walk(child)

    walk(tree)
    return nets


ComponentMap = Dict[str, Dict[str, str]]


def read_components(path: Path | str) -> ComponentMap:
    """``{ref: {"value": ..., "footprint": ...}}`` from a KiCad netlist file."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_components(text)


def parse_components(text: str) -> ComponentMap:
    """The ``(components ...)`` half of the netlist, keyed by designator.

    The ``(nets ...)`` half says what is CONNECTED; it says nothing about
    what the connected thing IS. A check that has to divide by a resistance
    - the I²C pull-up window is the first - needs the value, and the value
    is here or it is nowhere: reading it out of the schematic file instead
    reintroduces exactly the drawn-versus-connected gap this module exists
    to close, because a symbol can carry a value and still not be joined to
    the net.

    A component with no ``value`` field keeps the key with an empty string
    rather than being dropped, so a caller can tell "no such part" from
    "part with nothing written in it".
    """
    tree = parse_sexpr(text)
    if not tree:
        raise ValueError("not a parseable s-expression")

    components: ComponentMap = {}

    def walk(node) -> None:
        if not isinstance(node, list):
            return
        if node and node[0] == "comp":
            fields: Dict[str, str] = {}
            ref = None
            for child in node[1:]:
                if not isinstance(child, list) or len(child) < 2:
                    continue
                if child[0] == "ref":
                    ref = unquote(child[1])
                elif child[0] in ("value", "footprint", "datasheet"):
                    fields[child[0]] = unquote(child[1])
            if ref is not None:
                fields.setdefault("value", "")
                components[ref] = fields
            return
        for child in node:
            walk(child)

    walk(tree)
    return components


def refs_on_net(nets: NetMap, net: str) -> List[str]:
    """Distinct refs touching ``net``, sorted. Empty for an unknown net -
    callers that care about the difference must check membership first."""
    return sorted({ref for ref, _ in nets.get(net, [])})


GENERATED_PREFIX = "Net-"
NO_CONNECT_PREFIX = "unconnected-"
AUTONAME_PREFIXES = (GENERATED_PREFIX, NO_CONNECT_PREFIX)


def is_autonamed(net: str) -> bool:
    """Did KiCad name this net, rather than the designer?

    A net joined by wire alone gets a generated name built from a component
    that happens to touch it - ``Net-(D101-IO1)``. Two consequences follow,
    and both are silent:

      * There is no stable, designer-owned address to attach anything to.
        A netclass, a ``.kicad_dru`` rule, a length-matching group and an
        interface contract all address nets by name. Binding to the generated
        string is *possible* - and boards in this repo do exactly that - but
        it binds to an accident rather than to a decision.
      * The name is not stable. It is derived from a reference designator,
        so renumbering or replacing that component renames the net, and any
        rule that HAD been bound by name comes unbound without an error.

    The first bullet used to read "nothing can be attached to it by name".
    That was too absolute and it hid the more common failure: PROC-A binds
    ``CML_DIAG`` to ``Net-(U201B-CMLOUT?)`` and ``FPD_COAX`` to
    ``Net-(D300?-A2)`` today, and CAM-W's ``CSI2`` class was bound to
    ``Net-(D10x-IOy)`` until the schematic was given contract names. An
    auditor looking only for "unbindable" nets walks straight past those -
    the rule IS bound, to a string that changes when a diode is renumbered.
    """
    return net.startswith(AUTONAME_PREFIXES)


def autonamed_nets(nets: NetMap) -> List[str]:
    """Every net KiCad named for the designer, sorted."""
    return sorted(net for net in nets if is_autonamed(net))


def generated_nets(nets: NetMap) -> List[str]:
    """Nets carrying real conductors that the designer did not name.

    Kept separate from :func:`no_connect_nets` because only this list is a
    problem. A declared no-connect is intentional and carries nothing; a
    generated ``Net-`` name means a conductor exists that no rule, netclass
    or contract can address. Reporting the two together buries ten real
    cases under eighteen harmless ones.
    """
    return sorted(net for net in nets if net.startswith(GENERATED_PREFIX))


def no_connect_nets(nets: NetMap) -> List[str]:
    """Declared no-connects. Intentional; listed for completeness only."""
    return sorted(net for net in nets if net.startswith(NO_CONNECT_PREFIX))


def refs_with_prefix(nets: NetMap, net: str, prefix: str | Iterable[str]) -> List[str]:
    """Refs on ``net`` whose designator starts with ``prefix``.

    Reference designator prefixes are a CONVENTION, not a guarantee - a
    board that names its capacitors ``CAP1`` is legal and would read as
    zero capacitors here. Callers that use this to judge a design should
    say so in their finding rather than reporting a confident zero.
    """
    prefixes = (prefix,) if isinstance(prefix, str) else tuple(prefix)
    return sorted({ref for ref in refs_on_net(nets, net)
                   if ref.startswith(prefixes)})
