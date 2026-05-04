"""
3D Arbitrage Surface Renderer.

Builds an animated GIF that visualises the cross-platform arbitrage
profit landscape between Kalshi and Polymarket. The X axis is the
Kalshi YES price, the Y axis is the Polymarket YES price and the Z
axis is the net profit percentage after fees and slippage. Detected
opportunities are projected onto the surface as glowing markers so
that the operator can immediately see where the live edges sit on
the theoretical landscape.
"""

from __future__ import annotations

import io
import logging
import math
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

import imageio.v2 as imageio
import matplotlib
matplotlib.use("Agg")  # headless rendering
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import cm
from matplotlib.colors import LinearSegmentedColormap, Normalize

logger = logging.getLogger(__name__)


# Cyberpunk palette tuned to match the terminal frontend
_TERMINAL_BG = "#0a0e14"
_PANEL_BG = "#0d1117"
_TEXT_PRIMARY = "#e6edf3"
_TEXT_SECONDARY = "#8b949e"
_GRID_COLOR = "#30363d"

_ARB_CMAP = LinearSegmentedColormap.from_list(
    "arb_cyberpunk",
    [
        (0.00, "#1f6feb"),  # deep blue (loss zone)
        (0.40, "#39c5cf"),  # cyan (break-even)
        (0.55, "#d29922"),  # amber (modest edge)
        (0.75, "#3fb950"),  # green (profit)
        (1.00, "#a371f7"),  # purple highlight (extreme edge)
    ],
)


@dataclass
class ArbPoint:
    """A single opportunity projected onto the surface."""

    kalshi_yes: float
    poly_yes: float
    net_profit_pct: float
    label: str = ""


def _profit_grid(
    kalshi_axis: np.ndarray,
    poly_axis: np.ndarray,
    fee_pct: float,
    slippage_pct: float,
) -> np.ndarray:
    """Compute the net edge surface on a grid of YES prices.

    For each (k, p) in the grid we model the optimal cross-platform
    YES trade: buy on the cheaper platform, sell on the richer one.
    The metric is the absolute spread expressed in cents per dollar
    of notional, minus fees and slippage. This produces a clean
    V-valley along the diagonal (no arbitrage when prices match)
    that rises smoothly toward the corners (largest dislocations).
    """
    k, p = np.meshgrid(kalshi_axis, poly_axis, indexing="xy")
    spread_cents = np.abs(p - k) * 100.0
    return spread_cents - (fee_pct + slippage_pct)


class ArbitrageSurfaceRenderer:
    """Render the arbitrage profit surface as a rotating GIF."""

    def __init__(
        self,
        grid_size: int = 70,
        fee_pct: float = 3.0,
        slippage_pct: float = 1.0,
        frames: int = 30,
        elev: float = 32.0,
        figsize: tuple = (7.0, 5.2),
        dpi: int = 100,
        price_floor: float = 0.05,
        z_clip: float = 30.0,
    ):
        self.grid_size = grid_size
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.frames = frames
        self.elev = elev
        self.figsize = figsize
        self.dpi = dpi
        self.price_floor = price_floor
        self.z_clip = z_clip

        # Real prediction markets rarely trade below a few cents; clipping the
        # axis to [price_floor, 1 - price_floor] keeps the corner singularities
        # (where dividing by ~0 explodes) out of the visible surface.
        self._kalshi_axis = np.linspace(price_floor, 1.0 - price_floor, grid_size)
        self._poly_axis = np.linspace(price_floor, 1.0 - price_floor, grid_size)
        self._z = _profit_grid(
            self._kalshi_axis, self._poly_axis, fee_pct, slippage_pct
        )

    @property
    def z_min(self) -> float:
        return float(np.min(self._z))

    @property
    def z_max(self) -> float:
        return float(np.max(self._z))

    def _draw_frame(self, azim: float, points: Sequence[ArbPoint]) -> np.ndarray:
        fig = plt.figure(figsize=self.figsize, dpi=self.dpi, facecolor=_TERMINAL_BG)
        ax = fig.add_subplot(111, projection="3d", facecolor=_PANEL_BG)

        x, y = np.meshgrid(self._kalshi_axis, self._poly_axis, indexing="xy")
        norm = Normalize(vmin=self.z_min, vmax=max(self.z_max, 0.5))

        ax.plot_surface(
            x,
            y,
            self._z,
            cmap=_ARB_CMAP,
            norm=norm,
            linewidth=0.0,
            antialiased=True,
            alpha=0.92,
            rstride=2,
            cstride=2,
            edgecolor="none",
            shade=True,
        )

        # Break-even contour at z = 0 to highlight the edge of profit.
        try:
            ax.contour(
                x,
                y,
                self._z,
                levels=[0.0],
                colors=["#e6edf3"],
                linewidths=1.0,
                linestyles="dashed",
                offset=0.0,
            )
        except Exception:
            # contour at offset can fail on degenerate surfaces; skip silently
            pass

        # Project live opportunities onto the surface, slightly above so the
        # markers do not get hidden inside the mesh.
        if points:
            xs = [pt.kalshi_yes for pt in points]
            ys = [pt.poly_yes for pt in points]
            zs = [pt.net_profit_pct + 1.5 for pt in points]
            colors = ["#3fb950" if pt.net_profit_pct >= 0 else "#f85149" for pt in points]
            ax.scatter(
                xs,
                ys,
                zs,
                c=colors,
                s=110,
                edgecolors="#ffffff",
                linewidths=1.0,
                depthshade=False,
                zorder=10,
            )
            # Drop a vertical drop-line so the marker reads against the surface
            base_z = min(self.z_min, -self.fee_pct - self.slippage_pct - 2.0)
            for xi, yi, zi in zip(xs, ys, zs):
                ax.plot(
                    [xi, xi],
                    [yi, yi],
                    [base_z, zi],
                    color="#58a6ff",
                    linewidth=0.8,
                    alpha=0.65,
                )

        ax.set_xlabel("Kalshi YES price", color=_TEXT_SECONDARY, fontsize=9, labelpad=8)
        ax.set_ylabel("Polymarket YES price", color=_TEXT_SECONDARY, fontsize=9, labelpad=8)
        ax.set_zlabel("Net edge (cents)", color=_TEXT_SECONDARY, fontsize=9, labelpad=8)
        ax.set_title(
            "Cross-Platform Arbitrage Surface",
            color=_TEXT_PRIMARY,
            fontsize=12,
            pad=14,
            fontweight="bold",
        )

        ax.set_xlim(0.0, 1.0)
        ax.set_ylim(0.0, 1.0)
        ax.set_zlim(min(self.z_min, -self.fee_pct - self.slippage_pct - 2.0), max(self.z_max, 5.0))
        ax.view_init(elev=self.elev, azim=azim)

        for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
            axis.set_pane_color((0.04, 0.05, 0.07, 1.0))
            axis._axinfo["grid"]["color"] = _GRID_COLOR
            axis._axinfo["grid"]["linewidth"] = 0.4
            for tick in axis.get_majorticklabels():
                tick.set_color(_TEXT_SECONDARY)
                tick.set_fontsize(8)

        fig.text(
            0.02,
            0.04,
            f"fees+slippage modeled: {self.fee_pct + self.slippage_pct:.1f}%",
            color=_TEXT_SECONDARY,
            fontsize=8,
        )
        fig.text(
            0.98,
            0.04,
            f"opportunities: {len(points)}",
            color=_TEXT_SECONDARY,
            fontsize=8,
            ha="right",
        )

        fig.subplots_adjust(left=0.04, right=0.98, top=0.93, bottom=0.06)

        buf = io.BytesIO()
        fig.savefig(
            buf,
            format="png",
            facecolor=fig.get_facecolor(),
            dpi=self.dpi,
        )
        plt.close(fig)
        buf.seek(0)
        return imageio.imread(buf)

    def render(
        self,
        output_path: str,
        points: Optional[Sequence[ArbPoint]] = None,
        fps: int = 18,
    ) -> str:
        """Render the rotating surface GIF to ``output_path``.

        Args:
            output_path: Destination path for the GIF.
            points: Optional list of opportunity points to overlay.
            fps: Frames per second of the resulting animation.

        Returns:
            The absolute path of the written GIF.
        """
        points = list(points or [])
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        azimuths = np.linspace(-60, 300, self.frames, endpoint=False)
        frames = [self._draw_frame(float(az), points) for az in azimuths]

        # Loop forever (loop=0). Use a moderate quantizer for compact size.
        imageio.mimsave(output_path, frames, format="GIF", fps=fps, loop=0)
        logger.info(
            "Rendered arbitrage surface GIF: %s (%d frames, %d points)",
            output_path,
            len(frames),
            len(points),
        )
        return os.path.abspath(output_path)


def _opportunity_to_point(opp) -> Optional[ArbPoint]:
    """Best-effort projection of an ArbitrageOpportunity onto the surface."""
    try:
        matched = opp.matched_market
        kalshi_yes = float(matched.kalshi_market.yes_ask or matched.kalshi_market.yes_bid or 0)
        poly_yes = float(matched.polymarket_market.yes_price or 0)
        if kalshi_yes <= 0 or poly_yes <= 0:
            return None
        # Surface metric is the absolute spread in cents minus fees/slippage,
        # which is independent of the buy price. Project the live opportunity
        # onto the same scale so the marker sits on (or just above) the surface.
        spread_cents = abs(poly_yes - kalshi_yes) * 100.0
        fees_cents = float(getattr(opp, "estimated_fees", 0.0)) * 100.0
        net_edge = spread_cents - fees_cents - 1.0
        return ArbPoint(
            kalshi_yes=kalshi_yes,
            poly_yes=poly_yes,
            net_profit_pct=net_edge,
            label=getattr(opp, "id", ""),
        )
    except AttributeError:
        return None


def render_arbitrage_gif(
    output_path: str,
    opportunities: Optional[Iterable] = None,
    fee_pct: float = 3.0,
    slippage_pct: float = 1.0,
    frames: int = 36,
    fps: int = 18,
    grid_size: int = 60,
) -> str:
    """Convenience wrapper used by the server and CLI.

    ``opportunities`` may be ``ArbitrageOpportunity`` instances, plain
    ``ArbPoint`` instances, or dicts with the keys ``kalshi_yes``,
    ``poly_yes`` and ``net_profit_pct``. Anything we cannot interpret
    is skipped so that the surface still renders.
    """
    points: List[ArbPoint] = []
    for opp in opportunities or []:
        if isinstance(opp, ArbPoint):
            points.append(opp)
            continue
        if isinstance(opp, dict):
            try:
                points.append(
                    ArbPoint(
                        kalshi_yes=float(opp["kalshi_yes"]),
                        poly_yes=float(opp["poly_yes"]),
                        net_profit_pct=float(opp["net_profit_pct"]),
                        label=str(opp.get("label", "")),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
            continue
        projected = _opportunity_to_point(opp)
        if projected is not None:
            points.append(projected)

    renderer = ArbitrageSurfaceRenderer(
        grid_size=grid_size,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
        frames=frames,
    )
    return renderer.render(output_path, points=points, fps=fps)


def _demo_points(seed: int = 7, count: int = 8) -> List[ArbPoint]:
    """Generate a deterministic set of demo opportunities for previews."""
    rng = np.random.default_rng(seed)
    pts: List[ArbPoint] = []
    for i in range(count):
        # Bias toward visible spreads so the demo looks meaningful.
        k = float(rng.uniform(0.15, 0.85))
        offset = float(rng.choice([-1, 1]) * rng.uniform(0.04, 0.18))
        p = float(np.clip(k + offset, 0.05, 0.95))
        spread_cents = abs(p - k) * 100.0
        net_edge = spread_cents - 4.0
        pts.append(
            ArbPoint(
                kalshi_yes=k,
                poly_yes=p,
                net_profit_pct=net_edge,
                label=f"DEMO-{i + 1:02d}",
            )
        )
    return pts


if __name__ == "__main__":
    # Allow `python -m src.viz.arbitrage_surface` to refresh the demo asset.
    target = os.environ.get(
        "ARB_VIZ_OUTPUT",
        os.path.join(
            os.path.dirname(__file__), "..", "..", "frontend", "assets", "arbitrage_surface.gif"
        ),
    )
    target = os.path.abspath(target)
    render_arbitrage_gif(target, opportunities=_demo_points())
    print(f"Wrote {target}")
