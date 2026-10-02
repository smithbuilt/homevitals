# Security

HomeVitals handles passwords for Garmin, Eufy and OMRON connect, and health readings. If you find a way for any of these to leak (into logs, files other than the system password store, the window, notifications, or another person's Garmin account), please don't open a public issue. Use GitHub's **Report a vulnerability** button on the Security tab of this repository instead.

What HomeVitals promises, and tests on every change:

- Passwords and session tokens are kept only in the system password store (or, when you opt in with `homevitals --use-file-store`, a file only your user can read).
- Weights and blood pressure numbers never appear in the log, the window's messages or notifications.
- A person's readings only ever go to their own Garmin Connect account; scale profiles not linked to anyone are never sent anywhere.
- The tests never contact real servers.
