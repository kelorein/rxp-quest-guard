# 0.5.0 verification and compatibility

## Compatibility target

Version 0.5.0 targets **WoW Forever 1.60.1**, interface **16001**, and the RestedXP **v4.11.36** integration. The addon can use OneForAll's minimap collection and can request a TomTom waypoint on player action. [RestedXP v4.11.36 public TOC](https://raw.githubusercontent.com/RestedXP/RXPGuides/v4.11.36/RXPGuides.toc)

## Automated checks

The test suite runs on Windows with CPython 3.12, Tk, and Lua 5.1 through Lupa. Install `requirements-test.txt`, then run `python -m unittest discover -s tests -v`. It uses synthetic route, snapshot, game API and provider fixtures. [Run a fictional preflight](DEMO.md)

| Area | Verified in tests |
| --- | --- |
| Provider authorization and failures | Documented HTTPS request and authentication headers, zero calls before consent, bounded retries, cancellation and backoff, invalid key, quota, rate limits with Retry-After, offline fallback, malformed and incomplete payloads, and browser-only Wowhead references. The API transport used fixtures. |
| Evidence and eligibility | Field-level attribution, Forever and Classic claims, explicit prerequisite groups, advisory chain relations, comparable source conflicts, disputed coordinates, an unknown dependency staying unknown, version and age checks, and stored evidence surviving cache and route updates. |
| Chapter planning | Missing-pickup priority, default next-two-chapter scope, linked routes, request deduplication, game- and guide-version changes, variants and conditional steps, optional quests, and future pickups. |
| Desktop | Native Tk dashboard, verified per-character snapshot monitoring, safe reads during file replacement, saved-step selection and manual overrides, authorization, a separate lookup worker, preflight rerun, and visible evidence, source links and differences. |
| Addon and progress | Lua simulations for login and reload, zone, guide, step and quest changes, 30-step scans across chapters, notification suppression, combat deferral, named minimap registration, `/qg status`, state capture and offline operation. A source check covers direct in-game network interfaces. |
| Installation and publishing | Upgrade and atomic replacement in temporary directories, preservation of SavedVariables, prior published quest evidence, and RestedXP files, client and character gates, full route coverage diagnostics, and exclusion of API credentials from the cache, status and generated Lua. |

## Finding examples

- A missing pickup points to an earlier applicable acceptance step. The report retains warnings for objectives already passed as historical findings. Active or completed state suppresses the missing-pickup warning.
- A starting item can show **Ready to activate** when the game confirms the item-to-quest association. The test fixture also covers separate countdown observations and conflicting metadata. A Classic-only claim stays advisory.
- When the game reports a quest as active, the addon keeps that state even if provider evidence is incomplete or contradictory.

## Live verification still needed

1. Use an authorized ForeverDB key in **Quest Verification → Provider settings…**. Verify one quest, check the returned source, then run a chapter prefetch. Confirm the response fields, rate limits and attribution with the account. Network verification uses simulated provider fixtures in the automated suite. [ForeverDB.net](https://foreverdb.net)
2. Install Quest Guard in WoW Forever, enable Quest Guard and RestedXP, and open `/qg status`. With an authorized test guide selected, enable analysis with `/qg guides allow`. Check login and `/reload`, change zones, advance through a chapter and observe an actual quest-log change. Confirm alert timing and presentation, combat deferral, OneForAll collection with the toggle on and off, and desktop import after the client saves.
3. With a permitted test guide and a route published, check an objective whose quest is neither active nor completed. Verify the earlier pickup warning and confirm it disappears after the quest becomes active.
4. With a quest-starting item carried and its quest absent from the log, run `/qg check <quest ID>` before manually activating it. For a timed quest, compare the displayed countdown with the game objective tracker.

WoW writes new SavedVariables at logout or `/reload`. Published Lua loads at startup or `/reload`. If a new snapshot is partial, let the quest log finish loading and expand collapsed categories before saving again. A RestedXP format update or unavailable guide chapter can reduce route coverage. WoW Forever and authenticated ForeverDB behavior remain separate from the simulated checks above.
