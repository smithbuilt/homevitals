# Changelog

## 1.0.0

First release of HomeVitals, a fork of [eufy-sync](https://github.com/sturimcode/eufy-sync) 1.13.2 for whole households.

- Several people, each syncing to their own Garmin Connect account; scale profiles not linked to anyone are never sent anywhere.
- Blood pressure from OMRON monitors through OMRON connect (written from scratch), including older readings on request.
- A window for everything: one Person window per person with a box per account (Garmin, Eufy scale, OMRON connect), each connected on its own after a real login; Fix problems; progress symbols during a sync; tray icon; open with Windows; automatic sync every 4 hours.
- Weights and blood pressure numbers never written to the log; passwords only in the system password store.
- Its own data folder (`~/.homevitals`) and keychain entry, copied over from eufy-sync on first start.
