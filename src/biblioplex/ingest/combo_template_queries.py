# ruff: noqa: E501
"""Our queries for Spellbook templates that ship none, or one our DSL can't run.

Keyed by Spellbook template id; wins over Spellbook's ``scryfallQuery``.
Templates absent here and without a runnable query stay unchecked.
"""

from __future__ import annotations

TEMPLATE_QUERIES: dict[int, str] = {
    # Spellbook's queries need Scryfall's `\sc` and ignore reminder text.
    37: r"t:land o:/^cycling (\{B\}|\{1\}|\{2\}|\{1\}\{B\})( \(|$)/",
    38: r"t:land o:/^cycling (\{1\}|\{[WUBRG]\})( \(|$)/",
    82: r"t:creature (o:/\{t\}[^\n:]*: add (\{[wubrgc]\}){2,}/ or o:/\{t\}[^\n:]*: add (two|three|four|five|x) /)",
    84: r"o:/don't lose the game (for|due to|because of) having 0 or less life/",
    85: "otag:gives-haste -t:instant -t:sorcery",
    87: "otag:gives-deathtouch -otag:gives-deathtouch-noncreature",
    88: r"(t:instant or t:sorcery) o:/(^|\. )(target player )?takes? an extra turn after this one\./"
        r' -o:"exile ~" -o:library -o:"lose the game"',
    92: r"t:creature o:/when (this creature|[^,\n]*) enters[^\n]*create (two|three|four|five|x|\d+) [^\n]*creature tokens/",
    96: r"o:/\{t\}[^\n:]*: [^\n]*deals? \d+ damage to any target/",
    99: r"o:/at the beginning of (your|each|the)[^\n.]*(create|creates) [^\n.]*creature token/ -t:instant -t:sorcery",
    101: r"o:/prevent all combat damage that would be dealt (to|by) [^\n.]*creature/",
    106: r"o:/(landfall|whenever a land (you control )?enters)[^\n]*create[^\n]*creature token/",
    109: r'o:/(creatures?)[^\n.]*(get|gets) -1\/-1/ -o:"until end of turn"',
    110: r"t:creature mv=1 o:/when (this creature|[^,\n]*) dies[^\n]*create[^\n]*creature token/",
    112: "otag:anthem",
    118: "otag:animate-land",
    119: r"o:/tap an untapped creature you control:/",
    120: r"t:creature o:/when (this creature|[^,\n]*) enters[^\n]*create [^\n]*creature token/",
    157: r"t:creature o:/(when|whenever)[^\n]*(enters|dies)[^\n]*you gain ([4-9]|1[0-9]) life/",
    183: r"t:creature o:/(enters|enter)[^\n]*earthbend/",
    195: r"o:/(if|as)[^\n]*\+1\/\+1 counters? would be (put|placed)[^\n]*(plus|twice|that many|additional)/",
    196: r"o:/(each|another|other|a|any) [^\n.]*creature[^\n.]*enters[^\n.]*(with|put) (an|a|one|two|x|that many)"
         r"[^\n.]*\+1\/\+1 counter/ -t:instant -t:sorcery",
    199: r"o:/untap (up to )?(five|six|seven|eight|x|all) (target )?(other )?lands/ -t:instant -t:sorcery",
    204: r"t:creature mv=1 o:/when (this creature|[^,\n]*) (enters|dies)[^\n]*create[^\n]*creature token/",
}
