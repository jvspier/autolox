# Workflow, naming rules and product decisions

## The current manual process

1. Copy names from a Google Sheet (HR administration, kept for cross-referencing).
2. Paste into in-house employee backoffice software - creates a company user each.
3. Multi-select all the new people, click "print card" - prints DESFire EV3 4K cards
   with the employee name on them.
4. Carry a batch of cards to a wall-mounted Loxone NFC Code Touch.
5. In the Loxone web UI: "NFC Tag Aanleren", tap one card, type the name, submit.
   **Repeat ~250 times.** ← this is the only step this project replaces.

## The seasonal user model

All temp cards are attached to **one** Loxone user, e.g. `temp.xmas`. After the season
that single user is deleted in one click and every card becomes useless.

This is a good design and should not be changed. It gives one-click revocation of 250
credentials with no expiry dates to get wrong. It also means the tool never has to
create or delete users - it only ever attaches tags to an existing one.

The name varies by season and is chosen by whoever runs it - `temp.xmas`,
`temp.mothersday`, etc. **Therefore the target user must be selected at runtime from a
dropdown populated by `getuserlist2`, never hardcoded or typed as a UUID.**

Note: Loxone does not care about the tag name at all, only the tag ID. The name exists
purely so admins can read an access log. That makes name collisions a legibility
problem rather than a functional one - but still worth flagging to a human, since
during an incident the name is the only human-readable handle.

## Constraints that shape everything

- **Cards are never returned.** ~250 consumed per season. A reusable pre-enrolled card
  pool was considered and ruled out for this reason.
- **The employee name must be printed on the card.** Hard requirement. This is also
  what makes camera-based OCR viable later - the card carries its own identity.
- One physical tap per card is irreducible (see `docs/protocol.md` - the Miniserver
  writes to the card).

## Naming rules

Backoffice holds `John Smith`. Loxone gets `john.smith`.

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
`ß` fail similarly. Use the `unidecode` library. This matters: seasonal intake in NL
skews heavily toward Polish, Romanian and Turkish names.

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
silently mis-names cards. The CSV journal is the only record. This is what the camera
fixes.

## Phase 2 - camera

Purpose: remove the ordering dependency. The name is already printed on the card, so
the card can identify itself.

**Ordering: camera first, then tap.** Show card to camera → name resolved and locked →
tap card → bound. If tapping came first the script would hold a tag ID with no name and
need a pending state plus an abandon path. This way the tap *is* the confirm action, and
a bad read costs nothing - re-show the card.

**OCR only ever matches, never generates.** Read text → fuzzy match (`rapidfuzz`)
against the ~250-name roster → the value written to Loxone comes from the roster row.
A closed candidate list makes this a much easier problem than general OCR: `Jeroen van
Soier` still resolves correctly. Only stop and ask when the top two candidates are
within a few points of each other.

**Introduce it as a verifier before trusting it as an input.** Keep the queue, OCR each
card, refuse to bind on mismatch. Stack-shuffle detection at near-zero risk, because a
wrong read costs a pause rather than a mis-bind. Promote to primary input only after it
has agreed with the queue across a full batch.

Practical issues: glare on glossy dye-sub PVC (diffuse side lighting, camera slightly
off-axis), and consistent card placement (a cardboard jig gets most of the benefit).

**If the card print template can carry a QR or Code128, use that instead** - machine
readable beats OCR on every axis. Whether the template is editable is still unknown.

**Ergonomics problem worth solving first:** the Code Touch is wall-mounted. Holding a
card flat against a wall while aiming it at a clamped webcam, 250 times, is bad. A spare
Code Touch on a short Tree run at a desk turns this into a proper enrolment bench -
camera on an arm above, reader below, jig between. Worth doing before the camera.

## Phase 3 - web app

FastAPI backend holding the Loxone session, browser frontend.

The browser is chosen specifically for the camera: `getUserMedia` gives live preview and
frame capture in ~10 lines, versus fighting OpenCV window handling on Wayland. Also a
big readable screen for free, and it runs on a tablet at the bench.

### Roster input: paste box, NOT the Sheets API

This tool runs once or twice a year. OAuth consent, service-account keys, a renamed
sheet, a token that expired in March - each is something that quietly breaks between
seasons and gets discovered while standing at a reader holding 250 cards. A textarea
has no moving parts. Accept a CSV drop too.

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

Header: reader name, connection dot, `47 / 250`, rough ETA.
Left: camera preview with a fixed target rectangle.
Right: state panel - the entire UI, one big word plus a name. Four colour-coded states:
grey *show a card*, amber *john.smith - matched, tap now*, green *bound*, red *already
assigned to anne.de.wit*.
Below: last ten bound, newest first, each with undo.
Roster tab: all 250 with status, searchable. Useless for 240 cards, indispensable for
the last ten.
Footer: manual type-ahead pick, for when a card is scuffed and OCR will not cooperate.
There must always be a way to proceed without the camera.

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

Rotate the visu password each January - it is used once a year, so rotation is nearly
free.

Note: if the tool is foolproof enough for a colleague to run, the visu password is
effectively theirs too, and it is a credential for the building sitting in a config
file. Restrict file permissions; consider whether that machine is encrypted.

## Self-hosting on Proxmox

Works, and the split is clean: `getUserMedia` runs in the browser, so the camera belongs
to whatever device the operator is holding. Backend in an LXC container, browser on the
bench laptop or tablet. The container needs no camera, only reachability to the
Miniserver. Bonus: credentials live on a controlled server rather than a laptop that
goes home with someone.

**Gotcha that will bite:** `getUserMedia` requires a secure context.
`http://10.40.x.x:8000` gets no camera access. Either put it behind a reverse proxy
with a real certificate (internal CA, or Let's Encrypt via DNS-01 on an internal name)
or run on localhost. Run locally on a laptop for the first season, move into Proxmox
once the workflow has survived a real run.

## Out of scope, deliberately

- Creating users in the backoffice software. Better fixed there: a bulk endpoint
  ("create these N users and queue print jobs") removes the checkbox-clicking entirely
  and is not this project's job.
- Card printing.
- Deleting the seasonal user in January - one click today, already fine.
- Replacing the Google Sheet. HR uses it for cross-referencing. This tool exports CSV
  back into it and stays a tool, not a system of record.
