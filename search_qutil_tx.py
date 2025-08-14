#!/usr/bin/env python3
import os, json, time, random, pathlib, binascii, requests
from datetime import datetime
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

# Import configuration constants
from config import START_TICK, END_TICK, TARGET_AMOUNT

# ---------- CONFIG ----------
ARCHIVER_BASE   = os.environ.get("QUBIC_RPC_URL", "https://rpc.qubic.org")
EXPLORER_BASE   = os.environ.get("QUBIC_EXPLORER_URL", "https://explorer.qubic.org")

# Optional: filter only SC calls to a specific contract (destId). Leave empty to scan all.
# Example QUTIL destId if you know it: "EAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVWRF"
QUTIL_DEST_ID   = os.environ.get("QUTIL_DEST_ID", "EAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVWRF").strip() or None

# Politeness / files
MIN_REQUEST_INTERVAL = float(os.environ.get("REQ_INTERVAL", "0.4"))  # seconds between requests
STATE_FILE      = os.environ.get("STATE_FILE", "progress.json")
RESULTS_FILE    = os.environ.get("RESULTS_FILE", "matches.json")
# ----------------------------

session = requests.Session()
session.headers.update({
    "Accept": "application/json",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) Safari/537.36 qubic-tx-search/1.0"
})
_last_request = 0.0

def _rate_limit():
    global _last_request
    now = time.monotonic()
    wait = _last_request + MIN_REQUEST_INTERVAL - now
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()

def polite_get(url, params=None, timeout=20, retries=6):
    attempt = 0
    while True:
        _rate_limit()
        try:
            r = session.get(url, params=params, timeout=timeout)
        except requests.RequestException:
            attempt += 1
            if attempt > retries:
                raise
            time.sleep(min(30, 2 ** attempt + random.random()))
            continue

        if r.status_code == 429:
            attempt += 1
            if attempt > retries:
                r.raise_for_status()
            ra = r.headers.get("Retry-After")
            try:
                backoff = float(ra) if ra else min(30, 2 ** attempt + random.random())
            except ValueError:
                backoff = min(30, 2 ** attempt + random.random())
            print(f"[429] backing off {backoff:.1f}s for {url}")
            time.sleep(backoff)
            continue

        if r.status_code >= 500:
            attempt += 1
            if attempt > retries:
                r.raise_for_status()
            backoff = min(30, 2 ** attempt + random.random())
            print(f"[{r.status_code}] retrying in {backoff:.1f}s for {url}")
            time.sleep(backoff)
            continue

        r.raise_for_status()
        return r

def get_tick_transactions(tick: int):
    """
    Returns list of wrapped txs for a tick:
    shape: { "transactions": [ { "transaction": {...}, "timestamp": "...", "moneyFlew": bool }, ... ] }
    """
    url = f"{ARCHIVER_BASE}/v2/ticks/{tick}/transactions"
    r = polite_get(url, params={"transfers": "false", "approved": "false"})
    data = r.json()
    txs = data.get("transactions")
    return txs if isinstance(txs, list) else []

def find_tail_amounts(raw: bytes):
    """
    Scan from end for a contiguous block of 8-byte little-endian integers that look like QUBIC amounts.
    Returns (start_index, count, [amounts]).
    """
    i = len(raw)
    chunks = []
    while i >= 8:
        x = int.from_bytes(raw[i-8:i], "little", signed=False)
        if 0 < x < 10**12:  # plausible integer QUBIC amounts
            chunks.append(x)
            i -= 8
        else:
            break
    chunks.reverse()
    return i, len(chunks), chunks

def decode_qutil_outputs(input_hex: str):
    """
    Heuristic decoder for QUTIL-style payouts inside inputHex.
    Returns list of (recipient_pubkey_bytes, amount_int).
    """
    try:
        raw = binascii.unhexlify(input_hex)
    except binascii.Error:
        return []

    amt_start, n, amounts = find_tail_amounts(raw)
    if n == 0:
        return []

    addr_block = raw[amt_start - n*32 : amt_start]
    if len(addr_block) != n*32:
        return []

    recipients = [addr_block[i*32:(i+1)*32] for i in range(n)]
    return list(zip(recipients, amounts))

def load_state():
    if pathlib.Path(STATE_FILE).exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"next_tick": START_TICK}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def append_match(rec):
    existing = []
    p = pathlib.Path(RESULTS_FILE)
    if p.exists():
        try:
            with open(p, "r") as f:
                existing = json.load(f)
        except Exception:
            existing = []
    existing.append(rec)
    with open(p, "w") as f:
        json.dump(existing, f, indent=2)

def scan():
    state = load_state()
    tick = int(state.get("next_tick", START_TICK))
    end = END_TICK

    total_ticks = max(0, END_TICK - START_TICK + 1)
    initial_done = max(0, tick - START_TICK)

    print(f"Scanning ticks {tick}..{end} on {ARCHIVER_BASE} for amount {TARGET_AMOUNT:,} QUBIC")

    pbar = None
    if tqdm and total_ticks > 0:
        pbar = tqdm(total=total_ticks, initial=initial_done, unit="tick", desc="Scanning")

    while tick <= end:
        try:
            wraps = get_tick_transactions(tick)
        except Exception as e:
            print(f"[warn] tick {tick}: {e} (skipping for now)")
            tick += 1
            state["next_tick"] = tick
            save_state(state)
            if pbar: pbar.update(1)
            continue

        for w in wraps:
            tx = (w or {}).get("transaction") or {}
            if QUTIL_DEST_ID and tx.get("destId") != QUTIL_DEST_ID:
                continue
            if tx.get("inputType") != 1 or "inputHex" not in tx:
                continue

            outputs = decode_qutil_outputs(tx["inputHex"])
            if not outputs:
                continue

            txid = tx.get("txId") or tx.get("id")
            tick_no = tx.get("tickNumber", tick)
            explorer_url = f"{EXPLORER_BASE}/network/tx/{txid}?type=latest" if txid else ""

            for pk, amt in outputs:
                if amt == TARGET_AMOUNT:
                    pubkey_hex = pk.hex()
                    line = f"{tick_no} | {txid} | {pubkey_hex} | {amt:,} | {explorer_url}"
                    print(line)
                    rec = {
                        "tick": tick_no,
                        "txId": txid,
                        "pubkey_hex": pubkey_hex,
                        "amount": amt,
                        "explorer_url": explorer_url,
                        "found_at": datetime.utcnow().isoformat() + "Z",
                    }
                    append_match(rec)

        tick += 1
        state["next_tick"] = tick
        save_state(state)
        if pbar: pbar.update(1)

    if pbar: pbar.close()
    print("Done. Matches (if any) are saved in", RESULTS_FILE)


if __name__ == "__main__":
    scan()
