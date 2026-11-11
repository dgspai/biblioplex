"""Portuguese printed-name extraction (ingest/pt_names.py)."""

from __future__ import annotations

from biblioplex.ingest.pt_names import pt_pairs

_OID = "aaaaaaaa-1111-2222-3333-444444444444"


def test_single_faced_pt_printing() -> None:
    card = {"lang": "pt", "oracle_id": _OID, "printed_name": "Banco de Nevoeiro"}
    assert pt_pairs(card) == [("Banco de Nevoeiro", _OID)]


def test_english_printing_yields_nothing() -> None:
    card = {"lang": "en", "oracle_id": _OID, "name": "Fog Bank"}
    assert pt_pairs(card) == []


def test_multi_faced_collects_each_face() -> None:
    card = {
        "lang": "pt",
        "oracle_id": _OID,
        "card_faces": [
            {"printed_name": "Frente"},
            {"printed_name": "Verso"},
        ],
    }
    assert pt_pairs(card) == [("Frente", _OID), ("Verso", _OID)]


def test_dedupes_repeated_face_name() -> None:
    card = {
        "lang": "pt",
        "oracle_id": _OID,
        "printed_name": "Nome",
        "card_faces": [{"printed_name": "Nome"}, {"printed_name": "Nome"}],
    }
    assert pt_pairs(card) == [("Nome", _OID)]


def test_missing_oracle_id_skipped() -> None:
    assert pt_pairs({"lang": "pt", "printed_name": "Sem Oracle"}) == []


def test_no_portuguese_spelling_skipped() -> None:
    # A pt row that somehow carries no printed_name anywhere.
    assert pt_pairs({"lang": "pt", "oracle_id": _OID, "name": "Fog Bank"}) == []
