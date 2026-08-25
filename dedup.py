"""Deduplication: perceptual hashing for images, shingle-based similarity for text.

Both checks run against (a) the current in-progress batch and (b) the SQLite
index of everything previously fetched, so re-running a similar query later
won't re-download the same photo or re-save a near-identical article.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from config import IMAGE_PHASH_HAMMING_THRESHOLD, TEXT_JACCARD_THRESHOLD
from logging_setup import get_logger

logger = get_logger(__name__)


# --- images ---

def phash_for_image(path: str) -> Optional[str]:
    try:
        import imagehash
        from PIL import Image

        with Image.open(path) as img:
            return str(imagehash.phash(img))
    except Exception as exc:  # corrupt/truncated download, unsupported format, etc.
        logger.warning("phash failed for %s: %s", path, exc)
        return None


def _hamming(a: str, b: str) -> int:
    import imagehash

    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def is_duplicate_image(phash: str, existing: list[str], threshold: int = IMAGE_PHASH_HAMMING_THRESHOLD) -> bool:
    for other in existing:
        try:
            if _hamming(phash, other) <= threshold:
                return True
        except Exception:
            continue
    return False


# --- text ---

def _normalize(text: str) -> list[str]:
    return "".join(c.lower() if c.isalnum() or c.isspace() else " " for c in text).split()


def text_signature(text: str, k_shingle: int = 5, sketch_size: int = 32) -> list[int]:
    """Bottom-k MinHash sketch: cheap approximate Jaccard similarity without
    storing full shingle sets per document."""
    tokens = _normalize(text)
    if len(tokens) < k_shingle:
        shingles = {" ".join(tokens)} if tokens else {text[:64]}
    else:
        shingles = {" ".join(tokens[i:i + k_shingle]) for i in range(len(tokens) - k_shingle + 1)}
    hashes = sorted(int(hashlib.sha1(s.encode("utf-8")).hexdigest(), 16) for s in shingles)
    return hashes[:sketch_size]


def signature_similarity(sig_a: list[int], sig_b: list[int]) -> float:
    set_a, set_b = set(sig_a), set(sig_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def is_duplicate_text(signature: list[int], existing: list[list[int]], threshold: float = TEXT_JACCARD_THRESHOLD) -> bool:
    for other in existing:
        if signature_similarity(signature, other) >= threshold:
            return True
    return False


def encode_signature(sig: list[int]) -> str:
    return ",".join(str(h) for h in sig)


def decode_signature(s: str) -> list[int]:
    return [int(x) for x in s.split(",") if x]
