import copy
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


LOGGER = logging.getLogger(__name__)

# OrcaSlicer reads filament per slot from Moonraker's lane_data namespace. On the
# AD5X, HelixScreen and SpoolSync already keep "lane1".."lane4" there, so the
# same keys are used and only color/material are merged into existing entries.
# On the Creator 5 HelixScreen keeps "T0".."T3"; an entry is found by its
# "lane" field, so those are updated instead of adding laneN duplicates.
# Zmod's slots are what is loaded, so an entry another writer leaves on a
# different spool (HelixScreen rewrites them from Spoolman when it starts) is
# corrected on the next recheck.
LANE_DATA_NAMESPACE = "lane_data"
LANE_DATA_POLL = 2.0
LANE_DATA_RETRY = 10.0
LANE_DATA_RECHECK = 15.0
# Polls Zmod's slots must read the same before they are written. Right after
# boot the IFS reports every slot empty until its sensors are read.
LANE_DATA_SETTLE_POLLS = 3
# Fields that describe one particular spool; dropped from an entry once its
# color, material or AFC lane spool no longer matches.
LANE_DATA_SPOOL_KEYS = (
    "spool_id", "spool_name", "name", "vendor", "vendor_name",
    "helix_spoolman_filament_id", "spoolman_vendor_id",
    "remaining_weight_g", "total_weight_g", "nozzle_temp", "bed_temp",
    "ff_nfc_uid", "zmod_nfc_uid",   # FF_NFC's, and zmod_nfc's before it
)
# Marks entries this plugin created; zmod_afclite used its own name.
LANE_DATA_OWNER_KEYS = ("zmod_afcplus", "zmod_afclite")
UNSET_MATERIALS = ("NONE", "N/A", "?", "")

# Zmod's built-in AFC panel (zmod_afc, Zmod 1.7.5+) keeps a lane's Spoolman
# spool and weight in save_variables as afc_spool_lane<N> / afc_weight_lane<N>.
# zmod_afclite kept them per lane E0.. as below; they are copied over once.
AFC_SPOOL_VARIABLE = "afc_spool_lane"
AFC_WEIGHT_VARIABLE = "afc_weight_lane"
AFCLITE_VARIABLES = (
    ("zmod_afclite_spool_e", AFC_SPOOL_VARIABLE),
    ("zmod_afclite_weight_e", AFC_WEIGHT_VARIABLE),
)

# Spoolman is reached through Moonraker's [spoolman] proxy.
SPOOLMAN_TIMEOUT = 30.0
# A spool whose color could not be fetched is tried again after this long.
SPOOLMAN_COLOR_RETRY = 60.0
# Zmod briefly reports IFS slots as empty while it starts up, so a lane must
# read empty this long before zmod_afc may clear its spool assignment.
SPOOL_CLEAR_DELAY = 30.0


def json_request(url, method, query=None, body=None, timeout=5):
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def bare_hex(value):
    """A color as 6 uppercase hex digits without '#', or "" if it is not one."""
    color = str(value or "").replace("#", "").strip().upper()[:6]
    try:
        int(color, 16)
    except ValueError:
        return ""
    return color if len(color) == 6 else ""


class ZmodSlots:
    """Zmod's slots as zmod_color reports them, the same data the built-in
    AFC panel shows."""

    def __init__(self, printer):
        self.printer = printer
        self.zmod_color = None
        self.save_variables = None
        self.print_stats = None
        # True on Zmod models with one extruder per lane (Creator 5); False on
        # models that feed one extruder from an IFS (AD5X).
        self.toolchanger = False

    def handle_ready(self):
        self.zmod_color = self.printer.lookup_object("zmod_color")
        self.save_variables = self.printer.lookup_object("save_variables", None)
        self.print_stats = self.printer.lookup_object("print_stats", None)
        status = self.zmod_color.get_status(self.printer.get_reactor().monotonic())
        self.toolchanger = int(status.get("total_tools", 1) or 1) > 1

    def variables(self):
        return getattr(self.save_variables, "allVariables", None) or {}

    def printing(self, eventtime):
        if self.print_stats is None:
            return False
        return self.print_stats.get_status(eventtime).get("state") in ("printing", "paused")

    def spool_id(self, zmod_slot):
        try:
            spool_id = int(self.variables().get(f"{AFC_SPOOL_VARIABLE}{zmod_slot}") or 0)
        except (TypeError, ValueError):
            return None
        return spool_id if spool_id > 0 else None

    def read(self, eventtime):
        status = self.zmod_color.get_status(eventtime)
        slots = {}
        for slot in status.get("slots", []):
            try:
                slots[int(slot.get("ID"))] = slot
            except (TypeError, ValueError):
                continue
        # With the stock Flashforge screen enabled ("display"), Zmod only knows
        # the AD5X's IFS slots when it reports filament_known; the Creator 5
        # reads its toolhead sensors either way.
        presence_known = self.toolchanger or not status.get("display")
        if "filament_known" in status:
            presence_known = self.toolchanger or bool(status["filament_known"])
        palette = [c for c in (bare_hex(entry) for entry in status.get("palette") or []) if c]
        return {
            "slot_count": max(1, int(status.get("color_limit", 4) or 4)),
            "slots": slots,
            "presence_known": presence_known,
            "palette": palette,
        }

    def snap(self, color, palette):
        """The palette entry Zmod stores for a color, picked as zmod_afc does
        (CIE76 distance); the Creator 5 keeps only a palette index. Without a
        palette (AD5X) any color is stored as it is."""
        if not palette or color in palette:
            return color
        zmod_color = self.zmod_color

        def lab(value):
            return zmod_color.rgb_to_lab(
                int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))

        target = lab(color)
        return min(palette, key=lambda entry: zmod_color.delta_e76(*target, *lab(entry)))


class LaneDataSync:
    """Mirror Zmod's slot color/material into Moonraker's lane_data namespace.

    The reactor timer only takes a snapshot of Zmod's slots; HTTP runs on a
    worker thread so Klipper never blocks on Moonraker. Entries are rechecked
    every LANE_DATA_RECHECK seconds and written only where they disagree with
    Zmod: an exact spool color from another writer (FF_NFC, SpoolSync,
    HelixScreen) stays while it is the same Zmod color, and an empty slot has
    no material. The built-in AFC panel's lane spool wins over a spool another
    writer left in the entry."""

    def __init__(self, printer, slots, url):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.slots = slots
        self.url = url.rstrip("/") + "/server/database/item"
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self._desired = None
        self._palette = []
        self._reading = None
        self._settled = 0
        self._slot_count = 0
        self._timer = None
        printer.register_event_handler("klippy:disconnect", self._handle_disconnect)

    def start(self):
        thread = threading.Thread(target=self._run, name="zmod_afcplus lane_data")
        thread.daemon = True
        thread.start()
        self._timer = self.reactor.register_timer(self._poll, self.reactor.NOW)

    def _handle_disconnect(self):
        # Klipper RESTART builds new objects in the same process; stop this
        # instance's thread so only the new one writes.
        self._stop = True
        self._wake.set()
        if self._timer is not None:
            self.reactor.unregister_timer(self._timer)
            self._timer = None

    def _poll(self, eventtime):
        try:
            snapshot = self.slots.read(eventtime)
            desired = {}
            for zmod_slot in range(1, snapshot["slot_count"] + 1):
                slot = snapshot["slots"].get(zmod_slot)
                if slot is None:
                    continue
                material = str(slot.get("Material", "")).upper()
                desired[f"lane{zmod_slot}"] = {
                    "lane": str(zmod_slot - 1),
                    "color": "#" + (bare_hex(slot.get("HEX")) or "FFFFFF"),
                    "material": None if material in UNSET_MATERIALS else material,
                    # Without presence data a slot counts as loaded.
                    "loaded": bool(slot.get("hasFilament")) or not snapshot["presence_known"],
                    "spool_id": self.slots.spool_id(zmod_slot),
                }
            palette = snapshot["palette"]
            reading = (desired, palette)
            if reading != self._reading:
                self._reading = reading
                self._settled = 1
            elif self._settled < LANE_DATA_SETTLE_POLLS:
                self._settled += 1
            if self._settled < LANE_DATA_SETTLE_POLLS:
                return eventtime + LANE_DATA_POLL
            with self._lock:
                changed = desired != self._desired or palette != self._palette
                self._desired = desired
                self._palette = palette
                self._slot_count = snapshot["slot_count"]
            if changed:
                self._wake.set()
        except Exception:
            LOGGER.exception("Zmod AFC Plus: unable to read slots for lane_data")
        return eventtime + LANE_DATA_POLL

    def _run(self):
        while not self._stop:
            # Woken by a change in Zmod's slots, else a recheck for entries
            # another writer changed.
            self._wake.wait(LANE_DATA_RECHECK)
            self._wake.clear()
            if self._stop:
                break
            with self._lock:
                desired = copy.deepcopy(self._desired)
                palette = list(self._palette)
                slot_count = self._slot_count
            if desired is None:
                continue
            try:
                self._sync(desired, palette, slot_count)
            except Exception as exc:
                LOGGER.warning("Zmod AFC Plus: lane_data sync failed, retrying: %s", exc)
                self._wake.wait(LANE_DATA_RETRY)
                self._wake.set()

    def _request(self, method, query=None, body=None):
        return json_request(self.url, method, query, body)

    @staticmethod
    def _target_key(current, key, lane):
        """The entry holding this lane under another key (HelixScreen's T0..T3
        on the Creator 5), else our laneN key."""
        others = sorted(k for k, v in current.items()
                        if k != key and isinstance(v, dict) and str(v.get("lane")) == lane)
        return others[0] if others else key

    @staticmethod
    def _owned(entry):
        return isinstance(entry, dict) and any(entry.get(k) for k in LANE_DATA_OWNER_KEYS)

    def _same_zmod_color(self, color, zmod_color, palette):
        """True when Zmod would store `color` as `zmod_color`: equal, or the
        same entry of the Creator 5's palette, so a spool's exact color is
        kept rather than replaced by Zmod's approximation."""
        color, zmod_color = bare_hex(color), bare_hex(zmod_color)
        if color == zmod_color:
            return True
        if not palette or not color:
            return False
        return self.slots.snap(color, palette) == zmod_color

    def _lane_value(self, existing, zmod, palette):
        """The entry to store for one lane, starting from what is there."""
        value = copy.deepcopy(existing) if isinstance(existing, dict) else {
            LANE_DATA_OWNER_KEYS[0]: True,
        }
        value["lane"] = zmod["lane"]
        value.pop("zmod_afclite_color", None)  # from old zmod_afclite versions
        if not zmod["loaded"]:
            # OrcaSlicer reads a lane without material as no filament. The
            # rest stays, so a slot that only read empty for a moment keeps
            # its spool.
            value["material"] = ""
            return value

        same_spool = True
        if not value.get("helix_locked_color"):
            if not self._same_zmod_color(value.get("color"), zmod["color"], palette):
                value["color"] = zmod["color"]
                same_spool = False
        if zmod["material"] is not None and not value.get("helix_locked_material"):
            material = str(value.get("material") or "").upper()
            if material != zmod["material"]:
                value["material"] = zmod["material"]
                # An empty material is the slot reading empty, not another spool.
                same_spool = same_spool and not material
        # A spool assigned on the AFC panel is the lane's spool; one another
        # writer left in the entry is stale.
        spool_id = zmod["spool_id"]
        if spool_id is not None and value.get("spool_id") not in (None, spool_id):
            same_spool = False
        if not same_spool:
            for key in LANE_DATA_SPOOL_KEYS:
                value.pop(key, None)
        if spool_id is not None and "spool_id" not in value:
            value["spool_id"] = spool_id
        return value

    def _sync(self, desired, palette, slot_count):
        try:
            current = self._request("GET", {"namespace": LANE_DATA_NAMESPACE})
            current = current.get("result", {}).get("value") or {}
        except urllib.error.HTTPError as exc:
            if exc.code != 404:  # 404: the namespace does not exist yet
                raise
            current = {}

        for key, zmod in desired.items():
            target = self._target_key(current, key, zmod["lane"])
            if target != key and self._owned(current.get(key)):
                # A duplicate made before the lane's other entry existed.
                self._request("DELETE", {"namespace": LANE_DATA_NAMESPACE, "key": key})
            existing = current.get(target)
            value = self._lane_value(existing, zmod, palette)
            if value != existing:
                self._request("POST", body={
                    "namespace": LANE_DATA_NAMESPACE, "key": target, "value": value,
                })

        # Remove only entries this plugin created for slots that no longer
        # exist (an IFS Jacker unit was removed); never other writers' lanes.
        for key, existing in current.items():
            if not self._owned(existing):
                continue
            try:
                slot = int(key[len("lane"):]) if key.startswith("lane") else 0
            except ValueError:
                continue
            if slot > slot_count:
                self._request("DELETE", {"namespace": LANE_DATA_NAMESPACE, "key": key})


class BuiltinAFC:
    """Additions to the lanes of Zmod's built-in AFC panel (zmod_afc).

    zmod_afc has no hooks, so its lane status is wrapped once it is ready.
    Anything it does not have as expected leaves the panel untouched."""

    def __init__(self, printer, slots, moonraker_url, config):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.slots = slots
        self.proxy_url = moonraker_url.rstrip("/") + "/server/spoolman/proxy"
        self.gcode = printer.lookup_object("gcode")
        self.idle_map = config.getboolean("toolchanger_idle_map", True)
        self.spool_colors = config.getboolean("spool_colors", True)
        self.idle_sensors = config.getboolean("toolchanger_idle_sensors", True)
        self.clear_delay = config.getfloat("spool_clear_delay", SPOOL_CLEAR_DELAY, minval=0.)
        self.afc = None
        self._lane_status = None
        self._check_spools = None
        self._load = None
        # Per slot: True while it last read loaded, and since when it reads empty.
        self._loaded = {}
        self._empty_since = {}
        # Spoolman color ("RRGGBB" or None) per spool ID, and when a fetch is
        # running or last failed.
        self._colors = {}
        self._fetching = set()
        self._failed = {}
        self._warned = False
        self.gcode.register_command(
            "_AFCPLUS_LOAD", self.cmd_AFCPLUS_LOAD,
            desc="Run zmod_afc's toolhead load, then Zmod's idle sensor setup",
        )

    def attach(self):
        afc = self.printer.lookup_object("zmod_afc", None)
        if afc is None or not getattr(afc, "enabled", False):
            LOGGER.info("Zmod AFC Plus: Zmod's AFC panel is off; lane_data only")
            return
        if not callable(getattr(afc, "_lane_status", None)):
            LOGGER.warning("Zmod AFC Plus: unknown zmod_afc version; AFC panel left as it is")
            return
        self.afc = afc
        self._migrate_afclite()
        if self.idle_map or self.spool_colors:
            self._lane_status = afc._lane_status
            # zmod_afc's AFC_lane objects call self._lane_status on every query.
            afc._lane_status = self._wrapped_lane_status
        # Zmod versions with empty_since wait for an empty slot themselves.
        if (self.clear_delay and not hasattr(afc, "empty_since")
                and callable(getattr(afc, "_check_spools", None))
                and isinstance(getattr(afc, "prev_has", None), dict)
                and isinstance(getattr(afc, "slots", None), dict)):
            self._check_spools = afc._check_spools
            afc._check_spools = self._wrapped_check_spools
        if (self.slots.toolchanger and self.idle_sensors and isinstance(getattr(afc, "load", None), str)
                and self.printer.lookup_object("gcode_macro _DISABLE_SENSOR", None) is not None):
            self._load = afc.load
            afc.load = "_AFCPLUS_LOAD SLOT={slot}"

    def _migrate_afclite(self):
        """Copy zmod_afclite's lane spools and weights to the built-in panel's
        variables where it has none, then zero the old ones so a later clear
        on the panel is not undone at the next start."""
        variables = self.slots.variables()
        lines = []
        for key, value in sorted(variables.items()):
            for old, new in AFCLITE_VARIABLES:
                index = key[len(old):]
                if not key.startswith(old) or not index.isdigit():
                    continue
                try:
                    value = int(float(value))
                except (TypeError, ValueError):
                    continue
                if value <= 0:
                    continue
                zmod_slot = int(index) + 1
                target = f"{new}{zmod_slot}"
                if not variables.get(target):
                    lines.append(f"SAVE_VARIABLE VARIABLE={target} VALUE={value}")
                    if new == AFC_SPOOL_VARIABLE and isinstance(getattr(self.afc, "spools", None), dict):
                        self.afc.spools[zmod_slot] = value
                    LOGGER.info("Zmod AFC Plus: %s=%d copied to %s", key, value, target)
                lines.append(f"SAVE_VARIABLE VARIABLE={key} VALUE=0")
        if lines:
            self.gcode.run_script("\n".join(lines))

    def _wrapped_lane_status(self, eventtime, zmod_slot):
        status = self._lane_status(eventtime, zmod_slot)
        try:
            if self.idle_map and self.slots.toolchanger:
                status["map"] = self._tool_map(eventtime, zmod_slot, status.get("map"))
            if self.spool_colors:
                color = self._spool_color(zmod_slot, status.get("color"))
                if color:
                    status["color"] = color
        except Exception:
            if not self._warned:
                self._warned = True
                LOGGER.exception("Zmod AFC Plus: lane status addition failed")
        return status

    def _wrapped_check_spools(self):
        """zmod_afc clears a lane's spool the first time a loaded slot reads
        empty. Its last-seen presence (prev_has) is held at "empty" until the
        slot has read empty for clear_delay seconds, so a slot that only
        blinks empty (the IFS at boot) keeps its spool."""
        now = self.reactor.monotonic()
        prev_has = self.afc.prev_has
        for zmod_slot in list(getattr(self.afc, "spools", None) or {}):
            if (self.afc.slots.get(zmod_slot) or {}).get("hasFilament"):
                self._loaded[zmod_slot] = True
                self._empty_since.pop(zmod_slot, None)
                continue
            since = self._empty_since.setdefault(zmod_slot, now)
            if self._loaded.get(zmod_slot) and now - since >= self.clear_delay:
                self._loaded[zmod_slot] = False
                prev_has[zmod_slot] = True  # zmod_afc now clears the spool
            elif prev_has.get(zmod_slot):
                prev_has[zmod_slot] = False
                LOGGER.info("Zmod AFC Plus: lane%d reads empty, keeping its spool for %.0f s",
                            zmod_slot, self.clear_delay)
        self._check_spools()

    def _tool_map(self, eventtime, zmod_slot, mapped):
        """On a toolchanger each lane is its own toolhead between prints:
        file.json outlives the print that wrote it. During a print a lane the
        mapping leaves out still serves its own toolhead, unless that tool is
        mapped to another lane."""
        own_tool = f"T{zmod_slot - 1}"
        if not self.slots.printing(eventtime):
            return [own_tool]
        if mapped:
            return mapped
        tool_map = getattr(self.afc, "map", None) or []
        return [] if zmod_slot - 1 < len(tool_map) else [own_tool]

    def _spool_color(self, zmod_slot, color):
        """The lane spool's own Spoolman color while Zmod stores it as the
        slot's color (on the Creator 5, the same palette entry)."""
        spool_id = (getattr(self.afc, "spools", None) or {}).get(zmod_slot)
        slot_color = bare_hex(color)
        if not spool_id or not slot_color:
            return None
        if spool_id not in self._colors:
            self._fetch_color(spool_id)
            return None
        spool_color = self._colors[spool_id]
        palette = [c for c in (bare_hex(e) for e in getattr(self.afc, "palette", None) or []) if c]
        if spool_color and self.slots.snap(spool_color, palette) == slot_color:
            return f"#{spool_color}"
        return None

    def _fetch_color(self, spool_id):
        now = time.monotonic()
        if spool_id in self._fetching or now - self._failed.get(spool_id, -SPOOLMAN_COLOR_RETRY) < SPOOLMAN_COLOR_RETRY:
            return
        self._fetching.add(spool_id)

        def run():
            try:
                reply = json_request(self.proxy_url, "POST", body={
                    "request_method": "GET", "path": f"/v1/spool/{spool_id}",
                    "use_v2_response": True,
                }, timeout=SPOOLMAN_TIMEOUT)
                filament = ((reply.get("result") or {}).get("response") or {}).get("filament") or {}
                colors = [filament.get("color_hex")]
                colors += str(filament.get("multi_color_hexes") or "").split(",")
                self._colors[spool_id] = next((c for c in map(bare_hex, colors) if c), None)
                self._failed.pop(spool_id, None)
            except Exception as exc:
                self._failed[spool_id] = time.monotonic()
                LOGGER.info("Zmod AFC Plus: no color for spool %d: %s", spool_id, exc)
            finally:
                self._fetching.discard(spool_id)

        thread = threading.Thread(target=run, name="zmod_afcplus spoolman")
        thread.daemon = True
        thread.start()

    def cmd_AFCPLUS_LOAD(self, gcmd):
        """zmod_afc's configured toolhead load. Zmod's load ends on
        _ENABLE_SENSOR, which leaves only the active head's filament sensor on
        (its printing setup). When no print is running, _DISABLE_SENSOR puts
        back the idle setup (all head sensors on), as Zmod's end of print does,
        so inserting filament into any head still opens the color dialog."""
        zmod_slot = gcmd.get_int("SLOT", minval=1)
        if self._load is None:
            raise gcmd.error("_AFCPLUS_LOAD is only used by Zmod's AFC panel on the Creator 5")
        self.gcode.run_script_from_command(self._load.format(slot=zmod_slot, tool=zmod_slot - 1))
        if not self.slots.printing(self.reactor.monotonic()):
            self.gcode.run_script_from_command("_DISABLE_SENSOR")


class ZmodAFCPlus:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.slots = ZmodSlots(self.printer)
        moonraker_url = config.get("moonraker_url", "http://127.0.0.1:7125")
        self.lane_data = None
        if config.getboolean("lane_data", True):
            self.lane_data = LaneDataSync(self.printer, self.slots, moonraker_url)
        self.builtin = BuiltinAFC(self.printer, self.slots, moonraker_url, config)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        if self.printer.lookup_object("zmod_afclite", None) is not None:
            LOGGER.warning(
                "Zmod AFC Plus: zmod_afclite is still loaded and does all of "
                "this itself; remove it (Zmod 1.7.5+ has its own AFC panel)")
            return
        self.slots.handle_ready()
        if self.lane_data is not None:
            self.lane_data.start()
        # After every klippy:ready handler, so zmod_afc has read its settings.
        self.printer.get_reactor().register_callback(lambda e: self.builtin.attach())

    def get_status(self, eventtime=None):
        return {
            "lane_data": self.lane_data is not None,
            "afc_panel": self.builtin.afc is not None,
        }


def load_config(config):
    return ZmodAFCPlus(config)
