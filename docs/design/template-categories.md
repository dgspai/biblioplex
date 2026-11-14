# Template categories — strawman definitions

Draft for user red-line. Every **edge ruling** below is the lazy default
the classifier will follow until you say otherwise. Flip any of them and
both the gold set and the rules layer adjust accordingly.

Categories are **multilabel** — a card can occupy more than one
functional slot (e.g. a creature that ramps *and* draws). `plan` is the
residue and is **mutually exclusive** with the four functional labels in
practice: assign `plan` iff no functional label fits.

## 1. Ramp

**Definition.** Non-land mana acceleration: produces, finds, or doubles
mana faster than the natural one-land-per-turn drop.

**Positives**

- Sol Ring · Arcane Signet · Mana Crypt · Mind Stone
- Cultivate · Kodama's Reach · Rampant Growth · Nature's Lore
- Llanowar Elves · Birds of Paradise · Elvish Mystic · Bloom Tender
- Smothering Tithe · Treasure Map · Wayfarer's Bauble

**Negatives**

- Reflecting Pool · Command Tower · Mana Confluence  *(lands — see ruling R1)*
- Lightning Bolt · Counterspell  *(not mana-producing)*
- Mana Drain  *(adds mana but as side effect of counter — see ruling R2)*

**Edge rulings**

- **R1 — Lands are never ramp.** Even multi-color or fetch lands count
  as `land` (the 38-slot, excluded from the model output). The land
  slot is structural; the ramp slot is for cards that *accelerate
  beyond* the land drop.
- **R2 — Counterspells that also add mana** (Mana Drain) → `targeted-disrupt`
  only. The primary function is the counter; the mana is a bonus, not
  the reason it's in the deck.
- **R3 — Mana-fixing rocks that don't add net mana** (Chromatic Sphere,
  Prismatic Lens) → `ramp` (lazy default — they enable the next-turn
  play even if mana-neutral on cast).
- **R4 — Treasure-makers** (Smothering Tithe, Goldspan Dragon) → `ramp`.
  They generate disposable mana. Tithe is *also* `card-advantage` in
  some readings but lazy default: primary slot is ramp.
- **R28 — Cost reducers** (Foundry Inspector, Etherium Sculptor, Cloud
  Key) → `ramp`. Discounting every later spell is mana. A card that only
  discounts itself is not.

## 2. Card advantage

**Definition.** Net positive cards. A card is CA iff *one* of:

1. **Pure draw, ≥ 2 cards** (Divination, Sign in Blood, Concentrate).
   A spell that draws exactly 1 card (Inspiration cast for 3, any
   "draw a card") is **not** CA — net hand-size after casting is 0.
2. **Loot / rummage with net positive** — *drawn − discarded ≥ 1*
   (Cathartic Reunion: 3 − 2 = +1 → CA). Even draws like Faithless
   Looting (2 − 2 = 0) are **not** CA.
3. **Repeatable draw engine across turns** (Rhystic Study, Mystic
   Remora, Phyrexian Arena, Sylvan Library, Necropotence, Esper
   Sentinel). The engine carve-out — per-trigger net can be small or
   zero, but cumulative across the game is decisive. Conditional
   engines count too: combat-damage triggers (Reconnaissance Mission,
   Bident of Thassa, Coastal Piracy), end-of-turn triggers (Outpost
   Siege), ETB-on-other-permanent triggers (Risen Reef, Beast Whisperer)
   all qualify.

Card *selection* (look at N, keep 1, no net gain) is not CA. Tutors
are not CA. One-shot recursion of a single card is not CA.

**Positives**

- Rhystic Study · Mystic Remora · Esper Sentinel · Sylvan Library  *(engines — Rule 3)*
- Phyrexian Arena · Necropotence  *(engines — Rule 3)*
- Reconnaissance Mission · Bident of Thassa · Coastal Piracy  *(combat-damage engines — Rule 3)*
- Outpost Siege · Risen Reef · Beast Whisperer  *(conditional engines — Rule 3)*
- Sign in Blood · Read the Bones · Divination · Concentrate  *(pure draw N≥2 — Rule 1)*
- Mulldrifter  *(ETB draws 2 = net +1 — Rule 1)*
- Wheel of Fortune · Windfall · Timetwister  *(R8 — wheels default CA)*
- Cathartic Reunion  *(loot, drawn−discarded = +1 — Rule 2)*

**Negatives**

- Brainstorm · Ponder · Preordain · Opt  *(card selection — R6)*
- Sensei's Divining Top  *(card selection)*
- Demonic Tutor · Vampiric Tutor  *(tutors — R7)*
- Eternal Witness · Regrowth · Reclaim  *(one-shot recursion of 1 card — R9)*
- Elvish Visionary · Cloudkin Seer  *(ETB draws 1 = net 0 — R10)*
- Faithless Looting · Tormenting Voice  *(loot, net 0 — R5')*
- Inspiration  *(single-card draw — Rule 1)*

**Edge rulings**

- **R5 — Cantrips on removal** ("Destroy target X. Draw a card.") →
  `targeted-disrupt` **only**. Draw is +1, cast cost is −1, net
  hand-size = 0. The removal is the slot.
- **R5' — Loot effects** (draw N, discard M). CA iff
  *drawn − discarded ≥ 1*. Cathartic Reunion (3−2 = +1) → CA.
  Faithless Looting (2−2 = 0) → `plan`.
- **R6 — Card selection is `plan`, not CA.** Brainstorm, Ponder,
  Preordain, Opt, Sensei's Divining Top: net cards = 0. They dig but
  don't accumulate.
- **R7 — Tutors are `plan`, not CA.** Demonic / Vampiric / Mystical
  Tutor net 0 cards (one in, one out). They execute the plan by
  fetching; they don't enlarge your hand.
- **R8 — Wheels default CA.** Wheel of Fortune, Windfall, Timetwister
  → CA always. Strict per-cast math is situational (wheel with a full
  hand nets 0), but deckbuilder intent treats wheels as CA — that's
  why they're in the deck.
- **R9 — One-shot recursion of a single card** (Eternal Witness,
  Regrowth, Reclaim, Eternal Scourge-style self-return) → `plan`. Net
  hand-size unchanged. Card flow is real but not CA.
- **R10 — ETB-draw creatures, count-dependent.** Net = drawn − 1.
  Mulldrifter (draws 2 = +1) → CA. Elvish Visionary (draws 1 = 0) →
  `plan`. Solemn Simulacrum (death-trigger draws 1 = 0) → `plan` for
  CA purposes, but already `ramp` from its ETB land-fetch (R4
  pattern).
- **R29 — Playing off the library top** (Future Sight, The Reality
  Chip, The Lunar Whale) → CA. A Rule 3 engine: each turn the top card
  is an extra card.
- **R30 — `{T}` abilities that draw** (Iron Lad, Diverging Destiny) →
  CA, a Rule 3 engine. Break-even tap loot stays `plan` (R5'), as does
  a draw that sacrifices the card (Mind Stone).

## 3. Targeted disruption

**Definition.** Interacts with a *specific* opponent permanent, spell,
or resource. One card removed / countered / hated per cast.

**Positives**

- Swords to Plowshares · Path to Exile · Doom Blade · Go for the Throat
- Counterspell · Negate · Mana Drain · Force of Will
- Beast Within · Generous Gift · Anguished Unmaking
- Bojuka Bog · Tormod's Crypt · Relic of Progenitus
- Diabolic Edict · Liliana's Triumph

**Negatives**

- Wrath of God · Damnation  *(mass — see Section 4)*
- Propaganda · Ghostly Prison  *(pillowfort — see R13)*
- Sol Ring  *(no interaction)*

**Edge rulings**

- **R11 — Counterspells count.** Even though MtG-rules "target" is
  narrower, deckbuilder usage puts counterspells squarely in
  interaction. All counterspells → `targeted-disrupt`.
- **R12 — Silver-bullet hate** (Bojuka Bog, Tormod's Crypt, Grafdigger's
  Cage) → `targeted-disrupt`. They disrupt a specific resource (a
  graveyard, the library). For lands (Bojuka Bog), the card still lives
  in the `land` slot for *count* purposes, but its functional label is
  `targeted-disrupt` so the deckbuilder can see "my 38 lands include 1
  graveyard hate."
- **R13 — Pillowfort / group-hate taxation** (Propaganda, Ghostly
  Prison, Smothering Tithe-as-defense-effect) → `plan`. Doesn't
  remove or counter anything specific; it taxes future actions.
- **R14 — Edicts** (Diabolic Edict, Plaguecrafter,
  Innocent Blood) → `targeted-disrupt`. No formal "target", but
  functionally removes one creature per player. Don't tag as mass.
- **R15 — Modal removal** ("destroy target creature *or* enchantment")
  → `targeted-disrupt`. One target per cast regardless of mode.

## 4. Mass disruption

**Definition.** Removes, taxes, or neutralises *multiple* permanents
or resources across players in one effect.

**Positives**

- Wrath of God · Damnation · Day of Judgment · Supreme Verdict
- Toxic Deluge · Cyclonic Rift · Blasphemous Act · Pyroclasm
- Vandalblast · Bane of Progress · Austere Command
- Armageddon · Ravages of War  *(mass land destruction)*

**Negatives**

- Doom Blade · Go for the Throat  *(targeted, not mass)*
- Massacre Girl  *(triggered creature — see R17)*
- Propaganda  *(pillowfort — `plan`)*

**Edge rulings**

- **R16 — Damage-based mass removal** (Pyroclasm, Anger of the Gods,
  Blasphemous Act) → `mass-disrupt` even when not all creatures die;
  the *effect* hits everything.
- **R17 — Creatures that mass-remove on a trigger** (Massacre Girl,
  Plague Wind on a stick) → multilabel `mass-disrupt + plan`. The
  creature body and recursion potential warrant the plan tag too.
- **R18 — One-sided sweepers** (Plague Wind, Toxic Deluge for 0, Living
  Death asymmetries) → `mass-disrupt`. The asymmetry doesn't change
  the slot.
- **R19 — Mass artifact / enchantment removal** (Vandalblast, Bane of
  Progress, Farewell) → `mass-disrupt`. Same slot as creature wipes
  for template purposes; the deck wants ~6 of these total.
- **R20 — Mass land destruction** (Armageddon, Ravages of War) →
  `mass-disrupt`. Politically loaded, but functionally a sweeper.

## 5. Plan

**Definition.** Everything that defines *this deck's* path to winning
and isn't already covered by the four functional categories: tutors,
finishers, threats, value engines, payoffs, role-players, cantrips,
selection, pillowfort. The biggest slot (30) and the most varied.

**Positives**

- Demonic Tutor · Vampiric Tutor · Worldly Tutor  *(tutors — R7)*
- Brainstorm · Ponder · Preordain · Opt  *(cantrips — R6)*
- Eternal Witness · Regrowth · Reclaim  *(one-shot recursion — R9)*
- Elvish Visionary · Cloudkin Seer  *(ETB single-draw — R10)*
- Faithless Looting · Tormenting Voice  *(net-zero loot — R5')*
- Craterhoof Behemoth · Triumph of the Hordes  *(finishers)*
- Avenger of Zendikar · Doubling Season  *(threats / payoffs)*
- Propaganda · Ghostly Prison  *(pillowfort — R13)*
- Cathars' Crusade · Edgar Markov  *(payoff / engine)*

**Negatives** *(in the sense "these would-be-plan cards have a higher-priority label")*

- Llanowar Elves  *(`ramp`)*
- Mulldrifter  *(`card-advantage`)*
- Swords to Plowshares  *(`targeted-disrupt`)*

**Edge rulings**

- **R21 — Plan is the residue.** Assign `plan` iff *none* of the four
  functional labels score above their threshold. Don't double-tag
  `plan` alongside a functional label; the deckbuilder counts plan as
  "what's left after the functional slots fill."
  - Exception: R17 — creatures whose body / recursion value is the
    plan but whose ETB / trigger is functional.
- **R22 — Tutors → plan.** R7 above.
- **R23 — Cantrips → plan.** R6 above.
- **R24 — Pillowfort / hate-pieces that don't target → plan.** R13
  above.

## 6. Multi-faced cards

Applies to **MDFCs**, **adventures**, **splits**, **transforming DFCs**,
and **flip cards**. The card carries the **union** of every face's
functional labels. The classifier must read every face in `card_faces`,
run the rules layer against each, and OR the resulting label sets.

**Examples**

- **Tangled Florahedron // Tangled Vale** (creature MDFC, land back) →
  `ramp` (front-face creature taps for mana) + 1 land in the 38 slot
  (R26).
- **Sea Gate Restoration // Sea Gate** (sorcery MDFC, land back) →
  `card-advantage` (front draws N for X, classic engine wash on big
  X) + 1 land slot.
- **Bonecrusher Giant // Stomp** (adventure) → `targeted-disrupt`
  (Stomp deals 2 to any target) + `plan` (body has no functional label).
- **Murderous Rider // Swift End** (adventure) → `targeted-disrupt`
  (Swift End destroys creature/planeswalker) + `plan` (body).
- **Brazen Borrower // Petty Theft** (adventure) →
  `targeted-disrupt` (bounces non-land) + `plan` (body).
- **Fire // Ice** (split) → `targeted-disrupt` (both halves target).
- **Wear // Tear** (split) → `targeted-disrupt` (both halves destroy
  a permanent).
- **Delver of Secrets // Insectile Aberration** (transform) → `plan`
  on both faces (no functional label fits either).

**Edge rulings**

- **R25 — Union across faces.** Apply the rules layer to every face,
  OR the resulting label set. Don't average, don't pick one face.
  The classifier must read `card_faces`, not just the front-face
  `oracle_text` that `embed_source` currently uses.
- **R26 — MDFC lands count as 1 land + functional label.** A
  spell-front / land-back MDFC consumes one of the 38 land slots
  *and* carries the spell-side functional label. Deckbuilder gets a
  flex bonus without double-counting the slot. The classifier emits
  the functional label only; the land-slot bookkeeping is the
  deckbuilder UI's job.
- **R27 — Adventure creatures default `plan` on the body.** If the
  creature face has no functional label of its own (Bonecrusher Giant
  is just a 4/3), the body falls into `plan` and the adventure-side
  label carries the functional contribution. If both faces have
  functional labels (rare), union them.

## Cross-cutting principles

1. **Multilabel for split functions, single-label for primary
   functions.** Rakdos Charm targets *and* drains *and* sweeps artifacts
   modally → multilabel `targeted-disrupt + mass-disrupt`. Llanowar
   Elves only ramps → single label.
2. **Plan loses to functional labels.** R21 — never tag both `plan` and
   a functional label except for the explicit R17 case. Keeps the 30-
   plan count meaningful.
3. **Lands occupy the structural land slot, not the model.** R1, but
   they can *also* carry a functional label (R12 — Bojuka Bog, R26 —
   MDFC lands) for advisory purposes. The classifier emits the
   functional label; the deckbuilder's land counter handles slot
   accounting.
4. **CA is strictly net cards in hand.** R5, R5', R6, R7, R8, R9, R10
   collectively enforce: pure draw ≥ 2, loot net ≥ 1, or repeatable
   engine. Tutors, cantrips, single-card draw, single-card recursion,
   and break-even loot are all `plan`.
5. **Multi-faced cards aggregate across every face.** R25 — the
   classifier reads `card_faces`, not just the front face. Adventures,
   splits, MDFCs, transforms, flips all union their face-labels.
6. **When in doubt, regex first, encoder second, plan as fallback.**
   The rules-layer in Step 2 of the plan doc encodes most of these
   rulings as patterns; the KNN layer in Step 3 picks up cards whose
   templating is unusual.

## Things this doc explicitly does not decide

- Color identity / mana cost weighting for "is this castable in my
  deck" — that's the deckbuilder UI's job, not the classifier's.
- How to *rank* within a category. The scored output (Step 5 of the
  plan) gives `[0, 1]` per label; the UI sorts by that. No "ramp tier
  list" baked into definitions.
- Salt / power-level / banlist concerns. Out of scope.
