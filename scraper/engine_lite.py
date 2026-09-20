"""
Browser-less replacement for the price-reveal portion of engine.py.

Reverse-engineered contract (verified end-to-end against
https://demo.inelabteamdev.com via real network capture + WASM
disassembly — see docs/site-notes.md and the Antigravity investigation
this was built from):

    1. GET  /api/challenge
           -> { salt, ts, difficulty, csig, wasm (base64) }
    2. Client computes:
           attHash  = sha256(att_json)
           seed     = int(sha256(SHARED_KEY + "|seed|" + salt + "|" + attHash)[:8], 16)
                      cast to signed i32
           wasmOut  = wasm.exports.f(seed)      # signed i32
           nonce    = smallest int where sha256(f"{salt}:{nonce}")
                      starts with "0" * difficulty      (hashcash PoW)
           derived  = sha256(SHARED_KEY + "|derive|" + salt + "|" +
                              str(wasmOut) + "|" + attHash)
    3. POST /api/session  with {..challenge fields, nonce, derived,
                                 wasmOut, att, productId}
           -> { token, expiresInMs }             (token expires in ~30s)
    4. GET  /api/products/{id}/price  with Authorization: Bearer <token>
           -> { productId, v, e (base64 ciphertext), serverTime }
    5. Decrypt:
           key       = sha256_bytes(SHARED_KEY + "|enc|" + token)   # 32 bytes
           plaintext[i] = ciphertext[i] ^ key[i % 32]
           json.loads(plaintext)

IMPORTANT CAVEATS (read before wiring this into production):
  - SHARED_KEY, all derivation strings, and the exact `att` telemetry
    schema were extracted from the site's shipped JS bundle and one
    successful live replay. If the mock store rotates the key, changes
    field names, or tightens telemetry validation (e.g. checking move
    smoothness/acceleration, not just count+spacing), this will break
    until re-verified against fresh traffic.
  - The synthetic `moves` array below is a static, hardcoded shape. If
    the server starts doing anything beyond "count >= 8, dwell >= 600ms,
    spacing >= 40ms", this needs to generate more varied telemetry.
  - Token TTL is ~30s: fetch price immediately after getting a session.
  - Requires `wasmtime` (pip install wasmtime) to execute the per-challenge
    WASM mixer function — there is no way around this without hand-porting
    the WASM's arithmetic ops to Python, since the constants/ops shift
    per-challenge.
"""

import argparse
import base64
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Optional

import httpx
import wasmtime

SHARED_KEY = "ine-mock-store-shared-k3y"
BASE_URL = "https://demo.inelabteamdev.com"


@dataclass
class PriceResult:
    source_product_id: str
    price: Optional[float]
    payload: Optional[dict]
    status: str  # "success" | "failed"
    error_message: Optional[str]
    elapsed_seconds: float
    status_code: Optional[int] = None  # HTTP status of the failing call, if any
    # e.g. 401 = telemetry/PoW genuinely rejected (contract may have changed);
    #      429/503 = transient rate limit / chaos injection, safe to retry patiently


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _sha256_bytes(data: str) -> bytes:
    return hashlib.sha256(data.encode("utf-8")).digest()


def _to_signed_i32(n: int) -> int:
    n &= 0xFFFFFFFF
    return n - 0x100000000 if n >= 0x80000000 else n


def _build_synthetic_telemetry(now_ms: int) -> str:
    """
    Fabricates an `att` telemetry payload matching the schema the client
    normally builds from real DOM/pointer events. Shape verified against
    a captured real request; values are synthetic but satisfy the known
    server checks (dwell >= 600ms, >= 8 moves, >= 40ms spacing).
    """
    hover_at = now_ms - 1200
    moves = []
    for i in range(14):
        moves.append([600 + i * 8, 400 + (i % 3) * 6, hover_at + i * 60])
    click_at = now_ms - 100

    att_obj = {
        "env": {
            "canvas": "7459b87235cf7f16",
            "gl": "e62f3ef246684368",
            "hc": 8,
            "scr": [1280, 800, 1],
            "frames": [16.5, 16.6, 16.7, 16.6, 16.5, 16.7, 16.6, 16.7],
            "at": now_ms,
        },
        "ix": {
            "hoverAt": hover_at,
            "dwellMs": now_ms - hover_at,
            "moves": moves,
            "clickAt": click_at,
            "trusted": True,
        },
    }
    # separators=(",", ":") to match JS's compact JSON.stringify — the
    # attHash must be computed over the exact bytes the server re-derives.
    return json.dumps(att_obj, separators=(",", ":"))


def _run_wasm_f(wasm_b64: str, seed: int) -> int:
    wasm_bytes = base64.b64decode(wasm_b64)
    engine = wasmtime.Engine()
    module = wasmtime.Module(engine, wasm_bytes)
    store = wasmtime.Store(engine)
    instance = wasmtime.Instance(store, module, [])
    f = instance.exports(store)["f"]
    out = f(store, seed)
    return _to_signed_i32(out)


def _solve_pow(salt: str, difficulty: int) -> int:
    prefix = "0" * difficulty
    nonce = 0
    while not _sha256_hex(f"{salt}:{nonce}").startswith(prefix):
        nonce += 1
    return nonce


def fetch_price(source_product_id: str, client: Optional[httpx.Client] = None) -> PriceResult:
    t_start = time.time()
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)

    try:
        # 1. Challenge
        chal = client.get(f"{BASE_URL}/api/challenge").raise_for_status().json()

        # 2. Telemetry + seed + WASM + PoW + derived
        now_ms = int(time.time() * 1000)
        att = _build_synthetic_telemetry(now_ms)
        att_hash = _sha256_hex(att)

        seed_hex = _sha256_hex(f"{SHARED_KEY}|seed|{chal['salt']}|{att_hash}")[:8]
        seed = _to_signed_i32(int(seed_hex, 16))

        wasm_out = _run_wasm_f(chal["wasm"], seed)

        nonce = _solve_pow(chal["salt"], chal["difficulty"])

        derived = _sha256_hex(
            f"{SHARED_KEY}|derive|{chal['salt']}|{wasm_out}|{att_hash}"
        )

        # 3. Session
        session_payload = {
            **chal,
            "nonce": nonce,
            "derived": derived,
            "wasmOut": wasm_out,
            "att": att,
            "productId": int(source_product_id),
        }
        sess_resp = client.post(
            f"{BASE_URL}/api/session",
            json=session_payload,
            headers={
                "Content-Type": "application/json",
                "Referer": f"{BASE_URL}/product/{source_product_id}",
            },
        )
        sess_resp.raise_for_status()
        sess_data = sess_resp.json()
        token = sess_data["token"]

        # 4. Price (fetch immediately — token TTL is ~30s)
        price_resp = client.get(
            f"{BASE_URL}/api/products/{source_product_id}/price",
            headers={"Authorization": f"Bearer {token}"},
        )
        price_resp.raise_for_status()
        price_data = price_resp.json()

        # 5. Decrypt
        key = _sha256_bytes(f"{SHARED_KEY}|enc|{token}")
        cipher = base64.b64decode(price_data["e"])
        plain = bytes(b ^ key[i % len(key)] for i, b in enumerate(cipher))
        payload = json.loads(plain.decode("utf-8"))

        return PriceResult(
            source_product_id=str(source_product_id),
            price=float(payload["p"]),
            payload=payload,
            status="success",
            error_message=None,
            elapsed_seconds=round(time.time() - t_start, 2),
        )

    except httpx.HTTPStatusError as e:
        retry_after = e.response.headers.get("Retry-After")
        msg = f"HTTP {e.response.status_code} from {e.request.url}"
        if retry_after:
            msg += f" (Retry-After: {retry_after}s)"
        return PriceResult(
            source_product_id=str(source_product_id),
            price=None,
            payload=None,
            status="failed",
            error_message=msg,
            elapsed_seconds=round(time.time() - t_start, 2),
            status_code=e.response.status_code,
        )
    except Exception as e:
        return PriceResult(
            source_product_id=str(source_product_id),
            price=None,
            payload=None,
            status="failed",
            error_message=str(e),
            elapsed_seconds=round(time.time() - t_start, 2),
            status_code=None,
        )
    finally:
        if owns_client:
            client.close()


def main():
    parser = argparse.ArgumentParser(description="Browser-less INE price fetcher")
    parser.add_argument("--product", type=str, default="1", help="Source product ID")
    args = parser.parse_args()

    result = fetch_price(args.product)
    print(json.dumps(asdict(result), indent=2))


if __name__ == "__main__":
    main()
