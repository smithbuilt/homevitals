# Changelog

## 1.0.6

Updates from [eufy-sync](https://github.com/sturimcode/eufy-sync) 1.15.1 to 1.15.3.

- Fewer "too many requests" blocks when logging in to Garmin. HomeVitals now needs version 0.3.20 or later of the Garmin library it uses. That version restores a login method Garmin had refused with a 429 on every try since April, and it treats Garmin's new 427 answer as the rate limit it is, so HomeVitals stops and waits instead of trying again.
- Needs curl_cffi 0.17 or later, which drops a compromised certificate from its trusted list.
- `homevitals --version`, `--status`, `--update` and the other commands that don't talk to Garmin no longer load the Garmin libraries, so they start faster and still work if one of those libraries is broken.
- A sync with no internet connection is logged as "Could not reach ..." instead of "Authentication failed", which pointed at the wrong fix.
- Strava: a 403 answer is now explained (the account that owns the Strava API app most likely has no active subscription) instead of telling you to sign in again, which can't fix it.
- Python 3.15 is supported and tested.
- The README explains why a weigh-in with socks on arrives as weight only, where the BMI comes from, and why Garmin Connect's website can show a huge "metabolic age" (a display fault on Garmin's page; the data is correct).
- BMI now goes to Garmin with a weigh-in, from [eufy-sync](https://github.com/sturimcode/eufy-sync) 1.15.3. Garmin Connect shows a BMI it works out on screen from your height, but it stores none for an uploaded weigh-in, so other apps reading your Garmin data saw a blank. HomeVitals now sends the BMI the Eufy scale app calculated. A BMI outside 10 to 70 is left out, never adjusted. This applies to new weigh-ins that arrive with their full record; a weigh-in that reaches Eufy with the weight alone (for example with socks on, before the EufyLife app has picked it up) has no BMI to send. Earlier entries stay as they are.

## 1.0.5

- Windows: automatic sync works again while the HomeVitals window is open. The scheduled task wrote its output through a Windows command line that can't open the log while the window has it open, so every scheduled run stopped before it started, without a line in the log, for as long as the window sat in the tray. HomeVitals now writes to the log itself. Existing installs are updated the next time the window starts.
- The window rolls the log over at 1 MB when it starts, since the scheduled task can't while the window is open.
- Fewer Garmin logins. Within one sync (or one Fix problems check), a Garmin login that failed isn't tried again for the same account, so weight and blood pressure no longer each try their own. After a "too many requests" (429) answer, nobody in the household logs in again in that sync: Garmin blocks the internet connection, which everyone shares, and each extra try can extend the block. A passing network failure is still retried. Logging in from Connect, `--reauth` or setup is unchanged.
- Updated the GitHub Actions used for tests and releases (checkout and setup-python v7).

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
