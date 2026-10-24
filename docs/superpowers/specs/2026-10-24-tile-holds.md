# Decision memo: tile holds with put-back

> **Amendment (2026-10-24): a hold remembers one stream.** In use, the ordered list made repeated drops on one tile hard to read: each drop added a row to "Replaced". Now a hold is {holder, replaced}, where replaced is the stream on the tile before the first drop. A drop on a held tile releases the old holder to auto. Put back is the same as Unpin on that tile. Where this memo describes list[1..] or put back with i > 0, the amendment replaces it.

## 1. Recommendation

A drop on a tile makes a **hold**: "this tile shows X, and these streams were put off it, in order". "Replaced" is not a separate state. A stream is replaced only while it is on a live hold's list, so it comes back when that hold ends. Each reversal returns the tile to a state the user already saw. **Unpin** goes back to the tile as it was before the first drop. **Put back** goes back to the moment just before one chosen stream left. A reversal never changes another tile and never adds a pin the user did not make.

## 2. States and lifecycles

| State | Stored? | Starts | Ends | On end |
|---|---|---|---|---|
| Auto | no | default | n/a | n/a |
| Hold(slot) = {holder, replaced: ordered list} | yes, max 1 per slot (≤4) | a drop or click-to-place on a tile | Unpin, Put back on list[0], Unpin all, × on an extra holder, holder placed on another tile, or holder offline for 2 refreshes (~1 min) | the tile goes to list[0], unpinned, if list[0] is live. If not, auto fills the tile. Every other stream on the list goes back to auto. |
| Replaced | derived: membership in a list | when a drop puts the stream off a tile | when its hold ends, on Put back, or when the user places it | see above |
| Extra (quick add) | yes, in memory | Quick add | × or restart | if it holds a tile, the hold ends as with Unpin |
| Waiting / In quad / Live / Offline | derived | n/a | n/a | n/a |

- **list[0]** is always the auto stream that was on the tile when the hold started. **list[1..]** are the earlier holders, in order.
- A replaced stream has no timer and no grace. If it goes offline, it stays on the list, so it cannot reappear on another tile during the hold.
- An extra enters the quad only as a holder and is never on a list.
- **Removed:** manual bench, unbench, pin-in-place, the bench glyph, the "Benched" filter and stream affinity.
- **Restart:** holds whose holder is a config channel persist. Holds by extras end with the normal handover.

## 3. Gestures and controls

**Drop X on the tile that shows Y** (click-to-place works the same way). It means "X here, Y goes away". The other three tiles never change.

1. **Tile has no hold:** the drop makes {X, [Y]}. If Y is an extra, Y is left off the list and goes back to "Added".
2. **Tile already has a hold:** X becomes the holder, and the old holder goes on the end of the list (not if it is an extra). Entries already on the list stay off, so a later drop never brings anyone back. This fixes history item 2.
3. **X is on another tile** (a tile drag or an in-quad row): the drop is a **swap**. The streams exchange tiles, holds move with their holders, and no pin or replacement is made. Sticky positions keep unheld streams where they land.
4. **X is on another hold's list:** X leaves that list and holds this tile. The other hold continues.
5. **X is already on this tile:** nothing happens and nothing pulses.

**Reversals:**

| Control | Effect | Undoes |
|---|---|---|
| Hollow slot chip on a replaced row ("Put back") | Uses the stream's index i on the list. If i = 0, the hold ends and Y gets the tile back, unpinned. If i > 0, Y holds the tile again and the list is cut to list[0..i-1]. The current holder and the later entries go back to auto (extras go to "Added"). | Replaced |
| Pin glyph on the tile ("Unpin") | The same as Put back on list[0]. | Hold |
| × on an extra | Removes the extra. If it holds a tile, the hold ends as with Unpin. | Extra |
| "Unpin all" (top of the rail) | Runs Unpin on every held tile. Extras stay. No confirm. | All holds |
| Drag again | A new drop. Dragging a replaced stream onto its own old tile replaces the holder, because the user chose that tile. | Anything |
| Swap again | Swaps the tiles back. | Swap |

## 4. What the rail and the quad show

**Rail order:**

1. In-quad rows, in slot order.
2. A **"Replaced"** header (the same style as "Added", shown only when it has rows), with rows in tile order and then list order.
3. **"Added"**.
4. The rest, filtered and sorted.

Rows that change section animate their move, so the list does not jump under the pointer.

**Chip primitive, three looks:**

- **Viewers:** flat, a fact.
- **Filled slot number:** flat, a fact.
- **Hollow slot number:** the only button on a replaced row. On hover it takes the accent color (not red), and the glyph does not change. Hover or focus also outlines the target tile and shows a ghost thumbnail of the stream that will come back.
  - If the replaced stream is offline, the chip goes flat and does not respond. A Put back would only put filler on the tile.

**Replaced row:** the thumbnail and name are dimmed to 55%. The meta line shows the category, or "Offline".

**Extra row:** × always shows and turns red on hover. An extra never gets a hollow chip.

**Holder rows:** no mark. The pin shows only on the quad.

**Quad:**

- **Auto tile:** video only.
- **Held tile:** the pin glyph on the scrim. On hover it turns red and the tile shows a ghost of list[0], or nothing when auto will fill the tile. The result is certain, so the preview is honest. A click unpins.
- **Held tile, holder offline (grace):** the dimmed last frame, the pin, and the one word "Offline".
- **Pending:** every tile that an action changes pulses until the snapshot confirms the change. This covers the TV delay and stops repeat clicks. If the change fails, the tile shows the existing failed outline and the row goes back.
- No names, counts or other status text on tiles. A click on a tile still toggles sound.

**Exact wording:**

| Element | Text |
|---|---|
| Pin tooltip | "Unpin A" |
| Pin aria-label | "Unpin A, bring back B" (or "Unpin A") |
| Hollow chip tooltip | "Replaced by A" |
| Hollow chip aria-label | "Put B back on tile 2" |
| Replaced row aria-label | "B, replaced by A on tile 2" |
| Rail header | "Replaced" |
| Filters | "Live", "Waiting", "Replaced", "In quad", "Offline" |
| × tooltip and aria-label | "Remove A" |
| Top-of-rail button | "Unpin all" |
| Row meta | "Waiting · Category", "Offline", or the category |
| Tile scrim in grace | "Offline" |
| Quick add placeholder | "Add a Twitch channel" |

Never used: bench, benched, unbench, hide, hold, out, kept out, grace, override, restore, slot numbers inside words.

## 5. Edge cases

- **Quick add over B:** the hold is {E, [B]}.
  - × on E, or E offline past the grace: B gets the tile back, unpinned.
  - C dropped over E: the hold is {C, [B]}. E goes back to "Added" with no mark.
- **Repeated drops** (A over B, then C over A): the hold is {C, [B, A]}, and both rows show a hollow "2".
  - Put back A: {A, [B]}, the exact state before C's drop. C goes to auto.
  - Put back B, or Unpin: B is on the tile, unpinned. A and C go to auto. Auto did not choose them before the drops, so they normally stay in the rail.
- **Top-priority Y replaced:** Y stays off for the whole hold, as the first row under "Replaced". It never takes another tile or pushes out the lowest stream. When the hold ends, Y gets its own tile back directly.
- **Holder offline:** the tile shows "Offline" with the pin for about 1 minute, then the Unpin handover runs. If the holder comes back during the grace, nothing changes.
- **Replaced stream offline:** it stays on the list, and its chip goes flat. If it is list[0] when the hold ends, auto fills the tile. The handover does not go down the list.
- **Swap:** holds and lists move with their holders, and the hollow chip numbers update. No new pins.
- **Holder placed on another tile:** its old hold ends with the handover, and a new hold forms on the target.
- **Drop on an empty or loading tile:** the hold is {X, []}. Unpin gives the tile to auto.
- **Unpin all:** up to 4 tiles pulse, and "Replaced" empties.
- **Phone:** no hover preview. A tap on a chip or the pin acts at once, and the target tile pulses. Click-to-place stays as row tap, then tile tap.

## 6. Why this design, and the panel's split rulings

**Against the current model and the rejected options:**

- **vs. current pin + bench + tie:** one stored object, not three. No standalone bench state that can outlive or drift from its pin.
- **vs. pins only (item 4):** the displaced stream cannot move to another tile, because it is on the list until the hold ends.
- **vs. option a (unbench and compete):** Put back gives the same tile back, decided before the click. No ranking race.
- **vs. option b (reverse drop):** b made a new pin and a new casualty. Put back is an undo: it makes no pin, puts no stream off, and the ghost preview shows the result first.
- **vs. option c (info-only chip):** the chip is a control.
- **vs. hide (item 5):** nothing is hidden. Replaced rows sit directly under the quad block.
- **vs. bench wording (item 6):** a hollow tile number plus "Replaced by A". No new noun.

**Panel splits and rulings:**

- **How Unpin releases the list.**
  - Legibility and advocate: the listed streams compete with tile affinity.
  - Interaction and model: the tile goes directly to the first stream on the list.
  - **Ruling: direct handover, and Unpin is the same as Put back on list[0].** It is deterministic, so the hover preview is honest. It needs no new affinity logic, and the two controls never disagree on the same question.
- **list[0] offline at Unpin.** Interaction gives the tile to the first live stream on the list. Model gives it to auto. **Ruling: auto.** The first live entry would be an earlier holder: it was pinned before, and the handover would return it unpinned. That is not a state the user saw.
- **Status word on a tile in grace.** Legibility and interaction: dimmed frame and pin only. Model and advocate: the word "Offline". **Ruling: "Offline".** A dimmed frame alone reads as loading. The word is the same one the rail uses, not a status sentence.
- **Put back of an offline stream.** Legibility and advocate: the click works and the tile shows filler. Model: the chip is disabled. **Ruling: disabled.** The click would only produce a dead tile.
- **Restart.** Model and advocate: everything is lost. Interaction: config holds persist. **Ruling: persist config holds.** A deploy must not undo a deliberate drop.
- **Row moves.** Some panelists wanted to delay the move until the pointer leaves the rail. Others wanted an animation. **Ruling: animate.** A delayed move hides state.
- **Tooltip.** Legibility wanted a two-line tooltip. **Ruling: keep the approved one-line "Replaced by A".** The accent hover and the ghost preview already show the action.
- **Advocate's "Put back always pins Y" and legibility's pins on swaps:** rejected by the panel majority, because both create pins the user did not make.

**Open risks for the user to confirm:**

- With no manual bench, the only way to keep a stream off without a drop is the config.
- Put back must feel different from option b. The user must try it.
- Streams released to auto can still win another tile later. This happens only after an explicit Unpin or Put back.

## 7. Server and API changes

- **State:** replace the pin, bench and tie records with `holds: {slot: {holder, replaced: [channel…]}}`. Derive "replaced" and "waiting" from it. Persist config holds. Keep extras in memory.
- **Selection:** treat every channel on a list as not eligible. On hold end, put list[0] on the slot if it is live, then run auto. Sticky positions do the rest.
- **API:** `place(channel, slot)` (covers the make, extend, swap, take-from-list and no-op cases), `swap(a, b)`, `unpin(slot)`, `put_back(channel)`, `unpin_all`, `add`, `remove`. Remove `pin`, `bench`, `unbench` and `reset`.
- **Snapshot:** for each slot, send the holder, the ordered list and the grace flag. Send nothing per row beyond what the client derives.