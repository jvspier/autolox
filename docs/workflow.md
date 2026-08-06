# Workflow, naming rules and product decisions

This project generalizes from one recurring pain point - a seasonal hiring surge - but
the workflow and constraints below apply to any organization that needs to bulk-issue
NFC access cards to a batch of people at once: seasonal staff, event volunteers,
contractor cohorts, whatever the batch is.

## The manual process this replaces

1. Copy names from a roster (a spreadsheet, an HR system export - whatever your
   organization already keeps for cross-referencing).
2. Paste into your backoffice/HR software - creates a user record each.
3. Multi-select all the new people, click "print card" - prints DESFire EV3 4K cards
   with the person's name on them.
4. Carry a batch of cards to a wall-mounted Loxone NFC Code Touch.
5. In the Loxone web UI: "NFC Tag Aanleren", tap one card, type the name, submit.
   **Repeat once per card.** ← this is the only step this project replaces.

## The shared batch-user model

All cards from one batch are attached to **one** Loxone user, e.g. `temp.xmas` for a
Christmas hiring surge, `temp.event2026` for a one-off event, or anything else the
operator chooses. Once the batch's access is no longer needed, that single user is
deleted in one click and every card in the batch becomes useless.

This is a good design and should not be changed. It gives one-click revocation of an
entire batch's worth of credentials with no expiry dates to get wrong. It also means
the tool never has to create or delete users - it only ever attaches tags to an
existing one.

The name varies by batch and is chosen by whoever runs it - `temp.xmas`,
`temp.mothersday`, `temp.event2026`, etc. **Therefore the target user must be selected
at runtime from a dropdown populated by `getuserlist2`, never hardcoded or typed as a
UUID.**

Note: Loxone does not care about the tag name at all, only the tag ID. The name exists
purely so admins can read an access log. That makes name collisions a legibility
problem rather than a functional one - but still worth flagging to a human, since
during an incident the name is the only human-readable handle.

## Constraints that shape everything

- **Cards are never returned.** Hundreds get consumed per batch, and each batch is
  disjoint from the last. A reusable pre-enrolled card pool was considered and ruled
  out for this reason.
- **The person's name must be printed on the card.** Hard requirement — how the
  operator matches a physical card to a roster row when standing at the reader.
- One physical tap per card is irreducible (see `docs/protocol.md` - the Miniserver
  writes to the card).

## Naming rules

The roster holds `John Smith`. Loxone gets `john.smith`.

| Rule | Example |
|---|---|
| Lowercase, dot between every part | `John Smith` → `john.smith` |
| Name particles kept and dotted (e.g. `de`, `van`, `der`) | `Anne de Wit` → `anne.de.wit` |
| Accents folded to ASCII | `José` → `jose` |
| Dash preserved | `Anne-Marie Bakker` → `anne-marie.bakker` |
| Collision: append a digit | second `jan.jansen` → `jan.jansen2` |

Dash is the only special character in use.

**Implementation gotcha:** do not use `unicodedata.normalize('NFKD', …)` plus accent
stripping. It handles `é → e` and `ü → u` but silently mangles characters that are not
accented letters and have no decomposition - `Łukasz` becomes `ukasz`, and `ø`, `đ`,
`ß` fail similarly. Use the `unidecode` library. This matters for any roster with a
mix of nationalities, which a seasonal or event-driven hiring batch typically has.

**Collisions must be surfaced to a human, not silently auto-numbered.** Flag them on
the review screen and let the operator decide.

## Target workflow (phase 1, CLI)

Steps 1-3 unchanged. Then:

1. Save the name list as a text file, one per line, in the order the card stack is in.
2. Walk to a Code Touch with a laptop, start the script.
3. Tap cards one after another. Screen shows the next name each time.
4. Ctrl-C when done.

The script keeps learn mode armed, captures each tag ID as it arrives, binds it to the
next name in the queue, and journals every pair to CSV.

**Known weakness:** names and tags are joined by queue position. A shuffled stack
silently mis-names cards. The CSV journal is the only record — mitigation is
operator care with card ordering, plus the already-assigned safety check.

## Phase 3 - web app

FastAPI backend holding the Loxone session, browser frontend.

The browser gives a big readable screen for free and runs on a tablet at the
bench. FastAPI + a plain HTML/CSS/JS SPA-lite keeps the deploy trivial (one
Python process, no build step for the frontend).

### Roster input: paste box, NOT a spreadsheet API integration

This tool typically runs infrequently - once or twice a year for a seasonal operator,
maybe more often for others. OAuth consent, service-account keys, a renamed sheet, a
token that expired months ago - each is something that quietly breaks between runs and
gets discovered while standing at a reader holding a stack of cards. A textarea has no
moving parts. Accept a CSV drop too.

Flow is **paste → review → confirm**. The review screen derives the Loxone names, shows
both columns, and flags rows needing a human glance: duplicates, folded accents,
single-word entries, blanks, unexpected characters. Fix inline, then confirm.

### Paste the whole list every session

Do not ask which batch the operator is on. Check each name against what is already
bound and show `180 done / 70 to go`. No bookkeeping to get wrong, no "did I already do
these forty", stopping mid-batch is free. Sessions become stateless for the operator.

### Two settings scopes

- **Connection** - host, service account, credentials. Set once, behind a gear icon.
- **Session** - which user receives the cards, which reader. Chosen every run, from
  dropdowns populated from `getuserlist2` and `LoxAPP3.json`.

Dropdowns rather than free text, so a typo cannot be silently valid. A dropdown cannot
produce a UUID that does not exist. Show a confirmation line on the enrol screen -
*"cards will be assigned to temp.xmas"* - because picking last season's group by
accident is the one mistake dropdowns do not prevent.

### Screen layout

Header: workspace name, step title, status dot for "reader armed".
Center: big "Scan the card for" panel with the roster name in huge letters,
"up next" line, progress `X / N bound`, Stop button. Big enough to read
across the room.
Right: roster panel with per-row status (bound / pending / skipped) so the
operator can see the whole batch at a glance.
Below: live tap log — bound / skipped / errored events in order.
History (behind a slide-out): past sessions with roster audit + resume.

### Two things that matter more than the layout

**Audio.** The operator looks at cards, not the screen. Three distinct sounds - soft
click on matched, clear chime on bound, harsh buzz on anything wrong - are what allow
head-down work at ~4 seconds per card.

**Resumability.** State lives in Loxone and the CSV, not the app. Closing the laptop
mid-batch costs nothing; the already-assigned check makes re-tapping a bound card a
no-op.

Plus an end-of-run summary: bound count, roster rows that never got a card, duplicates.
Export CSV for pasting back into the Google Sheet.

### Run every check before the enrol screen appears

Each with a plain-language fix rather than a stack trace: Miniserver reachable, token
accepted, token has user-management rights, target user exists, reader online and
`deviceState: 0`, visu password accepted. Failing at setup is cheap; failing at card 90
is not. One screen at a time - Setup → Review → Enrol → Summary - so there is nothing
to document, because there is only ever one thing to do.

### Build the UI last

The core is unproven. A polished front end on top of an unproven core is an expensive
way to discover that learn mode does not stay armed.

## Credentials

Use a dedicated service account, e.g. `svc.cardenroll`. Not a shared human admin
account.

The reason is lifecycle, not security theatre: if the tool runs as a named person's
account and that person leaves, someone dutifully disables the account and the tool
breaks a year later with an authentication error nobody can explain. It also fixes
attribution - with a service account, anything it did was the enrolment tool by
definition, rather than being indistinguishable from that admin opening a door.

Setup in Loxone Config:

- Create `svc.cardenroll`
- User-management right only. No Config, no FTP, no admin.
- Set both its password **and** its visu password - two separate settings, both required
- Save to Miniserver

Network: TCP 80 from the enrolment machine to the Miniserver (the WebSocket rides the
same port). No port forwarding, no Loxone Cloud, no internet access needed.

Rotate the visu password after each enrolment run (or on whatever cadence fits) - if
it's only used occasionally, rotation is nearly free.

Note: if the tool is foolproof enough for a colleague to run, the visu password is
effectively theirs too, and it is a credential for the building sitting in a config
file. Restrict file permissions; consider whether that machine is encrypted.

## Self-hosting on Proxmox

Backend in an LXC container, browser on the bench laptop or tablet. The
container only needs reachability to the Miniservers (TCP 80 on LAN).
Credentials live on a controlled server rather than a laptop that goes
home with someone.

Optionally reverse-proxy the app so it's reachable at a hostname over
HTTPS across the LAN — nicer for colleagues than remembering an IP + port,
and lets more than one admin use it without co-locating with the container.

## Out of scope, deliberately

- Creating users in the backoffice/HR software. Better fixed there: a bulk endpoint
  ("create these N users and queue print jobs") removes the checkbox-clicking entirely
  and is not this project's job.
- Card printing.
- Deleting the batch user once its access is no longer needed - one click today,
  already fine.
- Replacing whatever roster source (spreadsheet, HR export) the operator already uses
  for cross-referencing. This tool exports CSV back into it and stays a tool, not a
  system of record.
