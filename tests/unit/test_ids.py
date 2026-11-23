import re

from app.ids import new_correlation_id, new_topup_id, new_ulid

ULID_RE = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def test_ulid_formato() -> None:
    assert ULID_RE.match(new_ulid())


def test_ulid_ordenable_por_tiempo() -> None:
    assert new_ulid(ms=1_000) < new_ulid(ms=2_000)


def test_ulid_codifica_el_tiempo() -> None:
    assert new_ulid(ms=0).startswith("0000000000")
    assert new_ulid(ms=2**48 - 1).startswith("7ZZZZZZZZZ")


def test_ulid_unicos() -> None:
    assert len({new_ulid() for _ in range(10_000)}) == 10_000


def test_topup_id() -> None:
    tp = new_topup_id()
    assert tp.startswith("tp_")
    assert len(tp) == 29
    assert ULID_RE.match(tp[3:])


def test_correlation_id_dentro_del_limite_de_prepaynation() -> None:
    assert 1 <= len(new_correlation_id()) <= 50
