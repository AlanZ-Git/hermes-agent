"""Byte-preserving interpretation for restricted skill text edits."""

import hashlib
import re

ALGORITHM = "skill-text/v2"


def normalize_skill_text(raw: bytes) -> tuple[str, dict]:
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw[3:].decode("utf-8") if bom else raw.decode("utf-8")
    crlf = "\r\n" in text
    remainder = text.replace("\r\n", "")
    if "\r" in remainder or (crlf and "\n" in remainder):
        raise ValueError("ambiguous_skill_newlines")
    witness = {"algorithm": ALGORITHM, "bom": bom, "newline": "CRLF" if crlf else "LF",
               "preimage": hashlib.sha256(raw).hexdigest()}
    return text.replace("\r\n", "\n"), witness


def encode_skill_text(text: str, witness: dict) -> bytes:
    if "\r" in text or "﻿" in text:
        raise ValueError("ambiguous_skill_text")
    newline = "\r\n" if witness["newline"] == "CRLF" else "\n"
    return (b"\xef\xbb\xbf" if witness["bom"] else b"") + text.replace("\n", newline).encode("utf-8")


def supporting_skill_bytes(raw: bytes | None, text: str) -> bytes:
    _, witness = normalize_skill_text(raw if raw is not None else b"")
    return encode_skill_text(text, witness)


def patch_skill_bytes(raw: bytes, old: str, new: str, *, skill_md: bool) -> tuple[bytes, dict]:
    text, witness = normalize_skill_text(raw)
    if encode_skill_text(text, witness) != raw:
        raise ValueError("skill_text_roundtrip")
    if not isinstance(old, str) or not old or not isinstance(new, str) or not new or old == new:
        raise ValueError("exact_patch_required")
    if "\r" in old + new or "﻿" in old + new or text.count(old) != 1:
        raise ValueError("unique_patch_anchor")
    start = text.index(old)
    if skill_md and text.startswith("---\n"):
        fence = re.search(r"^---[ \t]*\n", text[4:], re.MULTILINE)
        if fence is None or start < 4 + fence.end():
            raise ValueError("frontmatter_immutable")
    after_text = text[:start] + new + text[start + len(old):]
    if after_text.count(new) != 1 or after_text.replace(new, old, 1) != text:
        raise ValueError("unprovable_patch_postimage")
    after = encode_skill_text(after_text, witness)
    return after, witness
