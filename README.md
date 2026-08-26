# Safe4 — the payment firewall for AI agents, as an MCP server

Safe4 decides whether an AI agent's proposed payment should be allowed, by
testing the purchase against the task the agent was actually given.

The case it exists for is the one budget limits miss: a payment that is inside
every budget, in an allowed category, and to an approved counterparty — and is
still the wrong purchase, because it does not serve the task.

This repository is the public manifest and client example for the hosted MCP
server. The service itself runs at `api.safe4.ai`; there is no server to
install.

## Connect

Streamable HTTP, no installation:

```json
{
  "mcpServers": {
    "safe4": {
      "type": "http",
      "url": "https://api.safe4.ai/mcp/"
    }
  }
}
```

Connecting and listing tools are free. Only `safe4_authorize` is paid.

## Tools

### `safe4_price` — free

Returns the current price and the payment networks the endpoint accepts, so an
agent can see the cost before committing to a paid call.

### `safe4_authorize` — paid, settled per call in USDC over x402

Returns an `ALLOW` or `DENY` decision for a proposed payment, with a reason
code, the concepts it matched, and a hash-chained audit entry.

Called without a payment it returns the x402 challenge instead of a decision.
An x402-aware client pays and calls again with the resulting payload in the
`payment` argument.

Arguments:

| Argument | Meaning |
|---|---|
| `task` | The task the agent was given, as stated by its principal |
| `purchase` | What is being bought |
| `purchase_purpose` | Why this purchase serves the task |
| `amount`, `currency` | The proposed payment |
| `counterparty` | Who would receive it |
| `service_category` | Category of the thing being bought |
| `allowed_service_categories` | Categories the principal permits |
| `allowed_counterparties` | Optional. Payees the principal permits |
| `task_id` | Optional. Echoed into the audit entry |
| `payment` | An x402 payment payload. Omit to receive the price list |

The task and the two allow-lists are the principal's constraints, not the
agent's — they are what the purchase is tested against, so an agent that writes
its own `task` is grading its own homework. Safe4 records every field it was
given and marks the task context as request-supplied, so a substituted
constraint is visible in the audit entry afterwards.

## Try it without paying

The example runs the entire free surface — connect, list tools, read the price,
fetch the challenge — and stops before signing anything. It needs only `httpx`:
no key, no funded wallet.

```bash
python examples/mcp_buyer_demo.py https://api.safe4.ai --dry-run
```

Drop `--dry-run` and set `SAFE4_BUYER_PRIVATE_KEY` to buy a real decision. That
signs an EIP-3009 authorisation for exactly the amount and payee the server
advertised, and nothing else; the script holds no custody and Safe4 never sees
the key.

## What a decision rests on

Four checks, in order, and a purchase must clear all of them:

1. **Budget and caps** — per-transaction, daily, and agent-scoped limits.
2. **Service category** — the purchase's category must be one the principal
   permitted.
3. **Counterparty** — when the task declares `allowed_counterparties`, payment
   to anyone else is refused. This is the only check that sees a swapped payee;
   task text and category are identical in that attack.
4. **Task-to-purchase match** — the task must account for what the purchase
   says it is buying, not merely share a word or two with it.

Every decision is appended to a hash-chained audit log. Each entry carries the
previous entry's hash, so the record is tamper-evident and continuous across
restarts and redeploys.

## Payment

Priced per call in USDC over [x402](https://x402.org). The endpoint advertises
its terms in the `402` challenge; buyers pay on whichever advertised network
suits them. Safe4 holds no wallet key and takes no custody of buyer funds.

## Links

- API documentation — <https://api.safe4.ai/docs>
- OpenAPI schema — <https://api.safe4.ai/openapi.json>
- x402 discovery — <https://api.safe4.ai/.well-known/x402>
- Site — <https://safe4.ai>

## Security

Reporting instructions are in [SECURITY.md](SECURITY.md). Please do not open a
public issue containing exploit details.

## License

The manifest and client examples in this repository are [MIT](LICENSE) licensed.
The hosted service they describe is a separate commercial product.
