"""Run reports. v1 stub: content-addressed run codes; rendering lands in
Task 21 (spec §Run reports)."""
import base64
import hashlib
import json


def run_code(inputs: dict) -> str:
    """Short base32 code content-addressing a run's canonical inputs
    (deck, annotations, combos, n, seed, until_turn, ..., engine_version).

    Identical inputs -> the same 13-char code and (per the determinism
    criterion) byte-identical results, so the code doubles as a
    reproducibility claim anyone can verify by re-running."""
    canon = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canon.encode()).digest()
    return base64.b32encode(digest[:8]).decode().rstrip("=").lower()
