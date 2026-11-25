# Tidegate — Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana

[![make.fun](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)
[![rank](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/Moodymue2903/tiny-bot/rank.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/leaderboard)
[![reward](https://make.fun/badge/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1/Moodymue2903/tiny-bot/reward.svg)](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1)

Entry for the [make.fun bounty](https://make.fun/bounty/u-gorun-gadget-tiny-bot-easy-2-beat-a48f3ce1): a paper-trading bot for hourly Solana prices that beats buy-and-hold.

## Idea

Buy-and-hold is 100% invested in all 7 symbols at all times, so the only ways
to beat it on an unseen 3-day window are (a) not be invested during a fall
and (b) concentrate in the strongest symbols during a rally. Tidegate does
both:

- a **regime gate** (equal-weight index 12h return + breadth, with
  hysteresis) takes it to cash in a falling market;
- a **momentum sleeve** buys the top 4 names by trailing 12h return while the
  regime is ON, with uncapped winners (trailing stop only, no take-profit);
- a small **dip-scalp sleeve** (RSI(14) < 30 + below the lower Bollinger
  band) harvests bounces and is designed to clear the >= 8 trade guardrail in
  a market where the momentum sleeve never fires.

## Measured results (local replay harness, kit dev window, 136 bars)

- Full dev window: net PnL **-$21** on $10,000, 12 trades, max drawdown 1.0%,
  vs buy-and-hold **-$897** (margin **+$876**).
- Split halves: +$677 and +$257 over buy-and-hold; 8 and 9 trades.
- 72-bar walk-forward windows: median margin +$294 over buy-and-hold.
- 2x-fee stress: margin +$852. Honest limit: the rally leg is unproven — on a
  synthetic mirror-image rally the margin is -$844, and no tested variant
  beats buy-and-hold in rallies without losing about twice as much in falls.
  This entry is a bet that the final Nov 27-30 window falls or stays flat.

## Run it

Python 3.13, standard library only, one file:

```sh
python3 main.py
```

It speaks `agent-fun/bars-replay/v1` on stdin/stdout. Grade it locally with
the kit:

```sh
node <kit>/cli/makefun.mjs grade-local --kit <kit> --bot . --driver process
```
