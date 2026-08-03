# Oracle questionnaire

## How is the price derived?

Anthropic's implied valuation is derived from executable perpetual-futures order
books on Binance, Bitget, and OKX. On each venue, the oracle calculates the
average execution price for buying and selling a fixed $10,000 impact notional,
takes the midpoint of those executable prices, and multiplies it by the venue's
versioned nominal share basis. A 30-second TWAP is maintained for each normalized
venue value. The cross-venue median identifies divergent observations, after
which the oracle publishes a capped equal-weight mean of at least two independent
venues.

## Any smoothing / computation?

- None: No
- EMA: Yes, only for the local mark-price basis
- SMA: No
- TWAP: Yes
- VWAP: No historical trade-volume VWAP; fixed-notional order-book execution
  VWAPs are inputs to the impact midpoint
- Median: Yes, for cross-venue outlier detection and the three-component mark
- Custom: Yes

## Smoothing details

- External source: 30-second continuous-time TWAP per venue, updated about every
  three seconds. At least 27 seconds of window coverage is required after startup
  or an outage before the source becomes eligible.
- Mark: median of external oracle; oracle plus a 150-second continuous-time EMA
  of local perp basis; and median of local best bid, best ask, and last trade.
- Funding: time average of the local impact premium across the complete one-hour
  funding interval, with at least 95% sampling coverage required. It does not
  apply a one-hour EMA to the underlying oracle.
- Termination: 60-minute external-oracle TWAP ending at the final funding
  timestamp, requiring at least 95% window coverage.

## Clamps / circuit breakers?

- Maximum source age: 10 seconds.
- Crossed, malformed, excessively wide, or insufficient-depth books: excluded.
- Source less than 10% from cross-venue median: full weight.
- Source from 10% to under 20% from median: capped at ±10%.
- Source 20% or more from median: excluded.
- At least two independent valid sources are required.
- If only two remain, their high/low divergence must be below 20%.
- Fewer than two reliable sources: disable new positions and enter reduce-only.
- Oracle and mark publications: maximum 50bps change per approximately
  three-second update, matching the trade.xyz public methodology.
- A large move confirmed by multiple venues remains the target and is reached
  through successive clamped updates.
- Rebases require a verified, versioned nominal share-basis update.
- A single venue's forced settlement or delisting value cannot settle the market.

## Update frequency

- Source order books: continuous/public venue updates.
- Oracle calculation and publication: approximately every three seconds.
- Source staleness limit: ten seconds.
- Per-venue external TWAP: 30 seconds.
- Mark basis EMA time constant: 150 seconds.
- Funding premium sampling: every oracle update; payment hourly.
- Final settlement window: 60 minutes ending at final funding.

## API endpoint(s)

- OKX: `https://www.okx.com/api/v5/market/books?instId=ANTHROPIC-USDT-SWAP&sz=400`
- Bitget: `https://api.bitget.com/api/v2/mix/market/merge-depth?symbol=ANTHROPICUSDT&productType=USDT-FUTURES&precision=scale0&limit=100`
- Binance: `https://fapi.binance.com/fapi/v1/depth?symbol=ANTHROPICUSDT&limit=1000`
- SEDA Fast testnet: `POST https://fast-api.testnet.seda.xyz/execute`

Production should consume public WebSocket order-book deltas and use these REST
endpoints for snapshots and recovery. The reference implementation uses REST so
the complete calculation remains easy to audit.

## Data format

Venue inputs are JSON order books. Local output is JSON with decimal-string
whole-USD valuations, timestamps, venue observations, source count, health state,
dispersion, confidence, clamp state, and share-basis version. SEDA output is a
16-byte, big-endian `u128` in whole USD.

## Auth required?

- OKX public market data: No.
- Bitget public market data: No.
- Binance public market data: No, subject to jurisdictional availability.
- SEDA Fast: Yes, bearer API key and deployed program ID.
- SEDA on-chain request: signing account and network fees; no exchange API key.

## Oracle provider availability

- SEDA: Yes — custom Oracle Program implemented and delivered with the
  listing-review package.
- Pyth: No confirmed standard Anthropic pre-IPO composite feed.
- Chainlink: No confirmed standard Anthropic pre-IPO composite feed.
- RedStone: No confirmed standard Anthropic pre-IPO composite feed.
- Kaiko: Potential licensed raw-market-data path, subject to commercial
  instrument-coverage confirmation; not currently integrated.
- Other: Direct public OKX, Bitget, and Binance order-book APIs.
- None / unsure: No.

## Provider details

SEDA is the implemented programmable transport, not a SEDA-maintained turnkey
Anthropic benchmark. Each execution computes the instantaneous robust impact
composite. On-chain requests use five executor replicas and the tally phase takes
their median. SEDA Fast returns a signed low-latency execution but does not by
itself provide multi-executor decentralization. The consuming relayer supplies
stateful 30-second TWAP, mark, funding, and final-settlement logic.

Pyth, Chainlink, RedStone, or Kaiko may be able to provide a commercial custom
feed, but none is assumed unless the exact instrument coverage, methodology,
timestamps, licensing, and failure behavior are contractually confirmed.

## Funding rate

- Standard Hyperliquid default: No.
- Custom: Yes.

The hourly formula follows trade.xyz:

```text
F = 0.5 × [P + clamp(r - P, -0.0005, 0.0005)]
```

`P` is the one-hour time average of the local $1,000 impact-price premium and
`r` is `0.0000125` per hour. At least 95% of the funding window must be covered,
the maximum sample gap is 15 seconds, and funding is capped at ±4% per hour.
The funding payment is `position size × oracle price × funding rate`.

The 0.5× multiplier reduces Hyperliquid-style baseline carry from approximately
11.6% to approximately 5.5% simple annualized, which is more appropriate for an
equity-like market and dampens thin weekend price discovery. Funding payments
reduce account equity and can contribute to liquidation, but liquidations use
the separate three-component mark, not the funding calculation or a local last
trade.

## Historical validation

The reproducible [historical pricing report](research/HISTORICAL_PRICING.md)
uses 62 completed common UTC daily observations from OKX, Bitget, and Binance.
Pairwise venue-return correlations are 0.967–0.989, annualized median realized
volatility is 45.5%, and the largest daily median move is −9.66%.

The report includes source-linked CSVs, checksum validation, charts, a notebook,
rebase evidence, liquidity concentration, and explicit limitations. Historical
daily closes are diagnostic only and never substitute for the live executable
impact-price oracle.
