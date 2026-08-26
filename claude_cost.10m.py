#!/usr/bin/env python3
# <xbar.title>Claude Code Cost Tracker</xbar.title>
# <xbar.version>1.0</xbar.version>
# <xbar.author>gcrevell</xbar.author>
# <xbar.desc>Sums local Claude Code token-usage logs and shows the current month's cost, with a 3-month breakdown by model and token type.</xbar.desc>
# <xbar.dependencies>python3</xbar.dependencies>
#
# ==============================================================================
# Claude Code Cost Tracker (SwiftBar plugin)
# ==============================================================================
#
# WHAT THIS DOES
#   Claude Code writes a JSONL transcript for every session under
#   ~/.claude/projects/<project>/<session>.jsonl. Every assistant turn in
#   those files carries a `usage` block (input/output/cache tokens) and the
#   model that served it. This script reads those files directly off disk,
#   sums tokens per model/token-type/month, and multiplies by Anthropic's
#   published list pricing to estimate cost.
#
#   This is a pure local computation - it never invokes the `claude` CLI or
#   any network request, so it's cheap to run on every refresh.
#
#   The menu bar shows the current calendar month's estimated cost. Clicking
#   it opens a breakdown for the current month plus the previous two,
#   itemized by model and token type (input / output / cache write / cache
#   read).
#
#   Costs are estimates based on published list pricing - they will not
#   exactly match your Anthropic invoice (e.g. if you're on a plan with
#   included usage, volume discounts, or Claude subscription credits rather
#   than metered API billing).
#
# INSTALL
#   1. Put this file in your SwiftBar plugin folder, keeping the "10m" in
#      the filename - that sets the refresh interval. e.g.:
#        ~/swiftbar_plugins/claude_cost.10m.py
#   2. Make it executable:
#        chmod +x claude_cost.10m.py
#   3. Refresh SwiftBar (SwiftBar menu > Refresh All).
#
# ==============================================================================

import glob
import json
import os
from datetime import datetime, timedelta

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# Per-million-token list pricing, in USD. Cache write rates are derived from
# input price (5m TTL = 1.25x, 1h TTL = 2x); cache read = 0.1x input price.
# Source: Anthropic published pricing (see claude-api skill / platform.claude.com/docs/en/pricing).


def _rates(input_price, output_price):
    return {
        "input": input_price,
        "output": output_price,
        "cache_write_5m": input_price * 1.25,
        "cache_write_1h": input_price * 2.0,
        "cache_read": input_price * 0.1,
    }


_OPUS_CURRENT = _rates(5.00, 25.00)      # Opus 5, Opus 4.8, 4.7, 4.6
_OPUS_LEGACY = _rates(15.00, 75.00)      # Opus 4.5, 4.1, 4.0 and older
_SONNET_CURRENT = _rates(3.00, 15.00)    # Sonnet 4.6, 4.5, 4.0
_SONNET_5_INTRO = _rates(2.00, 10.00)    # Sonnet 5 introductory pricing (through 2026-08-31)
_SONNET_5_REGULAR = _rates(3.00, 15.00)  # Sonnet 5 pricing from 2026-09-01
_HAIKU = _rates(1.00, 5.00)
_FABLE = _rates(10.00, 50.00)            # Fable 5 / Mythos 5

# Sonnet 5's intro pricing window ends 2026-08-31 (inclusive), UTC.
_SONNET_5_INTRO_CUTOFF = datetime(2026, 9, 1, tzinfo=None)

_STATIC_PRICING = {
    "claude-opus-5": _OPUS_CURRENT,
    "claude-opus-4-8": _OPUS_CURRENT,
    "claude-opus-4-7": _OPUS_CURRENT,
    "claude-opus-4-6": _OPUS_CURRENT,
    "claude-opus-4-5": _OPUS_LEGACY,
    "claude-opus-4-5-20251101": _OPUS_LEGACY,
    "claude-opus-4-1": _OPUS_LEGACY,
    "claude-opus-4-1-20250805": _OPUS_LEGACY,
    "claude-opus-4-0": _OPUS_LEGACY,
    "claude-opus-4-20250514": _OPUS_LEGACY,
    "claude-sonnet-4-6": _SONNET_CURRENT,
    "claude-sonnet-4-5": _SONNET_CURRENT,
    "claude-sonnet-4-5-20250929": _SONNET_CURRENT,
    "claude-sonnet-4-0": _SONNET_CURRENT,
    "claude-sonnet-4-20250514": _SONNET_CURRENT,
    "claude-haiku-4-5": _HAIKU,
    "claude-haiku-4-5-20251001": _HAIKU,
    "claude-fable-5": _FABLE,
    "claude-mythos-5": _FABLE,
}

TOKEN_TYPE_LABELS = [
    ("input", "Input"),
    ("output", "Output"),
    ("cache_write_5m", "Cache write (5m)"),
    ("cache_write_1h", "Cache write (1h)"),
    ("cache_read", "Cache read"),
]


def rates_for(model, ts_utc):
    if model == "claude-sonnet-5":
        return _SONNET_5_INTRO if ts_utc < _SONNET_5_INTRO_CUTOFF else _SONNET_5_REGULAR
    return _STATIC_PRICING.get(model)


def parse_timestamp(raw):
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def month_key_local(dt_utc):
    local_dt = dt_utc.astimezone()  # convert to system local timezone
    return local_dt.strftime("%Y-%m"), local_dt


def scan_usage():
    """Returns {month_key: {model: {token_type: count}}} and the set of months seen with a display label."""
    pattern = os.path.join(CLAUDE_PROJECTS_DIR, "*", "*.jsonl")
    usage = {}
    seen_message_ids = set()

    for path in glob.glob(pattern):
        try:
            fh = open(path, "r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue

                if entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                model = message.get("model")
                if not model or model == "<synthetic>":
                    continue
                token_usage = message.get("usage")
                if not isinstance(token_usage, dict):
                    continue

                msg_id = message.get("id")
                if msg_id:
                    if msg_id in seen_message_ids:
                        continue
                    seen_message_ids.add(msg_id)

                ts = parse_timestamp(entry.get("timestamp"))
                if ts is None:
                    continue

                month_key, _local_dt = month_key_local(ts)

                cache_creation = token_usage.get("cache_creation") or {}
                cache_write_5m = cache_creation.get("ephemeral_5m_input_tokens")
                cache_write_1h = cache_creation.get("ephemeral_1h_input_tokens")
                if cache_write_5m is None and cache_write_1h is None:
                    # No breakdown available - treat all cache writes as 5m TTL (the default).
                    cache_write_5m = token_usage.get("cache_creation_input_tokens", 0) or 0
                    cache_write_1h = 0

                counts = {
                    "input": token_usage.get("input_tokens", 0) or 0,
                    "output": token_usage.get("output_tokens", 0) or 0,
                    "cache_write_5m": cache_write_5m or 0,
                    "cache_write_1h": cache_write_1h or 0,
                    "cache_read": token_usage.get("cache_read_input_tokens", 0) or 0,
                }

                model_bucket = usage.setdefault(month_key, {}).setdefault(
                    model, {k: 0 for k, _ in TOKEN_TYPE_LABELS}
                )
                for token_type in model_bucket:
                    model_bucket[token_type] += counts[token_type]

    return usage


def model_cost(model, counts, month_key):
    # Use the middle of the month as a representative timestamp for pricing
    # lookups that depend on date (e.g. Sonnet 5 intro pricing).
    year, month = (int(x) for x in month_key.split("-"))
    approx_ts = datetime(year, month, 15)
    rates = rates_for(model, approx_ts)
    if not rates:
        return 0.0
    total = 0.0
    for token_type, count in counts.items():
        total += (count / 1_000_000.0) * rates.get(token_type, 0.0)
    return total


def month_total(month_models):
    return sum(model_cost(model, counts, mk) for mk, model, counts in month_models)


def fmt_usd(amount):
    if amount == 0:
        return "$0.00"
    if amount < 0.01:
        return "${:.4f}".format(amount)
    return "${:,.2f}".format(amount)


def fmt_tok(n):
    return "{:,}".format(n)


def last_n_month_keys(n):
    today = datetime.now()
    keys = []
    y, m = today.year, today.month
    for _ in range(n):
        keys.append("{:04d}-{:02d}".format(y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return keys


def month_label(month_key):
    y, m = (int(x) for x in month_key.split("-"))
    return datetime(y, m, 1).strftime("%B %Y")


def main():
    usage = scan_usage()
    months = last_n_month_keys(3)
    current_month_key = months[0]

    current_total = 0.0
    if current_month_key in usage:
        for model, counts in usage[current_month_key].items():
            current_total += model_cost(model, counts, current_month_key)

    print("\U0001F916 {}".format(fmt_usd(current_total)))
    print("---")
    print("Claude Code Cost Tracker")
    print("Estimated from local usage logs · list pricing, may differ from your invoice | size=10 color=gray")
    print("Updated {} | size=10 color=gray".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
    print("---")

    for month_key in months:
        month_data = usage.get(month_key, {})
        total = 0.0
        for model, counts in month_data.items():
            total += model_cost(model, counts, month_key)

        label = month_label(month_key)
        if month_key == current_month_key:
            label += " (current)"
        print("\U0001F4C5 {} — {}".format(label, fmt_usd(total)))

        if not month_data:
            print("--No usage recorded | size=11 color=gray")
        else:
            for model in sorted(month_data.keys(), key=lambda m: -model_cost(m, month_data[m], month_key)):
                counts = month_data[model]
                cost = model_cost(model, counts, month_key)
                print("--{} — {} | size=12".format(model, fmt_usd(cost)))
                for token_type, token_label in TOKEN_TYPE_LABELS:
                    count = counts.get(token_type, 0)
                    if count == 0:
                        continue
                    rates = rates_for(model, datetime(*(int(x) for x in month_key.split("-")), 15))
                    rate = rates.get(token_type, 0.0) if rates else 0.0
                    line_cost = (count / 1_000_000.0) * rate
                    print(
                        "----{}: {} tok — {} | size=11 color=gray".format(
                            token_label, fmt_tok(count), fmt_usd(line_cost)
                        )
                    )
        print("---")

    print("Refresh | refresh=true")
    print("Open ~/.claude/projects | bash=/usr/bin/open param1=" + CLAUDE_PROJECTS_DIR + " terminal=false")


if __name__ == "__main__":
    main()
