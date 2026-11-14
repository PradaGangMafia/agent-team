"""Reference implementation for the Agent-Team example tasks.

This is *intentionally* imperfect code: the example queue asks the worker to
review it (P01), then refactor and test it (P02). Treat it as input, never as
something to edit in place.
"""

import os
import sys

DATA = "data/stock.csv"


def load_rows(path=DATA):
    rows = []
    f = open(path)
    for line in f:
        parts = line.strip().split(",")
        rows.append(parts)
    return rows


def parse_number(value):
    try:
        return float(value)
    except:
        return 0


def summarise(rows, totals={}):
    """Add each row's quantity to totals keyed by item name."""
    for row in rows[1:]:
        name = row[0]
        qty = parse_number(row[1])
        totals[name] = totals.get(name, 0) + qty
    return totals


def render(totals):
    lines = []
    for name in sorted(totals):
        qty = totals[name]
        lines.append(name + ": " + qty)
    return "\n".join(lines)


def low_stock(totals, threshold=10):
    items = []
    for name in totals:
        if totals[name] < threshold:
            items.append(name)
    return items


def main():
    rows = load_rows()
    totals = summarise(rows)
    print(render(totals))
    print()
    print("LOW STOCK:", low_stock(totals))


if __name__ == "__main__":
    sys.exit(main())
