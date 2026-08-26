"""Buy one payment authorization decision from Safe4, as an external agent would.

This is the buyer side of ``docs/product/MARKETPLACE_CONTRACT.md``. It performs
the full x402 v2 loop against a live deployment: an unpaid call to collect the
`402` price list, an EIP-3009 authorization signed against the advertised terms,
and a retry carrying `PAYMENT-SIGNATURE`. It then does it a second time with a
purchase that does not serve the task, so both an ALLOW and a DENY are observed
from the same paid endpoint.

    python examples/marketplace_buyer_demo.py https://api.safe4.ai

**Your private key is never handled by this repository's maintainers, printed,
or logged.** It is read from ``SAFE4_BUYER_PRIVATE_KEY`` in your own
environment, used locally by ``eth_account`` to sign, and never transmitted:
only the resulting signature goes over the wire, which is the whole point of
EIP-3009. Use a throwaway funded wallet, not a wallet you care about.

The endpoint may advertise two rails in the same price list, told apart by the
``extra`` block of each ``accepts[]`` entry:

* **Circle Gateway** entries name ``GatewayWalletBatched`` and settle from your
  off-chain Gateway balance (fund it with
  ``examples/marketplace_gateway_deposit.py``).
* **Vanilla x402** entries name the USDC contract's own domain and settle
  on-chain from your wallet's plain USDC balance — no Gateway deposit involved.
  Testnet USDC on Base Sepolia comes from <https://faucet.circle.com>.

Pick a rail with ``--rail gateway`` or ``--rail vanilla``; the default takes the
first entry the endpoint offers on the chosen network. On the vanilla rail this
script also echoes the endpoint's ``bazaar`` discovery extension into the
payment, which is how facilitators that support cataloging index the endpoint.

Prerequisites:

* ``SAFE4_BUYER_PRIVATE_KEY`` set to a hex private key.
* Funds matching the rail: a Circle Gateway balance for Gateway entries, or
  on-chain USDC for vanilla entries. Without funds the settlement fails closed:
  no decision is returned and nothing is charged.
* ``pip install eth-account httpx`` (both are already in requirements.txt).

Each successful decision costs the advertised price, so this script buys exactly
two: one ALLOW and one DENY.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data


AUTHORIZE_PATH = "/v1/authorize"

#: Gateway rejects an authorization valid for less than seven days.
MIN_VALIDITY_SECONDS = 604_800
VALIDITY_BUFFER_SECONDS = 3_600

MATCHING_REQUEST = {
    "task": "Research competitor pricing for the Q4 strategy deck",
    "purchase": "Market data API subscription",
    "purchase_purpose": "Pull competitor pricing data for the research",
    "amount": "25.00",
    "currency": "USDC",
    "counterparty": "market-data-provider.example",
    "service_category": "market-data",
    "allowed_service_categories": ["market-data", "research"],
    "task_id": "task-4417",
    "agent_id": "marketplace-buyer-demo",
}

#: Identical amount, category, and counterparty. Only the purpose differs, which
#: is the whole claim: this is the payment ordinary controls would wave through.
MISMATCHED_REQUEST = dict(
    MATCHING_REQUEST,
    purchase="Gift card",
    purchase_purpose="Buy a gift card for the team",
)


def fetch_challenge(client: httpx.Client, endpoint: str, body: dict) -> dict:
    """Make the unpaid call and return the x402 price list."""

    response = client.post(f"{endpoint}{AUTHORIZE_PATH}", json=body)
    if response.status_code != 402:
        raise SystemExit(
            f"Expected 402 from an unpaid call, got {response.status_code}: "
            f"{response.text[:400]}"
        )
    return response.json()


def _entry_rail(entry: dict) -> str:
    """Which rail one ``accepts[]`` entry belongs to.

    Gateway entries carry ``extra.verifyingContract`` because the buyer signs
    Circle's contract domain; vanilla entries omit it because the buyer signs
    the asset's own domain.
    """

    extra = entry.get("extra") or {}
    return "gateway" if "verifyingContract" in extra else "vanilla"


def choose_terms(challenge: dict, preferred_network: str | None, rail: str | None) -> dict:
    """Pick which advertised option to pay."""

    accepts = challenge.get("accepts") or []
    if not accepts:
        raise SystemExit("The 402 challenge carried no payment options.")
    candidates = [
        entry
        for entry in accepts
        if (not preferred_network or entry.get("network") == preferred_network)
        and (not rail or _entry_rail(entry) == rail)
    ]
    if not candidates:
        available = ", ".join(
            f"{entry.get('network')} ({_entry_rail(entry)})" for entry in accepts
        )
        raise SystemExit(
            f"No advertised option matches network={preferred_network or 'any'} "
            f"rail={rail or 'any'}. Offered: {available}"
        )
    return candidates[0]


def sign_authorization(terms: dict, account: Any) -> dict:
    """Sign an EIP-3009 TransferWithAuthorization against the advertised terms.

    The signature commits to the exact amount and recipient the server
    advertised. Signing anything else is pointless: the server settles against
    its own terms and Circle reports the mismatch.
    """

    chain_id = int(str(terms["network"]).split(":", 1)[1])
    now = int(time.time())
    # Gateway requires a validity of at least seven days; vanilla facilitators
    # broadcast promptly and expect a short window bounded by the advertised
    # maxTimeoutSeconds.
    if _entry_rail(terms) == "gateway":
        valid_before = now + MIN_VALIDITY_SECONDS + VALIDITY_BUFFER_SECONDS
    else:
        valid_before = now + min(int(terms.get("maxTimeoutSeconds", 600)), 600)
    authorization = {
        "from": account.address,
        "to": terms["payTo"],
        "value": int(terms["amount"]),
        "validAfter": 0,
        "validBefore": valid_before,
        "nonce": "0x" + secrets.token_hex(32),
    }

    typed_data = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        # Gateway terms name Circle's contract as the verifying domain; vanilla
        # terms sign against the asset contract itself.
        "domain": {
            "name": terms["extra"]["name"],
            "version": terms["extra"]["version"],
            "chainId": chain_id,
            "verifyingContract": terms["extra"].get("verifyingContract", terms["asset"]),
        },
        "message": authorization,
    }

    signed = account.sign_message(encode_typed_data(full_message=typed_data))
    return {
        "signature": signed.signature.hex()
        if signed.signature.hex().startswith("0x")
        else "0x" + signed.signature.hex(),
        "authorization": {
            "from": authorization["from"],
            "to": authorization["to"],
            "value": str(authorization["value"]),
            "validAfter": str(authorization["validAfter"]),
            "validBefore": str(authorization["validBefore"]),
            "nonce": authorization["nonce"],
        },
    }


def build_payment_header(challenge: dict, terms: dict, signed: dict) -> str:
    payload = {
        "x402Version": challenge.get("x402Version", 2),
        "resource": challenge.get("resource", {}),
        "accepted": terms,
        "payload": signed,
    }
    # Echoing the endpoint's discovery extension is how Bazaar cataloging
    # happens: facilitators that support it index the endpoint on settlement.
    if challenge.get("extensions"):
        payload["extensions"] = challenge["extensions"]
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def buy_decision(
    client: httpx.Client,
    endpoint: str,
    body: dict,
    account: Any,
    preferred_network: str | None,
    rail: str | None,
) -> tuple[int, dict]:
    challenge = fetch_challenge(client, endpoint, body)
    terms = choose_terms(challenge, preferred_network, rail)
    signed = sign_authorization(terms, account)
    header = build_payment_header(challenge, terms, signed)

    response = client.post(
        f"{endpoint}{AUTHORIZE_PATH}",
        json=body,
        headers={"PAYMENT-SIGNATURE": header},
    )
    # Bazaar cataloging outcome, when the settling facilitator supports the
    # discovery extension. Absence means the facilitator does not catalog.
    extension_responses = response.headers.get("EXTENSION-RESPONSES")
    if extension_responses:
        try:
            decoded = json.loads(base64.b64decode(extension_responses))
        except (ValueError, TypeError):
            decoded = {"raw": extension_responses}
        print(f"  discovery      : {json.dumps(decoded)}")
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"raw": response.text[:400]}


def report(label: str, status: int, body: dict) -> str | None:
    print(f"--- {label} ---")
    print(f"HTTP {status}")
    if status != 200:
        print(f"  refused: {body.get('error')}  {body.get('message', '')}")
        if body.get("details"):
            print(f"  details: {json.dumps(body['details'])}")
        return None

    print(f"  decision       : {body['decision']}")
    print(f"  reason_code    : {body['reason_code']}")
    print(f"  reason         : {body['reason']}")
    print(f"  matched        : {body['matched_concepts']}")
    # Both hashes, because the link between them is the claim: entry_hash is
    # computed over the entry *including* previous_hash, so a reader can check
    # that consecutive decisions chain — including across a service restart,
    # which is what durable storage is for.
    print(f"  audit entry    : #{body['audit']['sequence_number']} {body['audit']['entry_hash'][:16]}...")
    print(f"  chained after  : {body['audit']['previous_hash'][:16]}...")
    print(f"  paid           : {body['payment']['amount']} atomic on {body['payment']['network']}")
    print(f"  settlement tx  : {body['payment']['transaction']}")
    print()
    return str(body["decision"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("endpoint", help="Base URL, e.g. https://api.safe4.ai")
    parser.add_argument(
        "--network",
        default=os.getenv("SAFE4_BUYER_NETWORK"),
        help="CAIP-2 network to pay on, e.g. eip155:5042002. Defaults to the first offered.",
    )
    parser.add_argument(
        "--rail",
        choices=("gateway", "vanilla"),
        default=os.getenv("SAFE4_BUYER_RAIL") or None,
        help=(
            "Payment rail: 'gateway' settles from a Circle Gateway balance, "
            "'vanilla' settles on-chain USDC through an open x402 facilitator. "
            "Defaults to the first entry offered."
        ),
    )
    args = parser.parse_args()

    key = os.getenv("SAFE4_BUYER_PRIVATE_KEY")
    if not key:
        print(
            "SAFE4_BUYER_PRIVATE_KEY is not set. Set it to a throwaway funded "
            "wallet's key; it is used locally to sign and is never transmitted.",
            file=sys.stderr,
        )
        return 2

    account = Account.from_key(key)
    endpoint = args.endpoint.rstrip("/")
    print(f"Buyer address : {account.address}")
    print(f"Endpoint      : {endpoint}")
    print()

    with httpx.Client(timeout=60.0) as client:
        allow_status, allow_body = buy_decision(
            client, endpoint, MATCHING_REQUEST, account, args.network, args.rail
        )
        allow = report("Purchase that serves the task", allow_status, allow_body)

        deny_status, deny_body = buy_decision(
            client, endpoint, MISMATCHED_REQUEST, account, args.network, args.rail
        )
        deny = report(
            "Same amount, category and counterparty - different purpose",
            deny_status,
            deny_body,
        )

    if allow == "ALLOW" and deny == "DENY":
        print("Observed one ALLOW and one DENY from the paid endpoint.")
        return 0

    print(
        "Did not observe both an ALLOW and a DENY. "
        f"Got {allow!r} and {deny!r}.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
