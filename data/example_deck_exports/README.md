# Example deck exports

Real exports captured from Moxfield, Archidekt and Deckbox, kept as sample
input for deck import (`POST /decks/import`) and as a reality check on
`decklist/parser.py`.

Committed because guessing at third-party formats goes wrong: the parser only
matched real files once real files were used, and the same trap applies here.

| File | Source | Shape |
| --- | --- | --- |
| `moxfield/big-boys-plain.txt` | Moxfield | `1 Card Name` |
| `moxfield/big-boys-moxfield.txt` | Moxfield | `1 Card Name (SET) 123` |
| `moxfield/big-boys-arena.txt` | Moxfield → Arena | `About` / `Name <deck>` header block, then `1 Card (SET) 123` |
| `moxfield/big-boys-mtgo.txt` | Moxfield → MTGO | `1 Card Name` |
| `moxfield/big-boys-20260817-200848.txt` | Moxfield | timestamped filename, plain body |
| `archidekt/colorless_arsenal.txt` | Archidekt | `Commander` / section headings, then `1 Card Name` |
| `deckbox/iroh-gift.txt` | Deckbox | `1 Card Name`, leading space on the first line |
| `deckbox/Iroh gift_2026.August.18.csv` | Deckbox | deck-scoped CSV — note the `Section` column (`main` / sideboard) that the collection export lacks |
| `deckbox/Deckbox.org card set print view.html` | Deckbox | print view, `<br>`-separated |

## Quirks the parser handles

These exports contain details that are easy to mistake for cards. The parser
handles each of them:

- **Section headings are not cards.** Archidekt writes `Commander`, and the
  Arena export opens with `About` / `Name Big Boys`. Headings are recognised and
  skipped, and a `Commander` heading sets the commander.
- **Set/collector suffixes** (`(SET) 123`) are stripped from card names.
- **Leading whitespace** before the quantity is tolerated.
- The Deckbox deck CSV carries a `Section` column (`main` / sideboard) that the
  collection export does not; the importer reads it.
- The plain and Arena exports of "Big Boys" are different snapshots of the deck
  and contain different card lists.
