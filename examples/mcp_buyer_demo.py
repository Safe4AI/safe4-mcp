"""Buy a Safe4 decision through the MCP tool, paying over x402.

The HTTP buyer demo (``marketplace_buyer_demo.py``) proves the endpoint can be
paid. This proves the *tool* can be paid: same decision, same settlement, but
reached through ``tools/call`` on the MCP server instead of a POST to the
route. That distinction is what a registry reviewer would check first.

**This script signs a payment with a real private key and moves real USDC when
pointed at a mainnet network.** The key is read from
``SAFE4_BUYER_PRIVATE_KEY`` in your own environment; it is never written to
disk or logged. Use a throwaway key funded with the few cents this costs.

    export SAFE4_BUYER_PRIVATE_KEY=0x...
    python examples/mcp_buyer_demo.py https://api.safe4.ai --rail vanilla --network eip155:8453

Requirements:

* ``--dry-run`` needs only ``httpx``. It connects, lists the tools, reads the
  price and fetches the challenge — the whole free surface a registry reviewer
  would check — without a key, a funded wallet, or ``eth-account``.
* Paying additionally needs ``SAFE4_BUYER_PRIVATE_KEY`` set to a hex private
  key funded with USDC on the network you choose, and
  ``pip install -r requirements-arc.txt`` for ``eth-account``.

Drop ``--network eip155:8453`` to stay on whatever the server offers first,
which is testnet. Nothing here holds custody: the signature authorises exactly
the amount and recipient the server advertised, and nothing else.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx


def _signing_helpers() -> Any:
    """Import the HTTP demo's signing helpers, and only when actually signing.

    Deferred on purpose: that module imports ``eth_account`` at module scope, so
    importing it up here would make ``--dry-run`` — the leg a reviewer runs to
    check the server without a key or a funded wallet — fail on a dependency it
    never uses.
    """

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import marketplace_buyer_demo

    return marketplace_buyer_demo


MCP_PATH = "/mcp/"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}

#: The same request the HTTP demo pays for, so the two runs are comparable.
DECISION_ARGS = {
    "task": "Research competitor pricing for the Q4 strategy deck",
    "purchase": "Market data API subscription",
    "purchase_purpose": "Pull competitor pricing data for the research",
    "amount": "25.00",
    "currency": "USDC",
    "counterparty": "market-data-provider.example",
    "service_category": "market-data",
    "allowed_service_categories": ["market-data", "research"],
}


def _result(response: httpx.Response) -> dict:
    """Read a JSON-RPC result from either transport encoding."""

    if response.status_code != 200:
        raise SystemExit(f"MCP call failed: HTTP {response.status_code} {response.text[:300]}")
    body = response.text
    if "text/event-stream" in response.headers.get("content-type", ""):
        match = re.search(r"^data: (.*)$", body, re.M)
        if match is None:
            raise SystemExit(f"No SSE data frame in MCP response: {body[:300]}")
        payload = json.loads(match.group(1))
    else:
        payload = response.json()
    if "error" in payload:
        raise SystemExit(f"MCP error: {json.dumps(payload['error'])}")
    return payload["result"]


def rpc(client: httpx.Client, endpoint: str, method: str, params: dict, request_id: int) -> dict:
    return _result(
        client.post(
            f"{endpoint}{MCP_PATH}",
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            headers=HEADERS,
        )
    )


def call_tool(
    client: httpx.Client, endpoint: str, name: str, arguments: dict, request_id: int
) -> dict:
    result = rpc(
        client, endpoint, "tools/call", {"name": name, "arguments": arguments}, request_id
    )
    return result.get("structuredContent", result)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="Base URL, e.g. https://api.safe4.ai")
    parser.add_argument(
        "--network",
        default=os.getenv("SAFE4_BUYER_NETWORK"),
        help="CAIP-2 network to pay on, e.g. eip155:8453 for Base mainnet.",
    )
    parser.add_argument(
        "--rail",
        choices=("gateway", "vanilla"),
        default=os.getenv("SAFE4_BUYER_RAIL") or None,
        help="Payment rail. 'vanilla' settles on-chain USDC through an open facilitator.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the free legs only: connect, list tools, read the price, and "
        "fetch the challenge. Signs nothing and spends nothing.",
    )
    args = parser.parse_args()

    endpoint = args.endpoint.rstrip("/")

    account: Any = None
    if not args.dry_run:
        key = os.getenv("SAFE4_BUYER_PRIVATE_KEY")
        if not key:
            raise SystemExit(
                "SAFE4_BUYER_PRIVATE_KEY is not set. Set it to a throwaway funded "
                "key, or pass --dry-run to exercise the free legs only."
            )
        from eth_account import Account

        account = Account.from_key(key)

    with httpx.Client(timeout=60.0) as client:
        print("--- connect ---")
        init = rpc(
            client,
            endpoint,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "safe4-mcp-buyer-demo", "version": "1"},
            },
            1,
        )
        print(f"  server         : {init['serverInfo']['name']} {init['serverInfo']['version']}")
        print(f"  protocol       : {init['protocolVersion']}")

        tools = rpc(client, endpoint, "tools/list", {}, 2)["tools"]
        print(f"  tools          : {[tool['name'] for tool in tools]}  (listed without paying)")
        print()

        print("--- safe4_price (free) ---")
        price = call_tool(client, endpoint, "safe4_price", {}, 3)
        print(f"  status         : {price.get('status')}")
        for entry in price.get("accepts", []):
            print(f"  offers         : {entry.get('network')}  {entry.get('amount')} atomic")
        print()

        print("--- safe4_authorize, unpaid ---")
        unpaid = call_tool(client, endpoint, "safe4_authorize", dict(DECISION_ARGS), 4)
        print(f"  status         : {unpaid.get('status')}")
        challenge = unpaid.get("x402_challenge") or {}
        if unpaid.get("status") != "payment_required":
            raise SystemExit(f"Expected payment_required, got: {json.dumps(unpaid)[:400]}")
        print(f"  challenge      : {len(challenge.get('accepts', []))} payment options")
        print()

        if args.dry_run:
            print("--- dry run: stopping before signing. Nothing was spent. ---")
            return 0

        buyer = _signing_helpers()
        terms = buyer.choose_terms(challenge, args.network, args.rail)
        print("--- paying ---")
        print(f"  network        : {terms.get('network')}")
        print(f"  amount         : {terms.get('amount')} atomic units")
        print(f"  payTo          : {terms.get('payTo')}")
        print(f"  buyer          : {account.address}")
        print()

        signed = buyer.sign_authorization(terms, account)
        header = buyer.build_payment_header(challenge, terms, signed)

        print("--- safe4_authorize, paid ---")
        paid = call_tool(
            client, endpoint, "safe4_authorize", dict(DECISION_ARGS, payment=header), 5
        )
        if paid.get("status") != "ok":
            print(f"  status         : {paid.get('status')}")
            print(f"  detail         : {json.dumps(paid)[:600]}")
            return 1

        decision = paid["decision"]
        print(f"  decision       : {decision['decision']}")
        print(f"  reason_code    : {decision['reason_code']}")
        print(f"  matched        : {decision['matched_concepts']}")
        print(
            f"  audit entry    : #{decision['audit']['sequence_number']} "
            f"{decision['audit']['entry_hash'][:16]}..."
        )
        print(f"  chained after  : {decision['audit']['previous_hash'][:16]}...")
        print(
            f"  paid           : {decision['payment']['amount']} atomic on "
            f"{decision['payment']['network']}"
        )
        print(f"  settlement tx  : {decision['payment']['transaction']}")
        print()
        print("A decision was bought and settled through the MCP tool.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
