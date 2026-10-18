"""Append-only transparency log: RFC 6962 Merkle tree hashing plus a linear hash chain.

Leaves are canonical JSON entries. ``leaf_hash = SHA-256(0x00 || entry)``, interior nodes
``SHA-256(0x01 || left || right)``. Each stored row also carries
``chain_hash = SHA-256(previous_chain_hash || leaf_hash)`` (computed by a database trigger),
so any rewrite of history is detectable even without recomputing the tree.
"""

from __future__ import annotations

import hashlib
from typing import Any

from app.marketplace.canonical import canonical_json

ZERO_HASH = b"\x00" * 32


def leaf_hash(entry: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + entry).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def entry_bytes(entry: dict[str, Any]) -> bytes:
    return canonical_json(entry)


def chain_hash(previous: bytes, leaf: bytes) -> bytes:
    return hashlib.sha256(previous + leaf).digest()


def _split(n: int) -> int:
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


def merkle_root(leaves: list[bytes]) -> bytes:
    """Root over leaf *hashes* (empty tree = SHA-256 of the empty string)."""
    if not leaves:
        return hashlib.sha256(b"").digest()
    if len(leaves) == 1:
        return leaves[0]
    k = _split(len(leaves))
    return node_hash(merkle_root(leaves[:k]), merkle_root(leaves[k:]))


def inclusion_proof(index: int, leaves: list[bytes]) -> list[bytes]:
    """RFC 6962 audit path for ``leaves[index]`` in the tree of all ``leaves``."""
    if not 0 <= index < len(leaves):
        raise ValueError("leaf index outside the tree")
    if len(leaves) == 1:
        return []
    k = _split(len(leaves))
    if index < k:
        return [*inclusion_proof(index, leaves[:k]), merkle_root(leaves[k:])]
    return [*inclusion_proof(index - k, leaves[k:]), merkle_root(leaves[:k])]


def verify_inclusion(
    index: int, tree_size: int, leaf: bytes, proof: list[bytes], root: bytes
) -> bool:
    """RFC 9162 §2.1.3.2 inclusion verification."""
    if not 0 <= index < tree_size:
        return False
    fn, sn = index, tree_size - 1
    current = leaf
    for sibling in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            current = node_hash(sibling, current)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            current = node_hash(current, sibling)
        fn >>= 1
        sn >>= 1
    return sn == 0 and current == root


def verify_chain(rows: list[tuple[bytes, bytes]], previous: bytes = ZERO_HASH) -> int:
    """Returns the number of chain breaks across ``(leaf_hash, chain_hash)`` rows in order."""
    breaks = 0
    for leaf, stored in rows:
        expected = chain_hash(previous, leaf)
        if expected != stored:
            breaks += 1
        previous = stored
    return breaks


def proof_document(
    seq: int, entry: str, leaves: list[bytes]
) -> dict[str, Any]:
    """JSON proof bundle for log entry ``seq`` (1-based) against the current tree."""
    index = seq - 1
    return {
        "seq": seq,
        "entry": entry,
        "leaf_hash": leaves[index].hex(),
        "tree_size": len(leaves),
        "root_hash": merkle_root(leaves).hex(),
        "proof": [node.hex() for node in inclusion_proof(index, leaves)],
    }


def verify_proof_document(document: dict[str, Any]) -> bool:
    try:
        entry = str(document["entry"]).encode("utf-8")
        leaf = bytes.fromhex(document["leaf_hash"])
        if leaf_hash(entry) != leaf:
            return False
        return verify_inclusion(
            int(document["seq"]) - 1,
            int(document["tree_size"]),
            leaf,
            [bytes.fromhex(node) for node in document["proof"]],
            bytes.fromhex(document["root_hash"]),
        )
    except (KeyError, TypeError, ValueError):
        return False
