# Leash design spec

Source of truth for how the Leash frontend should look. Exported from Bhavi's Claude Design file.
`dashboard.html` in this folder is the full merchant dashboard mock (open it in a browser, or read the
inline styles). It's a design mock with fake data: build it with our real components and real API data,
and build the table as AG Grid (theme params + cell renderers), not as an HTML table.

## Tokens

Fonts (Google Fonts):
- Sans: Schibsted Grotesk 400-800. Everything by default.
- Mono: IBM Plex Mono 400-600, `font-variant-numeric: tabular-nums`. Money, times, ids, labels.
- Serif: Newsreader italic 400/500. Only for the buyer's mandate quote.

Colors:
| Role | Value |
|---|---|
| Page background | #F5F6F4 |
| Surface (cards, grid) | #FFFFFF |
| Border | #D8DDE2 |
| Row divider | #E3E6E9 |
| Row hover outline | #C9CFD5 |
| Ink (text, primary buttons) | #0F1215, hover #2A3038 |
| Muted text | #4D5560 |
| Faint text | #5E6670 |
| Secondary button hover | #F0F2F4 |
| Mismatch / held accent | #C2410C (dark text on light bg: #8A2E08) |
| Sharp-mode banner | bg #FDF0E9, border #F3C9B2 |
| Highlight (changed value, new row flash) | #FFE58A |
| Links, focus ring, "Fixed" | #0B63CE (text #0A55B0) |
| Fix panel | bg #F2F7FD, border #B9D3F3, label #0A55B0 |
| Captured / matches | #1F7A4C (text #17603B), bg #EAF5EE |
| Dark panel (login left side) | #0F1215, secondary text #AEB5BD |

Shape: 4px radius on buttons and inputs, square boxes elsewhere, 1px borders, no shadows. Pills (status
chips) are fully rounded with a 1px colored border, a 6px dot, 12.5px weight 600 text.

Type scale: h1 40px / 800 / letter-spacing -0.035em, line-height 1. Big empty-state h1 52px. Section
title 28px / 800. Body 15.5 to 17px. Labels `.lab`: mono 11.5px, weight 500, letter-spacing .07em,
uppercase, muted.

Diff marks (used on the quote, receipts and the grid "What changed" column):
- `.del` old value: faint text, orange #C2410C 2px strike drawn as a background line at 58%, animates
  in over .24s
- `.ins` new value: #FFE58A background, sans, weight 700, padding 0 5px

Motion: transitions .14s cubic-bezier(.2,0,0,1). New grid rows flash #FFE58A and fade over 1.2s. Live
dot pulses every 2s. All animation off under `prefers-reduced-motion`.

Buttons: primary = ink bg, white text, 52 to 58px tall, weight 700. Secondary = white with 1px ink border,
weight 600. Every tap target at least 44px.

Logo: LeashMark (circle with a check whose tail curls into a hook) + "leash" lowercase, weight 800,
letter-spacing -0.03em.

## Screens

### Merchant dashboard (dashboard.html, 1440 wide)
- Top bar: logo, "Fernwood Supply Co", "Sandbox" tag, nav (Orders, Disputes with a count), Reset demo,
  avatar.
- Hero: `.lab` "Held, authorized but not charged", h1 with the held count, one line: "Any agent's order
  lands here. Clean matches charge on their own. The rest wait for a person, with a fix when the
  catalog has one."
- Stat tiles from GET /stats: orders, charged on their own, held now, median decision. Only show tiles we
  have real data for (no "Disputes won", we don't track outcomes; "Fixed" only once accept-fix exists).
- Rubber-stamp watch banner when `rubber_stamp_active`: "Rubber-stamp watch is on", "Your last 3
  approvals came in under 2 seconds each. While this is on, only risk 60 and up escalates, the reasons
  get sharper, and unsure checks always come to you." plus the recent decision times, fast ones in orange.
- Grid header: "Orders", "Live, updated every 3s" with a pulsing dot, filter segmented control (All, Held,
  Fixed, Captured, Voided, Disputed; ink fill on the active one), "Group by agent" toggle.
- Columns: Order, Item, Amount (right aligned, mono), Status (pill), Risk, What changed (diff marks, or
  "Nothing, matches mandate"), Suggested fix, Decision (who/how + time in mono, under 2s in orange).
- Headers: mono 11.5px uppercase muted, 40px tall, 1px ink bottom border. Rows 50px, 1px #E3E6E9 divider.
  Held rows get the orange accent.
- Group rows (by agent): agent name, key, "N orders today", "X% needed a human" and a breakdown.
- Footnotes under the grid: "1.1s in orange: a person approved in under 2 seconds" and "Fixes come from
  the catalog, never from Claude".
- Side panel on row click: order id, status, item, amount, "Ordered by <agent> (<key>)", replaced-by /
  replaces links, the mandate quote with diff marks, Claude's check + risk + reason, "Unsure always goes
  to a person, even in sharp mode, whatever the risk score.", the suggested fix, action buttons that go
  through the single-use review link ("These open the order's single-use review link, so the server times
  the decision."), and for disputed orders an evidence pack card (PDF name, size, dispute id, Download
  PDF, list of what's inside) plus a timeline.
- Do NOT build: Undo after capture (captures can't be undone), pagination footer (AG Grid handles it).

### Empty state (grid)
`.lab` "Held, authorized but not charged", 52px h1 "Nothing needs you right now". White box with a dashed
#C9CFD5 border: green-outlined logo square, "The queue is clear.", "When any agent's order doesn't
match what someone asked for, it turns up as an amber row in the grid, before any money moves." and on
the right a divider then `.lab` "Charged on their own" with the real count in 44px mono green.

### Loading state (grid)
Same header and filters, 8 skeleton rows: 9px #E3E6E9 bars of varied widths, pill-shaped skeleton in
Status, pulsing 1.4s. `aria-busy="true"`.

### Login (1200 x 760)
Two columns. Left (flex 1, #0F1215, white, 48px padding, spread top to bottom): logo in white; the quote
in 40px serif italic "White running sneakers, women's ~~8~~ **8.5**, under $110" with diff marks, then
"Leash holds every agent's PayPal order until it matches what the person asked for." in #AEB5BD 18px;
bottom `.lab` "PayPal sandbox only. No real money moves." Right (520px, 56px padding, centered):
`.lab` "Merchant dashboard", h1 "Sign in to Fernwood Supply Co", read-only Username "demo" (mono), "Demo
password" field (48px, 1px ink border), hint under it, full-width 52px "Sign in", divider, a line about
Reset demo and a "How the demo works" link to the README. Under 800px wide, hide the left column.

### Confirm card (390 wide, phone page and dashboard drawer)
- Header: logo + "leash", order id in `.lab` on the right, 1px bottom border.
- `.lab` "<agent name> wants to buy", h1 "Check this order".
- White box: `.lab` "What you asked for", mandate quote 29px serif italic with diff marks on mismatched
  fields.
- Attached box below (no top border): item (16.5px bold) + store (muted), price mono 21px right; then an
  8px #C2410C square + the one-line reason ("Your agent picked size 8.5. You asked for 8.").
- Fix panel if there's a suggested fix: `.lab` "Leash found a fix", fix sentence 17px bold, muted line
  "We'll cancel the 8.5 and order the 8 instead. It gets checked again before any money moves."
- Buttons pinned to the bottom: with a fix, primary "Accept fix: size 8, $96" (58px), then secondary
  "Keep 8.5 anyway, charge $96" and "Reject, charge nothing" (52px). Without a fix: primary "Approve,
  charge $96" + secondary Reject. Centered `.lab` "No money moves until you tap".

### Confirm card, sharp mode (rubber-stamp watch on)
Same card plus, above the quote, a banner (#FDF0E9 bg, #F3C9B2 border, eye icon in #C2410C, text
#8A2E08): "You've approved 3 in a row in under 2 seconds." / "Look closely at this one. It's the older
model." The quote box gets a 1.5px #C2410C border. Keeping the mismatched item becomes press-and-hold
(1.5s, progress ring in #C2410C, keyboard: hold Space/Enter), label "Hold to keep E40, charge $59", then
"Keep holding", then a check and "Kept the E40, charging $59". Caption: "Keeping the E40 takes a press
and hold". NICE TO HAVE ONLY: build this last, only if time allows before the feature freeze.

### Confirm result (after Accept fix)
Blue-outlined square with a swap icon, h1 "Fixed. You're getting size 8.", muted line "The 8.5 was
cancelled before any money moved. The size 8 order matched your request, so it went through." Receipt
box (`.lab` "Receipt" with 1px ink bottom border) with rows: Old order (id + blue "Fixed" pill), What
changed (size ~~8.5~~ **8**), New order checked ("Matches, risk 5" in green), You took (mono time),
Charged (green "Captured" pill + mono amount, bold row). Primary "Done" button, `.lab` "You can close
this page". If the new order went to review instead, say so honestly.

## Copy rules
No em dashes anywhere. Plain, human sentences. Fictional brands only (Fernwood Supply Co, Kestrel).
