# RXP Quest Guard

RXP Quest Guard is a World of Warcraft addon and Windows companion for checking RestedXP quest pickups and prerequisites. It is an independent community project with no affiliation with Blizzard Entertainment or RestedXP.

- **In game** — Check quest state, missed pickups, starting items, known timers, and the next 30 guide steps. View evidence for a finding in the addon window.
- **On desktop** — Preflight a linked route from your saved quest state. Monitor for new saves and publish route evidence for the addon to use offline.
- **Optional online verification** — Investigate uncertain findings and prefetch quest metadata for upcoming chapters with approved developer access. [Quest data from ForeverDB.net](https://foreverdb.net)

[See a preflight with fictional data](docs/DEMO.md) · [Read the privacy and data policy](docs/DATA-AND-PRIVACY.md)

## Install on Windows

Quest Guard v0.5.0 targets WoW Forever **1.60.1**, interface **16001**, and the RestedXP **v4.11.36** integration. [Build the installation packages](#build-and-verify) from the source.

1. Extract the **RXP Quest Guard Desktop** ZIP. Keep the executable and its `_internal` folder together. Open `RXP Quest Guard Desktop.exe`.
2. Open **Setup & updates**. Locate WoW Forever, or select the Forever folder that Battle.net opens with **Show in Explorer**. The expected addon directory ends in `_forever_\Interface\AddOns`.
3. Select **Install or update Quest Guard**. After installation, sign in and enable **RXP Quest Guard** on the character selection screen. If you are already logged in, use `/reload` or relaunch WoW if the addon was absent from the last addon list.
4. Use `/qg` in chat to open Quest Guard. To enable route analysis, select a self-authored or otherwise authorized guide in RestedXP and run `/qg guides allow`.

### Upgrade from an earlier release

Extract the 0.5.0 desktop ZIP to a new folder and run its executable. In **Setup & updates**, use **Install or update Quest Guard** against the existing Forever installation. This preserves the published `Data.lua`, the desktop quest cache, and WoW's existing addon settings and RestedXP progress. Log into the character you want to scan and use `/reload` or log out once Quest Guard has captured your quest log.

For a manual upgrade, extract the addon ZIP to a separate folder first. Copy the updated TOC and Lua files into your existing `AddOns\RXPQuestGuard`, keeping your existing `Data.lua` if present. The ZIP's `Data.lua` is a starter file for fresh installs.

For manual installation, extract the addon ZIP into `Interface\AddOns`. Check for `Interface\AddOns\RXPQuestGuard\RXPQuestGuard.toc`. For another interface number, use the desktop installer's detection or enter the value returned by `/dump select(4, GetBuildInfo())` in Setup. Functional compatibility depends on the APIs available in that client.

The Windows x64 desktop ZIP is a standalone, unsigned release with Python and Tk bundled. Extract it before launching. For source use on Windows, run `python -m questguard` with Python 3.12 or later and Tk available.

## Use the addon

| Command | Action |
| --- | --- |
| `/qg` | Open or close the planning window |
| `/qg scan` | Recheck quests using the selected lookahead |
| `/qg next` | Run a next-chapter check when the chapter is available |
| `/qg status` | Print the addon version, RestedXP connection, guide and step, quest snapshot readiness, and published route status in chat |
| `/qg debug` | Open the detailed diagnostics panel |
| `/qg check <quest ID>` | Check a quest directly using the live quest log |
| `/qg guides` | Check whether RestedXP guide analysis is authorized |
| `/qg guides off` | Turn off guide analysis before switching to restricted content |

Automatic preflight is on by default when guide analysis is authorized. Quest Guard checks progress on login or reload, zone changes, RestedXP chapter and step changes, and quest updates. On a chapter change it checks the entered chapter and its linked successor where available. The step lookahead defaults to 30 and can continue into linked chapters when their steps are available. Automatic checks coalesce nearby events, and broader work queues until combat ends. Direct quest checks work before guide authorization.

Use `/qg settings` to control automatic preflight, zone and chapter triggers, upcoming-step scanning, lookahead, warning notifications, and repeated-warning suppression. Suppression quiets repeated alerts while their findings remain available in the report. Already configured notification preferences carry forward on upgrade.

The planning window shows the current guide, scope, findings and route coverage. Use `/qg scan route` for an on-demand scan of the available published path. A missing-pickup warning needs reliable quest state. Optional objectives and uncertain or structural findings stay in the report. When the published route has a coverage gap or a version or character mismatch, the report explains which context is unavailable. When a pickup has a usable map ID and zone coordinates, the TomTom control adds a waypoint on request.

The Quest Guard minimap button opens and closes the planning window. With OneForAll's **Collect minimap buttons** enabled, look for it in OneForAll's button bar. You can also open Quest Guard with `/qg` when the button is hidden.

Quest Guard first checks whether the quest is active, ready to turn in or completed. A missing warning requires a usable quest log and completion check. Preflight considers the order of pickups and objectives, skipped steps, restrictions, known branches and prerequisites. An earlier applicable pickup followed by a missing quest is evidence of a missed handoff. Future pickups on the same route account for quests a player has yet to reach. A conditional path, a guide version mismatch or incomplete state can yield an unknown or a review finding. A guide objective can require an active quest even when that objective's role in the overall leveling route is unknown.

When WoW identifies a matching quest-starting item in your bags, an upcoming objective can show **Ready to activate**. A verified published item association can also support that notice when you have the item. Activate it manually when ready. When the client exposes a countdown for an active quest, Quest Guard shows the time it last read. Use `/qg check <quest ID>` and the report to inspect the evidence. Unresolved source conflicts stay visible, and disputed pickup locations are withheld from waypoint guidance.

## Run a desktop preflight

1. Open the desktop app and click **Refresh character progress**. For route analysis, open **Setup & updates**, enable **Analyze guides I wrote or have permission to use**, and use **Guide folder…** to select a folder containing only authorized inputs. A paid RestedXP guide purchase alone does not grant permission for automated processing. [RestedXP guide terms](https://shop.restedxp.com/terms-conditions/)
2. Click **Refresh character progress** after `/reload` or logout. Select the character and verify the guide, runtime step, quest counts and saved snapshot time. The app maps saved stable step IDs to the imported chapter when it can. A manual step edit is shown as an override until you return to the saved position.
3. Review the setup indicators and run **Preflight** from the chosen chapter. The planned list follows the selected linked chapters forward from the scan start. Earlier linked guides can provide pickup history. Findings distinguish missing pickups, confirmed prerequisite blockers, optional quests and reviews. If quest history is unavailable, use **Structural-only scan** to inspect the guide structure and its gaps.
4. After a successful scan, publish the selected linked route and optional quest cache. The publisher retains earlier pickup chapters and extends the linked route through level 60 where the guide source covers it. Quest Guard uses that context for automatic in-game checks. Use `/reload` to load the new file. Republish when RestedXP moves to a new guide release or Quest Guard flags a coverage issue.

WoW writes addon files on logout or UI reload. The desktop reads the latest successfully saved file when you import or refresh, even if WoW is running. A legacy account-level export with no character identity requires explicit association. Quest Guard remembers a confirmed association for that exact export. A changed anonymous export can belong to another character and requires a new confirmation. Use the per-character Quest Guard snapshot for automatic association on future saves.

Enable **Monitor saved progress** to import newly written Quest Guard snapshots and rerun preflight for the selected character and route while the desktop is open. The dashboard shows **Last completed preflight** and **Snapshot used**. Game progress appears after WoW saves the file. Publishing remains a separate action.

## Verify quests online

1. In **Quest Verification**, open **Provider settings…**. Request a ForeverDB developer API key approved for your use. Enter it and choose **Authorize ForeverDB**. The desktop keeps authorization and the key for the session. [ForeverDB.net](https://foreverdb.net) · [Developer portal and attribution requirements](https://foreverdb.net/developers)
2. Select a character and route, then refresh saved progress or enable **Monitor saved progress**. After a scan, intelligent verification prioritizes unresolved prerequisites, suspected missed pickups, and objectives in the current and next two linked chapters. It reuses fresh evidence. Adjust the scope and cache ages in the verification settings.
3. Search by quest name or ID. Select a row for the character state, confidence, source claims, conflicts and recommended action. Use **Verify selected quest**, **Verify current chapter**, **Prefetch upcoming**, or **Refresh route to level 60** for a deliberate lookup. **Wowhead reference** opens the quest page in your browser.
4. Review **View preflight differences** when the lookup completes. Choose **Publish verified data** after a character preflight and use `/reload` to load the update. If you enable automatic publishing, it applies to the chosen installation and verified character for that session.

The desktop receives in-game progression after WoW writes SavedVariables on logout or `/reload`. The addon uses the route and evidence loaded at startup or the last reload. It continues checking quests while the desktop is closed. You can also import permitted [local JSON quest data](docs/DATA-AND-PRIVACY.md#local-json-schema) and publish it with the route.

## Known limitations

- RestedXP visibility varies by guide version and route. Partial or stale saves and unknown prerequisites can leave a finding unconfirmed.
- The desktop observes newly saved files. Use `/reload` or log out to save progress, and reload after publishing data to the addon.
- Authenticated provider and live WoW behavior still require validation. The automated tests use provider and game fixtures. [Verification notes](docs/VERIFICATION.md)

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Addon absent or out of date | Check that the folder has one level of `RXPQuestGuard` under the Forever `AddOns` folder. Reinstall with the client interface detected by the desktop. |
| RestedXP integration unavailable | Enable RestedXP, select a guide and reload. Use the desktop report or `/qg check <quest ID>` while the integration is unavailable. |
| Guide analysis is off | Enable it in **Setup & updates** or run `/qg guides allow` in game once you have selected inputs you have permission to analyze. |
| Active or completed quests show Unknown | Enable Quest Guard on the character, wait for the quest log to initialize, use `/reload` or log out, then refresh progress. If only a legacy anonymous export is available, associate it explicitly or use Structural-only scan. |
| Quest Guard button is missing from the OneForAll bar | Check that Quest Guard's minimap button and OneForAll's **Collect minimap buttons** are both enabled. Use `/reload` after upgrading, then open the bar. |
| Desktop still displays an older step | Save with `/reload` or logout and refresh. If you set a manual override, clear it to use the new saved position. Check the guide variant and stable step details in Diagnostics. |
| Quest data shows Stale or age Unknown | Refresh after a new game save. A legacy export may be undated. Treat its counts as a record of that export and use a new Quest Guard snapshot for confirmed current findings. |
| Route ends early or has multiple variants | Import the full client folder or relevant local guide data. Inspect the route diagnostics and pick the exact guide group and start chapter. Unresolved links and unavailable chapters limit forward scans. |
| Missing pickup location or waypoint | Some guides use world coordinates. A waypoint requires a verified UI map ID and percent coordinates. Use the named pickup and chapter when only those facts are available. |
| Route publishing fails | Confirm the target is the intended Forever installation and Quest Guard 0.5.0 is installed for its interface. Recheck the character selection and run preflight again after changing the route. A failed atomic publish leaves the previous file available. |
| In-game route coverage is limited or outdated | Refresh the desktop guide source, rerun the selected preflight, publish the full linked route, and use `/reload`. Quest Guard can still check objectives in the currently loaded RestedXP guide. |
| Desktop background refresh has no new scan | Confirm it is enabled for the selected installation and character. Use `/reload` or log out to write a new Quest Guard snapshot. |
| Published route waits for a matching realm | Refresh using a new Quest Guard capture. Older files may identify the realm only by the folder name. The addon requires a matching realm value before using the published route. |
| ForeverDB is unavailable or rejects a key | Open **Provider settings…** and check your approved key and account quota. Allow a rate-limit cooldown to finish. Cached evidence and local checks remain available. |
| Quest Verification shows uncertain or conflicting results | Inspect the selected row for the exact field and its source. Check the current character state separately from guide or provider claims. Treat old or Classic-only timers, locations and dependencies as references. |

## Build and verify

Run from the source folder with 64-bit Windows CPython 3.12 and Tk. The explicit interface number lets you build without a game installation.

```powershell
python -m pip install -r requirements-build.txt -r requirements-test.txt
python -m unittest discover -s tests -v
python scripts/build_release.py --interface 16001
```

Use `--addon-only` to build just the Lua addon ZIP. The build writes the packages and SHA-256 checksums to `dist/`.

For a command-line audit of a self-authored or otherwise authorized guide, use `python -m questguard audit --help`. Include `--authorized-guides` when you have permission to process the guide inputs.

## Privacy and license

Character state and guide analysis stay on your machine. Optional ForeverDB requests send the quest ID and the authorized key. Reports, local caches and published addon data can contain private information. Keep them out of public issues and commits. [What Quest Guard stores and transmits](docs/DATA-AND-PRIVACY.md)

The [MIT license](LICENSE) covers original Quest Guard code and documentation. Blizzard and RestedXP material remains subject to its own rights and terms. Quest Guard uses ForeverDB data only with applicable permission and credits [ForeverDB.net](https://foreverdb.net) where its data is displayed.

[Third-party notices](THIRD-PARTY-NOTICES.md) · [Contribute](CONTRIBUTING.md) · [Report a security issue](SECURITY.md)
