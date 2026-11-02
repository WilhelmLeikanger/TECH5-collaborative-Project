"""Partial decoder for TimeEdit's scrambled URL keys (``ri1Q56.html``).

TimeEdit hides query strings behind keys such as ``ri1Q56``.  The scheme is a
substitution cipher: common query fragments are replaced by short tokens,
then every character is mapped through a fixed table, then characters inside
each 50-char block are swapped pairwise using a fixed index pattern.

Only the parts needed to recover the search id (``sid``) from a short key are
implemented here.  The pairwise block swaps never touch the first four
characters for keys this short, so short keys decode exactly.

``ri1Q56`` → ``1Q56`` → ``6=12`` → ``h=t&sid=12``.
"""

from __future__ import annotations

import re

#: Fixed character pairs (self-inverse).
_PAIRS = {"=": "Q", "&": "Z", ",": "X", ".": "Y", " ": "V", "-": "W"}
_PAIRS_INV = {v: k for k, v in _PAIRS.items()}

#: Query-fragment tokens applied before the character mapping (``find`` → ``token``).
_TOKENS = [("h=t&sid=", "6="), ("objects=", "1="), ("sid=", "2="), ("&h=t&p=", "3=")]


def _unmap_char(ch: str) -> str:
    """Invert the per-character substitution."""
    if ch in _PAIRS_INV:
        return _PAIRS_INV[ch]
    if ch in _PAIRS:
        return _PAIRS[ch]
    if "a" <= ch <= "z":  # forward: chr(97 + ((ord(ch) - 88) % 26)) == shift +9
        return chr(97 + ((ord(ch) - 97 - 9) % 26))
    if "1" <= ch <= "9":  # forward: chr(49 + ((ord(ch) - 45) % 9)) == shift +4 within 1..9
        return chr(49 + ((ord(ch) - 49 - 4) % 9))
    return ch


def _map_char(ch: str) -> str:
    if ch in _PAIRS:
        return _PAIRS[ch]
    if ch in _PAIRS_INV:
        return _PAIRS_INV[ch]
    if "a" <= ch <= "z":
        return chr(97 + ((ord(ch) - 88) % 26))
    if "1" <= ch <= "9":
        return chr(49 + ((ord(ch) - 45) % 9))
    return ch


def decode_key(key: str) -> str:
    """Best-effort decode of a short scrambled key (without the ``ri`` prefix).

    Works exactly for keys shorter than the first swapped index pair.  Longer
    keys are decoded approximately (block swaps are not undone).
    """
    key = key.strip()
    if key.endswith(".html") or key.endswith(".json"):
        key = key.rsplit(".", 1)[0]
    if key.startswith("ri") and len(key) > 2 and key[2] in "0123456789":
        key = key[2:]
    text = "".join(_unmap_char(ch) for ch in key)
    for find, token in _TOKENS:
        text = text.replace(token, find)
    return text


def encode_query(query: str) -> str:
    """Encode a short query string (inverse of :func:`decode_key`)."""
    text = query
    for find, token in _TOKENS:
        text = text.replace(find, token)
    return "".join(_map_char(ch) for ch in text)


def sid_from_key(key: str) -> int | None:
    """Extract ``sid`` from a scrambled key such as ``ri1Q56.html``."""
    match = re.search(r"sid=(\d+)", decode_key(key))
    return int(match.group(1)) if match else None
