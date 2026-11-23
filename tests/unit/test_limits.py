from datetime import UTC, datetime

from app.domain.limits import period_starts


def test_dia_y_mes_en_hora_de_costa_rica() -> None:
    # 1 de diciembre 03:00 UTC = 30 de noviembre 21:00 en Costa Rica (UTC-6).
    dia, mes = period_starts(datetime(2026, 12, 1, 3, 0, tzinfo=UTC), "America/Costa_Rica")
    assert dia == datetime(2026, 11, 30, 6, 0, tzinfo=UTC)
    assert mes == datetime(2026, 11, 1, 6, 0, tzinfo=UTC)


def test_dia_en_utc() -> None:
    dia, mes = period_starts(datetime(2026, 12, 1, 3, 0, tzinfo=UTC), "UTC")
    assert dia == datetime(2026, 12, 1, tzinfo=UTC)
    assert mes == datetime(2026, 12, 1, tzinfo=UTC)
