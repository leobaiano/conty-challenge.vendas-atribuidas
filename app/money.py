from decimal import Decimal, InvalidOperation


def to_cents(value: str | int | float) -> int:
    """Converte "199.90" (formato Shopify) em 19990. Rejeita mais de 2 casas decimais."""
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError(f"valor monetário inválido: {value!r}")
    if not amount.is_finite():
        raise ValueError(f"valor monetário inválido: {value!r}")
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"valor com mais de 2 casas decimais: {value!r}")
    return int(cents)


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
