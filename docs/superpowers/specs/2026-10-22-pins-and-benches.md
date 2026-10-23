# Pins, Benches and Manual Placement

## Goal

Status: accepted for a first build; the rules may change after real-world testing.

Let the operator change the quad from the web dashboard (drag a configured channel onto a tile, swap tiles, pin, bench, reset) without fighting the automatic selection, and have the quad return to a normal state on its own as channels come and go.

## Terms

- **Slot**: one of the four QuadStream tiles, numbered 0–3 (QuadStream's `stream0`–`stream3`; the relay serves them at `/streams/1`–`/streams/4`).
- **Channel**: a configured Twitch channel, keyed by its lowercase login.
- **Pool**: the live channels that pass at least one configured entry's rules in the current refresh.
- **Natural selection**: what `compose()` picks with no overrides: the four highest channels by adjusted priority (stability, saturation, diversity, continuity), with sticky slots.

## State

Two override sets, both set only by an operator action:

| State | Meaning |
|---|---|
| **Pin** (channel → slot) | The channel is always in the quad, in that slot, while it is live. |
| **Bench** (channel) | The channel is never in the quad, whatever its priority. |

A channel is never both pinned and benched. Slot positions of unpinned channels are not overrides: they are the existing sticky positions in `QuadState`.

### Lifetime

- When a pinned channel leaves the pool (offline, no video, rejected by every entry, or lookup failed), its slot refills at once by natural selection; no slot sits on filler. The pin stays on record for 2 more refreshes (about 1 minute): if the channel returns in that time, it takes its slot back and the temporary occupant leaves. After that the pin clears. A bench clears after the same grace.
- A pin or bench clears when its channel is removed from the config.
- **Reset** clears every pin and bench.
- Pins, benches and the sticky slot positions survive a daemon restart: they are saved to a state file under the service's state directory (`/var/lib/quadlink`), written atomically after every change.

## Composition

`compose(pool, state, overrides, config)` applies overrides in this order:

1. Remove benched channels from the pool.
2. Pinned channels that are in the pool take their pinned slots.
3. The remaining slots fill from the rest of the pool by adjusted priority, exactly as natural selection does today. Channels already on screen keep their slots; newcomers take free slots lowest-first.
4. Category bonuses and penalties count pinned channels as present (a pinned channel's category saturates like any on-screen channel).

Consequences:

- When a higher-priority channel comes online, it displaces the lowest-priority **unpinned** on-screen channel and takes that channel's slot. Pinned channels are never displaced.
- With four pins, the quad is frozen. Channels that would otherwise be selected show as **waiting** in the dashboard.
- If benches leave fewer than four eligible channels, slots stay empty (the relay serves filler). The bench is honored.

## Scenario

Six configured channels in one category (so category bonuses drop out), stability bonus 30: A 100, B 90, C 80, D 70, E 60, F 50. Natural selection starts with A, B, C, D in slots 0–3.

| Step | Event | Slots 0 / 1 / 2 / 3 | Why |
|---|---|---|---|
| 1 | Drag F onto slot 3 (D's tile) | A / B / C / F (pinned) | F is pinned to slot 3; D is benched. |
| 2 | G (120) comes online | A / B / G / F (pinned) | G competes for the 3 unpinned slots. The lowest unpinned on screen is C at 80 + 30 = 110; G beats it and takes C's slot. The low pin costs C its place, not F. |
| 2′ | Same, but G is 95 | A / B / C / F (pinned) | 95 does not beat C's 110: the stability bonus protects channels on screen. |
| 3 | F's stream drops | A / B / G / C | Slot 3 refills at once: C (80) is the best live channel off screen; E is lower and D is benched. F's pin waits about a minute. |
| 4a | F is back within a minute | A / B / G / F (pinned) | F reclaims slot 3; C leaves again. |
| 4b | F is gone after a minute | A / B / G / C | F's pin clears; F later competes at 50 and does not get in. |
| 5 | D goes offline | no change | D's bench clears; next time it competes at 70. |
| 6 | Drag tile 0 (A) onto tile 3 (F) | F (pinned) / B / G / A | A swap moves channels; F's pin moves with it to slot 0. |
| 7 | Pin A, B and G too | all four pinned | The quad is frozen; a new channel at 200 shows as waiting until an unpin or Reset. |

## Operator actions

| Action | Result |
|---|---|
| Drag a listed channel X onto an empty slot | X pinned to that slot. |
| Drag X onto a slot holding channel C (pinned or not) | X pinned to that slot; C unpinned and benched. |
| Drag a benched channel onto a slot | It is unbenched and pinned there; the slot's occupant is benched. |
| Drag tile A onto tile B | The two channels swap slots. A channel's pin moves with it to its new slot. |
| Pin a tile | Its channel is pinned to its current slot. |
| Unpin a tile | The pin is removed; the channel stays until natural selection displaces it. |
| Bench a tile or a listed channel | The channel is benched; its slot refills by natural selection. |
| Unbench | The channel is eligible again and returns by priority. |
| Reset | All pins and benches clear. Membership recomputes by priority; on-screen channels that stay keep their slots. |

Every action applies at once: the web handler updates the overrides and requests a recompose. No action waits for the next refresh.

## Dashboard states for a configured channel

- **in quad** (slot N), with a pin marker when pinned
- **benched**
- **waiting**: live and would be selected, but the quad is full of higher or pinned channels
- **live**: live but not selected
- **offline**, **no video**, **rejected** (with the reason), **lookup failed**

## Out of scope

- Authentication (network-level access for now; passkeys later, behind the same request check the config API uses).
- Pins or benches that persist across a channel's streams (both clear when it goes offline).
- Automatic expiry by time.
