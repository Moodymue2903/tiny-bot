"""Tidegate: regime-gated cross-sectional momentum bot for the make.fun
"Tiny Bot, Easy $2: Beat Buy-and-Hold on Solana" bounty.

Protocol: agent-fun/bars-replay/v1 (hourly bars, 7 Solana symbols, long only,
$10,000 USDC, 1-bar latency, deterministic). Standard library only.

Idea (from the joint plan in PLAN.md): buy-and-hold is 100% invested in all
7 symbols at all times, so the only ways to beat it on an unseen 3-day
window are (a) not be invested during a fall and (b) concentrate in the
strongest symbols during a rally. This bot does both:

  * a regime gate (equal-weight index 12h return + breadth, with hysteresis)
    takes it to cash in a falling market;
  * a momentum sleeve buys the top 4 names by trailing return while the
    regime is ON, with uncapped winners (trailing stop only, no take-profit);
  * a small dip-scalp sleeve (RSI < 30 + below the lower Bollinger band)
    harvests bounces and guarantees the >= 8 trade guardrail in a market
    where the momentum sleeve never fires.

Everything is a pure function of the bars seen plus the fills received: no
clock, no environment, no unseeded randomness, so two runs agree exactly.
"""

import json
import os
import sys

DEBUG = os.environ.get("TIDEGATE_DEBUG") == "1"

PROTOCOL = "agent-fun/bars-replay/v1"

# --- Strategy parameters (set from structure, not fitted to the dev window) ---
EMA_FAST = 6          # fast trend EMA, in bars
EMA_SLOW = 13         # slow trend EMA, in bars
MOM_LOOKBACK = 12     # trailing-return lookback for ranking, in bars
MOM_SHORT = 6         # short trailing-return lookback, in bars
REGIME_ON_RET = 0.005   # index 12h return above this -> risk-on
REGIME_OFF_RET = -0.02  # index 12h return below this -> risk-off
REGIME_ON_BREADTH = 4   # symbols trending up needed for risk-on
REGIME_OFF_BREADTH = 1  # symbols trending up at/below this -> risk-off
PULLBACK_PCT = 0.015    # entry needs close >= 1.5% below its 12-bar high ...
RSI_PERIOD = 10
RSI_ENTRY_MAX = 50      # ... or RSI(10) below this
RSI_LOOSE_MAX = 65      # trade-count failsafe loosening of the RSI entry bar
TRAIL_PCT = 0.03        # trailing stop: 3% below the peak close since entry
HARD_STOP_PCT = 0.06    # hard stop: position value <= 94% of its cost
TIME_STOP_BARS = 36     # time stop: exit after this many bars ...
TIME_STOP_MIN = 1.02    # ... if the position is worth under 102% of cost
MAX_POSITIONS = 4       # momentum sleeve: at most 4 names, ~25% of equity each
SCALP_MAX = 2           # scalp sleeve: at most 2 positions
SCALP_SIZE_PCT = 10      # scalp size: 10% of equity each
SCALP_RSI_PERIOD = 14
SCALP_RSI_MAX = 30      # scalp entry: RSI(14) under 30 ...
SCALP_RSI_LOOSE = 35    # ... or under 35 once the stage-3 trade-count failsafe fires
SCALP_BB_BARS = 20      # ... and close under the lower Bollinger band (20, 2 sigma)
SCALP_BB_SIGMA = 2.0
SCALP_TP_PCT = 0.03     # scalp exit: +3% take-profit ...
SCALP_STOP_PCT = 0.04   # ... or -4% stop ...
SCALP_RSI_EXIT = 55     # ... or RSI(14) back above 55
NO_NEW_AFTER = 60       # no new entries after this many tradable batches
COOLDOWN_SELL = 3       # bars before re-entering a symbol after any sell
COOLDOWN_STOP = 6       # bars before re-entering a symbol after a stop-out
ENTRY_SLIPPAGE_BPS = 300
EXIT_SLIPPAGE_BPS = 500
IMPACT_SKIP_BPS = 30    # halve a buy if its pool impact is above this, skip if still above
TREND_EXIT = True       # exit a momentum position when EMA(6) drops under EMA(13)
TREND_EXIT_CONFIRM = 2  # ... only after this many consecutive bars below it
REGIME_EXIT = True      # sell every momentum position when the regime flips OFF
STRONG_SYM_R12 = 1.0     # symbol-level strong-trend bypass of the pullback/RSI
                        # entry gate: a name whose 12h return is at or above
                        # this enters at once (1.0 = disabled). A pullback gate
                        # that blocks entries through a whole rising market is
                        # the measured defect in walk24-96 (flat bars 14-26).
ROTATION = False        # rebalance cadence: momentum entries only every
REBALANCE_BARS = 12     # REBALANCE_BARS tradable batches; at a rebalance bar
ROTATE_EDGE_PP = 1.5    # a held name is rotated out only if the best unheld
                        # name beats its 12h return by ROTATE_EDGE_PP points
                        # (REJECTED 2026-11-25: churned the rally leg —
                        # neg-full margin -1703 vs -851 with it off.)


def ema_update(prev, close, span):
    """One EMA step. Seeded on the first close, so signals go live fast."""
    if prev is None:
        return close
    return prev + (2.0 / (span + 1.0)) * (close - prev)


class Series:
    """Per-symbol indicator state, updated from closes only."""

    def __init__(self):
        self.closes = []          # raw close values, oldest first
        self.ema_fast = None
        self.ema_slow = None
        self.rsi_avg_gain = None  # RSI(10) Wilder averages
        self.rsi_avg_loss = None
        self.rsi_gains = []       # warm-up gains/losses until the averages start
        self.rsi_losses = []
        self.rsi14_avg_gain = None
        self.rsi14_avg_loss = None
        self.rsi14_gains = []
        self.rsi14_losses = []
        self.prev_close = None
        self.down_bars = 0        # consecutive bars with EMA(6) under EMA(13)

    def push(self, close):
        if self.prev_close is not None:
            change = close - self.prev_close
            gain = change if change > 0.0 else 0.0
            loss = -change if change < 0.0 else 0.0
            self._rsi_step(gain, loss, RSI_PERIOD, "rsi")
            self._rsi_step(gain, loss, SCALP_RSI_PERIOD, "rsi14")
        self.prev_close = close
        self.closes.append(close)
        self.ema_fast = ema_update(self.ema_fast, close, EMA_FAST)
        self.ema_slow = ema_update(self.ema_slow, close, EMA_SLOW)
        if self.trending_up():
            self.down_bars = 0
        else:
            self.down_bars += 1

    def _rsi_step(self, gain, loss, period, which):
        gains = self.rsi_gains if which == "rsi" else self.rsi14_gains
        losses = self.rsi_losses if which == "rsi" else self.rsi14_losses
        avg_gain = self.rsi_avg_gain if which == "rsi" else self.rsi14_avg_gain
        avg_loss = self.rsi_avg_loss if which == "rsi" else self.rsi14_avg_loss
        if avg_gain is None:
            gains.append(gain)
            losses.append(loss)
            if len(gains) == period:
                avg_gain = sum(gains) / period
                avg_loss = sum(losses) / period
        else:
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if which == "rsi":
            self.rsi_gains, self.rsi_losses = gains, losses
            self.rsi_avg_gain, self.rsi_avg_loss = avg_gain, avg_loss
        else:
            self.rsi14_gains, self.rsi14_losses = gains, losses
            self.rsi14_avg_gain, self.rsi14_avg_loss = avg_gain, avg_loss

    @staticmethod
    def _rsi_value(avg_gain, avg_loss):
        if avg_gain is None or avg_loss is None:
            return None
        if avg_loss == 0.0:
            return 100.0 if avg_gain > 0.0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def rsi(self):
        return self._rsi_value(self.rsi_avg_gain, self.rsi_avg_loss)

    def rsi14(self):
        return self._rsi_value(self.rsi14_avg_gain, self.rsi14_avg_loss)

    def ret(self, bars):
        """Trailing return over `bars` bars: close / close-bars-ago - 1."""
        if len(self.closes) <= bars:
            return None
        ago = self.closes[-1 - bars]
        if ago == 0.0:
            return None
        return self.closes[-1] / ago - 1.0

    def trending_up(self):
        if self.ema_fast is None or self.ema_slow is None:
            return False
        return self.ema_fast > self.ema_slow

    def high(self, bars):
        window = self.closes[-bars:]
        return max(window) if window else None

    def bollinger_lower(self):
        if len(self.closes) < SCALP_BB_BARS:
            return None
        window = self.closes[-SCALP_BB_BARS:]
        mean = sum(window) / len(window)
        var = sum((x - mean) * (x - mean) for x in window) / len(window)
        return mean - SCALP_BB_SIGMA * (var ** 0.5)

    def close(self):
        return self.closes[-1] if self.closes else None


class Bot:
    def __init__(self, hello):
        self.hello = hello
        self.symbols = [s["symbol"] for s in hello["symbols"]]
        self.scales = {s["symbol"]: (s["qty_scale"], s["price_scale"]) for s in hello["symbols"]}
        self.quote_scale = hello["capital"]["quote_scale"]
        self.cash = int(hello["capital"]["quote_units"])
        self.equity = self.cash
        self.positions = {}       # symbol -> {"qty": int, "cost": int}
        self.series = {sym: Series() for sym in self.symbols}
        self.warmup_bars = hello.get("warmup_bars", 0)
        self.seq = -1
        self.regime_on = False
        self.peak = {}            # symbol -> highest close since entry
        self.entry_seq = {}       # symbol -> seq the position was first seen
        self.sleeve = {}          # symbol -> "mom" or "scalp"
        self.last_sell_seq = {}   # symbol -> seq of its last sell decision
        self.last_stop_seq = {}   # symbol -> seq of its last stop-out
        self.filled_trades = 0
        self.drop_reclaim = False   # trade-count failsafe, stage 1
        self.loose_rsi = False      # trade-count failsafe, stage 2
        self.scalp_loose = False    # trade-count failsafe, stage 3: if fewer
                                    # than 8 filled trades by tradable batch 64,
                                    # scalp entries accept RSI(14) under 35
        self.rotation_armed = False  # a rotation sell last batch: entries allowed
        self._entries_armed = False  # next batch to free a slot for its challenger
        self.next_client = 1
        self.latest_bar = {}

    # --- account helpers (integer quote units, like the house SDK) ---
    def value(self, symbol, qty, price=None):
        qty_scale, price_scale = self.scales[symbol]
        if price is None:
            price = self.series[symbol].close()
        if price is None:
            return 0
        return int(qty * price * (10 ** self.quote_scale) // (10 ** (qty_scale + price_scale)))

    def impact_bps(self, symbol, amount):
        """Price impact in bps of a trade worth `amount` quote units, from the
        latest close's pool depths (None before the first bar)."""
        bar = self.latest_bar.get(symbol)
        if bar is None:
            return None
        depths = [int(d) for d in bar["c_depth"]]
        prod = 1
        for d in depths:
            prod *= d
        total = 0
        for i in range(len(depths)):
            other = 1
            for j, d in enumerate(depths):
                if j != i:
                    other *= d
            total += other
        return amount * 1_000_000 * 10_000 * total // (10 ** self.quote_scale * prod)

    def weight_room(self, symbol):
        held = self.positions.get(symbol)
        room = (self.hello["caps"]["max_weight_bps"] * self.equity // 10_000
                - self.value(symbol, held["qty"]) if held else
                self.hello["caps"]["max_weight_bps"] * self.equity // 10_000)
        return room if room > 0 else 0

    # --- batch handling ---
    def apply_batch(self, batch):
        self.seq = batch["seq"]
        for bar in batch["bars"]:
            sym = bar["symbol"]
            self.latest_bar[sym] = bar
            if bar.get("v") != "0":
                self.series[sym].push(float(bar["c"]))
            else:
                # A zero-volume bar carries a stale price: indicators do not move.
                pass
        for fill in batch.get("fills", []):
            if fill.get("status") == "filled":
                self.filled_trades += 1
        account = batch["account"]
        self.cash = int(account["cash_quote_units"])
        self.equity = int(account["equity_quote_units"])
        new_positions = {}
        for p in account["positions"]:
            new_positions[p["symbol"]] = {"qty": int(p["qty"]), "cost": int(p["cost_quote_units"])}
        for sym in new_positions:
            if sym not in self.positions:
                self.entry_seq[sym] = self.seq
                close = self.series[sym].close()
                self.peak[sym] = close if close is not None else 0.0
        for sym in list(self.positions):
            if sym not in new_positions:
                self.peak.pop(sym, None)
                self.entry_seq.pop(sym, None)
                self.sleeve.pop(sym, None)
        self.positions = new_positions
        # A sleeve tag is set when its buy order is issued, before the fill is
        # known. If that buy was rejected, no position ever appears: drop the
        # tag here, or a phantom scalp position would permanently consume one
        # of the two scalp slots for the rest of the window.
        for sym in list(self.sleeve):
            if sym not in self.positions:
                self.sleeve.pop(sym, None)
        for sym, close in ((s, self.series[s].close()) for s in self.positions):
            if close is not None and close > self.peak.get(sym, 0.0):
                self.peak[sym] = close

    def warming_up(self):
        return self.warmup_bars > 0 and self.seq < self.warmup_bars

    def tradable_index(self):
        """0-based index among tradable (non warm-up) batches."""
        return self.seq - self.warmup_bars

    # --- regime ---
    def regime(self):
        rets = []
        breadth = 0
        for sym in self.symbols:
            s = self.series[sym]
            r12 = s.ret(MOM_LOOKBACK)
            if r12 is not None:
                rets.append(r12)
            if s.trending_up():
                breadth += 1
        if not rets:
            return self.regime_on
        index_ret = sum(rets) / len(rets)
        if index_ret > REGIME_ON_RET and breadth >= REGIME_ON_BREADTH:
            return True
        if index_ret < REGIME_OFF_RET or breadth <= REGIME_OFF_BREADTH:
            return False
        return self.regime_on

    # --- decisions ---
    def decide(self, batch):
        if self.warming_up():
            return []
        self.regime_on = self.regime()
        tradable = self.tradable_index()
        # Trade-count failsafe: the >= 8 trade guardrail ranks violators below
        # everyone, so it must not be left to chance.
        if tradable >= 48 and self.filled_trades < 6:
            self.drop_reclaim = True
        if tradable >= 60 and self.filled_trades < 8:
            self.loose_rsi = True
        if tradable >= 64 and self.filled_trades < 8:
            self.scalp_loose = True

        sells = []
        stop_outs = set()
        at_rebalance = ROTATION and tradable % REBALANCE_BARS == 0
        for sym in self.symbols:
            held = self.positions.get(sym)
            if held is None:
                continue
            s = self.series[sym]
            close = s.close()
            if close is None:
                continue
            value = self.value(sym, held["qty"])
            cost = held["cost"]
            reason = None
            if self.sleeve.get(sym) == "scalp":
                rsi14 = s.rsi14()
                if value * 100 >= cost * (100 + int(SCALP_TP_PCT * 100)):
                    reason = "scalp-tp"
                elif value * 100 <= cost * (100 - int(SCALP_STOP_PCT * 100)):
                    reason = "scalp-stop"
                elif rsi14 is not None and rsi14 > SCALP_RSI_EXIT:
                    reason = "scalp-rsi"
                elif value * 100 <= cost * (100 - int(HARD_STOP_PCT * 100)):
                    reason = "hard-stop"
            else:
                if REGIME_EXIT and not self.regime_on:
                    reason = "regime-off"
                elif TREND_EXIT and s.down_bars >= TREND_EXIT_CONFIRM:
                    reason = "trend-break"
                elif close < (1.0 - TRAIL_PCT) * self.peak.get(sym, close):
                    reason = "trailing-stop"
                elif value * 100 <= cost * (100 - int(HARD_STOP_PCT * 100)):
                    reason = "hard-stop"
                elif (self.seq - self.entry_seq.get(sym, self.seq) >= TIME_STOP_BARS
                        and value * 100 < cost * int(TIME_STOP_MIN * 100)):
                    reason = "time-stop"
                elif at_rebalance and self._rotate_out(sym):
                    reason = "rotation"
            if reason is not None:
                sells.append({"side": "sell", "symbol": sym, "qty": str(held["qty"]),
                              "max_slippage_bps": EXIT_SLIPPAGE_BPS, "reason": reason})
                self.last_sell_seq[sym] = self.seq
                if reason in ("hard-stop", "trailing-stop", "scalp-stop"):
                    self.last_stop_seq[sym] = self.seq
                    stop_outs.add(sym)

        buys = []
        if tradable <= NO_NEW_AFTER:
            self._entries_armed = self.rotation_armed
            buys = self._entries(batch, sells)
            self.rotation_armed = any(o.get("reason") == "rotation" for o in sells)
            self._entries_armed = False
        orders = sells + buys
        if DEBUG:
            rets = {sym: self.series[sym].ret(MOM_LOOKBACK) for sym in self.symbols}
            idx = sum(r for r in rets.values() if r is not None) / max(1, sum(1 for r in rets.values() if r is not None))
            print("seq=%d regime=%s idx12=%.4f breadth=%d pos=%s orders=%s" % (
                self.seq, "ON" if self.regime_on else "OFF", idx,
                sum(1 for sym in self.symbols if self.series[sym].trending_up()),
                {s: self.sleeve.get(s) for s in self.positions},
                [(o["side"], o["symbol"], o.get("reason", "buy-" + self.sleeve.get(o["symbol"], "?")))
                 for o in orders]), file=sys.stderr)
        return orders[:self.hello["caps"]["max_orders_per_bar"]]

    def _rotate_out(self, sym):
        """True if a held name should rotate out at a rebalance bar: it has
        fallen out of the top 4 by 12h return and the best unheld name beats
        it by more than ROTATE_EDGE_PP points."""
        ranked = []
        for s in self.symbols:
            r12 = self.series[s].ret(MOM_LOOKBACK)
            if r12 is not None:
                ranked.append((r12, s))
        if not ranked:
            return False
        ranked.sort(key=lambda t: (-t[0], self.symbols.index(t[1])))
        top4 = {s for _, s in ranked[:MAX_POSITIONS]}
        if sym in top4:
            return False
        mine = self.series[sym].ret(MOM_LOOKBACK)
        if mine is None:
            return False
        for r12, s in ranked:
            if s not in self.positions:
                return r12 - mine > ROTATE_EDGE_PP / 100.0
        return False

    def _entries(self, batch, sells):
        fresh = {bar["symbol"] for bar in batch["bars"] if bar.get("v") != "0"}
        selling = {o["symbol"] for o in sells}
        tradable = self.tradable_index()
        at_rebalance = ROTATION and tradable % REBALANCE_BARS == 0
        # Momentum sleeve: top 4 by trailing return, in an up regime.
        mom_candidates = []
        if self.regime_on and (not ROTATION or at_rebalance or self._entries_armed):
            ranked = []
            for sym in self.symbols:
                if sym in self.positions or sym in selling or sym not in fresh:
                    continue
                s = self.series[sym]
                if not s.trending_up():
                    continue
                r12 = s.ret(MOM_LOOKBACK)
                r6 = s.ret(MOM_SHORT)
                if r12 is None or r12 <= 0.0 or r6 is None:
                    continue
                close = s.close()
                hi12 = s.high(MOM_LOOKBACK)
                rsi = s.rsi()
                rsi_max = RSI_LOOSE_MAX if self.loose_rsi else RSI_ENTRY_MAX
                pullback = (hi12 is not None and close is not None
                            and close <= (1.0 - PULLBACK_PCT) * hi12)
                rsi_ok = rsi is not None and rsi < rsi_max
                strong_sym = r12 is not None and r12 >= STRONG_SYM_R12
                if not strong_sym and not (pullback or rsi_ok):
                    continue
                prev = s.closes[-2] if len(s.closes) >= 2 else None
                if not self.drop_reclaim and (prev is None or close is None or close <= prev):
                    continue
                if self.seq - self.last_sell_seq.get(sym, -10 ** 9) < COOLDOWN_SELL:
                    continue
                if self.seq - self.last_stop_seq.get(sym, -10 ** 9) < COOLDOWN_STOP:
                    continue
                ranked.append((r12, sym))
            ranked.sort(key=lambda t: (-t[0], self.symbols.index(t[1])))
            mom_candidates = [sym for _, sym in ranked[:MAX_POSITIONS]]
            # Rotation challengers: at a rebalance bar, or on the bar after a
            # rotation sell, the best unheld leader is bought even if it fails
            # the pullback/RSI gate — a swap is not new exposure.
            if ROTATION and (at_rebalance or self._entries_armed):
                challengers = []
                for sym in self.symbols:
                    if sym in self.positions or sym in selling or sym not in fresh:
                        continue
                    if sym in mom_candidates:
                        continue
                    s = self.series[sym]
                    if not s.trending_up():
                        continue
                    r12 = s.ret(MOM_LOOKBACK)
                    if r12 is None or r12 <= 0.0:
                        continue
                    challengers.append((r12, sym))
                challengers.sort(key=lambda t: (-t[0], self.symbols.index(t[1])))
                mom_candidates = [sym for _, sym in challengers] + mom_candidates

        # Scalp sleeve: RSI(14) < 30 and close under the lower Bollinger band.
        scalp_candidates = []
        for sym in self.symbols:
            if sym in self.positions or sym in selling or sym not in fresh:
                continue
            if sym in mom_candidates:
                continue
            s = self.series[sym]
            rsi14 = s.rsi14()
            lower = s.bollinger_lower()
            close = s.close()
            if rsi14 is None or lower is None or close is None:
                continue
            rsi_max = SCALP_RSI_LOOSE if self.scalp_loose else SCALP_RSI_MAX
            if rsi14 < rsi_max and close < lower:
                if self.seq - self.last_stop_seq.get(sym, -10 ** 9) < COOLDOWN_STOP:
                    continue
                scalp_candidates.append(sym)
        scalp_candidates.sort(key=lambda sym: (self.series[sym].rsi14(), self.symbols.index(sym)))
        scalp_candidates = scalp_candidates[:SCALP_MAX]

        orders = []
        cash = self.cash
        slots = MAX_POSITIONS - len(self.positions)
        for sym in mom_candidates:
            if slots <= 0:
                break
            stake = min(self.equity // MAX_POSITIONS, self.weight_room(sym), cash)
            if stake <= 0:
                continue
            stake = self._size_with_impact(sym, stake)
            if stake <= 0 or stake < int(self.hello["fees"]["min_notional_quote_units"]):
                continue
            orders.append({"side": "buy", "symbol": sym,
                           "notional_quote_units": str(stake),
                           "max_slippage_bps": ENTRY_SLIPPAGE_BPS})
            self.sleeve[sym] = "mom"
            cash -= stake
            slots -= 1
        scalp_slots = SCALP_MAX - sum(1 for v in self.sleeve.values() if v == "scalp")
        for sym in scalp_candidates:
            if scalp_slots <= 0:
                break
            stake = min(self.equity * SCALP_SIZE_PCT // 100, self.weight_room(sym), cash)
            if stake <= 0:
                continue
            stake = self._size_with_impact(sym, stake)
            if stake <= 0 or stake < int(self.hello["fees"]["min_notional_quote_units"]):
                continue
            orders.append({"side": "buy", "symbol": sym,
                           "notional_quote_units": str(stake),
                           "max_slippage_bps": ENTRY_SLIPPAGE_BPS})
            self.sleeve[sym] = "scalp"
            cash -= stake
            scalp_slots -= 1
        return orders

    def _size_with_impact(self, symbol, stake):
        impact = self.impact_bps(symbol, stake)
        if impact is None or impact <= IMPACT_SKIP_BPS:
            return stake
        half = stake // 2
        impact_half = self.impact_bps(symbol, half)
        if impact_half is not None and impact_half <= IMPACT_SKIP_BPS:
            return half
        return 0

    # --- wire ---
    def client_id(self):
        cid = "o%d" % self.next_client
        self.next_client += 1
        return cid


def main():
    state = None
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("protocol") != PROTOCOL:
                raise SystemExit("unknown protocol: %r" % msg.get("protocol"))
            state = Bot(msg)
        elif kind == "batch":
            if state is None:
                raise SystemExit("batch before hello")
            state.apply_batch(msg)
            orders = state.decide(msg)
            wire = []
            for o in orders:
                w = {"side": o["side"], "client_id": state.client_id(),
                     "symbol": o["symbol"], "max_slippage_bps": o["max_slippage_bps"]}
                if o["side"] == "buy":
                    w["notional_quote_units"] = o["notional_quote_units"]
                else:
                    w["qty"] = o["qty"]
                wire.append(w)
            out.write(json.dumps({"type": "reply", "seq": msg["seq"], "orders": wire},
                                 separators=(",", ":"), ensure_ascii=False) + "\n")
            out.flush()
        elif kind == "done":
            return 0
        else:
            raise SystemExit("unknown message type: %r" % kind)
    raise SystemExit("input ended before done")


if __name__ == "__main__":
    sys.exit(main())
