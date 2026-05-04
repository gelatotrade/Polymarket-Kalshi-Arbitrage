"""
3D Arbitrage Surface Renderer.

Builds an animated GIF that visualises the cross-platform arbitrage
profit landscape between Kalshi and Polymarket. The X axis is the
Kalshi YES price, the Y axis is the Polymarket YES price and the Z
axis is the net edge (cents per dollar of notional) after fees and
slippage. The camera is fixed; instead the surface itself deforms
elastically frame-by-frame to reflect live market activity. Each
opportunity contributes a localised, pulsing bump on the surface
and travels along a small orbit so the operator can see how the
edge landscape "breathes" as prices move.
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


def _base_surface(
    kalshi_axis: np.ndarray,
    poly_axis: np.ndarray,
    fee_pct: float,
    slippage_pct: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute the static base surface (mesh + Z) on a grid of YES prices.

    The base metric is the absolute spread expressed in cents per
    dollar of notional, minus fees and slippage. This produces a
    clean V-valley along the diagonal (no arbitrage when prices
    match) that rises smoothly toward the corners.
    """
    K, P = np.meshgrid(kalshi_axis, poly_axis, indexing="xy")
    spread_cents = np.abs(P - K) * 100.0
    Z = spread_cents - (fee_pct + slippage_pct)
    return K, P, Z


class ArbitrageSurfaceRenderer:
    """Render the arbitrage profit surface as an elastic, breathing GIF.

    The camera is fixed; the surface itself deforms over time. Three
    deformation components are summed onto the static base:

    * a diagonal traveling wave that ripples along the no-arbitrage
      valley, simulating shared market sentiment moving both venues
      in tandem;
    * localised Gaussian "activity bumps" centred on each live
      opportunity, with pulsing amplitude;
    * a slow global heave so the whole surface feels alive even when
      no opportunities are present.
    """

    def __init__(
        self,
        grid_size: int = 70,
        fee_pct: float = 3.0,
        slippage_pct: float = 1.0,
        frames: int = 36,
        elev: float = 30.0,
        azim: float = -62.0,
        figsize: tuple = (7.0, 5.2),
        dpi: int = 100,
        price_floor: float = 0.05,
        wave_amplitude: float = 4.5,
        cross_wave_amplitude: float = 2.5,
        bump_amplitude: float = 9.0,
        bump_sigma: float = 0.075,
        orbit_radius: float = 0.075,
        heave_amplitude: float = 1.6,
    ):
        self.grid_size = grid_size
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.frames = frames
        self.elev = elev
        self.azim = azim
        self.figsize = figsize
        self.dpi = dpi
        self.price_floor = price_floor
        self.wave_amplitude = wave_amplitude
        self.cross_wave_amplitude = cross_wave_amplitude
        self.bump_amplitude = bump_amplitude
        self.bump_sigma = bump_sigma
        self.orbit_radius = orbit_radius
        self.heave_amplitude = heave_amplitude

        # Real prediction markets rarely trade at the extremes; clipping the
        # axis to [price_floor, 1 - price_floor] keeps the corner singularities
        # out of the visible surface.
        self._kalshi_axis = np.linspace(price_floor, 1.0 - price_floor, grid_size)
        self._poly_axis = np.linspace(price_floor, 1.0 - price_floor, grid_size)
        self._K, self._P, self._Z_base = _base_surface(
            self._kalshi_axis, self._poly_axis, fee_pct, slippage_pct
        )

        # Pre-compute the rotated diagonal coordinates used by the wave term.
        # diag goes along the k=p axis, cross is perpendicular to it.
        self._diag = (self._K + self._P) / math.sqrt(2.0)
        self._cross = (self._P - self._K) / math.sqrt(2.0)

    # --- animation primitives -------------------------------------------------

    def _orbit_params(self, index: int) -> Tuple[float, float, float, float]:
        """Deterministic orbit (radius, freq_x, freq_y, phase) per point."""
        radius = self.orbit_radius * (0.7 + 0.5 * ((index * 13) % 7) / 7.0)
        freq_x = 1.0 + 0.25 * ((index * 5) % 4)
        freq_y = 1.0 + 0.25 * ((index * 7) % 4) + 0.1
        phase = (index * 1.7) % (2.0 * math.pi)
        return radius, freq_x, freq_y, phase

    def _animate_points(self, base: Sequence[ArbPoint], t: float) -> List[ArbPoint]:
        """Return new opportunity positions at time ``t`` (radians)."""
        out: List[ArbPoint] = []
        floor = self.price_floor
        for i, pt in enumerate(base):
            radius, fx, fy, phase = self._orbit_params(i)
            dx = radius * math.cos(t * fx + phase)
            dy = radius * math.sin(t * fy + phase * 1.3)
            k = float(np.clip(pt.kalshi_yes + dx, floor, 1.0 - floor))
            p = float(np.clip(pt.poly_yes + dy, floor, 1.0 - floor))
            net = abs(p - k) * 100.0 - (self.fee_pct + self.slippage_pct)
            out.append(
                ArbPoint(
                    kalshi_yes=k,
                    poly_yes=p,
                    net_profit_pct=net,
                    label=pt.label,
                )
            )
        return out

    def _deformed_surface(self, animated: Sequence[ArbPoint], t: float) -> np.ndarray:
        """Return the deformed Z grid at time ``t``.

        Combines the static base with two traveling wave families,
        a slow global heave, and Gaussian bumps centred on each
        animated opportunity. All time terms use sin/cos of ``t``
        so the GIF loops cleanly when ``t`` sweeps a full 2π
        interval.
        """
        Z = self._Z_base.copy()

        # 1. Diagonal traveling wave (concentrated near the no-arb valley).
        wave = (
            self.wave_amplitude
            * np.sin(7.0 * self._diag - 2.0 * t)
            * np.exp(-(self._cross ** 2) / 0.06)
        )
        Z = Z + wave

        # 2. Cross-diagonal wave so deformation isn't a single 1D ripple.
        cross_wave = (
            self.cross_wave_amplitude
            * np.cos(5.0 * self._cross + 1.7 * t)
            * np.exp(-((self._diag - math.sqrt(2.0) / 2.0) ** 2) / 0.18)
        )
        Z = Z + cross_wave

        # 3. Slow global heave so the whole sheet breathes.
        heave = self.heave_amplitude * math.sin(t)
        Z = Z + heave

        # 4. Pulsing Gaussian bumps at each live opportunity. Each bump
        #    breathes harder than before so individual opportunities
        #    visibly throb on the surface.
        sigma2 = 2.0 * self.bump_sigma ** 2
        for i, pt in enumerate(animated):
            _, _, _, phase = self._orbit_params(i)
            pulse = self.bump_amplitude * (0.55 + 0.75 * math.sin(1.5 * t + phase))
            Z = Z + pulse * np.exp(
                -((self._K - pt.kalshi_yes) ** 2 + (self._P - pt.poly_yes) ** 2) / sigma2
            )

        return Z

    # --- rendering ------------------------------------------------------------

    def _draw_frame(
        self,
        t: float,
        base_points: Sequence[ArbPoint],
        z_norm: Normalize,
        z_floor: float,
        z_ceil: float,
        n_points: int,
    ) -> np.ndarray:
        animated = self._animate_points(base_points, t)
        Z = self._deformed_surface(animated, t)

        fig = plt.figure(figsize=self.figsize, dpi=self.dpi, facecolor=_TERMINAL_BG)
        ax = fig.add_subplot(111, projection="3d", facecolor=_PANEL_BG)

        ax.plot_surface(
            self._K,
            self._P,
            Z,
            cmap=_ARB_CMAP,
            norm=z_norm,
            linewidth=0.0,
            antialiased=True,
            alpha=0.93,
            rstride=2,
            cstride=2,
            edgecolor="none",
            shade=True,
        )

        # Project the animated opportunities onto the deformed surface.
        if animated:
            xs = [pt.kalshi_yes for pt in animated]
            ys = [pt.poly_yes for pt in animated]
            # Sample the deformed Z field at each marker so the glyph rides on
            # top of the breathing surface, slightly lifted to stay readable.
            zs: List[float] = []
            for k, p in zip(xs, ys):
                ix = int(np.clip(round((k - self.price_floor) / (1.0 - 2 * self.price_floor) * (self.grid_size - 1)), 0, self.grid_size - 1))
                iy = int(np.clip(round((p - self.price_floor) / (1.0 - 2 * self.price_floor) * (self.grid_size - 1)), 0, self.grid_size - 1))
                zs.append(float(Z[iy, ix]) + 1.5)
            colors = ["#3fb950" if pt.net_profit_pct >= 0 else "#f85149" for pt in animated]
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
            for xi, yi, zi in zip(xs, ys, zs):
                ax.plot(
                    [xi, xi],
                    [yi, yi],
                    [z_floor, zi],
                    color="#58a6ff",
                    linewidth=0.8,
                    alpha=0.6,
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
        ax.set_zlim(z_floor, z_ceil)
        # Fixed camera: no rotation across frames.
        ax.view_init(elev=self.elev, azim=self.azim)

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
            f"live opportunities: {n_points}",
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
        """Render the elastic surface GIF to ``output_path``.

        Args:
            output_path: Destination path for the GIF.
            points: Optional list of opportunity points to overlay.
            fps: Frames per second of the resulting animation.

        Returns:
            The absolute path of the written GIF.
        """
        points = list(points or [])
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        # Pre-compute global Z range so the colormap and Z-axis stay stable
        # across frames (otherwise the colours and tick marks would jitter
        # as the surface deforms).
        wave_headroom = self.wave_amplitude + self.cross_wave_amplitude + self.heave_amplitude
        z_min = float(np.min(self._Z_base)) - wave_headroom
        z_max = float(np.max(self._Z_base)) + wave_headroom + (
            self.bump_amplitude * 1.4 if points else 0.0
        )
        z_floor = min(z_min, -self.fee_pct - self.slippage_pct - 2.0)
        z_ceil = max(z_max, 5.0)
        z_norm = Normalize(vmin=z_min, vmax=max(z_max, 0.5))

        # Sweep t over a full period so the loop is seamless.
        ts = np.linspace(0.0, 2.0 * math.pi, self.frames, endpoint=False)
        frames = [
            self._draw_frame(float(t), points, z_norm, z_floor, z_ceil, len(points))
            for t in ts
        ]

        imageio.mimsave(output_path, frames, format="GIF", fps=fps, loop=0)
        logger.info(
            "Rendered elastic arbitrage surface GIF: %s (%d frames, %d points)",
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
    grid_size: int = 70,
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
