UNIT_ALIASES = {
    "PC": ("pc", "pcs", "piece", "pieces", "unit", "units", "szt", "sztuk", "件", "个", "개")
}


def canonical_unit(unit: str | None) -> str | None:
    """Unit code for a supplier's unit word; unknown words pass through unchanged."""
    if unit is None:
        return None
    folded = unit.strip().casefold()
    return next(
        (code for code, words in UNIT_ALIASES.items() if folded in (code.casefold(), *words)),
        unit.strip(),
    )
