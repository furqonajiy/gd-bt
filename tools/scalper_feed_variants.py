"""SC24 scalper feed variants used by the RSI/Bollinger/R:R sweeps."""
from __future__ import annotations

from pathlib import Path


SCALPER_WIDE_ARGS: tuple[str, ...] = (
    "--rr1 1.5",
    "--rr2 2.5",
    "--rr3 4.0",
)

SCALPER_RBR_VARIANTS: dict[str, tuple[str, ...]] = {
    "base": (),
    "rr08": ("--rr1 0.8", "--rr2 1.5", "--rr3 3.0"),
    "rr40": ("--rr1 1.0", "--rr2 2.0", "--rr3 4.0"),
    "pctb80": ("--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20"),
    "pctb80_rr08": (
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "pctb80_rr40": (
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "sqz6": ("--bb-bandwidth-min 0.0006",),
    "sqz6_rr08": (
        "--bb-bandwidth-min 0.0006",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "sqz6_rr40": (
        "--bb-bandwidth-min 0.0006",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi70": ("--rsi-buy-max 70", "--rsi-sell-min 30"),
    "rsi70_rr08": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi70_rr40": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi70_pctb80": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
    ),
    "rsi70_pctb80_rr08": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi70_pctb80_rr40": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi70_sqz6": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-bandwidth-min 0.0006",
    ),
    "rsi70_sqz6_rr08": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-bandwidth-min 0.0006",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi70_sqz6_rr40": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-bandwidth-min 0.0006",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi75": ("--rsi-buy-max 75", "--rsi-sell-min 25"),
    "rsi75_rr08": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi75_rr40": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi75_pctb80": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
    ),
    "rsi75_pctb80_rr08": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi75_pctb80_rr40": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi75_sqz6": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--bb-bandwidth-min 0.0006",
    ),
    "rsi75_sqz6_rr08": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--bb-bandwidth-min 0.0006",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi75_sqz6_rr40": (
        "--rsi-buy-max 75", "--rsi-sell-min 25",
        "--bb-bandwidth-min 0.0006",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi70_rr25": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--rr1 1.0", "--rr2 1.5", "--rr3 2.5",
    ),
    "rsi70_pctb80_rr25": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--rr1 1.0", "--rr2 1.5", "--rr3 2.5",
    ),
    "rsi70_sqz6_rr25": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-bandwidth-min 0.0006",
        "--rr1 1.0", "--rr2 1.5", "--rr3 2.5",
    ),
    "rsi70_pbsqz_rr08": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--bb-bandwidth-min 0.0006",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi70_pbsqz_rr40": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-buy-pctb-max 0.80", "--bb-sell-pctb-min 0.20",
        "--bb-bandwidth-min 0.0006",
        "--rr1 1.0", "--rr2 2.0", "--rr3 4.0",
    ),
    "rsi70_pctb85_rr08": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-buy-pctb-max 0.85", "--bb-sell-pctb-min 0.15",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
    "rsi70_sqz10_rr08": (
        "--rsi-buy-max 70", "--rsi-sell-min 30",
        "--bb-bandwidth-min 0.0010",
        "--rr1 0.8", "--rr2 1.5", "--rr3 3.0",
    ),
}


def _stem(value: str) -> str:
    return Path(str(value or "").strip().replace("\\", "/")).stem


def scalper_variant_from_feed(feed: str) -> str | None:
    name = str(feed or "").strip()
    if name in SCALPER_RBR_VARIANTS:
        return name
    stem = _stem(name)
    if stem.endswith("_live"):
        stem = stem[:-5]
    if stem == "self_scalper24":
        return "base"
    prefix = "self_scalper24_"
    if stem.startswith(prefix):
        variant = stem[len(prefix):]
        if variant in SCALPER_RBR_VARIANTS:
            return variant
    return None


def scalper_variant_args(feed: str) -> list[str] | None:
    variant = scalper_variant_from_feed(feed)
    if variant is None:
        return None
    return list(SCALPER_RBR_VARIANTS[variant])


def scalper_feed_args(feed: str) -> list[str] | None:
    name = str(feed or "").strip().replace("\\", "/")
    stem = _stem(name)
    if name == "scalperwide24" or stem in {"self_scalper_widerr24", "self_scalper_widerr24_live"}:
        return list(SCALPER_WIDE_ARGS)
    if name == "scalper24":
        return []
    return scalper_variant_args(feed)


def scalper_variant_archive(feed: str) -> str | None:
    variant = scalper_variant_from_feed(feed)
    if variant is None:
        return None
    if variant == "base":
        return "generated/self_scalper24.txt"
    return f"generated/self_scalper24_{variant}.txt"
