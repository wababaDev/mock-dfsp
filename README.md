# mockdfsp

A small, self-contained mock DFSP core banking system — built to develop and test Mojaloop Core Connectors against something with real, persistent state, instead of a stateless canned mock.

## What this is

A FastAPI + SQLite service that behaves like a real bank's API: real accounts, a real balance that actually changes, and a real general ledger recording every movement. Unlike a stateless mock (which just returns the same canned response every time), reserving funds here actually decrements a balance, committing actually finalizes it, and you can pull the ledger afterward to see exactly what happened, in order.

**One codebase, any bank.** Which bank's identity and accounts get loaded is chosen at startup via the `SEED_FILE` environment variable — nothing is hardcoded. Run the same image as BlueBank, GreenBank, SunBank, or any other participant you need, just by pointing it at a different seed file.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Which bank this instance is (name, FSP ID) — no auth required |
| `GET` | `/health` | Same identity info, for health checks — no auth required |
| `GET` | `/accounts/{account_id}` | Look up a party |
| `GET` | `/accounts/{account_id}/balance` | Current balance |
| `GET` | `/accounts/{account_id}/ledger` | Every balance-changing entry for this account, in order |
| `POST` | `/quotes` | Get a fee quote |
| `POST` | `/funds/reserve` | Payee side: create a hold — does **not** touch the balance yet |
| `POST` | `/funds/commit` | Payee side: finalize a hold — this is the only point the balance actually changes |
| `POST` | `/funds/unreserve` | Payee side: cancel a hold — nothing to undo, since nothing was ever credited |
| `POST` | `/debits/refund` | Payer side: reverse a real debit, looked up by `home_transaction_id` |
| `POST` | `/simulate/send-money` | Stands in for the app — ask for a quote (see Simulate mode below) |
| `POST` | `/simulate/accept-quote` | Stands in for the app — accept a quote; always really debits |

Every route except `/` and `/health` requires `Authorization: Bearer <AUTH_TOKEN>`. Every response follows the same envelope: `{"success": bool, "data"/"error": ...}`.

## Seed files — how a deployment becomes "a specific bank"

```
seeds/
├── bluebank.json
├── greenbank.json
└── sunbank.json
```

Each file names the bank, its FSP ID, and its seeded accounts. On first startup (an empty database), the app reads whichever file `SEED_FILE` points at and creates those accounts. Add a new bank by adding a new JSON file here in the same shape — no code changes needed.

## Configuration

```bash
cp .env.example .env
```

| Variable | Default | Purpose |
|---|---|---|
| `AUTH_TOKEN` | `1000000000` | Required as `Authorization: Bearer <token>` on every route except `/` and `/health` |
| `SEED_FILE` | `seeds/bluebank.json` | Which bank this instance is — point at a different file in `seeds/` to run as a different participant |
| `MODE` | `test` | Only affects `/simulate/*` — `test` never calls a real connector, `live` really does |
| `CORE_CONNECTOR_URL` | `http://localhost:3004` | Where the connector's DFSP-facing server is reachable, for `MODE=live` |

## Running locally

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --reload-dir app --port 4040
```

## Running with Docker

```bash
docker build -t mockdfsp:latest .
docker run -p 4040:4040 --env-file .env mockdfsp:latest
```

Run several at once, as different banks, on different ports:

```bash
docker run -p 4040:4040 -e SEED_FILE=seeds/bluebank.json --name bluebank mockdfsp:latest
docker run -p 4041:4040 -e SEED_FILE=seeds/greenbank.json --name greenbank mockdfsp:latest
```

## Reservation lifecycle (payee side)

```
reserveFunds  → a pending marker only — the customer's balance is NOT touched
commitReservedFunds → the ONLY point the balance actually changes — real credit
unreserveFunds → cancels the marker — nothing to undo, since nothing was ever credited
```

## Debit lifecycle (payer side)

```
/simulate/accept-quote → ALWAYS really debits — real money leaves immediately, no hold step
/debits/refund → reverses a completed debit, looked up by home_transaction_id
                  (never a CBS-internal ID — that's the only reference a real
                  connector actually has when a refund needs to happen)
```

## Not implemented (on purpose, for now)

- Multiple currencies per account
- Any notion of a due-diligence/compliance check
- Token expiry

Everything here is deliberately minimal — the goal is a fast, honest, stateful target to build and test a real connector against, not a full bank simulator.