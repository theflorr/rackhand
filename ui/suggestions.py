from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from rich.console import Group
from rich.text import Text
from textual import events
from textual.widget import Widget
from textual.widgets import Input

from agents import AGENTS


@dataclass
class CommandSuggestion:
    value: str             # e.g. "/agent"
    display: str           # e.g. "/agent <name>"
    description: str       # e.g. "Switch active specialist agent"
    category: str = "Command"
    completion: str = ""   # text to insert when completed

    def get_completion(self) -> str:
        return self.completion if self.completion else (self.value + " ")


def get_suggestions(raw_text: str, app: Any = None) -> list[CommandSuggestion]:
    if not raw_text:
        return []

    val = raw_text
    trimmed = val.strip()
    matches: list[CommandSuggestion] = []

    # 1. Slash commands & subarguments
    if val.startswith("/"):
        if val.startswith("/agent"):
            return [CommandSuggestion("/agent", "/agent", "Expand specialist agent selector dropdown", completion="/agent")]

        if val.startswith(("/effort", "/variant", "/thinking")):
            parts = trimmed.split(maxsplit=1)
            prefix = parts[0].lower()
            sub = parts[1].lower() if len(parts) > 1 else ""
            effort_opts = [
                ("off", "Off / Minimal", "Standard inference with no extended reasoning tokens"),
                ("low", "Low", "Light reasoning budget for fast diagnostics"),
                ("medium", "Medium (Default)", "Balanced reasoning effort for network tasks"),
                ("high", "High", "Deep reasoning budget for complex routing & debugging"),
                ("max", "Max / Extra High", "Maximum reasoning depth for exhaustive verification"),
            ]
            matches = []
            for opt_key, opt_label, opt_desc in effort_opts:
                if not sub or opt_key.startswith(sub):
                    matches.append(CommandSuggestion(
                        value=f"{prefix} {opt_key}",
                        display=f"{prefix} {opt_key}",
                        description=f"{opt_label} - {opt_desc}",
                        completion=f"{prefix} {opt_key}",
                    ))
            if not sub:
                matches.insert(0, CommandSuggestion(
                    value=prefix,
                    display=prefix,
                    description="Open interactive reasoning effort / model variant picker",
                    completion=prefix,
                ))
            return matches

        # Top-level slash commands
        all_cmds = [
            CommandSuggestion("/plan", "/plan", "Enter plan mode (device discovery, read-only inspection, task planning)", completion="/plan"),
            CommandSuggestion("/build", "/build", "Exit plan mode to normal execution (apply changes & run tasks)", completion="/build"),
            CommandSuggestion("/compact", "/compact", "Summarize conversation checkpoint, clear history, and continue", completion="/compact"),
            CommandSuggestion("/fork", "/fork", "Clone conversation and open it in a new session", completion="/fork"),
            CommandSuggestion("/rename", "/rename <title>", "Rename the current session title", completion="/rename "),
            CommandSuggestion("/effort", "/effort [level]", "Choose model reasoning effort / thinking variant (off, low, medium, high, max)", completion="/effort"),
            CommandSuggestion("/variant", "/variant [level]", "Alias for /effort (choose model variant)", completion="/variant"),
            CommandSuggestion("/thinking", "/thinking [level]", "Alias for /effort (set reasoning tokens budget)", completion="/thinking"),
            CommandSuggestion("/agent", "/agent", "Expand specialist agent selector dropdown", completion="/agent"),
            CommandSuggestion("/usage", "/usage", "Show provider usage, rate limits, and rolling token windows", completion="/usage"),
            CommandSuggestion("/settings", "/settings", "Open settings and guardrails configuration", completion="/settings"),
            CommandSuggestion("/clear", "/clear", "Clear current session chat history", completion="/clear"),
            CommandSuggestion("/help", "/help", "Show all available commands and shortcuts", completion="/help"),
        ]
        target = trimmed.lower()
        for c in all_cmds:
            if c.value.lower().startswith(target) or target == "/":
                matches.append(c)
        return matches

    # 2. Network CLI commands
    cli_commands = [
        CommandSuggestion("show ip interface brief", "show ip interface brief", "Brief interface IP and operational status", category="Network"),
        CommandSuggestion("show running-config", "show running-config", "Current active running configuration", category="Network"),
        CommandSuggestion("show startup-config", "show startup-config", "Non-volatile NVRAM startup configuration", category="Network"),
        CommandSuggestion("show version", "show version", "Hardware model, serial, software version, uptime", category="Network"),
        CommandSuggestion("show vlan brief", "show vlan brief", "VLAN names, IDs, and port assignments", category="Network"),
        CommandSuggestion("show interfaces status", "show interfaces status", "Port status, VLAN, duplex, speed, and type", category="Network"),
        CommandSuggestion("show cdp neighbors", "show cdp neighbors", "Directly connected Cisco Discovery Protocol neighbors", category="Network"),
        CommandSuggestion("show cdp neighbors detail", "show cdp neighbors detail", "Detailed neighbor info including IP and platform", category="Network"),
        CommandSuggestion("show lldp info remote-device", "show lldp info remote-device", "Link Layer Discovery Protocol neighbors", category="Network"),
        CommandSuggestion("show ip route", "show ip route", "Current IP routing table and gateways", category="Network"),
        CommandSuggestion("show mac address-table", "show mac address-table", "MAC address forwarding database", category="Network"),
        CommandSuggestion("show ip arp", "show ip arp", "ARP cache table IP to MAC resolution", category="Network"),
        CommandSuggestion("show logging", "show logging", "System logging buffer and syslog status", category="Network"),
        CommandSuggestion("configure terminal", "configure terminal", "Enter global configuration mode", category="Network"),
        CommandSuggestion("ping ", "ping <ip/host>", "Test ICMP reachability to destination IP", category="Network", completion="ping "),
        CommandSuggestion("traceroute ", "traceroute <ip/host>", "Trace hop-by-hop path to destination IP", category="Network", completion="traceroute "),
        CommandSuggestion("write memory", "write memory", "Save running configuration to startup config", category="Network"),
    ]
    low = trimmed.lower()
    if low.startswith(("show", "conf", "ping", "trace", "write", "copy")):
        for cmd in cli_commands:
            if cmd.value.lower().startswith(low):
                matches.append(cmd)
    return matches


class CommandSuggestionsBox(Widget):
    DEFAULT_CSS = """
    CommandSuggestionsBox {
        width: 100%;
        height: auto;
        max-height: 11;
        background: #111111;
        border: round #262626;
        padding: 0 1;
        margin-bottom: 1;
        overflow-x: hidden;
        display: none;
    }
    CommandSuggestionsBox.-visible {
        display: block;
    }
    """
    can_focus = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.suggestions: list[CommandSuggestion] = []
        self.selected_index: int = 0
        self._prompt_input: Optional[Any] = None

    def set_prompt_input(self, inp: Any) -> None:
        self._prompt_input = inp

    def show_suggestions(self, suggestions: list[CommandSuggestion], selected: int = 0) -> None:
        self.suggestions = suggestions
        self.selected_index = max(0, min(selected, len(suggestions) - 1)) if suggestions else 0
        if suggestions:
            self.add_class("-visible")
        else:
            self.remove_class("-visible")
        self.refresh()

    def hide_suggestions(self) -> None:
        self.suggestions = []
        self.selected_index = 0
        self.remove_class("-visible")
        self.refresh()

    @property
    def is_active(self) -> bool:
        return self.has_class("-visible") and len(self.suggestions) > 0

    def move_selection(self, delta: int) -> None:
        if not self.suggestions:
            return
        self.selected_index = (self.selected_index + delta) % len(self.suggestions)
        self.refresh()

    def get_selected(self) -> Optional[CommandSuggestion]:
        if self.suggestions and 0 <= self.selected_index < len(self.suggestions):
            return self.suggestions[self.selected_index]
        return None

    def on_click(self, event: events.Click) -> None:
        if not self.suggestions or not self._prompt_input:
            return
        # Calculate which row was clicked (accounting for top border/header)
        y = event.y
        # y=0 is header, y=1..6 are items
        item_y = y - 1
        if 0 <= item_y < len(self.suggestions):
            max_visible = 6
            start_idx = 0
            if len(self.suggestions) > max_visible:
                start_idx = max(0, min(self.selected_index - max_visible // 2, len(self.suggestions) - max_visible))
            clicked_idx = start_idx + item_y
            if 0 <= clicked_idx < len(self.suggestions):
                self.selected_index = clicked_idx
                sel = self.suggestions[clicked_idx]
                self._prompt_input.apply_completion(sel)

    def render(self) -> Group:
        if not self.suggestions:
            return Group(Text(""))

        lines = []
        cat = self.suggestions[0].category.lower() if self.suggestions else "commands"
        cat_labels = {
            "agent": "agents",
            "command": "commands",
            "network": "cli",
        }
        title_text = cat_labels.get(cat, cat)

        # Header: clean, lowercase, dim text matching .section in Rackhand
        hdr = Text(no_wrap=True, overflow="ellipsis")
        hdr.append(title_text, style="bold #777777")
        hdr.append(f"  {self.selected_index + 1}/{len(self.suggestions)}", style="dim #444444")
        lines.append(hdr)

        max_visible = 6
        total = len(self.suggestions)
        start_idx = 0
        if total > max_visible:
            start_idx = max(0, min(self.selected_index - max_visible // 2, total - max_visible))
        visible_items = self.suggestions[start_idx : start_idx + max_visible]

        box_width = self.size.width if self.size and self.size.width > 0 else 80
        avail_width = max(20, box_width - 4)
        col_w = 26
        prefix_len = 2
        sep_len = 2
        desc_max_len = max(5, avail_width - (prefix_len + col_w + sep_len))

        for i, item in enumerate(visible_items):
            actual_idx = start_idx + i
            is_sel = (actual_idx == self.selected_index)
            row = Text(no_wrap=True, overflow="ellipsis")
            bg = " on #1c1c1c" if is_sel else ""

            desc = item.description or ""
            if len(desc) > desc_max_len:
                desc = desc[:max(0, desc_max_len - 1)] + "…"

            display_str = item.display
            if len(display_str) > col_w:
                display_str = display_str[:col_w - 1] + "…"

            if is_sel:
                row.append("› ", style=f"bold white{bg}")
                row.append(f"{display_str:<{col_w}}", style=f"bold white{bg}")
                row.append(f"  {desc}", style=f"#999999{bg}")
            else:
                row.append("  ", style="default")
                row.append(f"{display_str:<{col_w}}", style="#bbbbbb")
                row.append(f"  {desc}", style="dim #555555")
            lines.append(row)

        footer = Text(no_wrap=True, overflow="ellipsis")
        footer.append("Tab", style="bold #888888")
        footer.append(" complete  ", style="dim #444444")
        footer.append("↑↓", style="bold #888888")
        footer.append(" select  ", style="dim #444444")
        footer.append("Enter", style="bold #888888")
        footer.append(" run  ", style="dim #444444")
        footer.append("Esc", style="bold #888888")
        footer.append(" close", style="dim #444444")
        lines.append(footer)

        return Group(*lines)


class PromptInput(Input):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.suggestions_box: Optional[CommandSuggestionsBox] = None

    def apply_completion(self, sel: CommandSuggestion) -> None:
        completion = sel.get_completion()
        self.value = completion
        self.cursor_position = len(self.value)
        self._update_suggestions()
        self.focus()

    def on_key(self, event: events.Key) -> None:
        if not self.suggestions_box:
            return

        box = self.suggestions_box

        if event.key == "tab":
            event.prevent_default()
            event.stop()
            if box.is_active:
                sel = box.get_selected()
                if sel:
                    self.apply_completion(sel)
                    return
            else:
                if not self.value:
                    self.value = "/"
                    self.cursor_position = 1
                self._update_suggestions()
                return

        elif event.key == "up":
            if box.is_active:
                event.prevent_default()
                event.stop()
                box.move_selection(-1)
                return

        elif event.key == "down":
            if box.is_active:
                event.prevent_default()
                event.stop()
                box.move_selection(1)
                return

        elif event.key == "escape":
            if box.is_active:
                event.prevent_default()
                event.stop()
                box.hide_suggestions()
                return

        elif event.key == "enter":
            if box.is_active:
                sel = box.get_selected()
                if sel and self.value.strip().lower() != sel.value.lower():
                    if not sel.value.endswith(" "):
                        self.value = sel.value
                box.hide_suggestions()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._update_suggestions()

    def _update_suggestions(self) -> None:
        if not self.suggestions_box:
            return
        matches = get_suggestions(self.value, self.app)
        if matches:
            self.suggestions_box.show_suggestions(matches)
        else:
            self.suggestions_box.hide_suggestions()
