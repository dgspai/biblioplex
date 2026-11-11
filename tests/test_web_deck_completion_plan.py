"""D19.46 — the Decks tab's "What should I buy next?" panel."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.web_assets import app_js

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "web" / "index.html"
CSS = ROOT / "web" / "style.css"


@pytest.fixture(scope="module")
def js() -> str:
    return app_js()


@pytest.fixture(scope="module")
def html() -> str:
    return INDEX.read_text()


def _fn(js: str, name: str) -> str:
    """Lift one function out of the template, `async` prefix included.

    Anchoring on `function <name>(` alone silently drops the `async`, and
    the extracted body then fails under node with "await is only valid in
    async functions" — a harness bug that looks like a source bug.
    """
    start = js.index(f"function {name}(")
    if js[max(0, start - 6) : start] == "async ":
        start -= 6
    return js[start : js.index("\n}\n", start) + 3]


def test_panel_exists_with_both_limits(html: str) -> None:
    assert 'id="deckPlanCard"' in html
    for el in ("deckPlanBudget", "deckPlanMaxCards", "deckPlanResult"):
        assert f'id="{el}"' in html, f"missing #{el}"


def test_panel_is_collapsed_by_default(html: str) -> None:
    """It answers a pre-shopping question, not an every-visit one."""
    block = html[html.index('id="deckPlanCard"') :][:200]
    assert block.lstrip().startswith('id="deckPlanCard"')
    assert " open" not in block.split(">")[0]


def test_both_plans_are_rendered(js: str) -> None:
    """The whole point of the endpoint returning two.

    At budget=10 the plans complete the same decks while buying 20 cards vs
    5. Showing one would hide that the caller has a choice.
    """
    fn = _fn(js, "_renderCompletionPlan")
    assert "by_cost" in fn and "by_cards" in fn


def test_ui_never_reranks_or_recomputes(js: str) -> None:
    """Every number is server-computed (D19.45).

    A second implementation of "what does this deck still need" is exactly
    how Bug.36's 78x disagreement happened.
    """
    for name in ("_renderCompletionPlan", "_planStep", "_planColumn"):
        fn = _fn(js, name)
        assert ".sort(" not in fn, f"{name} must not re-order the server's plan"
        for arith in ("reduce(", "* c.price", "+ c.price"):
            assert arith not in fn, f"{name} must not recompute totals"


def test_skipped_unpriced_decks_are_listed(js: str) -> None:
    """Hiding them is how the cheapest-looking deck silently vanishes."""
    fn = _fn(js, "_renderCompletionPlan")
    assert "skipped_unpriced" in fn


def test_greedy_note_is_shown(js: str) -> None:
    """A reader told the answer is optimal stops looking."""
    assert "res.note" in _fn(js, "_renderCompletionPlan")


def test_submit_is_wired_and_prevents_default(js: str) -> None:
    block = js[js.index("if (deckPlanFormEl) {") :][:260]
    assert "preventDefault" in block, "a form submit would reload the page"
    assert "loadCompletionPlan()" in block


def test_limits_are_omitted_when_blank(tmp_path: Path, js: str) -> None:
    """Executed, not substring-matched.

    `?budget=` is not the same request as omitting it, and a static pin
    cannot tell an empty string from an absent parameter.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    fn = _fn(js, "loadCompletionPlan")
    script = tmp_path / "plan.js"
    script.write_text(
        "let budget = '', maxCards = '';\n"
        "let called = null;\n"
        "const deckPlanBudgetEl = { get value() { return budget; } };\n"
        "const deckPlanMaxCardsEl = { get value() { return maxCards; } };\n"
        "const deckPlanResultEl = { set innerHTML(_v) {} };\n"
        "const deckPlanStatusEl = {};\n"
        "const _deckStatus = () => {};\n"
        "const _renderCompletionPlan = () => {};\n"
        "const api = async (u) => { called = u; return { incomplete_decks: 0 }; };\n"
        + fn
        + """
const fail = (m) => { console.error(m); process.exit(1); };
(async () => {
  await loadCompletionPlan();
  if (called !== "/decks/completion-plan") fail(`blank limits must send no query, got ${called}`);
  budget = "10";
  await loadCompletionPlan();
  if (called !== "/decks/completion-plan?budget=10") fail(`got ${called}`);
  maxCards = "6";
  await loadCompletionPlan();
  if (called !== "/decks/completion-plan?budget=10&max_cards=6") fail(`got ${called}`);
  budget = "";
  await loadCompletionPlan();
  if (called !== "/decks/completion-plan?max_cards=6") fail(`got ${called}`);
})();
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_floor_total_says_at_least(tmp_path: Path, js: str) -> None:
    """`spend_usd_is_floor` must change the words, not just a tooltip.

    by_cards can include a deck whose gap has an unpriced card, so its total
    omits cards. Rendering that as an exact price is the trap `missing_usd`
    already sets.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = tmp_path / "total.js"
    script.write_text(
        _fn(js, "_planMoney")
        + _fn(js, "_planTotal")
        + """
const fail = (m) => { console.error(m); process.exit(1); };
const exact = _planTotal({ decks_completed: 3, cards_to_buy: 20, spend_usd: 9.27, spend_usd_is_floor: false });
if (!exact.includes("$9.27")) fail(`expected $9.27, got ${exact}`);
if (/at least/i.test(exact)) fail("an exact total must not read as a floor");
const floor = _planTotal({ decks_completed: 3, cards_to_buy: 5, spend_usd: 8.14, spend_usd_is_floor: true });
if (!/at least/i.test(floor)) fail(`a floor must say so, got ${floor}`);
if (!floor.includes("$8.14")) fail("the figure must survive");
// singular/plural, because "1 decks · 1 cards" reads as a bug
const one = _planTotal({ decks_completed: 1, cards_to_buy: 1, spend_usd: 1, spend_usd_is_floor: false });
if (one.includes("1 decks") || one.includes("1 cards")) fail(`bad pluralisation: ${one}`);
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_unpriced_card_renders_a_dash_not_zero(tmp_path: Path, js: str) -> None:
    """`$0.00` claims a card is free; `—` says the price is unknown.

    The same distinction D19.31 drew for the deck card table.
    """
    node = shutil.which("node")
    if node is None:  # pragma: no cover
        pytest.skip("node not available")
    script = tmp_path / "step.js"
    script.write_text(
        "const escapeHtml = (s) => String(s);\n"
        + _fn(js, "_planMoney")
        + _fn(js, "_planStep")
        + """
const fail = (m) => { console.error(m); process.exit(1); };
const html = _planStep({
  deck_name: "D", marginal_cards: 2, marginal_usd: 2.87, running_usd: 2.87,
  cost_is_partial: true,
  cards: [{ name: "Priced", qty: 1, price_usd: 2.87 }, { name: "Unpriced", qty: 1, price_usd: null }],
});
if (!html.includes("—")) fail("an unpriced card must render a dash");
if (/Unpriced[^<]*<[^>]*>\\$0\\.00/.test(html)) fail("an unpriced card must never render $0.00");
if (!html.includes("$2.87")) fail("a real price must still render");
if (!/price incomplete/i.test(html)) fail("a partial-cost step must say so");
const free = _planStep({
  deck_name: "Dup", marginal_cards: 0, marginal_usd: 0, running_usd: 9.27,
  cost_is_partial: false, cards: [],
});
if (!/already covered/i.test(free)) fail("a zero-cost step must explain itself, not show a bare $0.00");
"""
    )
    proc = subprocess.run([node, str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_two_column_layout_stacks_on_narrow_screens(css: str = "") -> None:
    text = CSS.read_text()
    assert ".plan-cols" in text
    block = text[text.index(".plan-cols") :]
    assert "grid-template-columns: 1fr 1fr" in block[:200]
    assert "max-width: 720px" in block[:600], "two shopping lists side by side need a stack breakpoint"
