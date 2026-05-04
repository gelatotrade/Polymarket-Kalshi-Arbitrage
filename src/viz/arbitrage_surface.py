"""
3D Arbitrage Surface Renderer.

Plots two 3D surfaces in the same axes — one for Kalshi YES prices
and one for Polymarket YES prices — as a function of (market_index,
time). The vertical gap between the surfaces at any (market, time)
is the cross-platform arbitrage edge.

The camera stays fixed; the surfaces themselves scroll forward in
time so the "now" edge advances with each frame, simulating a live
price tape from both venues. Markets where the two surfaces diverge
at the current time are highlighted with vertical green bars — those
are the actionable arbitrage opportunities.
"""

from __future__ import annotations

import io
import logging
import math
import os
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

import imageio.v2 as imageio
import matplotlib
matplotlib.use("Agg")  # headless rendering
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

logger = logging.getLogger(__name__)


# Cyberpunk palette tuned to match the terminal frontend
_TERMINAL_BG = "#0a0e14"
_PANEL_BG = "#0d1117"
_TEXT_PRIMARY = "#e6edf3"
_TEXT_SECONDARY = "#8b949e"
_GRID_COLOR = "#30363d"

# Per-platform colours
_KALSHI_COLOR = "#39c5cf"      # cyan
_KALSHI_EDGE = "#1f6feb"
_POLY_COLOR = "#d29922"        # amber
_POLY_EDGE = "#db6d28"

# Arbitrage bar tiers (net edge in cents after fees + slippage)
_ARB_PROFIT_HIGH = "#3fb950"   # net >= 5  c (bright green)
_ARB_PROFIT_LOW = "#d29922"    # 1 <= net < 5 (amber, thin edge)
_ARB_MARGINAL = "#f85149"      # net <  1 (red, barely covers fees)


@dataclass
class ArbPoint:
    """A single opportunity used for the optional overlay."""

    kalshi_yes: float
    poly_yes: float
    net_profit_pct: float
    label: str = ""


class ArbitrageSurfaceRenderer:
    """Render two scrolling YES-price surfaces (Kalshi vs Polymarket).

    The price tapes are synthetic but periodic with period
    ``frames``, so the resulting GIF loops seamlessly. Each market
    has its own slow trend plus a divergence component that pulls
    Kalshi and Polymarket apart at different times — exactly the
    pattern a real cross-venue arbitrage tape exhibits.
    """

    def __init__(
        self,
        n_markets: int = 14,
        window: int = 18,
        frames: int = 30,
        seed: int = 42,
        figsize: tuple = (7.6, 5.4),
        dpi: int = 100,
        elev: float = 26.0,
        azim: float = -64.0,
        fee_pct: float = 3.0,
        slippage_pct: float = 1.0,
        arb_threshold: float = 0.045,
    ):
        self.n_markets = n_markets
        self.window = window
        self.frames = frames
        self.seed = seed
        self.figsize = figsize
        self.dpi = dpi
        self.elev = elev
        self.azim = azim
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.arb_threshold = arb_threshold

        self._kalshi_tape, self._poly_tape = self._generate_tapes()

    # --- price tape generation -----------------------------------------------

    def _generate_tapes(self) -> Tuple[np.ndarray, np.ndarray]:
        """Build periodic synthetic Kalshi/Polymarket price tapes.

        Each market has its own baseline, slow trend, and a divergence
        term that periodically pulls the two platforms apart. The
        divergence amplitude is drawn from a long-tailed distribution
        so a few markets show large arbitrage gaps while most show
        none.

        All time terms use sin(2π·t·k/F) with integer k, so
        tape[t + F] == tape[t] and the GIF loops cleanly.
        """
        rng = np.random.default_rng(self.seed)
        F = self.frames
        W = self.window
        N = self.n_markets
        T = F + W
        t = np.arange(T)

        kalshi = np.zeros((N, T))
        poly = np.zeros((N, T))

        for m in range(N):
            baseline = float(rng.uniform(0.32, 0.68))
            amp = float(rng.uniform(0.06, 0.18))
            base_k = int(rng.choice([1, 2, 3]))
            base_phase = float(rng.uniform(0.0, 2.0 * math.pi))

            # Per-market divergence amplitude. Draw from a mixture so
            # roughly a third of markets show clearly visible arb gaps.
            r = rng.random()
            if r < 0.18:
                div_amp = float(rng.uniform(0.10, 0.16))   # large gap
            elif r < 0.45:
                div_amp = float(rng.uniform(0.05, 0.10))   # medium gap
            else:
                div_amp = float(rng.uniform(0.005, 0.03))  # tight tracking

            div_k = int(rng.choice([1, 2, 3]))
            div_phase = float(rng.uniform(0.0, 2.0 * math.pi))

            base = baseline + amp * np.sin(2.0 * math.pi * t / F * base_k + base_phase)
            div = div_amp * np.sin(2.0 * math.pi * t / F * div_k + div_phase)

            # Occasional asymmetric jitter that affects only one venue
            jitter_amp = float(rng.uniform(0.005, 0.025))
            jitter_k = int(rng.choice([2, 3, 4]))
            jitter_phase = float(rng.uniform(0.0, 2.0 * math.pi))
            jitter_k_arr = jitter_amp * np.sin(2.0 * math.pi * t / F * jitter_k + jitter_phase)

            kalshi[m] = np.clip(base - 0.5 * div + 0.5 * jitter_k_arr, 0.04, 0.96)
            poly[m] = np.clip(base + 0.5 * div - 0.5 * jitter_k_arr, 0.04, 0.96)

        return kalshi, poly

    # --- rendering ------------------------------------------------------------

    def _draw_frame(
        self,
        frame_idx: int,
        overlay: Sequence[ArbPoint],
    ) -> np.ndarray:
        f = frame_idx
        W = self.window
        N = self.n_markets

        K = self._kalshi_tape[:, f : f + W]    # (N, W)
        P = self._poly_tape[:, f : f + W]      # (N, W)

        # X = market index, Y = time within window (negative = past, 0 = now)
        market_axis = np.arange(N)
        time_axis = np.arange(W) - (W - 1)
        X, Y = np.meshgrid(market_axis, time_axis, indexing="ij")  # (N, W)

        fig = plt.figure(figsize=self.figsize, dpi=self.dpi, facecolor=_TERMINAL_BG)
        ax = fig.add_subplot(111, projection="3d", facecolor=_PANEL_BG)

        # Kalshi surface (cyan)
        ax.plot_surface(
            X, Y, K,
            color=_KALSHI_COLOR,
            alpha=0.55,
            shade=True,
            edgecolor=_KALSHI_EDGE,
            linewidth=0.25,
            antialiased=True,
            rstride=1,
            cstride=1,
            zorder=1,
        )
        # Polymarket surface (amber)
        ax.plot_surface(
            X, Y, P,
            color=_POLY_COLOR,
            alpha=0.55,
            shade=True,
            edgecolor=_POLY_EDGE,
            linewidth=0.25,
            antialiased=True,
            rstride=1,
            cstride=1,
            zorder=2,
        )

        # Bold "now" edge for each platform
        now_y = 0
        ax.plot(
            market_axis,
            np.full(N, now_y),
            K[:, -1],
            color=_KALSHI_COLOR,
            linewidth=2.2,
            zorder=4,
        )
        ax.plot(
            market_axis,
            np.full(N, now_y),
            P[:, -1],
            color=_POLY_COLOR,
            linewidth=2.2,
            zorder=4,
        )

        # Vertical arbitrage bars at "now": one per market with a visible gap.
        # Each bar is rendered as a layered glow + star endpoints + floor
        # spotlight + cent label so the operator can immediately see the
        # actionable spread, its size and its profitability after fees.
        n_arbs = 0
        for m in range(N):
            k_now = float(K[m, -1])
            p_now = float(P[m, -1])
            gap = abs(p_now - k_now)
            if gap < self.arb_threshold:
                continue
            n_arbs += 1
            z_lo = min(k_now, p_now)
            z_hi = max(k_now, p_now)
            gap_cents = gap * 100.0
            net_cents = gap_cents - (self.fee_pct + self.slippage_pct)

            # Colour band by net profitability after fees and slippage.
            if net_cents >= 5.0:
                bar_color = _ARB_PROFIT_HIGH   # bright green: clear profit
            elif net_cents >= 1.0:
                bar_color = _ARB_PROFIT_LOW    # amber: thin edge
            else:
                bar_color = _ARB_MARGINAL      # red: barely covers fees

            # Floor projection (vertical guide from the ground up) to anchor
            # the bar in the (market, time) plane and make the column easy
            # to track even when the bar is short.
            ax.plot(
                [m, m],
                [now_y, now_y],
                [0.0, z_lo],
                color=bar_color,
                linewidth=0.9,
                alpha=0.45,
                linestyle=(0, (3, 2)),
                zorder=4,
            )

            # Layered glow bar (outer halo + mid + crisp inner line).
            for lw, alpha in ((9.0, 0.18), (5.5, 0.40), (2.8, 1.0)):
                ax.plot(
                    [m, m],
                    [now_y, now_y],
                    [z_lo, z_hi],
                    color=bar_color,
                    linewidth=lw,
                    alpha=alpha,
                    solid_capstyle="round",
                    zorder=5,
                )

            # Star markers at each endpoint with white outline so they read
            # cleanly against either surface. Colour the marker by venue so
            # it is obvious which side is the buy and which is the sell.
            ax.scatter(
                [m],
                [now_y],
                [k_now],
                c=[_KALSHI_COLOR],
                s=170,
                marker="*",
                edgecolors="#ffffff",
                linewidths=1.3,
                depthshade=False,
                zorder=8,
            )
            ax.scatter(
                [m],
                [now_y],
                [p_now],
                c=[_POLY_COLOR],
                s=170,
                marker="*",
                edgecolors="#ffffff",
                linewidths=1.3,
                depthshade=False,
                zorder=8,
            )

            # Floor spotlight diamond directly under the arb column.
            ax.scatter(
                [m],
                [now_y],
                [0.005],
                c=[bar_color],
                s=90,
                marker="D",
                edgecolors="#ffffff",
                linewidths=1.0,
                depthshade=False,
                zorder=7,
            )

            # Cent label floating next to the bar, in the bar colour so the
            # profitability tier is encoded redundantly.
            mid_z = 0.5 * (k_now + p_now)
            ax.text(
                m + 0.35,
                now_y + 0.2,
                mid_z,
                f"+{gap_cents:.1f}¢",
                color=bar_color,
                fontsize=8,
                fontweight="bold",
                zorder=10,
            )

        # Optional overlay of explicit ArbPoint instances. Distribute
        # across the markets so they are visible alongside the synthetic
        # surfaces without overwriting them.
        if overlay:
            slots = np.linspace(0, N - 1, num=min(len(overlay), N))
            for slot, pt in zip(slots, overlay):
                m = float(slot)
                if pt.net_profit_pct >= 5.0:
                    color = _ARB_PROFIT_HIGH
                elif pt.net_profit_pct >= 1.0:
                    color = _ARB_PROFIT_LOW
                else:
                    color = _ARB_MARGINAL
                ax.plot(
                    [m, m],
                    [now_y + 0.4, now_y + 0.4],
                    [pt.kalshi_yes, pt.poly_yes],
                    color=color,
                    linewidth=1.8,
                    alpha=0.9,
                    zorder=7,
                )

        # Axes / labels
        ax.set_xlim(0, N - 1)
        ax.set_ylim(-(W - 1), 0)
        ax.set_zlim(0.0, 1.0)
        ax.view_init(elev=self.elev, azim=self.azim)

        ax.set_xlabel("Matched market", color=_TEXT_SECONDARY, fontsize=9, labelpad=8)
        ax.set_ylabel("Time → now", color=_TEXT_SECONDARY, fontsize=9, labelpad=8)
        ax.set_zlabel("YES price", color=_TEXT_SECONDARY, fontsize=9, labelpad=8)
        ax.set_title(
            "Kalshi vs Polymarket — Live YES Price Surfaces",
            color=_TEXT_PRIMARY,
            fontsize=12,
            pad=14,
            fontweight="bold",
        )

        for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
            axis.set_pane_color((0.04, 0.05, 0.07, 1.0))
            axis._axinfo["grid"]["color"] = _GRID_COLOR
            axis._axinfo["grid"]["linewidth"] = 0.4
            for tick in axis.get_majorticklabels():
                tick.set_color(_TEXT_SECONDARY)
                tick.set_fontsize(8)

        # Legend (proxy artists since plot_surface doesn't auto-legend).
        # We show the two venue ribbons and the three arbitrage tiers so
        # the operator can read the colour code on the bars at a glance.
        legend_handles = [
            Patch(facecolor=_KALSHI_COLOR, edgecolor=_KALSHI_EDGE, alpha=0.7, label="Kalshi YES"),
            Patch(facecolor=_POLY_COLOR, edgecolor=_POLY_EDGE, alpha=0.7, label="Polymarket YES"),
            Line2D([0], [0], color=_ARB_PROFIT_HIGH, linewidth=4, label="arb ≥ 5¢ net"),
            Line2D([0], [0], color=_ARB_PROFIT_LOW, linewidth=4, label="arb 1–5¢ net"),
            Line2D([0], [0], color=_ARB_MARGINAL, linewidth=4, label="arb < 1¢ (fees)"),
            Line2D(
                [0], [0], marker="*", color="none",
                markerfacecolor=_KALSHI_COLOR, markeredgecolor="#ffffff",
                markersize=10, linestyle="none", label="Kalshi price",
            ),
            Line2D(
                [0], [0], marker="*", color="none",
                markerfacecolor=_POLY_COLOR, markeredgecolor="#ffffff",
                markersize=10, linestyle="none", label="Poly price",
            ),
        ]
        legend = ax.legend(
            handles=legend_handles,
            loc="upper left",
            frameon=False,
            fontsize=7.5,
            labelcolor=_TEXT_SECONDARY,
            ncol=1,
            handlelength=1.6,
            handletextpad=0.6,
            borderaxespad=0.2,
        )

        fig.text(
            0.02,
            0.04,
            f"fees+slippage modeled: {self.fee_pct + self.slippage_pct:.1f}%",
            color=_TEXT_SECONDARY,
            fontsize=8,
        )
        # Total tradable cents across all live arbs gives a quick read of
        # how much edge the market is offering right now.
        total_gap_cents = 0.0
        for m in range(N):
            g = abs(float(K[m, -1]) - float(P[m, -1]))
            if g >= self.arb_threshold:
                total_gap_cents += g * 100.0
        fig.text(
            0.98,
            0.04,
            f"live arbs: {n_arbs}   total spread: {total_gap_cents:.1f}¢",
            color=_TEXT_SECONDARY,
            fontsize=8,
            ha="right",
        )

        fig.subplots_adjust(left=0.02, right=0.98, top=0.93, bottom=0.06)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor(), dpi=self.dpi)
        plt.close(fig)
        buf.seek(0)
        return imageio.imread(buf)

    def render(
        self,
        output_path: str,
        points: Optional[Sequence[ArbPoint]] = None,
        fps: int = 14,
    ) -> str:
        """Render the dual-surface GIF to ``output_path``.

        Args:
            output_path: Destination path for the GIF.
            points: Optional opportunity overlay (kalshi/poly prices
                projected onto the "now" edge).
            fps: Frames per second of the resulting animation.

        Returns:
            The absolute path of the written GIF.
        """
        overlay = list(points or [])
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        rendered = [self._draw_frame(f, overlay) for f in range(self.frames)]

        imageio.mimsave(output_path, rendered, format="GIF", fps=fps, loop=0)
        logger.info(
            "Rendered Kalshi/Polymarket dual surface GIF: %s (%d frames, %d markets, %d overlay points)",
            output_path,
            len(rendered),
            self.n_markets,
            len(overlay),
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
        return ArbPoint(
            kalshi_yes=kalshi_yes,
            poly_yes=poly_yes,
            net_profit_pct=float(getattr(opp, "net_profit_pct", 0.0)),
            label=getattr(opp, "id", ""),
        )
    except AttributeError:
        return None


def render_arbitrage_gif(
    output_path: str,
    opportunities: Optional[Iterable] = None,
    fee_pct: float = 3.0,
    slippage_pct: float = 1.0,
    frames: int = 30,
    fps: int = 14,
    n_markets: int = 14,
    window: int = 18,
    seed: int = 42,
) -> str:
    """Convenience wrapper used by the server and CLI.

    ``opportunities`` may be ``ArbitrageOpportunity`` instances, plain
    ``ArbPoint`` instances, or dicts with the keys ``kalshi_yes``,
    ``poly_yes`` and ``net_profit_pct``. Anything we cannot interpret
    is skipped so the surfaces still render.
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
        n_markets=n_markets,
        window=window,
        frames=frames,
        seed=seed,
        fee_pct=fee_pct,
        slippage_pct=slippage_pct,
    )
    return renderer.render(output_path, points=points, fps=fps)


def _demo_points(seed: int = 7, count: int = 6) -> List[ArbPoint]:
    """Generate a deterministic set of demo overlay points."""
    rng = np.random.default_rng(seed)
    pts: List[ArbPoint] = []
    for i in range(count):
        k = float(rng.uniform(0.20, 0.80))
        offset = float(rng.choice([-1, 1]) * rng.uniform(0.05, 0.18))
        p = float(np.clip(k + offset, 0.05, 0.95))
        net = abs(p - k) * 100.0 - 4.0
        pts.append(
            ArbPoint(
                kalshi_yes=k,
                poly_yes=p,
                net_profit_pct=net,
                label=f"DEMO-{i + 1:02d}",
            )
        )
    return pts


if __name__ == "__main__":
    target = os.environ.get(
        "ARB_VIZ_OUTPUT",
        os.path.join(
            os.path.dirname(__file__), "..", "..", "frontend", "assets", "arbitrage_surface.gif"
        ),
    )
    target = os.path.abspath(target)
    render_arbitrage_gif(target, opportunities=_demo_points())
    print(f"Wrote {target}")
