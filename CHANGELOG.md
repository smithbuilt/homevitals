# Changelog

## 1.0.4

More reliability fixes from [eufy-sync](https://github.com/sturimcode/eufy-sync) 1.14 and 1.15.

- Garmin on blocked networks: on some connections (VPNs, some internet providers) Garmin's Cloudflare protection refuses every call after a good login, with "403 Forbidden". A refused call is now tried once more through a browser-like connection using the same login, and if that works, the rest of the sync uses it. This covers weigh-ins and blood pressure.
- At most one Garmin re-login per person per sync. Repeated logins in one run make Garmin's "too many requests" block more likely, and a second try minutes later meets the same security-code request or wrong password anyway.
- Eufy login: if Eufy's original login address stops working, HomeVitals tries once at the address the current EufyLife app uses. Never after a wrong password or a rate limit.
- One weigh-in that keeps failing no longer holds back newer ones. Failed uploads wait in a retry list; after 14 days or 84 tries a stuck one stops blocking (it still gets one try per sync), and it is given up only after 30 days of failures while newer weigh-ins kept uploading, so an outage never loses a measurement. `homevitals --status` shows how many uploads are waiting.
- Garmin's answers to an upload are sorted properly: "already have it" (409) counts as uploaded only once a lookup finds the weigh-in on Garmin, a rate limit stops Garmin for the rest of that sync, and a refusal that a fresh login can't fix is reported once instead of being retried.
- Blood pressure: a reading Garmin refuses outright (an HTTP 4xx that isn't about the login or the rate) is skipped, never changed, and reported, instead of holding back every newer reading. Login, rate-limit and server problems still stop the sync and are retried next time.
- The look-back for weight-only Garmin entries still waiting for their full record is capped at 14 days, so an old one no longer makes every sync refetch everything since then.

## 1.0.3

Reliability fixes, most of them taken from [eufy-sync](https://github.com/sturimcode/eufy-sync) 1.14 and 1.15. Thanks to Elias Sturim for pointing them out.

- Saved logins are crash-safe. A household's saved logins are too big for one Credential Manager entry, so they are split into pieces. Each save used to overwrite those pieces one by one, so a save cut short (a crash, a shutdown) could leave a mix of old and new pieces that read as empty, and the next save would then overwrite every saved login. Each save now writes new pieces and switches over only at the very end, with a checksum. A damaged store is reported in Fix problems instead of being treated as empty, and two HomeVitals processes (the window and the scheduled sync) take turns saving.
- Moving over from eufy-sync 1.14 or later now copies the saved logins too. Before, the newer storage format wasn't recognised, so nothing was copied and people had to connect their accounts again.
- A Garmin re-login keeps the saved session until the new login works. Before, it deleted the session first, so a cancelled or missing security code (two-step verification), or a passing Garmin error, cost a session that may still have been good. The same now applies to OMRON connect.
- After updating, don't go back to 1.0.2 or earlier: those versions can't read the new storage format.

## 1.0.2

- The window's title bar and the tray icon's hover text show the version (for example "HomeVitals 1.0.2"), so you can see which version is running.

## 1.0.1

- A note at the top of the window: Runalyze, intervals.icu and TrainingPeaks get these readings from Garmin Connect, so there's nothing to set up for them here (Runalyze: weight and blood pressure; intervals.icu and TrainingPeaks: weight). Strava takes activities from Garmin, but not weight or blood pressure. The README lists the sources.
- The launcher path for shortcuts and automatic sync keeps its spelling when completed (fixes the tests on machines with a `C:\tools` folder).

## 1.0.0

First release of HomeVitals, a fork of [eufy-sync](https://github.com/sturimcode/eufy-sync) 1.13.2 for whole households.

- Several people, each syncing to their own Garmin Connect account; scale profiles not linked to anyone are never sent anywhere.
- Blood pressure from OMRON monitors through OMRON connect (written from scratch), including older readings on request.
- A window for everything: one Person window per person with a box per account (Garmin, Eufy scale, OMRON connect), each connected on its own after a real login; Fix problems; progress symbols during a sync; tray icon; open with Windows; automatic sync every 4 hours.
- Weights and blood pressure numbers never written to the log; passwords only in the system password store.
- Its own data folder (`~/.homevitals`) and keychain entry, copied over from eufy-sync on first start.
