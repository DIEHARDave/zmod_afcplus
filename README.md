# Zmod AFC Plus

> **Parts:** the printers, boards, readers and other gear that go with this project are listed at [projects.die-hard.net/gear](https://projects.die-hard.net/gear) (Amazon affiliate links).

Additions to the **AFC panel built into Zmod 1.7.5 and later** (`zmod_afc`),
for the Flashforge AD5X and Creator 5 (Pro).

Zmod 1.7.5 took over what [zmod_afclite](https://github.com/DIEHARDave/zmod_afclite)
did: the AFC panel in Mainsail and Fluidd, SET_COLOR / SET_MATERIAL /
SET_WEIGHT / SET_SPOOL_ID, the Spoolman active spool, IFS Jacker units, and the
Creator 5 load. While updating, Zmod removes zmod_afclite. This plugin brings back the
parts the built-in panel does not have. It registers no AFC objects or
commands of its own, so it does not conflict with Zmod.

## What it adds

- **lane_data for OrcaSlicer.** Zmod's slot color and material are mirrored
  into Moonraker's `lane_data` namespace, which OrcaSlicer's filament sync
  reads. Existing entries are merged, not replaced: `lane1..lane4` on the AD5X
  (HelixScreen's keys) and `T0..T3` on the Creator 5, found by their `lane`
  field. Entries are rechecked every 15 s, and an entry that another writer
  (HelixScreen, SpoolSync, FF_NFC) left on a different filament is corrected:
  - An exact spool color is kept while it is the same Zmod color (on the
    Creator 5, the same palette entry).
  - An empty slot gets material `""`; its other fields stay.
  - Spool fields (name, vendor, temperatures, `spool_id`...) are dropped when
    the color or material changes, or when the AFC panel has a different spool
    on that lane.
  - Slot reads must agree for 3 polls, because the IFS reports every slot
    empty for a few seconds after boot.
- **Creator 5 lane map between prints.** Zmod's `file.json` keeps the last
  print's tool mapping, so the panel (and HelixScreen, which reads the AFC
  map) showed lanes on the wrong tools when idle. Between prints each lane now
  maps to its own toolhead. During a print, a lane the mapping leaves out
  serves its own toolhead, unless that tool is mapped to another lane.
- **Spoolman colors on lanes.** A lane with a Spoolman spool shows the spool's
  own color while Zmod stores it as the slot color, on the AFC panel and in
  lane_data, so OrcaSlicer gets it too. That matters on the Creator 5, which
  keeps only a palette index per slot: an Ocean Blue `#C6EBEB` spool is
  stored as white `#FFFFFF`, but shown and synced as `#C6EBEB`.
- **Lane spools survive a reboot.** The built-in panel clears a lane's
  Spoolman spool as soon as the slot reads empty, and the AD5X IFS reports
  slots empty for a while after boot, so assignments were lost on every
  restart. A slot now has to read empty for 30 s before its spool is cleared.
- **Creator 5 head sensors after a load.** Zmod's load leaves only the active
  head's filament sensor on (its printing setup). After an AFC load with no
  print running, `_DISABLE_SENSOR` puts back the idle setup, so inserting
  filament into any head still opens the color dialog.
- **Migration from zmod_afclite.** Lane spools and weights saved by
  zmod_afclite (`zmod_afclite_spool_e0`.., `zmod_afclite_weight_e0`..) are
  copied once to the built-in panel's `afc_spool_lane1`.. and
  `afc_weight_lane1`.., for lanes that have none yet.

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
lane_data: True                  # mirror slots into Moonraker lane_data
toolchanger_idle_map: True       # Creator 5: own toolhead between prints
spool_colors: True               # lanes show the Spoolman spool's color
toolchanger_idle_sensors: True   # Creator 5: all head sensors on after an idle load
spool_clear_delay: 30            # seconds empty before a lane's spool is cleared
```

## Notes

- The built-in panel has no hooks, so this plugin wraps its lane status at
  startup. If a Zmod update changes the panel's internals, the plugin logs a
  warning and leaves the panel as it is; lane_data keeps working.
- If zmod_afclite is still loaded (Zmod before 1.7.5), this plugin stays idle
  and logs a warning. zmod_afclite does all of this itself.

## Uninstall

`DISABLE_PLUGIN name=zmod_afcplus`, then reboot.
