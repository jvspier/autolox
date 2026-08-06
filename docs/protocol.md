# Loxone Miniserver protocol reference

Everything here is either verified against a real Miniserver (Config 17.1.1, Gen 2) or
marked as unverified. **Provenance is given for each fact. Do not add anything to this
file without stating how it was verified.**

Verification sources:

- **[bundle]** - read directly out of the Miniserver's own `comps.js` (the web UI's
  JavaScript, served from the Miniserver). Authoritative: this is the code that
  performs the operation today.
- **[log]** - observed in a live browser devtools capture of the real web UI performing
  a real card enrolment.
- **[lib]** - read from the source of `pyloxone-api` 0.2.4, which is the library behind
  the Home Assistant Loxone integration.
- **[docs]** - Loxone's own published documentation.
- **[community]** - `mr-manuel/Loxone_api_documentation`, extracted from the app bundle.
- **[UNVERIFIED]** - inference. Treat as a hypothesis to test.

## Transport

WebSocket at `/ws/rfc6455` on the Miniserver's HTTP port (80 by default). **[log]**

The same `jdev/...` command strings also work over plain HTTP GET, but the WebSocket is
required for this project because state updates are pushed, not pollable. **[docs]**

Commands go out as text frames. State updates arrive as **binary event tables** (value
tables, text tables, daytimer, weather) which must be parsed into `{uuid: value}`.
`pyloxone-api` does this parsing. **[lib]**

## Connect handshake

In order, all **[lib]** unless noted:

| Step | Endpoint | Purpose |
|---|---|---|
| 1 | `jdev/cfg/apiKey` | capability / version probe |
| 2 | WS upgrade to `/ws/rfc6455` | **[log]** |
| 3 | `jdev/sys/getPublicKey` | Miniserver RSA public key |
| 4 | `jdev/sys/keyexchange/{rsa}` | client sends RSA-encrypted AES session key |
| 5 | `jdev/sys/getkey2/{user}` | returns salt + hash algorithm |
| 6 | `jdev/sys/gettoken/{hash}/{user}/{permission}/{uuid}/{info}` | HMAC challenge-response → JWT |
| 7 | `jdev/sps/enablebinstatusupdate` | starts the state stream |

The password never crosses the network: local hash against the salt from step 5, HMAC
sent in step 6. After step 4, commands are wrapped as `jdev/sys/enc/{aes}`. **[lib]**

Token lifecycle endpoints also exist: `jdev/sys/checktoken`, `jdev/sys/refreshtoken`,
`jdev/sys/getjwt`, `jdev/sys/refreshjwt`, `jdev/sys/killtoken`. **[lib]** **[log]**

### Token permission - RESOLVED 2026-08-04

Empirically, HTTP basic auth against `addusernfc` succeeds when svc.cardenroll has
the "User Management" right — no explicit token permission escalation was needed.
The token-permission question turned out to be irrelevant on the HTTP path we
actually took. If a websocket-with-token flow were needed later, this would
resurface.

A JWT captured from the real web UI while enrolling a card had
`tokenRights: 2080` with `tokenRightsText: [NONE, CHANGE_PWD, USER_MANAGEMENT]`. **[log]**
The numeric permission argument that produces 2080 is still not known and not
needed for this tool.

## Secured commands

Some operations require a second credential - the *visualisation password*, separate
from the login password.

```
jdev/sps/io/{uuid}/{command}                  normal
jdev/sps/ios/{visuHash}/{uuid}/{command}      secured   <- note the 's'
```

`visuHash` is derived from a salt fetched via `jdev/sys/getvisusalt/{user}`, requested
immediately before each secured command. **[log]** **[lib]**

NFC learn mode is a secured command, so the visu password is mandatory. **[log]**

## NFC enrolment

### Commands

```
jdev/sps/ios/{visuHash}/{codeTouchUuid}/nfc/startlearn
jdev/sps/ios/{visuHash}/{codeTouchUuid}/nfc/stoplearn
```

Both string literals appear verbatim in the bundle. **[bundle]** Neither appears in any
official or community documentation - they were recovered by reading
`LearnNfcTagScreen` in `comps.js`.

### Learn mode expires - re-arm every 3s

`_startLearnMode` sets an interval that **re-sends `startlearn` every `1e4/3` ms
(≈3333 ms)** for as long as the dialog is open. **[bundle]**

A client that arms learn mode once will capture one card and then go silent. Re-arm
on a timer comfortably inside 3.3s. The tool uses 3.0s. Verified 2026-08-04: this
keeps the reader armed indefinitely with no drops observed across the phase-1
verification session.

### Receiving the tag ID

The tag arrives as a **state**, not as a command response. `receivedStatesForControl`
reads `nfcLearnResult` from the `NfcCodeTouch` control. **[bundle]**

```json
{ "id": "55 15 43 98 57 0F CB 15 EC", "name": "NFC Tag 425",
  "userUuid": null, "deviceUuid": null }
```

The state's UUID is found in `LoxAPP3.json` at
`controls[*].states.nfcLearnResult` for controls of `type: "NfcCodeTouch"`. The
control's `uuidAction` is the target for the learn commands.

Tag IDs contain spaces and must be URL-encoded when used in a path.

### Error sentinel IDs - these are not cards

From `LearnNfcTagScreen.NFC_ERROR_IDS`. **[bundle]** Filter before binding:

| ID | Meaning |
|---|---|
| `00 00 00 00 00 00 00 00 E8` | read error |
| `00 00 00 00 00 00 00 00 EE` | authentication error (card not writable?) |
| `00 00 00 00 00 00 00 00 EF` | init error |
| `00 00 00 00 00 00 00 00` | invalid tag |

### Already-assigned detection

The app only auto-adds when `userUuid === null && deviceUuid === null`; otherwise it
routes to a reassign confirmation. **[bundle]** This gives "this card is already
enrolled" detection for free, and protects against a colleague tapping their own badge
during a session.

### Device state

`deviceState` on the same control. **[bundle]**

```
READY_TO_LEARN_TAG: 0, OFFLINE: 1, DUMMY_TREE: 3, CAN_NOT_READ_TAG: 4, DUMMY_AIR: 7
```

Guard on `0` before starting a session.

### Binding

```
jdev/sps/addusernfc/{userUuid}/{tagId}/{name}
```

**[log]** **[community]** Observed live in a real enrolment session. The tag ID
format is a space-separated hex byte sequence with a trailing `EC`; the name
Loxone assigns during learn mode follows a `NFC ID <n>` or `NFC Tag <n>` pattern.

**Calling it twice with the same tag ID updates the name.** The app itself does exactly
this - `_addTag` binds, shows a rename popup, then calls `addNfcTag` again with the new
name. **[bundle]** This is the repair path for a mis-bound card.

## Cards

The Miniserver **writes to the card** during learn - it generates a unique ID, encrypts
it, and stores it on the tag. The NFC ID shown in Config ends in `EC`; the
authentication ID is that value minus the `EC` suffix. **[docs]** Confirmed by an
observed ID of `07 D2 DB EC EB 0D 64 6A EC` (9 bytes, trailing `EC`) - an NXP UID would
be 7 bytes starting `04`.

Consequences: the tag ID does not exist until the card has met a Code Touch, so **one
physical tap per card is irreducible**. UIDs cannot be harvested at a desk in bulk, and
a supplier-provided UID list is useless.

There is a documented fallback to the unencrypted UID when an encrypted tag cannot be
decrypted **[docs]**, but this installation is on the encrypted path.

Cards in use: DESFire EV3 4K, arriving writable.

## User and group endpoints

All **[community]**, and `getuser` / `getgrouplist` additionally **[log]**:

| Endpoint | Purpose |
|---|---|
| `jdev/sps/getuserlist` | list of all users |
| `jdev/sps/getuserlist2` | extended user list - **use this to populate a user dropdown** |
| `jdev/sps/getuser/{username}` | single user detail, includes the `nfcTags` array |
| `jdev/sps/getusersettings` | current user's settings |
| `jdev/sps/getgrouplist` | group list |
| `jdev/sps/addoredituser/{json}` | create or edit a user **[docs]** |

`addoredituser` supports `userState: 4` (valid within a timespan) with
`validFrom` / `validUntil` (seconds since 2009-01-01) and `expirationAction`
(0 = deactivate, 1 = delete). **[docs]** Not used by this project - the shared
batch-user model (see `docs/workflow.md`) handles cleanup by deleting one user -
but relevant if the model ever changes.

**Do not rely on the ordering of the `nfcTags` array from `getuser`.** Whether it is
insertion-ordered is unknown, so any design that zips it against a name list positionally
can silently mis-bind every card. Bind at tap time instead.

## Trust cluster observations

In a multi-Miniserver Trust setup, control UUIDs and user UUIDs both encode the
serial of the owning Miniserver in their last 12 hex characters (the trailing
group after the last dash, e.g. `xxxxxxxx-xxxx-xxxx-xxxxSSSSSSSSSSSS`). BUT —
Tree devices (NFC Code Touch, extensions, etc.) encode the *device's* MAC as the
suffix, not the Miniserver's. Only Miniserver-hosted virtual controls carry the
Miniserver's serial. Config's UI is authoritative for reader ownership.

`addusernfc` Trust-routes cleanly — reachable from any peer. `getuser/{name}` does
not — must be sent to the owning Miniserver (and can still 404 in practice).
Reader-side commands (`nfc/startlearn`, state stream) must always go to the
reader-owning Miniserver.

## pyloxone-api surface used

**[lib]** All verified by reading the 0.2.4 source:

- `LoxAPI(host, port, user, password)`
- `await api.getJson()` - fetches `LoxAPP3.json` into `api.json`
- `await api.async_init()` - handshake and auth
- `await api.start()` - listen loop, blocks; run as a task
- `api.message_call_back` - awaited with `{uuid: value}` dicts as states stream in
- `await api.send_secured__websocket_command(uuid, value, visu_password)` - builds
  `jdev/sps/ios/{hash}/{uuid}/{value}`, matching the observed live format exactly
- `pyloxone_api.const.TOKEN_PERMISSION` - patchable

There is no public wrapper for arbitrary commands such as `addusernfc`; the prototype
sends it on `api._ws` directly. A cleaner approach may be worth finding.
