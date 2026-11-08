# Zmod AFC Plus

> **Parts:** the printers, boards, readers and other gear that go with this project are listed at [projects.die-hard.net/gear](https://projects.die-hard.net/gear) (Amazon affiliate links).

Additions to the **AFC panel built into Zmod 1.7.5 and later** (`zmod_afc`),
for the Flashforge AD5X and Creator 5 (Pro).

Zmod 1.7.5 took over what [zmod_afclite](https://github.com/DIEHARDave/zmod_afclite)
did: the AFC panel in Mainsail and Fluidd, SET_COLOR / SET_MATERIAL /
SET_WEIGHT / SET_SPOOL_ID, the Spoolman active spool, IFS Jacker units, and the
Creator 5 load. While updating, Zmod removes zmod_afclite. Since 2026-11-08
Zmod 1.7 also keeps lane spools through a reboot, resets the Creator 5 head
sensors after an idle load (`idle_load`), migrates zmod_afclite's lane spools
and weights, and writes lane_data itself while HelixScreen does not manage it.
This plugin adds only what the built-in panel still does not have. It
registers no AFC objects or commands of its own, so it does not conflict with
Zmod. Keep Zmod up to date: with an older 1.7.5 build the plugin logs a
warning, and lane spools are lost at every reboot.

## What it adds

- **lane_data for OrcaSlicer, with HelixScreen.** While HelixScreen manages
  Moonraker's `lane_data` (Zmod's `helix = 1`, where Zmod writes none), its
  entries are kept on Zmod's slots, which OrcaSlicer's filament sync reads:
  `lane1..lane4` on the AD5X and `T0..T3` on the Creator 5, found by their
  `lane` field. Entries are merged, never created. They are rechecked every
  15 s, and one that another writer (HelixScreen, SpoolSync, FF_NFC) left on a
  different filament is corrected:
  - An exact spool color is kept while it is the same Zmod color (on the
    Creator 5, the same palette entry).
  - An empty slot gets material `""`; its other fields stay.
  - Spool fields (name, vendor, temperatures, `spool_id`...) are dropped when
    the color or material changes, or when the AFC panel has a different spool
    on that lane.
  - Slot reads must agree for 3 polls, because the IFS reports every slot
    empty for a few seconds after boot.
- **Creator 5 lane map between prints**, on Zmod builds without the
  `[zmod_afc] idle_map` option. Zmod's `file.json` keeps the last print's tool
  mapping, so the panel (and HelixScreen, which reads the AFC map) showed
  lanes on the wrong tools when idle. Between prints each lane maps to its own
  toolhead. During a print, a lane the mapping leaves out serves its own
  toolhead, unless that tool is mapped to another lane. Zmod with `idle_map`
  resets `file.json` itself when a print ends, and then this part stays off.
- **Spoolman colors on lanes.** A lane with a Spoolman spool shows the spool's
  own color while Zmod stores it as the slot color, on the AFC panel and in
  lane_data, so OrcaSlicer gets it too. That matters on the Creator 5, which
  keeps only a palette index per slot: an Ocean Blue `#C6EBEB` spool is
  stored as white `#FFFFFF`, but shown and synced as `#C6EBEB`. The color
  comes from Zmod's `zmod_afc` where it keeps it (Zmod 1.7 of 2026-11-08 and
  later), else from Spoolman.

Turn the panel on with `SAVE_ZMOD_DATA AFC=1` (Zmod turns it on itself when it
replaces zmod_afclite). With the panel off, only lane_data is synced.

## Install

Add to `mod_data/user.moonraker.conf`:

```ini
[update_manager zmod_afcplus]
type: git_repo
channel: stable
path: /root/printer_data/config/mod_data/plugins/zmod_afcplus
origin: https://github.com/DIEHARDave/zmod_afcplus.git
is_system_service: False
primary_branch: main
```

Then run `ENABLE_PLUGIN name=zmod_afcplus` in the console and reboot the
printer. A Klipper RESTART does not load new plugin code.

`channel: stable` means updates show up only for tagged releases.

## Options

All options are on by default. To change one, add the section to
`mod_data/user.cfg`:

```ini
[zmod_afcplus]
lane_data: True                  # keep HelixScreen's lane_data on Zmod's slots
toolchanger_idle_map: True       # Creator 5: own toolhead between prints
spool_colors: True               # lanes show the Spoolman spool's color
```

## Notes

- The built-in panel has no hooks, so this plugin wraps its lane status at
  startup. If a Zmod update changes the panel's internals, the plugin logs a
  warning and leaves the panel as it is; lane_data keeps working.
- If zmod_afclite is still loaded (Zmod before 1.7.5), this plugin stays idle
  and logs a warning. zmod_afclite does all of this itself.

## Uninstall

`DISABLE_PLUGIN name=zmod_afcplus`, then reboot.
