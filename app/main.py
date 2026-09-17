"""
main.py — Blue Bank mock DFSP backend.

Payee side (money arriving for this DFSP's own customer):
  GET  /accounts/{account_id}   -> getAccountInfo
  POST /quotes                  -> getQuote
  POST /funds/reserve           -> reserveFunds   (a pending marker only — balance NOT touched yet)
  POST /funds/commit            -> commitReservedFunds (the ONLY point the balance actually changes — real credit)
  POST /funds/unreserve         -> unreserveFunds  (cancels the marker — nothing to undo, since nothing was ever credited)

Payer side (this DFSP's own customer sending money out):
  POST /debits/refund           -> handleRefund - looked up by home_transaction_id, since
                                    that's the only reference the connector ever actually has.
                                    (There's no standalone debit endpoint — the connector never
                                    calls one directly. Debits only happen via /simulate/accept-quote,
                                    matching how a real app debits its own CBS before telling the connector.)

Simulate (stands in for the DFSP's own customer-facing app):
  POST /simulate/send-money     -> ask for a quote. MODE=live really calls the
                                    connector's /send-money; MODE=test returns
                                    a canned quote in the same shape.
  POST /simulate/accept-quote   -> ALWAYS really debits (that's genuine CBS
                                    behavior either way). MODE=live really
                                    calls the connector's PUT /send-money/{id};
                                    MODE=test returns a canned accept response.

Visibility:
  GET /accounts/{account_id}/balance  -> current balance, for smoke tests
  GET /accounts/{account_id}/ledger   -> every GL entry for this account, in order

Every route requires a Bearer token (see auth.py). Every response follows the
same envelope: {"success": bool, "data"/"error": ...}

Run with: uvicorn app.main:app --reload --port 4040
"""
import os
import uuid

import httpx
from dotenv import load_dotenv

load_dotenv()  # must run BEFORE importing app.auth, since auth.py reads AUTH_TOKEN at import time

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.auth import verify_token  # noqa: E402
from app.db import get_connection, init_db, record_gl_entry, seed_accounts  # noqa: E402

app = FastAPI(title="Blue Bank Mock DFSP")

# MODE controls whether /simulate/* endpoints actually call the real core
# connector, or just return a canned response shaped like the real thing.
#   test — no network call to the connector at all
#   live — really calls the connector on CORE_CONNECTOR_URL
MODE = os.environ.get("MODE", "test")
CORE_CONNECTOR_URL = os.environ.get("CORE_CONNECTOR_URL", "http://localhost:3004")

init_db()
seed_accounts()


# ---- Consistent envelope for every response ----

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"success": False, "error": {"code": exc.status_code, "message": exc.detail}},
    )


def envelope(data: dict) -> dict:
    return {"success": True, "data": data}


# ---- Request/response shapes ----

class QuoteRequest(BaseModel):
    account_id: str
    amount: float
    currency: str


class ReserveRequest(BaseModel):
    account_id: str
    amount: float
    currency: str
    transfer_id: str


class CommitRequest(BaseModel):
    reserve_id: str


class UnreserveRequest(BaseModel):
    reserve_id: str
    reason: str | None = None


class DebitRefundRequest(BaseModel):
    home_transaction_id: str
    reason: str | None = None


# ---- Routes ----

@app.get("/accounts/{account_id}")
def get_account(account_id: str, _auth: None = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return envelope({
        "accountId": row["account_id"],
        "name": row["name"],
        "currency": row["currency"],
        "isActive": row["status"] == "active",
    })


@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: str, _auth: None = Depends(verify_token)):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()
    conn.close()

    if row is None:
        raise HTTPException(status_code=404, detail="Account not found")

    return envelope({
        "accountId": row["account_id"],
        "balance": row["balance"],
        "currency": row["currency"],
    })


@app.get("/accounts/{account_id}/ledger")
def get_ledger(account_id: str, _auth: None = Depends(verify_token)):
    """Every GL entry for this account, oldest first — the actual paper trail
    behind whatever the current balance number is."""
    conn = get_connection()
    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()

    if account is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Account not found")

    entries = conn.execute(
        "SELECT * FROM gl_entries WHERE account_id = ? ORDER BY id ASC", (account_id,)
    ).fetchall()
    conn.close()

    return envelope({
        "accountId": account_id,
        "currentBalance": account["balance"],
        "entries": [
            {
                "type": e["entry_type"],
                "amount": e["amount"],
                "balanceAfter": e["balance_after"],
                "referenceType": e["reference_type"],
                "referenceId": e["reference_id"],
                "description": e["description"],
                "createdAt": e["created_at"],
            }
            for e in entries
        ],
    })


@app.post("/quotes")
def create_quote(req: QuoteRequest, _auth: None = Depends(verify_token)):
    conn = get_connection()
    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (req.account_id,)
    ).fetchone()
    conn.close()

    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")

    fee = round(req.amount * 0.01, 2)  # flat 1% fee, purely for testing
    return envelope({
        "amount": req.amount,
        "fee": fee,
        "currency": req.currency,
    })


# ---- Payee side: reserve -> commit OR unreserve ----
# IMPORTANT: reserve does NOT touch the customer's balance at all. Bob never
# had this money — it's incoming, from outside the bank. The reservation is
# just a record ("expect this, don't act on it yet"). Only commit actually
# credits the account. This also means there's no "insufficient funds" check
# on reserve — Bob doesn't need existing funds to RECEIVE money.

@app.post("/funds/reserve")
def reserve_funds(req: ReserveRequest, _auth: None = Depends(verify_token)):
    conn = get_connection()
    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (req.account_id,)
    ).fetchone()

    if account is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Account not found")

    if account["status"] != "active":
        conn.close()
        raise HTTPException(status_code=422, detail="Account is not active")

    existing = conn.execute(
        "SELECT * FROM reservations WHERE reserve_id = ?", (req.transfer_id,)
    ).fetchone()
    if existing is not None:
        conn.close()
        raise HTTPException(status_code=409, detail="transfer_id already used for a reservation")

    conn.execute(
        "INSERT INTO reservations (reserve_id, account_id, amount, status) VALUES (?, ?, ?, 'RESERVED')",
        (req.transfer_id, req.account_id, req.amount),
    )
    record_gl_entry(
        conn, req.account_id, "PENDING", req.amount, account["balance"],
        "RESERVE", req.transfer_id, f"Incoming transfer {req.transfer_id} reserved — not yet credited",
    )
    conn.commit()
    conn.close()

    return envelope({"reserveId": req.transfer_id, "status": "RESERVED"})


@app.post("/funds/commit")
def commit_funds(req: CommitRequest, _auth: None = Depends(verify_token)):
    """This is the ONLY point on the payee side where the customer's real
    balance changes — the money genuinely arrives here, not at reserve."""
    conn = get_connection()
    reservation = conn.execute(
        "SELECT * FROM reservations WHERE reserve_id = ?", (req.reserve_id,)
    ).fetchone()

    if reservation is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Reservation not found")

    if reservation["status"] != "RESERVED":
        conn.close()
        raise HTTPException(status_code=422, detail=f"Cannot commit a reservation in state {reservation['status']}")

    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (reservation["account_id"],)
    ).fetchone()
    new_balance = account["balance"] + reservation["amount"]

    conn.execute(
        "UPDATE accounts SET balance = ? WHERE account_id = ?",
        (new_balance, reservation["account_id"]),
    )
    conn.execute(
        "UPDATE reservations SET status = 'COMMITTED' WHERE reserve_id = ?",
        (req.reserve_id,),
    )
    record_gl_entry(
        conn, reservation["account_id"], "CREDIT", reservation["amount"], new_balance,
        "COMMIT", req.reserve_id, f"Reservation {req.reserve_id} committed — funds credited",
    )
    conn.commit()
    conn.close()

    return envelope({"reserveId": req.reserve_id, "status": "COMMITTED"})


@app.post("/funds/unreserve")
def unreserve_funds(req: UnreserveRequest, _auth: None = Depends(verify_token)):
    """Cancels a hold that never completed. Since reserve never touched the
    balance, there's nothing to give back — this just closes out the pending
    marker. NOT the same as a refund, which reverses money that actually moved."""
    conn = get_connection()
    reservation = conn.execute(
        "SELECT * FROM reservations WHERE reserve_id = ?", (req.reserve_id,)
    ).fetchone()

    if reservation is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Reservation not found")

    if reservation["status"] != "RESERVED":
        conn.close()
        raise HTTPException(
            status_code=422,
            detail=f"Cannot unreserve a reservation in state {reservation['status']} — only RESERVED holds can be released",
        )

    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (reservation["account_id"],)
    ).fetchone()

    conn.execute(
        "UPDATE reservations SET status = 'RELEASED' WHERE reserve_id = ?",
        (req.reserve_id,),
    )
    # balance_after == current balance, unchanged — nothing was ever credited,
    # so there's nothing to reverse. This just closes the pending marker.
    record_gl_entry(
        conn, reservation["account_id"], "RELEASE", reservation["amount"], account["balance"],
        "UNRESERVE", req.reserve_id, f"Hold released for {req.reserve_id}: {req.reason or 'no reason given'} — no funds were ever credited",
    )
    conn.commit()
    conn.close()

    return envelope({"reserveId": req.reserve_id, "status": "RELEASED"})


# ---- Payer side: debit (immediate, real) -> refund (reverses a real debit) ----
# Keyed on home_transaction_id throughout, because that's the ONLY reference
# the connector actually has - it never sees the CBS's internal debit_id.

def perform_debit(account_id: str, amount: float, home_transaction_id: str) -> dict:
    """The actual debit logic, pulled out so both the direct /debits route
    AND the /simulate/accept-quote endpoint below can call it — one real
    implementation, not two copies that could drift apart."""
    conn = get_connection()

    existing = conn.execute(
        "SELECT * FROM debits WHERE home_transaction_id = ?", (home_transaction_id,)
    ).fetchone()
    if existing is not None:
        conn.close()
        raise HTTPException(status_code=409, detail="home_transaction_id already used for a debit")

    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (account_id,)
    ).fetchone()

    if account is None:
        conn.close()
        raise HTTPException(status_code=404, detail="Account not found")

    if account["status"] != "active":
        conn.close()
        raise HTTPException(status_code=422, detail="Account is not active")

    if account["balance"] < amount:
        conn.close()
        raise HTTPException(status_code=422, detail="Insufficient funds")

    debit_id = str(uuid.uuid4())
    new_balance = account["balance"] - amount

    conn.execute(
        "UPDATE accounts SET balance = ? WHERE account_id = ?",
        (new_balance, account_id),
    )
    conn.execute(
        "INSERT INTO debits (debit_id, home_transaction_id, account_id, amount, status) VALUES (?, ?, ?, ?, 'COMPLETED')",
        (debit_id, home_transaction_id, account_id, amount),
    )
    record_gl_entry(
        conn, account_id, "DEBIT", amount, new_balance,
        "DEBIT", home_transaction_id, f"Outgoing transfer debit for {home_transaction_id}",
    )
    conn.commit()
    conn.close()

    return {"debitId": debit_id, "homeTransactionId": home_transaction_id, "status": "COMPLETED"}


@app.post("/debits/refund")
def refund_debit(req: DebitRefundRequest, _auth: None = Depends(verify_token)):
    """Reverses a debit that already happened — real money already left the
    account, so this is a genuine second transaction giving it back, not a
    release of a hold. Looked up by home_transaction_id, since that's the
    only reference the connector ever actually saw."""
    conn = get_connection()
    debit = conn.execute(
        "SELECT * FROM debits WHERE home_transaction_id = ?", (req.home_transaction_id,)
    ).fetchone()

    if debit is None:
        conn.close()
        raise HTTPException(status_code=404, detail="No debit found for this home_transaction_id")

    if debit["status"] != "COMPLETED":
        conn.close()
        raise HTTPException(status_code=422, detail=f"Cannot refund a debit in state {debit['status']}")

    account = conn.execute(
        "SELECT * FROM accounts WHERE account_id = ?", (debit["account_id"],)
    ).fetchone()
    new_balance = account["balance"] + debit["amount"]

    conn.execute(
        "UPDATE accounts SET balance = ? WHERE account_id = ?",
        (new_balance, debit["account_id"]),
    )
    conn.execute(
        "UPDATE debits SET status = 'REFUNDED' WHERE home_transaction_id = ?",
        (req.home_transaction_id,),
    )
    record_gl_entry(
        conn, debit["account_id"], "CREDIT", debit["amount"], new_balance,
        "REFUND", req.home_transaction_id, f"Refund of {req.home_transaction_id}: {req.reason or 'no reason given'}",
    )
    conn.commit()
    conn.close()

    return envelope({
        "debitId": debit["debit_id"],
        "homeTransactionId": req.home_transaction_id,
        "status": "REFUNDED",
    })


# ---- Simulate the app: stands in for the DFSP's customer-facing app calling
# the core connector on the payer side. Lives here because the debit is real
# either way — only the connector call itself is toggled by MODE. ----

class SimulateSendMoneyRequest(BaseModel):
    payer_account_id: str
    payee_id: str
    amount: float
    send_currency: str
    receive_currency: str


class SimulateAcceptQuoteRequest(BaseModel):
    transaction_id: str
    home_transaction_id: str
    payer_account_id: str
    amount: float
    currency: str


@app.post("/simulate/send-money")
async def simulate_send_money(req: SimulateSendMoneyRequest, _auth: None = Depends(verify_token)):
    """Step 1: ask for a quote. Never touches money, in either mode."""
    home_transaction_id = f"HTX{uuid.uuid4().hex[:12].upper()}"

    payload = {
        "homeTransactionId": home_transaction_id,
        "payeeId": req.payee_id,
        "payeeIdType": "MSISDN",
        "sendAmount": f"{req.amount:.2f}",
        "sendCurrency": req.send_currency,
        "receiveCurrency": req.receive_currency,
        "purposeCode": "MP2P",
        "transactionType": "TRANSFER",
        "payer": {
            "name": "John Doe",
            "payerId": req.payer_account_id,
        },
    }

    if MODE == "live":
        async with httpx.AsyncClient() as client:
            response = await client.post(f"{CORE_CONNECTOR_URL}/send-money", json=payload, timeout=10)
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        return envelope(response.json())

    # TEST mode — canned quote, same shape a real connector response has.
    return envelope({
        "payeeDetails": {
            "idType": "MSISDN",
            "idValue": req.payee_id,
            "fspId": "airtelzambia",
            "fspLEI": "984500BA13DAVB8B6C61",
            "name": "Niza",
        },
        "sendAmount": payload["sendAmount"],
        "sendCurrency": req.send_currency,
        "receiveAmount": f"{req.amount * 0.93:.2f}",  # pretend FX/fee math
        "receiveCurrency": req.receive_currency,
        "targetFees": "10.00",
        "sourceFees": "10.00",
        "transactionId": f"TXN{uuid.uuid4().hex[:16].upper()}",
        "homeTransactionId": home_transaction_id,
    })


@app.post("/simulate/accept-quote")
async def simulate_accept_quote(req: SimulateAcceptQuoteRequest, _auth: None = Depends(verify_token)):
    """Step 2: customer agreed. The debit is ALWAYS real — that's genuine CBS
    behavior, nothing to fake there. Only the call onward to the connector is
    toggled by MODE. If it fails after this point, refunding is the
    connector's job (via /debits/refund), not this endpoint's."""
    debit_result = perform_debit(req.payer_account_id, req.amount, req.home_transaction_id)

    if MODE == "live":
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{CORE_CONNECTOR_URL}/send-money/{req.transaction_id}",
                json={"acceptQuote": True, "homeTransactionId": req.home_transaction_id},
                timeout=10,
            )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text)
        connector_response = response.json()
    else:
        # TEST mode — the debit above was still real; only the connector call is faked.
        connector_response = {"status": "ACCEPTED", "homeTransactionId": req.home_transaction_id}

    return envelope({"debit": debit_result, "connectorResponse": connector_response})