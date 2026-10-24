# Explore: decision memo

## 1. Recommendation

Explore replaces the rail on desktop and on phone. The user's own framing calls for this. The rail holds what you know, and Explore holds what you don't, so the two never need to show together. This gives Explore a full-height panel about 55% wide, which holds 9 to 12 cards. The quad stays full height beside it, live and droppable. Known channels are hidden by default, and a count line can show them again. Sort has two options: Viewers and New. The card has one chip: hollow "+", then hourglass, then "×", or the filled slot number when the stream is on the quad. A drop adds the channel to Added and starts a normal hold, so the drop is the preview.

## 2. Placement, entry and exit

- An **Explore** toggle sits in the top bar, left of "Unpin all". It shows a pressed state while Explore is open. Esc or a second click closes it. The URL gets `#explore`, so a reload keeps the mode.
- Explore is 55% of the viewport wide, with a minimum of 720px. The quad fills the rest of the main panel and keeps its top alignment. At 1440px wide, the quad is about 560px.
- "Unpin all" stays in the top bar, so you can end every hold without leaving Explore.
- On close, the rail comes back at its previous scroll position. Then it scrolls to the newest Added row and outlines it once, as quick add does. Nothing else in the rail changes.

## 3. Category finding

- At the top is a "Category…" field. It searches 250 ms after you stop typing. The dropdown shows results with 40px box art, and the field shows an hourglass while it searches.
- With the field empty, one row of pills shows, each with 24px box art. The order is: categories on the quad, then categories in your config, then the top 12 by viewers. Each category shows once.
- After a pick, a header replaces the pills: 40px box art, the name, "412 live" and a × that goes back to the pills.
- localStorage keeps the last category and sort.

## 4. Stream cards and listing

- Grid: `repeat(auto-fill, minmax(240px, 1fr))` with a 12px gap. This gives 3 columns at 1440px.
- Thumbnail: 16:9 at 440×248, the largest thing on the card. It gets a cache-buster each time Explore opens. There are no refresh timers.
- On the thumbnail:
  - viewers chip at bottom left
  - uptime ("2h 14m") at bottom right
  - action chip at top right, which never moves
- Under the thumbnail:
  - display name and language code ("EN")
  - title, clamped to 2 lines
  - up to 3 tags, in one muted line
- Sort is a glyph with two options: **Viewers** (the default) and **New** (shortest uptime first).
- Infinite scroll loads 30 per page. A sentinel sits 600px before the end.
- Known channels:
  - Config channels are hidden by default. A line above the grid gives the count: "3 known hidden · Show". "Show" brings them back, dimmed, and the line changes to "Hide".
  - Channels in Added or on the quad always show, with their chip.

## 5. Actions

| Chip | Meaning | Click |
|---|---|---|
| hollow **+** | not added | add to Added |
| hourglass | adding | none |
| **×** | in Added | remove from Added, same as the rail's × |
| filled slot number | on the quad | none |
| **Offline** | add failed | none; the + is disabled |

- **Drop:** drag a card onto a tile, or click a card and then a tile. The drop adds the channel to Added and starts a normal hold. The incoming thumbnail fades into the tile. Every hold must have a rail row that can end it, so a drop always adds.
- **Preview:** there is no separate preview, because a new player costs too much. The thumbnail and the title are enough to choose. A drop is the real preview, and the hold reverses cleanly.

## 6. Phone (<720px)

- Explore stays in the top bar. The 32svh quad is sticky at the top. Explore replaces the rail as the page body.
- 2 columns. The title is clamped to 1 line, and tags are hidden.
- Pills scroll sideways in one row.
- To place a stream, tap a card (it gets an outline), then tap a tile. Drag is not offered.

## 7. States and wording

- **Loading:** 6 skeleton cards in the grid size, with grey placeholder pills. They pulse, but not under `prefers-reduced-motion`.
- **No category match:** `No category matches "xyz".`
- **Empty category:** `No one is live in Chess.`
- **All known:** `All 14 live streams are known.` with **Show**.
- **Fetch failed:** `Twitch did not answer.` with **Try again**. Cards already loaded stay. A failed next page shows the same text at the end of the list.
- **End of list:** `That's everyone.`
- **Add failed:** the card shows the "Offline" chip and the + is disabled.

## 8. Panel splits and rulings

- **Placement (rail replaced, or Explore under the quad):** Explore replaces the rail. Under the quad, Explore gets about 480px of height, or 1.5 rows of cards. That moves the cramped feeling instead of fixing it. "Unpin all" and a one-click close cover undoing a hold.
- **Known channels (dim, hide, or hide with a count):** hide, with a count and Show. Hiding cuts clutter for "find new", and the count means nothing is hidden without the user seeing it.
- **Sort (none, or Viewers/New):** Viewers and New. New is the only tool that brings up small, fresh streams.
- **Tags:** one muted line on desktop, hidden on phone. Tags are a fast signal for unknown streams.
- **Language:** always show the code. A "your language" rule needs a hidden setting.
- **Added chip (✓ or ×):** ×. It is the rail's own chip with the rail's own meaning.
- **Memory:** localStorage for category and sort, plus `#explore`. These are per-viewer conveniences.
- **Close behavior:** outline the newest Added row once. This matches quick add.

## 9. Open questions

1. While Explore is open, the rail is hidden. Is "Unpin all" plus closing Explore enough to end holds and put streams back?
2. Should the New sort exist? It depends on Twitch GQL support (see 10).
3. Should an Added channel get a way to move into the config, or is that a separate feature?

## 10. Server/API needs

- `GET /api/explore/categories?q=`: name search with box art. Cache for 5 min per query.
- `GET /api/explore/categories/top`: top 12. Cache for 60 s.
- `GET /api/explore/streams?category=&sort=viewers|new&cursor=`: pages of 30 with login, display name, title, viewers, thumbnail template, started_at, language, tags and next cursor. Cache for 60 s per key.
  - The server marks each stream `known: config | added | slot:N`, so the client does not repeat that logic.
  - New uses the GQL directory sort `RECENT`. If GQL does not offer it, we drop New. Sorting only the loaded pages on the client would be wrong.
- Add and remove reuse the existing quick-add endpoints with no change. A drop calls add, then the existing hold call.
- Thumbnails load straight from the Twitch CDN at `{width}x{height}` = 440×248, plus `?t=<open time>`.
- All of this sits behind one interface, so a Helix version can replace GQL later.