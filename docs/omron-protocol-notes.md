> These notes were written before HomeVitals' first real-world test, from the public pages of
> two reference projects. They record protocol facts only; no code was copied. Since then,
> real use showed that the OMRON connect app's Qatar region keeps readings on the phone only
> (no cloud), and that accounts created in the United Kingdom sync through the EU v2 server.

# OMRON connect API reference notes (from omramin and omron-connect-mcp)

Researched 2026-10-01 by reading the public GitHub pages and raw files only. No code was
run, cloned or downloaded, and no account was used. Everything below is in my own words.
We use it as a reference for writing our own small client, not as code to copy.

Source tags used throughout:

- **[omramin]**: seen in bugficks/omramin (default branch `master`; files `omronconnect.py`,
  `regionserver.py`, `data/region_v1.json`, `data/region_v2.json`, its issues).
- **[mcp]**: seen in aircon-chen/omron-connect-mcp (default branch `main`; files
  `src/omron_connect_mcp/client.py`, `server.py`, `tests/test_parse.py`,
  `tests/fixtures/sync_bpm.json`, `README.md`).
- **[both]**: seen in both.
- **[inferred]**: my reading of the evidence. Not confirmed by either project.

---

## 1. Licenses

| Project | License | Where I saw it |
|---|---|---|
| omramin | **GNU GPL v2**. `pyproject.toml` says `GPL-2.0-or-later`. The GitHub sidebar says "GPL-2.0". `LICENSE` holds the standard GPL v2 (June 1991) text. | repo page, `LICENSE`, `pyproject.toml` |
| omron-connect-mcp | **GNU GPL v2 only**. `pyproject.toml` says `GPL-2.0-only`. `LICENSE` holds the standard GPL v2 text. Its README says the login flow and `bodyIndexList` parsing come from omramin's GPLv2 `omronconnect` module. | repo page, `LICENSE`, `pyproject.toml`, README |

**Can we reuse the code in an MIT project? No.** If we copy GPL code, or a close adaptation
of it, into eufy-sync, the combined program has to be distributed under the GPL. That
conflicts with keeping the fork MIT. Also, omron-connect-mcp depends on omramin directly
(`omramin @ git+https://github.com/bugficks/omramin.git`), so importing either one as a
library brings in GPL code.

What we can do is reimplement the protocol from facts: URLs, field names, units and
behaviour. Facts like these are not protected expression. We should write our client from
this document, not by reading their code side by side while we type.

**Special care: the v1 app credentials.** `data/region_v1.json` [omramin] contains an
`appID` and `appKey` for each v1 (Kii) server. They were pulled from the official OMRON app
and are sent as headers. I did not copy them here. Embedding them in our repo is both a
licensing gray area and a terms-of-service question. As section 2 shows, CA, US and GB
accounts use the v2 servers, which do not need these values.

---

## 2. Regions and servers

### 2.1 Two different API generations [omramin]

omramin builds a map from country code to server list out of two JSON files:

- `region_v1.json`: groups with `cloudUrl`, `appID`, `appKey` and `countries`. A group
  with no `cloudUrl` counts as offline.
- `region_v2.json`: groups with `region`, `countries` and `api_configuration`, which holds
  `prod_base_url` and `prod_secondary_base_url`. A group with no `api_configuration`
  counts as offline.
- If a country appears in both files, **v1 wins**.
- The lookup upper-cases the country code first. An unknown or offline country maps to
  nothing, and the client then raises a `ValueError` meaning "no servers available for
  country X".
- Which client gets used depends on the server host. A host matching
  `data-XX.omronconnect.com` gets the v1 (Kii) client. Any other host gets the v2 client.
- The client tries each server in the country's list in order and moves on after a
  connection error, timeout or HTTP status error (`try_servers`).

### 2.2 v1 (Kii cloud) servers [omramin]

| `cloudUrl` | Countries |
|---|---|
| `https://data-sg.omronconnect.com/api` | AU, BD, ID, MM, MY, NZ, PH, SG, TH, VN |
| `https://data-jp.omronconnect.com/api` | HK, JP, KR, TW |
| `https://data-in.omronconnect.com/api` | IN |
| `https://data-eu.omronconnect.com/api` | AR, BO, BR, CL, CO, CR, DO, EC, GT, HN, MX, NI, PA, PE, PY, SV, UY, VE (Latin America, even though the host says "eu") |
| (none, offline) | KH, LA, LK, MN, MO, RU |

### 2.3 v2 servers [omramin]

| Region | `prod_base_url` | `prod_secondary_base_url` | Countries |
|---|---|---|---|
| EU | `https://vlt-mobile-api.prd.eu.ohiomron.eu/prd` | `https://oi-api.ohiomron.eu/app` | AT, BE, BG, CH, CY, CZ, DE, DK, EE, ES, FI, FR, **GB**, GR, HR, HU, IE, IT, LT, LU, LV, MT, NL, PL, PT, RO, SE, SI, SK |
| NA | `https://vlt-mobile-api.prd.us.ohiomron.com/prd` | (empty) | AW, BM, BZ, **CA**, PR, SR, TT, **US** |
| OFFLINE | (none) | (none) | AE, AL, AM, AO, AZ, BA, BF, **BH**, BJ, BW, BY, CG, CI, CM, CV, DZ, EG, GA, GE, GH, GM, GW, IL, IQ, IS, JO, KE, KG, **KW**, KZ, LB, LI, LR, MA, MD, MK, ML, MR, MU, MW, MZ, NA, NE, NG, NO, **OM**, PK, **QA**, RS, RW, **SA**, SD, SL, SN, ST, SZ, TD, TG, TN, TR, TZ, UA, UG, UZ, YE, ZA, ZM, ZW |

### 2.4 Mapping for our countries

| Country | Result in omramin | API |
|---|---|---|
| CA | NA: `vlt-mobile-api.prd.us.ohiomron.com/prd` | v2 [omramin] |
| US | NA: same host | v2 [omramin] |
| GB | EU: `vlt-mobile-api.prd.eu.ohiomron.eu/prd`, then `oi-api.ohiomron.eu/app` | v2 [omramin] |
| QA | **OFFLINE**: omramin refuses with "no servers available" | none [omramin] |

omron-connect-mcp has no region table of its own. It passes `OMRON_COUNTRY` straight to
omramin's `OmronClient`. Its README says it was only tested with TW, JP and HK, which all
go to `data-jp` [mcp].

### 2.5 Server path rules for v2 [omramin]

- The v2 client appends `/v2` to data paths **only** when the base URL contains `/app`
  (the `oi-api...` secondary host). For the `/prd` hosts the data paths have no `/v2`.
- Issue #11 shows a request to `https://oi-api.ohiomron.jp/app/v2/sync/weight?...`. So a
  Japanese v2 host existed or was used at some point, even though the current region data
  sends JP to v1 `data-jp` [omramin].

---

## 3. Login

### 3.1 v1 (Kii) login [omramin; omron-connect-mcp reuses it unchanged]

- **POST** `{cloudUrl}/oauth2/token`, for example `https://data-sg.omronconnect.com/api/oauth2/token`.
- JSON body: `username` (the email address) and `password`. There is no `grant_type` in
  the login call.
- Headers sent on every v1 request:
  - `X-Kii-AppID` and `X-Kii-AppKey`: per-server values from `region_v1.json`
  - `X-OGSC-App-Version: 011.004.00000`
  - `X-OGSC-SDK-Version: 000.005`
  - `user-agent`: an iOS CFNetwork-style string that starts with
    `OmronConnect/011.004.00000.001`
  - after login, `authorization: Bearer <access_token>`
- Response keys read: `access_token` and `refresh_token`.
  - omramin does **not** read an expiry or a user id from this response. Kii responses
    normally also carry `expires_in` and a user `id`, but neither project uses them, so
    this is [inferred].
- **Refresh:** POST to the same `{cloudUrl}/oauth2/token` with JSON
  `{grant_type: "refresh_token", refresh_token: <token>}`. The response has the same keys.
- **Current user:** GET `{cloudUrl}/apps/{appID}/server-code/users/me` with the bearer
  token. omramin only uses it as a check that the session works.
- **Failure handling:** omramin calls `raise_for_status()` on the HTTP result. If either
  token key is missing, it logs the response text and returns `None`. Neither project
  checks for an `errorCode` field on login.

### 3.2 v2 (ohiomron) login [omramin only; omron-connect-mcp never tested this]

- **POST** `{base}/login`, for example `https://vlt-mobile-api.prd.us.ohiomron.com/prd/login`.
- JSON body: `emailAddress`, `password`, `country` (the ISO code) and `app: "OCM"`.
- Headers:
  - `user-agent` shaped like the iOS app: `OMRON connect/8.2.1 (com.omronhealthcare.omronconnect; build:24; iOS ...) Alamofire/...`
    (app version constant `8.2.1`)
  - **`Checksum`**: lowercase hex SHA-256 of the raw request body bytes. omramin adds it
    to every v2 request through an httpx event hook. For a GET with no body, this is
    presumably the hash of empty bytes [inferred].
  - after login, `authorization: <accessToken>` with **no** `Bearer` prefix.
  - omramin does not set Content-Type explicitly. httpx sends `application/json` for JSON
    bodies.
- Response: top-level `accessToken` and `refreshToken`. omramin reads nothing else (no
  expiry, no user id).
- **Refresh:** POST `{base}/login` with JSON `{app: "OCM", emailAddress, refreshToken}`.
  The response has the same keys.
- Other v2 calls:
  - GET `{base}/user?app=OCM` returns the user under `data`.
  - GET `{base}{/v2?}/init-user?app=OCM` returns `data.deviceList[]`, and each device has
    `attributes` with `macAddress`, `deviceModel`/`identifier`, `deviceCategory`,
    `userNumberInDevice` and `isActive`.
- **Failure handling:** if the token keys are missing, omramin catches the KeyError, logs
  it and returns `None`, and the CLI reports a failed OMRON login. omramin issue #8 shows
  this for CZ, US and GB accounts, with no status code or body posted.

### 3.3 Token handling and expiry

- Neither project tracks expiry. They keep the refresh token and refresh when they need
  to [both].
- omron-connect-mcp stores `{"refresh_token": "..."}` in a mode-600 JSON file. At startup
  it tries a refresh first and falls back to a password login on any exception [mcp].
- omramin stores tokens in the system keyring, or in a file on Windows [omramin].
- omramin issue #21 complains that tokens time out every few days. I did not confirm
  whether that is the OMRON token or the Garmin one.

---

## 4. Fetching readings

### 4.1 `measureData` (v1 only) [omramin]

- **POST** `{cloudUrl}/apps/{appID}/server-code/versions/current/measureData`, with the
  bearer token and Kii headers.
- JSON body:
  - `containCorrectedDataFlag: 1`
  - `containAllDataTypeFlag: 1`
  - `deviceCategory`: string, `"0"` = blood pressure monitor (BPM), `"1"` = scale
  - `deviceSerialID`: hex string derived from the BLE MAC (see below)
  - `userNumberInDevice`: integer user slot on the device. Slot 0 is the device itself and
    is ignored.
  - `searchDateFrom`, `searchDateTo`: **epoch milliseconds, UTC**. A negative from-value
    becomes 0, and a to-value of 0 or less becomes "now".
- **No paging** is used for `measureData` in omramin.
- Response shape:
  - `returnedValue`: an object, or a list whose first element is used. If it contains
    `errorCode`, or is missing, omramin logs and returns nothing.
    - `deviceCategory`
    - `deviceModelList[]`
      - `deviceModel` (for example `HEM-7600T`)
      - `deviceSerialIDList[]`
        - `deviceSerialID` (must equal the requested serial)
        - `userNumberInDevice`
        - `measureList[]`, one reading each, same per-reading shape as section 4.2
- Timestamp: omramin uses **`measureDateTo`** (epoch ms) as the reading time. Timezone:
  **`timeZone` is an IANA name** (for example `Asia/Tokyo`) and is passed to `pytz.timezone`.
- Serial derivation: omramin's `ble_mac_to_serial` reverses the last three MAC bytes, adds
  the bytes `fffe` (written `feff`), then adds the first three bytes reversed. MAC
  `11:22:33:44:55:66` becomes serial `665544feff332211`.

### 4.2 `synchronizeMeasureData` (v1 only) [mcp]

- **POST** `{cloudUrl}/apps/{appID}/server-code/versions/current/synchronizeMeasureData`.
  omron-connect-mcp builds this from omramin's v1 `_server` and `_APP_URL`
  (`/apps/{appID}/server-code`) and sends omramin's v1 headers.
- JSON body: `{lastSyncDate: <epoch ms>, countOnlyFlag: 0}`. With `lastSyncDate: 0` you
  get the account's whole history. No device serial and no user number are needed.
  - omramin uses the same body shape on the sibling endpoint `synchronizeDeviceConfData`
    [omramin].
- **Paging:** omron-connect-mcp makes **one call and does not page**. omramin's
  `synchronizeDeviceConfData` loop reads `returnedValue.nextPaginationKey` and keeps going
  while it is non-zero and different from the previous `lastSyncDate`. Whether
  `synchronizeMeasureData` pages the same way is **not confirmed**. We should handle
  `nextPaginationKey` defensively [inferred].
- Response: omron-connect-mcp reads `returnedValue.syncList`, a list. The fixture (one
  `syncList` element, de-identified real data) looks like this:
  - `objectId`: string
  - `transferStartDate`: epoch ms
  - `deviceCategoryList[]`
    - `deviceCategory`: **null in real responses**, so the category cannot be used
    - `deviceModelList[]`
      - `deviceModel`: string, for example `HEM-7600T`
      - `deviceSerialIDList[]`
        - `deviceSerialID`: 16 hex characters
        - `userNumberInDevice`: integer
        - `measureList[]`, each reading has:
          - `measureDateFrom`, `measureDateTo`: epoch ms. They were equal in every
            fixture row.
          - `measureDeviceDateFrom`, `measureDeviceDateTo`: string `YYYYMMDDHHmmssSSS`.
            This is probably the device's local wall-clock time [inferred].
          - `timeZone`: IANA name, for example `Asia/Taipei`
          - `deleteFlag`: 0 or 1. **1 means the user deleted it in the app. Skip it.**
          - `transferDate`, `userUpdateDate`: epoch ms
          - `measurementMode`: integer (0 in the fixture)
          - `bodyIndexList`: object keyed by value type, see section 4.3
- omron-connect-mcp reuses omramin's v1 BP parser, which takes `measureDateTo` as the
  time, so the `measurementDate` it outputs is really `measureDateTo` [both]. It shows the
  time as ISO 8601 with the offset of `timeZone`, for example `+08:00` [mcp].
- **Telling BP apart from weight:** `deviceCategory` is null, so omron-connect-mcp counts
  a reading as BP when `bodyIndexList` has keys `"1"` and `"2"` (systolic/diastolic), and
  as a scale reading otherwise [mcp].
- Date filtering in omron-connect-mcp is done locally, on the first 10 characters of the
  local-time ISO string (`YYYY-MM-DD`). Each query downloads the full history [mcp].

### 4.3 `bodyIndexList` encoding (v1 endpoints) [both]

Each key is a value-type code (a string). Each entry has four elements
`[value, unit/subtype, scale, measurementId]`. In the mcp fixture all four are **strings
holding integers**. The real value is value × 10^scale, so scale −1 means divide by 10.

| Key | Meaning | Notes |
|---|---|---|
| `"1"` | Systolic | unit code `20496` = mmHg in the fixture [mcp]. kPa variant exists per omramin enum names; its code is unconfirmed. |
| `"2"` | Diastolic | unit `20496` |
| `"3"` | Pulse | unit `61600` = beats/min [mcp] |
| `"6"` | Irregular heartbeat (arrhythmia) flag | non-zero = true |
| `"7"` | Body movement flag | non-zero = true |
| `"8"` | Cuff wrap check ("keep up check") flag | non-zero = true. Whether non-zero means "wrapped OK" or "poorly wrapped" is **not confirmed**. omramin passes it through as `cuffWrapDetect`. |
| `"11"`–`"15"` | Present in the fixture, all zero | meaning unknown [mcp] |
| `"257"` | Weight | subtype is the unit: 8192 = g, 8195 = kg, 8208 = lb, 8224 = st [omramin] |
| `"259"`, `"260"`, `"261"`, `"262"`, `"263"`, `"264"` | body fat %, resting metabolism, skeletal muscle %, BMI, metabolic age, visceral fat | [omramin] |

The fourth element (`measurementId`) was `"0"` for every entry in the fixture, so it is
**not** a usable reading id.

### 4.4 v2 BP sync: `sync/bp` (CA, US, GB accounts) [omramin only]

This is the endpoint that matters for CA, US and GB. Neither `measureData` nor
`synchronizeMeasureData` exists on the v2 hosts as far as either project shows.

- **GET** `{base}{/v2 if base contains /app}/sync/bp?nextpaginationKey=<int>&lastSyncedTime=<epoch ms or empty>&phoneIdentifier=<string>`.
  The weight version is `/sync/weight`.
  - Note the lowercase **`p`** in `nextpaginationKey`.
  - omramin sends `nextpaginationKey=0`, `lastSyncedTime` = start of the requested window
    in epoch ms (empty when it is 0 or less), and `phoneIdentifier` empty.
  - **omramin does not page.** It makes one call and does not read a next-key from the
    response.
- Response: `data[]`, a flat list of readings. omramin reads these keys from each one:
  - `systolic`, `diastolic`, `pulse`: plain numbers, no `bodyIndexList`
  - `measurementDate`: **epoch ms**
  - `timeZone`: **an integer offset in seconds** (converted as offset ÷ 60 minutes), not an IANA name
  - `irregularHB`, `movementDetect`, `cuffWrapDetect`: flags
  - `userNumberInDevice`: only readings matching the configured user slot are kept
  - `isManualEntry`: non-zero rows are skipped
  - `notes`
  - weight rows add `weight` (kg), `weightInLbs`, `bmiValue`, `bodyFatPercentage`,
    `restingMetabolism`, `skeletalMusclePercentage`, `visceralFatLevel`
- omramin also drops readings later than the window end. It never checks device serial or
  MAC on v2.
- **`deleteFlag` exists on v2 too.** omramin issue #27 (US account, v2) shows a reading
  logged with `deleteFlag=1` and a `seq=` number, and points out that omramin never
  filters on it, so readings deleted in the app get re-synced to Garmin. That issue is
  the only source showing a `seq` field. It might be a per-reading sequence number, which
  would make a good stable id, but this is **unconfirmed**.

### 4.5 Stable reading id

Neither project extracts a unique reading id [both]. The practical dedupe key is
(`deviceSerialID`, `userNumberInDevice`, `measureDateTo`) on v1, or (`userNumberInDevice`,
`measurementDate`) on v2, plus the BP values [inferred]. Kii's `objectId` belongs to the
sync batch, not to a single reading [inferred from fixture shape].

---

## 5. Why `measureData` returns nothing on some accounts [mcp, confirmed by omramin issue #13]

- On some accounts `measureData` returns `{"returnedValue": {"deviceCategory": "0", "deviceModelList": null}}`.
  It does this for every date range, user number and serial variant the mcp author tried
  [both: README/comments in mcp; same body in omramin issue #13 for an AU account on `data-sg`].
- The second cause is the serial: omramin's MAC-to-serial rule did not match the serial
  stored on the server. In the mcp author's example the last 6 hex digits differed, so a
  lookup keyed by serial cannot find the data [mcp].
- `synchronizeMeasureData` needs neither a serial nor a user number. On the same account
  it returned about 340 readings going back to February 2022, where `measureData`
  returned 0 [mcp].

---

## 6. Known errors

| Situation | What is known | Source |
|---|---|---|
| Unknown or offline country (including **QA**) | No HTTP call is made. omramin raises `ValueError`, "no servers available for country". omramin issue #14 shows a user typing `GER`/`D`/`EU`/`UK` and getting a "not a country / no servers" message. | [omramin] |
| Bad password, v1 | `raise_for_status()` raises on a 4xx, or a body without `access_token` gives `None`. The actual Kii error body is **not shown** anywhere. | [omramin] |
| Bad password, v2 | A body without `accessToken` gives KeyError, then `None`, then "Failed to login to OMRON connect" (issue #8). Status code and body **not shown**. | [omramin] |
| Wrong region (valid account, wrong server) | No special handling. It probably looks like a bad-password failure on the wrong server [inferred]. | [inferred] |
| 403 Forbidden on v2 sync | Issue #11: `403` from `https://oi-api.ohiomron.jp/app/v2/sync/weight?...`. No body, no resolution. A wrong `Checksum`, an expired token or a wrong host are plausible causes [inferred]. | [omramin] |
| Server says gzip but sends plain data | omramin patches httpx's gzip decoder to fall back to the raw bytes. Our client should tolerate this. | [omramin] |
| No devices found | Issue #12 (IT, v2): login works but no devices are listed. Unresolved. | [omramin] |
| Rate limits | **No OMRON rate limit is documented by either project.** The only 429 seen is from Garmin's login (omramin issue #15). | [omramin] |
| errorCode in body | v1 data calls can return `returnedValue.errorCode`. omramin treats this as "no data". Values are **not documented**. | [omramin] |

---

## 7. Qatar and other Middle East accounts

- omramin's `region_v2.json` puts **QA, AE, SA, KW, BH and OM in the OFFLINE group** [omramin].
  None of them appear in `region_v1.json`. So omramin, and omron-connect-mcp on top of it,
  will refuse a QA account before making any network call.
- OMRON's own help pages, found in a web search, describe OMRON connect as available in
  Europe, the Middle East (except Iran), Asia, the US and Canada. This disagrees with
  "offline". The likely explanation is that Middle East accounts are served by one of the
  real servers, most plausibly the EMEA/EU v2 host (`vlt-mobile-api.prd.eu.ohiomron.eu/prd`
  or `oi-api.ohiomron.eu/app`), and that "OFFLINE" in the app's region file means
  something else [inferred, **unconfirmed**]. Neither project says which server a QA
  account actually uses.
- **Recommendation for our client:** never hard-fail on a country missing from our table.
  Let config override the server (region key or base URL). For QA, try EU v2 first, then
  NA v2, and log clearly which host accepted the login. Do not loop over hosts endlessly
  with a real password; a few attempts is enough.

---

## 8. Gaps: things I could not confirm

1. The exact login error bodies and HTTP status codes for a bad password or wrong region,
   on v1 and v2.
2. Whether v1 or v2 login responses include an expiry (`expires_in`) or a user id (Kii
   normally does on v1; v2 unknown).
3. Whether `synchronizeMeasureData` pages with `nextPaginationKey`, and what
   `returnedValue` holds besides `syncList`.
4. The v2 `sync/bp` paging key in the response. omramin never pages there, which could
   silently truncate long histories.
5. The meaning of `"11"`–`"15"` in `bodyIndexList`, the kPa unit code, and the polarity of
   the cuff-wrap flag `"8"`.
6. Whether v2 records carry a stable id (`seq`?) and the full set of v2 keys. Only the
   keys omramin reads are known, plus `deleteFlag`/`seq` from issue #27.
7. Which server Qatar or other "OFFLINE" Middle East accounts actually use.
8. Whether `measureDeviceDateFrom` is local wall-clock time. It is probably, judging from
   the format.
9. Whether the v2 `Checksum` on GET requests is the hash of an empty body. It most likely
   is, given how the hook works.
10. Whether omron-connect-mcp works with v2 accounts at all. It builds URLs from a
    v1-only attribute and was only tested on `data-jp`. So it **probably does not work**
    for CA, US, GB or QA [inferred].

---

## 9. Made-up test fixtures (fake values only)

These follow the shapes described above. All ids, serials and values are invented.

### 9.1 v1 `synchronizeMeasureData` response (fake)

Contents: one BP reading kept, one deleted reading to skip, and one scale reading that has
no keys `"1"`/`"2"`. `deviceCategory` is null, as seen in real responses.

```json
{
  "returnedValue": {
    "syncList": [
      {
        "objectId": "fake-sync-object-0001",
        "transferStartDate": 1759300000000,
        "deviceCategoryList": [
          {
            "deviceCategory": null,
            "deviceModelList": [
              {
                "deviceModel": "FAKE-BPM-100",
                "deviceSerialIDList": [
                  {
                    "deviceSerialID": "fa4e5e41a1000001",
                    "userNumberInDevice": 1,
                    "measureList": [
                      {
                        "measureDateFrom": 1759312800000,
                        "measureDateTo": 1759312800000,
                        "measureDeviceDateFrom": "20251001130000000",
                        "measureDeviceDateTo": "20251001130000000",
                        "timeZone": "Asia/Qatar",
                        "deleteFlag": 0,
                        "transferDate": 1759312900000,
                        "userUpdateDate": 1759312900000,
                        "measurementMode": 0,
                        "bodyIndexList": {
                          "1": ["121", "20496", "0", "0"],
                          "2": ["79", "20496", "0", "0"],
                          "3": ["66", "61600", "0", "0"],
                          "6": ["0", "0", "0", "0"],
                          "7": ["1", "0", "0", "0"],
                          "8": ["0", "0", "0", "0"]
                        }
                      },
                      {
                        "measureDateFrom": 1759316400000,
                        "measureDateTo": 1759316400000,
                        "measureDeviceDateFrom": "20251001140000000",
                        "measureDeviceDateTo": "20251001140000000",
                        "timeZone": "Asia/Qatar",
                        "deleteFlag": 1,
                        "transferDate": 1759316500000,
                        "userUpdateDate": 1759320000000,
                        "measurementMode": 0,
                        "bodyIndexList": {
                          "1": ["199", "20496", "0", "0"],
                          "2": ["111", "20496", "0", "0"],
                          "3": ["99", "61600", "0", "0"],
                          "6": ["1", "0", "0", "0"],
                          "7": ["0", "0", "0", "0"],
                          "8": ["0", "0", "0", "0"]
                        }
                      }
                    ]
                  }
                ]
              },
              {
                "deviceModel": "FAKE-SCALE-200",
                "deviceSerialIDList": [
                  {
                    "deviceSerialID": "fa4e5e41a1000002",
                    "userNumberInDevice": 2,
                    "measureList": [
                      {
                        "measureDateFrom": 1759320000000,
                        "measureDateTo": 1759320000000,
                        "measureDeviceDateFrom": "20251001150000000",
                        "measureDeviceDateTo": "20251001150000000",
                        "timeZone": "Asia/Qatar",
                        "deleteFlag": 0,
                        "transferDate": 1759320100000,
                        "userUpdateDate": 1759320100000,
                        "measurementMode": 0,
                        "bodyIndexList": {
                          "257": ["7250", "8195", "-2", "0"],
                          "262": ["231", "0", "-1", "0"]
                        }
                      }
                    ]
                  }
                ]
              }
            ]
          }
        ]
      }
    ]
  }
}
```

Expected parse: one BP reading, 121/79 mmHg, pulse 66, `movementDetect` true, at
2025-10-01T13:00:00+03:00 (Asia/Qatar). The second reading is skipped (`deleteFlag` 1).
The scale row is ignored by the BP parser (72.50 kg, BMI 23.1).

### 9.2 v1 `measureData` responses (fake)

Normal case:

```json
{
  "returnedValue": {
    "deviceCategory": "0",
    "deviceModelList": [
      {
        "deviceModel": "FAKE-BPM-100",
        "deviceSerialIDList": [
          {
            "deviceSerialID": "fa4e5e41a1000001",
            "userNumberInDevice": 1,
            "measureList": [
              {
                "measureDateFrom": 1759395600000,
                "measureDateTo": 1759395600000,
                "timeZone": "America/Toronto",
                "deleteFlag": 0,
                "bodyIndexList": {
                  "1": ["118", "20496", "0", "0"],
                  "2": ["76", "20496", "0", "0"],
                  "3": ["61", "61600", "0", "0"],
                  "6": ["0", "0", "0", "0"],
                  "7": ["0", "0", "0", "0"],
                  "8": ["0", "0", "0", "0"]
                }
              }
            ]
          }
        ]
      }
    ]
  }
}
```

"Empty account" case (the shape reported in omramin issue #13 and the mcp README):

```json
{ "returnedValue": { "deviceCategory": "0", "deviceModelList": null } }
```

### 9.3 Bonus: v2 `sync/bp` response (fake, CA/US/GB shape)

Only the keys omramin reads are known for sure. `deleteFlag` and `seq` come from issue
#27 and are included so we test skipping deleted readings. The account email
`adult-a@example.com` is only used as the login `emailAddress` in tests and does not
appear in the response.

```json
{
  "data": [
    {
      "seq": 1001,
      "userNumberInDevice": 1,
      "measurementDate": 1759395600000,
      "timeZone": -14400,
      "systolic": 118,
      "diastolic": 76,
      "pulse": 61,
      "irregularHB": 0,
      "movementDetect": 0,
      "cuffWrapDetect": 0,
      "isManualEntry": 0,
      "deleteFlag": 0,
      "notes": ""
    },
    {
      "seq": 1002,
      "userNumberInDevice": 1,
      "measurementDate": 1759399200000,
      "timeZone": -14400,
      "systolic": 190,
      "diastolic": 110,
      "pulse": 95,
      "irregularHB": 1,
      "movementDetect": 0,
      "cuffWrapDetect": 0,
      "isManualEntry": 0,
      "deleteFlag": 1,
      "notes": "fake deleted reading"
    }
  ]
}
```

Expected parse: one reading, 118/76, pulse 61, at 2025-10-02T05:00:00-04:00. The second
reading is skipped (`deleteFlag` 1).
