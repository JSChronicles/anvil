from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import matplotlib.pyplot as plt

OUTPUT_DIR = Path("images")
BACKGROUND = "#0d1117"
GRID = "#ffffff"
TEXT = "#f8fafc"
ANVIL = "#7c4dff"
BASELINE = "#f97316"


class BenchmarkRow(TypedDict):
    """One runtime measurement displayed by the benchmark chart."""

    group: str
    region_label: str
    minutes: float


ROWS: list[BenchmarkRow] = [
    {
        "group": "Sequential orgs\nSequential accounts",
        "region_label": "1 region",
        "minutes": 6.966666666666667,
    },
    {
        "group": "Sequential orgs\nSequential accounts",
        "region_label": "2 regions",
        "minutes": 11.133333333333333,
    },
    {
        "group": "Parallel orgs\nSequential accounts",
        "region_label": "1 region",
        "minutes": 4.483333333333333,
    },
    {
        "group": "Parallel orgs\nSequential accounts",
        "region_label": "2 regions",
        "minutes": 7.316666666666666,
    },
    {
        "group": "Parallel orgs\nParallel accounts",
        "region_label": "1 region",
        "minutes": 1.5833333333333335,
    },
    {
        "group": "Parallel orgs\nParallel accounts",
        "region_label": "2 regions",
        "minutes": 2.8,
    },
]
MANUAL_MINUTES = 195.0


def fmt_minutes(value: float) -> str:
    mins = int(value)
    secs = round((value - mins) * 60)
    if secs == 60:
        mins += 1
        secs = 0
    if secs == 0:
        return f"{mins}m"
    return f"{mins}m {secs:02d}s"


def pct_faster(old: float, new: float) -> float:
    return ((old - new) / old) * 100.0


def speedup(old: float, new: float) -> float:
    return old / new


def plot_grouped(rows: list[BenchmarkRow], *, output_path: Path) -> None:
    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(15, 7.5))

    positions = [0, 1, 3, 4, 6, 7]
    values = [float(row["minutes"]) for row in rows]
    labels = [f"{row['group']}\n{row['region_label']}" for row in rows]

    bars = ax.barh(positions, values, color=ANVIL, height=0.7)

    ax.set_yticks(positions, labels=labels)
    ax.invert_yaxis()
    ax.set_xlabel("Minutes", color=TEXT)
    ax.grid(axis="x", color=GRID, alpha=0.12)
    ax.set_axisbelow(True)
    ax.set_facecolor(BACKGROUND)
    ax.tick_params(colors=TEXT, labelsize=10)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color("#64748b")

    max_value = max(max(values), MANUAL_MINUTES)

    for bar, value in zip(bars, values, strict=True):
        ax.text(
            value + (max_value * 0.015),
            bar.get_y() + (bar.get_height() / 2),
            fmt_minutes(value),
            va="center",
            ha="left",
            color=TEXT,
            fontsize=10,
            fontweight="bold",
        )

    manual_y = -0.95
    ax.axvline(MANUAL_MINUTES, color=BASELINE, linestyle="--", linewidth=2, alpha=0.95)
    ax.text(
        MANUAL_MINUTES + (max_value * 0.015),
        manual_y,
        f"Manual estimate: {fmt_minutes(MANUAL_MINUTES)}\n(260 accounts × 45s each, 1 region)",
        color=BASELINE,
        fontsize=10,
        ha="left",
        va="center",
        fontweight="bold",
    )

    comparisons = [
        (0, 2, "1 region"),
        (1, 3, "2 regions"),
        (2, 4, "1 region"),
        (3, 5, "2 regions"),
    ]

    for left_index, right_index, region_label in comparisons:
        old_value = values[left_index]
        new_value = values[right_index]
        midpoint_y = (positions[left_index] + positions[right_index]) / 2

        note_text = (
            f"{region_label}: "
            f"{pct_faster(old_value, new_value):.1f}% faster\n"
            f"({speedup(old_value, new_value):.2f}×)"
        )

        ax.text(
            max_value * 0.72,
            midpoint_y,
            note_text,
            color=TEXT,
            fontsize=9,
            ha="left",
            va="center",
            bbox={
                "boxstyle": "round,pad=0.3",
                "facecolor": "#111827",
                "edgecolor": "#334155",
                "alpha": 0.9,
            },
        )

    fig.patch.set_facecolor(BACKGROUND)
    fig.subplots_adjust(left=0.29, right=0.97, bottom=0.12, top=0.84)

    fig.text(
        0.5,
        0.93,
        "count_vpc Runtime Comparison",
        color=TEXT,
        fontsize=16,
        ha="center",
        va="center",
    )
    fig.text(
        0.5,
        0.885,
        "Ordered by execution model instead of runtime: sequential → org parallelism → account parallelism",
        color=TEXT,
        fontsize=10,
        alpha=0.85,
        ha="center",
        va="center",
    )
    fig.text(
        0.5,
        0.848,
        "Callouts compare the same region count between each parallelism step. Manual line is a 1-region estimate only.",
        color=TEXT,
        fontsize=9,
        alpha=0.78,
        ha="center",
        va="center",
    )

    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)
    output_path = OUTPUT_DIR / "count-vpc-grouped-comparison.png"
    plot_grouped(ROWS, output_path=output_path)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
