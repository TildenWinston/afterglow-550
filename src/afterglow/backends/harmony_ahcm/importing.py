"""Recover only proven portable meanings and retain the complete native evidence.

An import is not a build scaffold, and unresolved controls never acquire guessed names.

Format reference: docs/harmony_ahcm/reading.md
"""
from __future__ import annotations

import base64
from collections import Counter, defaultdict
import hashlib
import re

from ... import ir_signal, project_devices
from ..contracts import ImportResult
from .ahcm import Image, InvalidImage
from .labels import Text, UNKNOWN
from .readers import EXPANSION_BUDGET, Reader, UnsupportedNative

BACKEND = "harmony-ahcm"
DEVICE_VARIABLE = re.compile(
    r"(.+)_(Power|Input|InputType|TVInput|CompAVInput|Screen)_(\d+)$")
ACTIVITY_VARIABLE = re.compile(r"CurrentActivityState(?:_\d+)*$")


def _phase(words: list[int], *, press: bool) -> tuple[list[int], int]:
    pulses = []
    for word in words:
        duration = word & 0x7FFF
        if not duration:
            raise UnsupportedNative("zero-duration IR words have no portable pulse")
        pulse = duration if word & 0x8000 else -duration
        if pulses and (pulses[-1] > 0) == (pulse > 0):
            pulses[-1] += pulse
        else:
            pulses.append(pulse)
    leadin = 0
    if pulses and pulses[0] < 0:
        if not press:
            raise UnsupportedNative("non-press leading silence has unrepresented timing")
        leadin = -pulses.pop(0)
    return pulses, leadin


def waveform(record: dict, evidence: dict, *, budget: int = EXPANSION_BUDGET) -> dict:
    recordings = record["recordings"]
    if len(recordings) not in (1, 2):
        raise UnsupportedNative("pointer-group count is not a portable recording shape")
    period = record["period_ns"]
    if not period:
        raise UnsupportedNative("carrier period is zero")
    if record["on_ns"] != period // 2:
        raise UnsupportedNative("carrier duty cycle is not represented by the portable envelope")
    converted = []
    leadins = []
    count = 0
    for recording in recordings:
        phases = []
        for index, body in enumerate(recording):
            words = body["words"] if body is not None else []
            pulses, leadin = _phase(words, press=index == 0)
            count += len(pulses)
            if count > budget:
                raise UnsupportedNative("portable-output inspector budget exceeded")
            phases.append(pulses)
            if index == 0:
                leadins.append(leadin)
        intro, held, release = phases
        if not intro:
            raise UnsupportedNative("record has no portable press sequence")
        converted.append({
            "pulses_us": intro + held + release,
            "sections": {"intro_pulses": len(intro), "repeat_pulses": len(held),
                         "release_pulses": len(release)},
        })
    evidence["leading_silence_us"] = leadins
    first, *alternate = converted
    try:
        return ir_signal.waveform(
            first["pulses_us"], carrier_hz=round(1_000_000_000 / period),
            sections=first["sections"], alternate=alternate[0] if alternate else None,
            provenance={"kind": "decoded-native-duration-record", "backend": BACKEND},
            native={BACKEND: evidence})
    except ValueError as exc:
        raise UnsupportedNative(str(exc)) from exc


class Importer:
    def __init__(self, image: Image, profile):
        self.image, self.profile = image, profile
        self.reader = Reader(image)
        self.text = Text(self.reader, profile)
        self.rejected: Counter[str] = Counter()
        self.failures: list[dict] = []
        self.notes: list[str] = []
        self.devices: list[dict] = []
        self.commands: dict[tuple[int, int], tuple[dict, str]] = {}
        self.modes: dict[int, dict] = {}
        self.mode_bindings: dict[int, list[dict]] = {}
        self.draws: dict[tuple[int, int], list[dict]] = {}
        self.states: dict[int, dict] = {}
        self.variables: dict[int, tuple[str, str]] = {}
        self.variable_devices: dict[int, str] = {}
        self.power_variables: dict[int, str] = {}
        self.input_values: dict[tuple[int, int], tuple[str, str]] = {}
        self.activities: list[dict] = []
        self.activity_variable: int | None = None
        self._portable_pulses = 0
        self.digest = hashlib.sha256(image.payload).hexdigest()
        self.hard_keys = self._hard_keys()
        self.cells = self._cells()

    def reject(self, category: str, reason: str, *, location: str = "") -> None:
        self.rejected[category] += 1
        self.failures.append({"category": category, "location": location, "reason": reason})

    def _hard_keys(self) -> dict[int, str]:
        given = self.profile.vocabulary.get("hard_key_codes") or {}
        result = {}
        for name, code in given.items():
            if (name not in self.profile.hard_keys or isinstance(code, bool)
                    or not isinstance(code, int) or not 0 <= code <= 255 or code in result):
                raise ValueError("hard_key_codes must uniquely name declared keys with u8 codes")
            result[code] = name
        return result

    def _cells(self) -> dict[int, tuple[int, int, int, int]]:
        result = {}
        for cell in self.profile.vocabulary.get("screen_key_cells") or []:
            code, bounds = cell["code"], cell["bounds"]
            if (isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255
                    or code in result or code in self.hard_keys
                    or not isinstance(bounds, list) or len(bounds) != 4
                    or any(isinstance(value, bool) or not isinstance(value, int)
                           for value in bounds) or min(bounds[:2]) < 0 or min(bounds[2:]) <= 0):
                raise ValueError("screen_key_cells must uniquely declare u8 codes and rectangles")
            result[code] = tuple(bounds)
        return result

    def read_devices(self) -> None:
        for group_index, address in enumerate(self.reader.addresses(5, 1)):
            try:
                group = self.reader.group(address)
            except (InvalidImage, UnsupportedNative) as exc:
                self.reject("unreadable_groups", str(exc), location=f"group {group_index}")
                continue
            device = {
                "schema": project_devices.SCHEMA, "id": str(group_index),
                "label": f"Device {group_index + 1}", "type": None, "mfr": "", "model": "",
                "commands": [], "signals": {}, "screen_buttons": [],
            }
            for ordinal, pointer in enumerate(group["commands"]):
                name = f"Command {ordinal + 1}"
                evidence = {"image_sha256": self.digest, "group": group_index,
                            "command": ordinal, "address": pointer}
                try:
                    record = self.reader.command(pointer)
                    evidence["record"] = record
                    signal = waveform(
                        record, evidence, budget=EXPANSION_BUDGET - self._portable_pulses)
                    count = len(signal["pulses_us"])
                    if signal.get("alternate"):
                        count += len(signal["alternate"]["pulses_us"])
                    self._portable_pulses += count
                    if any(evidence["leading_silence_us"]):
                        self.reject("native_leadin_timing",
                                    "transmitter lead-in retained only as native timing evidence",
                                    location=f"group {group_index}, command {ordinal}")
                except (InvalidImage, UnsupportedNative) as exc:
                    evidence["unresolved"] = str(exc)
                    signal = ir_signal.backend_opaque({BACKEND: evidence}, name=name)
                    self.reject("opaque_commands", str(exc),
                                location=f"group {group_index}, command {ordinal}")
                device["commands"].append([name, name, "", "", None])
                signal["name"] = name
                device["signals"][name] = signal
                self.commands[group_index, ordinal] = device, name
            self.devices.append(device)

    def read_modes(self) -> None:
        for ordinal in range(len(self.reader.addresses(6, 3))):
            try:
                mode = self.reader.mode(ordinal)
            except (InvalidImage, UnsupportedNative) as exc:
                self.reject("unreadable_modes", str(exc), location=f"mode {ordinal}")
                continue
            self.modes[ordinal] = mode
            bindings = [{"source": "hard", "page": None, **entry}
                        for entry in mode["bindings"]]
            for page in mode["pages"]:
                bindings.extend({"source": "screen", "page": page["ordinal"], **entry}
                                for entry in page["bindings"])
                try:
                    self.draws[ordinal, page["ordinal"]] = self.text.draws(page["program"])
                except (InvalidImage, UnsupportedNative) as exc:
                    self.reject("unreadable_screens", str(exc),
                                location=f"mode {ordinal}, page {page['ordinal']}")
            for binding in bindings:
                try:
                    binding["actions"] = self.reader.expand(
                        binding["opcode"], binding["operand"])
                except (InvalidImage, UnsupportedNative) as exc:
                    binding["unresolved"] = str(exc)
            self.mode_bindings[ordinal] = bindings

    def read_states(self) -> None:
        for ordinal in range(len(self.reader.state_addresses())):
            try:
                self.states[ordinal] = self.reader.state(ordinal)
            except (InvalidImage, UnsupportedNative) as exc:
                self.reject("unreadable_states", str(exc), location=f"state {ordinal}")
        try:
            names = self.reader.names()
        except (InvalidImage, UnsupportedNative) as exc:
            self.reject("unreadable_names", str(exc))
            return
        labels: dict[int, set[str]] = defaultdict(set)
        for named in names:
            if named["parent"] != 1:
                continue
            if UNKNOWN in named["name"]:
                self.reject("unreadable_names", "non-ASCII named symbol")
                continue
            match = DEVICE_VARIABLE.fullmatch(named["name"])
            if match is None:
                continue
            variable = named["index"]
            self.variables[variable] = match[1], match[2]
            state = self.states.get(variable)
            if state is None:
                continue
            groups = set()
            for transition in state["transitions"]:
                try:
                    actions = self.reader.expand(transition["opcode"], transition["operand"])
                    groups.update(item["operand"] >> 8 for item in actions
                                  if item["opcode"] == 0x7D)
                except (InvalidImage, UnsupportedNative) as exc:
                    self.reject("unsupported_state_actions", str(exc),
                                location=f"state {variable}")
            if len(groups) == 1:
                labels[next(iter(groups))].add(match[1])
        for device in self.devices:
            candidates = labels.get(int(device["id"]), set())
            if len(candidates) == 1:
                device["label"] = next(iter(candidates))
            elif len(candidates) > 1:
                self.reject("ambiguous_device_labels", "state labels disagree",
                            location=f"group {device['id']}")
            else:
                self.reject("unreadable_device_labels", "no uniquely owned native device name",
                            location=f"group {device['id']}")
        by_label: dict[str, list[str]] = defaultdict(list)
        for device in self.devices:
            by_label[device["label"]].append(device["id"])
        for variable, (label, _kind) in self.variables.items():
            if len(by_label.get(label, [])) == 1:
                self.variable_devices[variable] = by_label[label][0]
        activity = [item["index"] for item in names if item["parent"] == 1
                    and ACTIVITY_VARIABLE.fullmatch(item["name"])]
        self.activity_variable = activity[0] if len(activity) == 1 else None
        if len(activity) > 1:
            self.reject("unresolved_activity_counter", "multiple activity state variables")

    def _sends(self, binding: dict) -> list[tuple[int, int]]:
        if binding["flags"]:
            raise UnsupportedNative("flagged binding search behavior is not portable")
        if "unresolved" in binding:
            raise UnsupportedNative(binding["unresolved"])
        actions = binding["actions"]
        if any(item["opcode"] not in (0, 0x7C, 0x7D) for item in actions):
            raise UnsupportedNative("binding contains non-send behavior")
        result = []
        for item in actions:
            if item["opcode"] == 0x7D:
                key = item["operand"] >> 8, item["operand"] & 255
                if key not in self.commands:
                    raise UnsupportedNative("binding names an unimported command")
                result.append(key)
        return result

    def _label(self, mode: int, page: int | None, tag: int) -> str | None:
        cell = self.cells.get(tag)
        if cell is None or page is None:
            return None
        x, y, width, height = cell
        lines: dict[int, set[str]] = defaultdict(set)
        for draw in self.draws.get((mode, page), []):
            if x <= draw["x"] < x + width and y <= draw["y"] < y + height:
                lines[draw["y"]].add(draw["text"])
        if any(len(texts) != 1 for texts in lines.values()):
            return None
        label = " ".join(next(iter(lines[row])) for row in sorted(lines)).strip()
        return label if label and UNKNOWN not in label else None

    def _effective_hard(self, mode: int, tag: int,
                        fallback: dict | None = None) -> dict | None:
        bindings = self.mode_bindings[mode]
        base = next((entry for entry in bindings
                     if entry["source"] == "hard" and entry["tag"] == tag), fallback)
        choices = [
            next((entry for entry in bindings if entry["source"] == "screen"
                  and entry["page"] == page["ordinal"] and entry["tag"] == tag), base)
            for page in self.modes[mode]["pages"]
        ] or [base]
        signatures = set()
        for choice in choices:
            if choice is None:
                signatures.add(None)
            elif choice["flags"] or "unresolved" in choice:
                raise UnsupportedNative("working-page key has unresolved search behavior")
            else:
                signatures.add(tuple((item["opcode"], item["operand"])
                                     for item in choice["actions"]))
        if len(signatures) != 1:
            raise UnsupportedNative("page-dependent hard key cannot be a project-wide binding")
        return choices[0]

    def assign_device_controls(self) -> None:
        candidates: dict[int, list[tuple[int, int]]] = defaultdict(list)
        for mode, bindings in self.mode_bindings.items():
            groups, score = set(), 0
            for binding in bindings:
                if binding["tag"] & 0xC0 != 0x80:
                    continue
                sends = [item["operand"] >> 8 for item in binding.get("actions", [])
                         if item["opcode"] == 0x7D]
                groups.update(sends)
                score += bool(sends)
            if len(groups) == 1:
                candidates[next(iter(groups))].append((score, mode))
        self.device_modes: dict[str, int] = {}
        for device in self.devices:
            found = sorted(candidates.get(int(device["id"]), []), reverse=True)
            if not found or (len(found) > 1 and found[0][0] == found[1][0]):
                self.reject("unresolved_device_controls", "no unique rooted device command mode",
                            location=f"group {device['id']}")
                continue
            mode = found[0][1]
            self.device_modes[device["id"]] = mode
            bindings = self.mode_bindings[mode]
            projected = []
            for tag in self.hard_keys:
                try:
                    chosen = self._effective_hard(mode, tag)
                    if chosen is not None:
                        projected.append({**chosen, "project_key": self.hard_keys[tag]})
                except (InvalidImage, UnsupportedNative) as exc:
                    self.reject("unsupported_device_bindings", str(exc),
                                location=f"mode {mode}, tag 0x{tag:02X}")
            for page in self.modes[mode]["pages"]:
                for tag in self.cells:
                    chosen = next((entry for entry in bindings
                                   if entry["source"] == "screen"
                                   and entry["page"] == page["ordinal"]
                                   and entry["tag"] == tag), None)
                    projected.append({"empty": True} if chosen is None else
                                     {**chosen, "project_cell": tag})
            for binding in bindings:
                if (binding["tag"] & 0xC0 == 0x80 and binding["tag"] not in self.hard_keys
                        and binding["tag"] not in self.cells):
                    self.reject("unmapped_key_codes",
                                "550 physical key or LCD cell is not established",
                                location=f"mode {mode}, tag 0x{binding['tag']:02X}")
            proposed: dict[tuple[int, int], list[str]] = defaultdict(list)
            for binding in projected:
                if binding.get("empty"):
                    continue
                try:
                    sends = self._sends(binding)
                except (InvalidImage, UnsupportedNative) as exc:
                    binding["unrepresented"] = str(exc)
                    self.reject("unsupported_device_bindings", str(exc),
                                location=f"mode {mode}, tag 0x{binding['tag']:02X}")
                    continue
                if not sends:
                    continue
                if len(sends) != 1 or self.commands[sends[0]][0] is not device:
                    binding["unrepresented"] = "device key is a macro or addresses another device"
                    self.reject("unsupported_device_bindings", binding["unrepresented"],
                                location=f"mode {mode}, tag 0x{binding['tag']:02X}")
                    continue
                binding["command_key"] = sends[0]
                label = self._label(mode, binding["page"], binding["tag"])
                if label:
                    proposed[sends[0]].insert(0, label)
                elif binding.get("project_key"):
                    proposed[sends[0]].append(binding["project_key"])
            renames, used = {}, set()
            for ordinal, command in enumerate(device["commands"]):
                key = int(device["id"]), ordinal
                preferred = next(iter(proposed.get(key, [])), command[0])
                name, suffix = preferred, 2
                while name in used:
                    name, suffix = f"{preferred} ({suffix})", suffix + 1
                used.add(name)
                renames[command[0]] = name
                command[0] = command[1] = name
                self.commands[key] = device, name
            device["signals"] = {renames[name]: signal
                                 for name, signal in device["signals"].items()}
            for name, signal in device["signals"].items():
                signal["name"] = name
            rows = {row[0]: row for row in device["commands"]}
            for binding in projected:
                if binding.get("empty"):
                    device["screen_buttons"].append(None)
                    continue
                if "command_key" not in binding:
                    if "project_cell" in binding:
                        device["screen_buttons"].append(None)
                    continue
                _owner, name = self.commands[binding["command_key"]]
                if binding.get("project_key"):
                    key = binding["project_key"]
                    current = rows[name][4]
                    rows[name][4] = ([*current, key] if isinstance(current, list)
                                    else [current, key] if current else key)
                elif "project_cell" in binding:
                    label = self._label(mode, binding["page"], binding["tag"])
                    if label is None:
                        label = name
                        self.reject("unreadable_control_labels", "screen label cannot be read",
                                    location=f"mode {mode}, page {binding['page']}")
                    device["screen_buttons"].append([name, label])

    def _state_send(self, transition: dict) -> tuple[int, int]:
        if transition["lead"]:
            raise UnsupportedNative("state transition flags are not interpreted")
        actions = self.reader.expand(transition["opcode"], transition["operand"])
        sends = self._sends({"flags": 0, "actions": actions})
        for item in actions:
            if item["opcode"] == 0x7C and item["operand"] & 255:
                self.reject("unrepresented_timing",
                            "per-device state delay is retained only as native evidence")
        if len(sends) != 1:
            raise UnsupportedNative("state transition does not send exactly one command")
        return sends[0]

    def assign_states(self) -> None:
        devices = {device["id"]: device for device in self.devices}
        for variable, (_label, kind) in self.variables.items():
            state, device_id = self.states.get(variable), self.variable_devices.get(variable)
            if state is None or device_id is None:
                self.reject("unrepresented_device_states", "device state ownership is unresolved",
                            location=f"state {variable}")
                continue
            device = devices[device_id]
            try:
                if kind == "Power":
                    transitions = state["transitions"]
                    if state["maximum"] != 1 or len(transitions) != 2:
                        raise UnsupportedNative("power state is not a two-transition binary state")
                    by_edge = {(item["from"], item["to"]): item for item in transitions}
                    if set(by_edge) != {(0, 1), (1, 0)}:
                        raise UnsupportedNative("power transition sentinels are not interpreted")
                    on, off = self._state_send(by_edge[0, 1]), self._state_send(by_edge[1, 0])
                    if on[0] != int(device_id) or off[0] != int(device_id):
                        raise UnsupportedNative("power state addresses another device")
                    on_name, off_name = self.commands[on][1], self.commands[off][1]
                    if on_name == off_name:
                        device["power_cmd"] = on_name
                    else:
                        device["power_on_cmd"], device["power_off_cmd"] = on_name, off_name
                    self.power_variables[variable] = device_id
                elif kind == "Input":
                    transitions = state["transitions"]
                    if not transitions or any(item["from"] != -2 for item in transitions):
                        raise UnsupportedNative(
                            "input transitions are not independent direct selections")
                    choices: dict[int, tuple[int, int]] = {}
                    for transition in transitions:
                        key = self._state_send(transition)
                        value = transition["to"]
                        if (value < 0 or value > state["maximum"] or key[0] != int(device_id)
                                or value in choices):
                            raise UnsupportedNative(
                                "input choices are not unique direct device commands")
                        choices[value] = key
                    if set(choices) != set(range(state["maximum"] + 1)):
                        raise UnsupportedNative("some native input choices cannot be represented")
                    if device.get("inputs"):
                        raise UnsupportedNative(
                            "multiple native input states need separate behavior")
                    device["inputs"] = []
                    for value, key in sorted(choices.items()):
                        name = self.commands[key][1]
                        device["inputs"].append([name, name])
                        self.input_values[variable, value] = device_id, name
                else:
                    raise UnsupportedNative(f"{kind} state behavior is not represented")
            except (InvalidImage, UnsupportedNative) as exc:
                self.reject("unrepresented_device_states", str(exc), location=f"state {variable}")

    def _macro(self, actions: list[dict], *, state_writes: bool = False) -> list[list]:
        steps = []
        for item in actions:
            opcode, operand = item["opcode"], item["operand"]
            if opcode == 0x7D:
                key = operand >> 8, operand & 255
                if key not in self.commands:
                    raise UnsupportedNative("action names an unimported command")
                device, name = self.commands[key]
                steps.append(["command", device["id"], name])
            elif opcode == 0x7C:
                if operand & 255:
                    raise UnsupportedNative("per-device queue delay is not a global portable pause")
            elif state_writes and opcode >= 0x80:
                variable = opcode - 0x80
                if variable == self.activity_variable:
                    continue
                if variable in self.power_variables and operand in (0, 1):
                    continue
                choice = self.input_values.get((variable, operand))
                if choice:
                    steps.append(["input", *choice])
                else:
                    raise UnsupportedNative(f"unrepresented state write {variable} := {operand}")
            elif state_writes and opcode == 0x7E:
                continue
            elif opcode != 0:
                raise UnsupportedNative(f"unrepresented action 0x{opcode:02X}")
        return steps

    def _activity_label(self, key_set: int) -> str | None:
        candidates = set()
        for mode, bindings in self.mode_bindings.items():
            for binding in bindings:
                if any(item["opcode"] == 0x1F and item["operand"] == (0xFF00 | key_set)
                       for item in binding.get("actions", [])):
                    label = self._label(mode, binding["page"], binding["tag"])
                    if label:
                        candidates.add(label)
        return next(iter(candidates)) if len(candidates) == 1 else None

    def read_activities(self) -> None:
        variable = self.activity_variable
        state = self.states.get(variable)
        if state is None:
            self.reject("unresolved_activity_counter", "no readable named activity state")
            return
        candidates: dict[int, list[dict]] = defaultdict(list)
        for ordinal, address in enumerate(self.reader.key_sets()):
            try:
                bindings = self.reader.tagged(self.image.offset(address))
                enters = [item for item in bindings if item["tag"] == 1][:1]
                if not enters:
                    continue
                if enters[0]["flags"]:
                    raise UnsupportedNative("flagged activity enter behavior is not interpreted")
                actions = self.reader.expand(enters[0]["opcode"], enters[0]["operand"])
                values = [item["operand"] for item in actions
                          if item["opcode"] == 0x80 + variable]
                if not values or set(values) == {state["first"]}:
                    continue
                if len(values) != 1 or not 0 <= values[0] <= state["maximum"]:
                    raise UnsupportedNative(
                        "activity enter does not set one non-idle activity value")
                candidates[values[0]].append(
                    {"key_set": ordinal, "bindings": bindings, "enter": actions})
            except (InvalidImage, UnsupportedNative) as exc:
                self.reject("unreadable_activity_contexts", str(exc),
                            location=f"key set {ordinal}")
        for value, found in sorted(candidates.items()):
            if len(found) != 1:
                self.reject("ambiguous_activities",
                            "several contexts write the same activity value")
                continue
            native = found[0]
            label = self._activity_label(native["key_set"])
            if label is None:
                label = f"Activity {value + 1}"
                self.reject("unreadable_activity_labels", "activity menu label is unresolved",
                            location=f"activity value {value}")
            activity = {"id": str(value), "label": label, "type": None,
                        "control": None, "display": None, "enter": [], "leave": [],
                        "hard_macros": {}, "soft_buttons": []}
            start_supported = False
            try:
                activity["enter"] = self._macro(native["enter"], state_writes=True)
                powered = {self.power_variables[item["opcode"] - 0x80]: item["operand"]
                           for item in native["enter"]
                           if item["opcode"] - 0x80 in self.power_variables}
                if set(powered) != set(self.power_variables.values()):
                    raise UnsupportedNative(
                        "activity does not explicitly set every known power state")
                activity["power_on_devices"] = [
                    device for device, on in powered.items() if on == 1]
                start_supported = True
            except (InvalidImage, UnsupportedNative) as exc:
                activity["enter"] = []
                self.reject("unsupported_activity_start", str(exc),
                            location=f"activity value {value}")
            seen_tags = set()
            for binding in native["bindings"]:
                tag = binding["tag"]
                if tag in seen_tags:
                    continue
                seen_tags.add(tag)
                if tag == 1:
                    continue
                if tag == 5:
                    self.reject("unsupported_activity_repicks",
                                "activity repick behavior has no portable project field")
                    continue
                if tag != 2 and tag & 0xC0 != 0x80:
                    self.reject("unsupported_activity_events",
                                "non-press activity events are retained as native evidence")
                    continue
                try:
                    if binding["flags"]:
                        raise UnsupportedNative("flagged activity event behavior is not portable")
                    actions = self.reader.expand(binding["opcode"], binding["operand"])
                    macro = self._macro(actions)
                    if tag == 2:
                        activity["leave"] = macro
                    elif tag & 0xC0 == 0x80:
                        key = self.hard_keys.get(tag)
                        if key is None:
                            raise UnsupportedNative("550 activity key code is not established")
                        activity["hard_macros"][key] = macro
                except (InvalidImage, UnsupportedNative) as exc:
                    if tag == 2 or tag & 0xC0 == 0x80:
                        self.reject("unsupported_activity_bindings", str(exc),
                                    location=f"activity value {value}, tag 0x{tag:02X}")
            work = [item["operand"] for item in native["enter"] if item["opcode"] == 0x7E]
            if start_supported and work and work[-1] in self.mode_bindings:
                fallbacks = {}
                for binding in native["bindings"]:
                    try:
                        actions = self.reader.expand(binding["opcode"], binding["operand"])
                        fallbacks.setdefault(binding["tag"], {**binding, "actions": actions})
                    except (InvalidImage, UnsupportedNative):
                        continue
                for tag, name in self.hard_keys.items():
                    try:
                        chosen = self._effective_hard(work[-1], tag, fallbacks.get(tag))
                        if chosen is not None:
                            activity["hard_macros"][name] = self._macro(chosen["actions"])
                    except (InvalidImage, UnsupportedNative) as exc:
                        activity["hard_macros"].pop(name, None)
                        self.reject("unsupported_activity_bindings", str(exc),
                                    location=f"activity work mode {work[-1]}, tag 0x{tag:02X}")
                for binding in self.mode_bindings[work[-1]]:
                    if binding["tag"] & 0xC0 != 0x80:
                        continue
                    if binding["tag"] in self.hard_keys:
                        continue
                    try:
                        if binding["flags"] or "unresolved" in binding:
                            raise UnsupportedNative("unresolved working-screen binding")
                        macro = self._macro(binding["actions"])
                        label = self._label(work[-1], binding["page"], binding["tag"])
                        if binding["source"] == "screen" and binding["tag"] in self.cells:
                            if not label:
                                raise UnsupportedNative("activity screen label is unreadable")
                            activity["soft_buttons"].append({"label": label, "macro": macro})
                        elif macro:
                            raise UnsupportedNative(
                                "550 working-screen key code is not established")
                    except (InvalidImage, UnsupportedNative) as exc:
                        self.reject("unsupported_activity_bindings", str(exc),
                                    location=f"activity work mode {work[-1]}")
            self.activities.append(activity)

    def result(self, context: dict) -> ImportResult:
        for device in self.devices:
            project_devices.validate(device)
        settings = {"remote": self.profile.id}
        if context.get("out_file"):
            settings["out_file"] = str(context["out_file"])
        unsupported_slots = [slot.index for slot in self.image.slots
                             if slot.offset is not None and slot.index not in
                             (0, 1, 5, 6, 7, 9, 10, 11, 13)]
        if unsupported_slots:
            self.rejected["native_subsystem_roots"] = len(unsupported_slots)
            self.notes.append(
                "Native subsystem roots retained only as evidence: "
                + ", ".join(str(index) for index in unsupported_slots))
        if self.text.unreadable:
            self.rejected["unreadable_glyphs"] = sum(self.text.unreadable.values())
        reasons = Counter(f"{item['category']}: {item['reason']}" for item in self.failures)
        self.notes.extend(f"{count} item(s) not represented: {reason}"
                          for reason, count in sorted(reasons.items()))
        self.notes.extend(f"{count} LCD occurrence(s) not read: {reason}"
                          for reason, count in sorted(self.text.unreadable.items()))
        self.notes.append(
            "Import is read-only and partial where reported. Device types, platform preferences, "
            "unrepresented power/input timing, state-dependent UI and devices without a rooted "
            "IR group are not inferred; the complete native payload is retained as evidence, "
            "never a build template.")
        return ImportResult(
            project={"devices": self.devices, "activities": self.activities,
                     "assets": [], "settings": settings},
            preservation={
                "mode": "lossless-native-evidence", "backend": BACKEND,
                "sha256": self.digest, "size": len(self.image.payload),
                "payload_base64": base64.b64encode(self.image.payload).decode("ascii"),
                "validation": self.image.report(), "regions": self.reader.evidence(),
                "states": list(self.states.values()), "modes": list(self.modes.values()),
                "failures": self.failures,
                "glyph_failures": [
                    {"font": font, "code": code, "reason": reason}
                    for (font, code), reason in sorted(self.text.details.items())],
            },
            warnings=tuple(self.notes), rejected=dict(self.rejected),
        )


def import_project(image: Image, profile, context: dict) -> ImportResult:
    imported = Importer(image, profile)
    imported.read_devices()
    imported.read_modes()
    imported.read_states()
    imported.assign_device_controls()
    imported.assign_states()
    imported.read_activities()
    return imported.result(context)
