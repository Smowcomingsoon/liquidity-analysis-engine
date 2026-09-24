from app.models import ConfluenceZone, LiquidityLevel, TimeframeAnalysis, TimeframeKey

_TF_ORDER: list[TimeframeKey] = ["htf", "mtf", "ltf"]
_MERGE_TOLERANCE_PCT = 0.0015  # 0.15% of price, allows nearby-but-not-identical levels to merge
_TYPE_LABEL_IT = {
    "order_block": "Order Block",
    "fvg": "Fair Value Gap",
    "eqh": "Equal Highs",
    "eql": "Equal Lows",
    "bos": "BOS",
    "choch": "CHoCH",
}


def _midpoint(level: LiquidityLevel) -> float:
    return (level.price_high + level.price_low) / 2


def compute_confluence(timeframes: dict[str, TimeframeAnalysis]) -> list[ConfluenceZone]:
    tagged: list[tuple[TimeframeKey, LiquidityLevel]] = [
        (tf_key, level)
        for tf_key, analysis in timeframes.items()
        for level in analysis.levels
        if not level.mitigated
    ]
    if not tagged:
        return []

    tagged.sort(key=lambda pair: _midpoint(pair[1]))

    zones: list[ConfluenceZone] = []
    used = [False] * len(tagged)

    for i, (_, level_i) in enumerate(tagged):
        if used[i]:
            continue
        group = [tagged[i]]
        used[i] = True
        low, high = level_i.price_low, level_i.price_high
        tolerance = _midpoint(level_i) * _MERGE_TOLERANCE_PCT

        for j in range(i + 1, len(tagged)):
            if used[j]:
                continue
            _, level_j = tagged[j]
            if level_j.price_low <= high + tolerance and level_j.price_high >= low - tolerance:
                group.append(tagged[j])
                used[j] = True
                low = min(low, level_j.price_low)
                high = max(high, level_j.price_high)

        distinct_tfs = sorted({tf for tf, _ in group}, key=_TF_ORDER.index)
        if len(distinct_tfs) < 2:
            continue  # confluence requires agreement across at least two timeframes

        types = list(dict.fromkeys(level.type for _, level in group))
        score = len(group) * 10 + len(distinct_tfs) * 5

        type_counts: dict[tuple[str, str], int] = {}
        for _, level in group:
            key = (level.type, level.direction)
            type_counts[key] = type_counts.get(key, 0) + 1
        type_summary = ", ".join(
            f"{_TYPE_LABEL_IT[t]} {'bullish' if d == 'bullish' else 'bearish'}" + (f" x{c}" if c > 1 else "")
            for (t, d), c in type_counts.items()
        )

        zones.append(
            ConfluenceZone(
                price_high=high,
                price_low=low,
                timeframes=distinct_tfs,
                types=types,
                score=score,
                label=f"Confluenza {'/'.join(tf.upper() for tf in distinct_tfs)}: {type_summary}",
            )
        )

    zones.sort(key=lambda z: z.score, reverse=True)
    return zones
