from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

try:
    import matplotlib.pyplot as plt
except ImportError as error:  # pragma: no cover - import guard for local usage
    raise SystemExit(
        "matplotlib is required to generate benchmark charts.\n"
        "Install it with: uv pip install matplotlib"
    ) from error


@dataclass(frozen=True, slots=True)
class BenchmarkRow:
    label: str
    minutes: float


OUTPUT_DIR = Path("images")
BACKGROUND = "#0d1117"
GRID = "#ffffff"
TEXT = "#f8fafc"
ANVIL = "#7c4dff"
BASELINE = "#f97316"

COUNT_VPC_COMPARISON = [
    BenchmarkRow("Anvil", 138.77 / 60.0),
    BenchmarkRow("Non-Anvil sequential", 43.0),
]


def _format_minutes(value: float) -> str:
    if value < 1:
        return f"{value * 60:.2f}s"

    whole_minutes = int(value)
    seconds = round((value - whole_minutes) * 60, 2)
    if seconds == 0:
        return f"{whole_minutes}m"

    if whole_minutes == 0:
        return f"{seconds:.2f}s"

    return f"{whole_minutes}m {seconds:.2f}s"


def _style_axes(ax, *, title: str, xlabel: str) -> None:
    ax.set_xlabel(xlabel, color=TEXT)
    ax.grid(axis="x", color=GRID, alpha=0.12)
    ax.set_axisbelow(True)
    ax.set_facecolor(BACKGROUND)
    ax.tick_params(colors=TEXT)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_visible(False)
    ax.spines["bottom"].set_color("#64748b")


def plot_count_vpc_comparison(
    rows: list[BenchmarkRow], *, output_path: Path
) -> None:
    labels = [row.label for row in rows]
    values = [row.minutes for row in rows]
    colors = [BASELINE, ANVIL]

    plt.style.use("dark_background")
    fig, ax = plt.subplots(figsize=(10.5, 4.8))
    bars = ax.barh(labels, values, color=colors, height=0.56)
    ax.invert_yaxis()

    for bar, value in zip(bars, values, strict=True):
        ax.text(
            value + 0.35,
            bar.get_y() + (bar.get_height() / 2),
            _format_minutes(value),
            va="center",
            ha="left",
            color=TEXT,
            fontsize=10,
            fontweight="bold",
        )

    _style_axes(
        ax,
        title="count_vpc Runtime Comparison",
        xlabel="Minutes",
    )

    fig.patch.set_facecolor(BACKGROUND)
    plt.tight_layout()
    axes_box = ax.get_position()
    title_center_x = (axes_box.x0 + axes_box.x1) / 2
    fig.text(
        title_center_x,
        axes_box.y1 + 0.07,
        "count_vpc Runtime Comparison",
        color=TEXT,
        fontsize=15,
        ha="center",
    )
    fig.text(
        title_center_x,
        axes_box.y1 + 0.03,
        "260 accounts across 3 organizations, 2 regions",
        color=TEXT,
        fontsize=10,
        alpha=0.85,
        ha="center",
    )
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    OUTPUT_DIR.mkdir(exist_ok=True)

    comparison_path = OUTPUT_DIR / "count-vpc-runtime-comparison.png"

    plot_count_vpc_comparison(COUNT_VPC_COMPARISON, output_path=comparison_path)

    print(f"Wrote {comparison_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
