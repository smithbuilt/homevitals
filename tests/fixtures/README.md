# Test fixtures

Everything in this folder is made up: names, emails, ids, tokens and every weight or health number. The files only copy the *shape* of real Eufy and Garmin responses so the tests can run without touching a real server. Never put real personal data here.

The household has four Eufy profiles: two adults (`adult-a-0001`, `adult-b-0002`) and two kids (`kid-c-0003`, `kid-d-0004`). The kids' weights are deliberately above the 22.7 kg upload filter, so a kid wrongly reaching an upload would show up in the tests instead of being hidden.

The OMRON connect files (`omron_v2_*.json`) and the Garmin blood pressure files (`garmin_blood_pressure_range_*.json`) are fake too. The blood pressure numbers were picked so none of them can be confused with a count or a date in a log line, and every `notes` field carries a `SENTINEL-NEVER-LOG-*` marker so the tests can prove that response bodies never reach the logs.

`config_household_partial.yaml` is a partly set up household: Chris has everything, Jane has only Garmin, Sam has only the scale. Nobody in it is misconfigured. The tests use it to prove that a person without a source never receives data, and that a profile linked to someone without Garmin still counts as taken. The kids' profiles stay unlinked.
