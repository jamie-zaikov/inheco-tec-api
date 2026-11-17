"""Duplicate-key-rejecting YAML loading for the manifest (design C2; FR-2.5, FR-2.8).

``yaml.safe_load`` keeps the last of two equal mapping keys and drops the first without a word, so
a duplicated article number or slot setting would pass unseen. :class:`UniqueKeySafeLoader` is a
``yaml.SafeLoader`` (same safe constructors, no arbitrary objects) that composes the document,
walks every mapping before construction, and raises :class:`DuplicateKeyError` with the dotted key
path of the second occurrence (for example ``article_numbers.7000179``).
"""

from __future__ import annotations

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


class DuplicateKeyError(ValueError):
    """A YAML mapping holds the same key twice; ``key_path`` names the duplicate."""

    def __init__(self, key_path: str, line: int) -> None:
        super().__init__(f"duplicate mapping key at line {line}")
        self.key_path = key_path
        self.line = line


def _join(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def _check_node(node: Node, path: str, visited: set[int]) -> None:
    """Raise :class:`DuplicateKeyError` for the first repeated key under ``node``.

    ``visited`` holds the ids of nodes already walked: an alias reuses its anchor's node, so the
    guard keeps a recursive or heavily aliased document from looping or blowing up.
    """
    if id(node) in visited:
        return
    visited.add(id(node))
    if isinstance(node, MappingNode):
        seen: set[tuple[str, str]] = set()
        for key_node, value_node in node.value:
            if isinstance(key_node, ScalarNode):
                # Tag plus text: the quoted string "1" and the integer 1 are different keys.
                identity = (key_node.tag, key_node.value)
                child = _join(path, key_node.value)
                if identity in seen:
                    raise DuplicateKeyError(child, key_node.start_mark.line + 1)
                seen.add(identity)
            else:
                child = _join(path, "?")
            _check_node(value_node, child, visited)
    elif isinstance(node, SequenceNode):
        for index, item in enumerate(node.value):
            _check_node(item, f"{path}[{index}]", visited)


class UniqueKeySafeLoader(yaml.SafeLoader):
    """``yaml.SafeLoader`` that rejects duplicate mapping keys before it constructs the data."""

    def get_single_data(self) -> object:
        node = self.get_single_node()
        if node is None:
            return None
        _check_node(node, "", set())
        return self.construct_document(node)


def load_unique(text: str) -> object:
    """Parse one YAML document safely, rejecting duplicate keys.

    Raises :class:`DuplicateKeyError` for a repeated key and ``yaml.YAMLError`` for invalid YAML.
    The loader derives from ``yaml.SafeLoader``, so this is as safe as ``yaml.safe_load``.
    """
    return yaml.load(text, Loader=UniqueKeySafeLoader)
