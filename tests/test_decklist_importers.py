"""D19.3 — Deckbox CSV and print-view HTML coerced to decklist text.

Ground truth measured off `data/example_deck_exports/deckbox/` before the
module existed: the CSV is 89 rows (main 81/81, sideboard 8/19) and the
print view resolves to exactly the same shape.
"""

from __future__ import annotations

from pathlib import Path

from biblioplex.decklist.importers import to_decklist
from biblioplex.decklist.sections import parse_sections

DECKBOX = (
    Path(__file__).resolve().parent.parent
    / "data" / "example_deck_exports" / "deckbox"
)


def _cards(entries: list) -> int:
    return sum(e.qty for e in entries)


# ---- format sniffing ----


def test_plain_decklist_passes_through_untouched() -> None:
    """Text that is already a decklist is stored as the user gave it.

    Re-serialising would silently drop `//` grouping comments that people
    use to organise a list.
    """
    raw = "// Ramp\n1 Sol Ring\n\nSideboard\n1 Grapeshot\n"
    assert to_decklist(raw) == raw


def test_comma_in_a_card_name_is_not_mistaken_for_csv() -> None:
    """`1 Radagast, the Brown` has a comma but is not a CSV row.

    Sniffing on "contains a comma" would misroute every legendary
    creature; detection requires an actual header with count + name
    columns.
    """
    raw = "1 Radagast, the Brown\n1 Thrasios, Triton Hero\n"
    assert to_decklist(raw) == raw


def test_format_is_sniffed_from_content_not_filename() -> None:
    """No filename is passed at all — the bytes decide."""
    csv_text = "Count,Name,Section\n1,Sol Ring,main\n"
    assert [e.name for e in parse_sections(to_decklist(csv_text)).main] == ["Sol Ring"]


def test_empty_input_yields_empty_output() -> None:
    assert to_decklist("") == ""
    assert parse_sections(to_decklist("")).main == []


# ---- CSV adapter ----


def test_csv_sections_map_to_headings() -> None:
    csv_text = (
        "Count,Name,Section\n"
        "1,Sol Ring,main\n"
        "4,Grapeshot,sideboard\n"
    )
    s = parse_sections(to_decklist(csv_text))
    assert [e.name for e in s.main] == ["Sol Ring"]
    assert [(e.name, e.qty) for e in s.sideboard] == [("Grapeshot", 4)]


def test_csv_commander_section_does_not_swallow_the_mainboard() -> None:
    """A `Commander` heading must be closed by a `Deck` heading.

    Emitting the commander block first and then bare main lines would put
    the whole mainboard in the commander section, since `parse_sections`
    keeps the current bucket until the next heading.
    """
    csv_text = (
        "Count,Name,Section\n"
        "1,Hope of Ghirapur,commander\n"
        "1,Sol Ring,main\n"
        "1,Grapeshot,sideboard\n"
    )
    s = parse_sections(to_decklist(csv_text))
    assert [e.name for e in s.commander] == ["Hope of Ghirapur"]
    # D19.10: the commander is in the pool too, but the mainboard is still
    # there — which is what "not swallowed" means.
    assert [e.name for e in s.main] == ["Hope of Ghirapur", "Sol Ring"]
    assert [e.name for e in s.sideboard] == ["Grapeshot"]


def test_csv_header_variants_are_accepted() -> None:
    """Deckbox has shipped Count/Quantity/Qty and Name/Card over time."""
    for header in ("Quantity,Card\n", "Qty,Name\n", "count,card name\n"):
        s = parse_sections(to_decklist(header + "2,Sol Ring\n"))
        assert [(e.name, e.qty) for e in s.main] == [("Sol Ring", 2)]


def test_csv_without_a_section_column_is_all_mainboard() -> None:
    """The M18 collection export has no `Section` — degrade, do not fail."""
    s = parse_sections(to_decklist("Count,Name\n1,Sol Ring\n"))
    assert [e.name for e in s.main] == ["Sol Ring"]
    assert s.sideboard == []


def test_csv_unparseable_count_degrades_to_one() -> None:
    """A bad cell loses one quantity, not the whole import."""
    s = parse_sections(to_decklist("Count,Name\n,Sol Ring\nx,Mana Crypt\n"))
    assert [(e.name, e.qty) for e in s.main] == [("Sol Ring", 1), ("Mana Crypt", 1)]


def test_csv_zero_and_negative_count_rows_drop() -> None:
    s = parse_sections(to_decklist("Count,Name\n0,Sol Ring\n-2,Mana Crypt\n1,Grapeshot\n"))
    assert [e.name for e in s.main] == ["Grapeshot"]


def test_csv_normalizes_split_card_names() -> None:
    """Deckbox writes `Fire//Ice`; `cards.name` is `Fire // Ice`."""
    s = parse_sections(to_decklist("Count,Name\n1,Fire//Ice\n"))
    assert [e.name for e in s.main] == ["Fire // Ice"]


def test_csv_rows_with_a_blank_name_are_skipped() -> None:
    s = parse_sections(to_decklist("Count,Name\n1,\n1,Sol Ring\n"))
    assert [e.name for e in s.main] == ["Sol Ring"]


# ---- HTML adapter ----


def test_html_head_content_never_becomes_a_card() -> None:
    """`<title>` sits before the card run and would otherwise parse as one."""
    raw = (
        "<html><head><title>Deckbox.org card set print view</title></head>"
        "<body>1 Sol Ring<br>1 Grapeshot<br></body></html>"
    )
    s = parse_sections(to_decklist(raw))
    assert [e.name for e in s.main] == ["Sol Ring", "Grapeshot"]


def test_html_sideboard_divider_is_honoured() -> None:
    """Tag-stripping leaves a bare `Sideboard:` line, which D19.2 already
    understands — no divider special-case in this module."""
    raw = (
        "<body>1 Sol Ring<br><p><strong>Sideboard:</strong></p>"
        "1 Grapeshot<br></body>"
    )
    s = parse_sections(to_decklist(raw))
    assert [e.name for e in s.main] == ["Sol Ring"]
    assert [e.name for e in s.sideboard] == ["Grapeshot"]


def test_html_entities_are_unescaped() -> None:
    s = parse_sections(to_decklist("<body>1 It&#39;ll Quench Ya!<br></body>"))
    assert [e.name for e in s.main] == ["It'll Quench Ya!"]


def test_html_script_and_style_blocks_are_dropped() -> None:
    raw = (
        "<body><style>.x{color:red}</style><script>var a=1</script>"
        "1 Sol Ring<br></body>"
    )
    s = parse_sections(to_decklist(raw))
    assert [e.name for e in s.main] == ["Sol Ring"]


# ---- real Deckbox exports ----


def test_real_csv_export_shape() -> None:
    s = parse_sections(to_decklist((DECKBOX / "Iroh gift_2026.August.18.csv").read_text()))
    assert (len(s.main), _cards(s.main)) == (81, 81)
    assert (len(s.sideboard), _cards(s.sideboard)) == (8, 19)
    assert s.commander == []


def test_real_html_export_shape() -> None:
    s = parse_sections(to_decklist((DECKBOX / "Deckbox.org card set print view.html").read_text()))
    assert (len(s.main), _cards(s.main)) == (81, 81)
    assert (len(s.sideboard), _cards(s.sideboard)) == (8, 19)


def test_all_three_deckbox_representations_agree() -> None:
    """txt (D19.2), CSV and print-view HTML are one deck in three shapes.

    They disagreed at 101/100/— before the section work; equality here is
    the whole point of the deliverable, and unlike the Moxfield variants
    these genuinely ARE the same export.
    """
    txt = parse_sections((DECKBOX / "iroh-gift.txt").read_text())
    csv_ = parse_sections(to_decklist((DECKBOX / "Iroh gift_2026.August.18.csv").read_text()))
    htm = parse_sections(to_decklist((DECKBOX / "Deckbox.org card set print view.html").read_text()))

    shapes = {
        (_cards(s.main), _cards(s.sideboard)) for s in (txt, csv_, htm)
    }
    assert shapes == {(81, 19)}

    names = {tuple(sorted(e.name for e in s.main)) for s in (txt, csv_, htm)}
    assert len(names) == 1, "mainboard card names diverge between representations"
