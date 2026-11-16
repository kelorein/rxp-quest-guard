# Third-party notices

The [MIT license](LICENSE) covers original RXP Quest Guard source code, documentation, and synthetic test material. Third-party software and data retain their own terms. Quest Guard is an independent community project.

## Game and guide integrations

- **World of Warcraft** and its assets belong to Blizzard and its licensors. The addon references a client-supplied icon by name. It ships no extracted game art or Blizzard UI code. Blizzard publishes separate rules for distributing and operating WoW addons. [Blizzard addon policy](https://us.forums.blizzard.com/en/wow/t/ui-add-on-development-policy/24534)
- **RestedXP** is a separate addon. The public RXPGuides v4.11.36 source declares CC BY-NC-SA 4.0. Paid guide content and derivatives are governed by separate terms. Use guide inputs you authored or have permission to process with companion tools. Keep paid guides, guide-derived routes, and report exports out of this repository. [RXPGuides license](https://github.com/RestedXP/RXPGuides/blob/v4.11.36/LICENSE) · [RestedXP guide terms](https://shop.restedxp.com/terms-conditions/)
- **TomTom** and **OneForAll** are optional, separately installed addons. Quest Guard includes its own integration code.
- **ForeverDB** provides optional quest lookups under its approved API access terms. Quest Guard credits [ForeverDB.net](https://foreverdb.net) in displays of its data. Some responses include labeled Classic fallback facts that ForeverDB attributes to CMaNGOS Classic-DB. No provider database or cache ships here. API access and attribution do not grant a blanket redistribution license for source data. [ForeverDB API and attribution](https://foreverdb.net/developers) · [ForeverDB data sources](https://foreverdb.net/about) · [CMaNGOS Classic-DB license](https://github.com/cmangos/classic-db/blob/master/LICENSE.md)
- **Wowhead** quest pages are optional browser references. [Wowhead terms](https://www.wowhead.com/forever/tos)

## Python, build and test tools

| Component | Where it is used and which terms apply |
| --- | --- |
| CPython and tkinter | Desktop runtime. CPython uses the Python Software Foundation License and retains notices for incorporated software. On Windows use the license and notices from the exact interpreter distribution used to build the application. [Python license](https://docs.python.org/3.12/license.html) |
| Tcl and Tk | Desktop toolkit used through tkinter. Retain the applicable Tcl/Tk notices when distributing their runtimes. [Tcl license terms](https://core.tcl-lang.org/tcl/file?name=license.terms) · [Tk license terms](https://github.com/tcltk/tk/blob/core-8-6-15/license.terms) |
| PyInstaller | Build dependency. Its bootloader exception permits distribution of the generated app under the app's own license, subject to the terms of bundled dependencies. PyInstaller itself has separate terms. [PyInstaller license and bootloader exception](https://www.pyinstaller.org/en/stable/license.html) |
| Lupa and Lua | Test dependencies installed separately. Lupa uses MIT-style terms and includes Lua runtimes with their own notices. [Lupa license](https://github.com/scoder/lupa/blob/master/LICENSE.txt) · [Lua license](https://www.lua.org/license.html) |

The source repository contains no dependency binaries. A standalone Windows build can collect CPython extension dependencies and Microsoft runtime files. Before distributing a bundle, review its exact files and retain the corresponding upstream notices. Microsoft runtime files remain subject to Microsoft's redistribution terms and the additional conditions in the Windows Python distribution's license. [Microsoft Visual C++ redistribution](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files)
