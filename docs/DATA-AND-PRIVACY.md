# Data, privacy and licensing

## Sources and confidence

| Source | What Quest Guard uses |
| --- | --- |
| WoW quest and bag APIs | Active, ready and completed state, carried quest-starting items and active quest timers when the client exposes those values. A missing API result is Unknown. |
| Loaded RestedXP addon | Reads the current guide, compiled quest actions and progression where exposed. This is a version-dependent integration with RestedXP's loaded Lua state. |
| Local guide files and SavedVariables | Parses literal tables, readable guide declarations and bounded, directly decompressible guide cache entries. Quest Guard's own save contains the captured character identity, quest IDs and guide position. Unreadable or protected records limit coverage. |
| User-supplied quest data | Uses explicit dependencies, restrictions, classification and locations from the local JSON file. Include a source and confidence for each record. |
| ForeverDB.net | Uses the documented per-quest `GET /api/v1/quests/{id}` endpoint and an approved key in the `X-API-Key` header. The documented free limits are 30 requests per minute and 5,000 per day across the account. Quest Guard conservatively spaces requests, honors rate-limit responses and uses bounded retries for transient errors. It displays ForeverDB attribution where its data is shown. The provider can fall back to Classic data. Chain edges remain advisory until their alternatives and dependency semantics can be established. [Data from ForeverDB.net](https://foreverdb.net) · [ForeverDB developer documentation](https://foreverdb.net/developers) · [ForeverDB verification](https://foreverdb.net/verify) · [ForeverDB data sources](https://foreverdb.net/about) |

Use only self-authored guides or other guide inputs you have permission to process and export. Guide analysis starts disabled and requires confirmation in the desktop app or in-game addon. Keep authorized guides in a separate folder on desktop and turn off in-game guide analysis before switching to a restricted guide. RestedXP's paid guide terms separately restrict automated processing, derivative content and sharing. Local access to a guide does not supply separate rights to process or redistribute it. [RestedXP terms and conditions](https://shop.restedxp.com/terms-conditions/)

Quest Guard opens Wowhead Forever quest references in the browser on request. No authorized Wowhead API for automated quest ingestion was verified. ForeverDB uses account-approved access and requires attribution. Follow the access terms for your key and obtain separate permission before redistributing cached data. [Wowhead terms](https://www.wowhead.com/forever/tos) · [Fanbyte terms](https://corp.fanbyte.com/legal/terms) · [ForeverDB developer documentation](https://foreverdb.net/developers)

The app ships with an empty quest cache. Each supported external fact can carry its own provider, URL, retrieval time, verified date, game version, method and confidence. The report distinguishes direct game observation, Forever-specific evidence, structured provider metadata, inherited Classic information, guide inference and unverified claims. Conflicts retain both values and their sources. When equally applicable sources disagree on a location or prerequisite, the report withholds it as an actionable fact. An online entry describes source evidence. Character quest state comes from WoW.

Quest eligibility, route classification, class restrictions and pickup locations stay Unknown when the applicable source cannot establish them. A known mandatory quest still needs reliable evidence before Quest Guard reports a missing prerequisite. The ForeverDB quest level is descriptive. An explicit minimum player level can establish a level gate. Coordinates from different map systems stay separate.

## Local JSON schema

Save a UTF-8 JSON object with `schemaVersion` `1` and a `quests` object keyed by positive quest IDs. The following values are invented to show the format.

```json
{
  "schemaVersion": 1,
  "source": "Example data",
  "sourceVersion": "example-1",
  "updatedAt": "2026-11-15T00:00:00+00:00",
  "quests": {
    "100003": {
      "name": "Example task",
      "classification": "unknown",
      "source": "Example data",
      "confidence": "example",
      "minLevel": 45,
      "faction": "Horde",
      "classes": ["HUNTER"],
      "requirements": [{"questId": 100001, "state": "completed"}],
      "requirementGroups": [
        [{"questId": 100002, "state": "active"}],
        [{"questId": 100004, "state": "completed"}]
      ],
      "startLocation": {"name": "Example sign", "zone": "Example zone", "mapId": 700, "x": 58.3, "y": 41.1}
    }
  }
}
```

All entries in `requirements` must be true. If `requirementGroups` is present, at least one inner group must also be fully satisfied. A `state` is `completed` or `active`. Use `mandatory` only with established route evidence. Other accepted classification values are `optional`, `breadcrumb` and `unknown`. Location `x` and `y` are percentages between 0 and 100. Include the actual UI map ID. An offset-bearing `updatedAt` is used for cache age. Omit uncertain fields.

Importing JSON replaces the local schema 1 database after validation. The verification workflow reads it as an offline base and combines it with the new cache without rewriting it. Existing published quest records remain available when a later publish adds different quest IDs.

### Verification cache schema 2

The desktop uses `verification-v2.json` alongside `quests-v1.json`. Its top-level `records` object is keyed by provider ID and then quest ID. Each row stores normalized `quest` facts, `sourceVersion`, `retrievedAt`, and the `clientVersion` and `guideVersion` known at lookup. Under `quest`, `fieldEvidence` maps each field to fact claims and `conflicts` retains disagreeing claims. Explicit prerequisite groups, advisory chain edges, starter item IDs and timer facts have separate fields. The top-level `failures` object stores temporary cooldown entries by provider and quest. Quest ID `0` in that object means the entire provider is cooling down.

Fresh metadata is reused for 30 days by default. Dependency-sensitive lookups default to 14 days. Both ages are adjustable. Game-version changes queue affected IDs for review. A RestedXP version change refreshes lookups tied to route dependencies, and descriptive facts remain available offline. Outdated or incompatible sensitive claims stay available as evidence. The desktop withholds them from new actionable data, and the addon also checks explicit evidence build numbers. Cache updates and addon publications use atomic replacement. Rate limits, cancellations and failed requests preserve existing successful records.

## Local storage and network

- The desktop accesses the installed files or archives you select. SavedVariables are parsed as data and kept in process for analysis. It never executes their Lua or edits WoW's SavedVariables. It reads archive members in place under size limits.
- Quest Guard collects recognized quest and guide progress in its own per-character WoW SavedVariables. WoW writes those files when it saves the UI, such as `/reload` or logout. The desktop imports the saved record and can warn about missing, partial or stale quest state.
- The optional desktop monitor checks the local Quest Guard save files for changes. Automatic preflight uses the newly imported snapshot for the selected, verified character. Automatic publishing is off by default and is scoped to the verified character and installation selected when enabled.
- A confirmed legacy association is stored locally under `%LOCALAPPDATA%\RXPQuestGuard`. This links an exact export to the selected character identity and avoids prompting again for the same export. A materially changed anonymous export needs a new confirmation. The application uses the account and per-character file provenance to keep separate characters distinct.
- Desktop quest databases are cached under `%LOCALAPPDATA%\RXPQuestGuard`. The addon receives normalized quests, evidence and route metadata in its own `Data.lua`. The published context includes the selected character, realm, linked route endpoint, and guide version when available. Active and completed quest history stays in SavedVariables. Reload WoW after publishing.
- WoW saves Quest Guard settings and captured progress through its standard SavedVariables mechanism. The working desktop report can contain character details or guide findings. Review saved reports before sharing them.
- The optional ForeverDB request sends a quest ID and the approved key in an HTTPS request after you authorize the provider. The masked key and authorization stay in the desktop session. Developers can supply `QG_FOREVERDB_API_KEY` and still must authorize it in the app. Character identity, quest history and guide text stay local. [ForeverDB developer documentation](https://foreverdb.net/developers)
- No telemetry or account sign-in is implemented. The desktop never reads process memory or sends inputs to WoW.

## Blizzard policy and distribution

Quest Guard uses ordinary addon APIs and local files. It does not inject code, read or write client memory, automate combat or navigation, accept or turn in quests, send synthetic inputs, modify the WoW client or circumvent protected functionality. The optional TomTom waypoint follows an explicit player action. Quest Guard reads RestedXP and leaves its files and progress under RestedXP's control.

Blizzard's published addon policy requires visible code and free distribution alongside its EULA. Distribute the addon free of charge, with its readable Lua, without ads or donation solicitations in game. Blizzard may change what APIs are available or what it permits. Quest Guard is an independent project without Blizzard's approval or endorsement. [Blizzard UI Add-On Development Policy](https://us.forums.blizzard.com/en/wow/t/ui-add-on-development-policy/24534) · [Blizzard EULA](https://www.blizzard.com/en-us/legal/08b946df-660a-40e4-a072-1fbde65173b1/blizzard-end-user-license-agreement)

The project's original code and documentation are under the MIT license. RestedXP and World of Warcraft are the property of their respective owners. The source distribution includes original code, empty starter data and synthetic fixtures. Keep local guide metadata and cached provider data private unless you have the relevant redistribution rights. Windows runtime and tool licenses are described in the [third-party notices](../THIRD-PARTY-NOTICES.md).
