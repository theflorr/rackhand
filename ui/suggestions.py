from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from rich.console import Console, Group
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input

from agents import AGENTS
from ui.clipboard import (
    PastedTextBlock,
    get_system_clipboard_image,
    get_system_clipboard_text,
    save_clipboard_image,
)


@dataclass
class CommandSuggestion:
    value: str             # e.g. "/agent"
    display: str           # e.g. "/agent <name>"
    description: str       # e.g. "Switch active specialist agent"
    category: str = "Command"
    completion: str = ""   # text to insert when completed

    def get_completion(self) -> str:
        return self.completion if self.completion else (self.value + " ")


def get_device_suggestions(mention_query: str, app: Any = None) -> list[CommandSuggestion]:
    """Return device completion suggestions for @mentions in chat."""
    devices: list[CommandSuggestion] = []
    seen_keys: set[str] = set()

    # 1. Connected device sessions
    sessions = getattr(app, "sessions", []) if app else []
    active_id = getattr(app, "active_id", None) if app else None

    for s in sessions:
        name = s.hostname or (s.profile.name if s.profile else "") or s.id
        clean_name = name.strip()
        if not clean_name:
            continue
        key = clean_name.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)

        vendor = ""
        if s.profile and s.profile.vendor:
            vendor = getattr(s.profile.vendor, "value", str(s.profile.vendor))
        host = ""
        if s.profile:
            if s.profile.ssh and s.profile.ssh.host:
                host = s.profile.ssh.host
            elif s.profile.serial and s.profile.serial.port:
                host = s.profile.serial.port
            else:
                host = s.profile.address
        desc_parts = []
        if vendor:
            desc_parts.append(vendor)
        if host:
            desc_parts.append(host)
        is_active = (s.id == active_id)
        desc_parts.append("● Active" if is_active else "● Connected")
        desc = " · ".join(desc_parts)

        q = mention_query.lower()
        if not q or q in key or (host and q in host.lower()):
            devices.append(CommandSuggestion(
                value=f"@{clean_name}",
                display=f"@{clean_name}",
                description=desc,
                category="Device",
                completion=f"@{clean_name} ",
            ))

    # 2. Saved profiles in backend inventory
    backend = getattr(app, "backend", None) if app else None
    saved_profiles: list[Any] = []
    if backend:
        if hasattr(backend, "get_profiles_sync"):
            try:
                saved_profiles = backend.get_profiles_sync()
            except Exception:
                pass
        elif hasattr(backend, "_profiles_cache"):
            saved_profiles = getattr(backend, "_profiles_cache", [])

    for p in saved_profiles:
        name = getattr(p, "name", "")
        if not name:
            continue
        clean_name = name.strip()
        key = clean_name.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)

        vendor = getattr(getattr(p, "vendor", None), "value", str(getattr(p, "vendor", "")))
        host = ""
        if hasattr(p, "ssh") and getattr(p, "ssh", None) and getattr(p.ssh, "host", None):
            host = p.ssh.host
        elif hasattr(p, "serial") and getattr(p, "serial", None) and getattr(p.serial, "port", None):
            host = p.serial.port
        elif hasattr(p, "address"):
            host = p.address
        desc_parts = []
        if vendor:
            desc_parts.append(vendor)
        if host:
            desc_parts.append(host)
        desc_parts.append("Saved")
        desc = " · ".join(desc_parts)

        q = mention_query.lower()
        if not q or q in key or (host and q in host.lower()):
            devices.append(CommandSuggestion(
                value=f"@{clean_name}",
                display=f"@{clean_name}",
                description=desc,
                category="Device",
                completion=f"@{clean_name} ",
            ))

    if not devices and not mention_query:
        devices.append(CommandSuggestion(
            value="@",
            display="@<device>",
            description="No devices connected (+ connect device or Ctrl+N)",
            category="Device",
            completion="@",
        ))

    return devices


def get_suggestions(raw_text: str, app: Any = None, cursor_pos: Optional[int] = None) -> list[CommandSuggestion]:
    if not raw_text:
        return []

    # Check for @ mention at cursor position or end of text
    text_to_check = raw_text[:cursor_pos] if cursor_pos is not None else raw_text
    at_idx = text_to_check.rfind("@")
    if at_idx != -1:
        # Verify @ is at start of text or preceded by whitespace / punctuation
        if at_idx == 0 or text_to_check[at_idx - 1].isspace() or text_to_check[at_idx - 1] in ('(', '[', '{', ':', '"', '\''):
            mention_token = text_to_check[at_idx + 1:]
            # Ensure user is currently typing the mention without spaces
            if not any(c.isspace() for c in mention_token):
                return get_device_suggestions(mention_token, app)

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
            CommandSuggestion("/new", "/new", "Start a new chat session (same as + new session)", completion="/new"),
            CommandSuggestion("/init", "/init", "Identify connected devices, verify access, and audit basic config overview", completion="/init"),
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
            CommandSuggestion("/update", "/update", "Check for updates and prompt to auto-update", completion="/update"),
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
            "device": "devices",
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
    BINDINGS = [
        *Input.BINDINGS,
        Binding("ctrl+enter", "new_line", "New Line", show=False),
        Binding("shift+enter", "new_line", "New Line", show=False),
        Binding("ctrl+j", "new_line", "New Line", show=False),
        Binding("alt+m", "send_mid_turn", "Send Mid-Turn", show=False),
    ]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.suggestions_box: Optional[CommandSuggestionsBox] = None
        self._pasted_blocks: list[PastedTextBlock] = []
        self._pasted_images: dict[str, Path] = {}
        self._image_counter: int = 0
        self._last_paste_text: str = ""
        self._last_paste_time: float = 0.0

    @property
    def _value(self) -> Text:
        """Value rendered with stylish pills for image tags and pasted text blocks."""
        t = Text(self.value, no_wrap=True, overflow="ignore", end="")
        for m in re.finditer(r"\[IMAGE #\d+\]", self.value):
            t.stylize("bold #38bdf8 on #0f2744", m.start(), m.end())
        for m in re.finditer(r"\[\d+ (?:characters|chars|lines)\]", self.value):
            t.stylize("bold #cbd5e1 on #27272a", m.start(), m.end())
        return t

    def _sync_multiline_height(self) -> None:
        """Adjust widget height dynamically based on newline count."""
        lines = self.value.split("\n")
        h = max(1, min(len(lines), 8))
        self.styles.height = h

    def action_new_line(self) -> None:
        """Insert a newline into the prompt field."""
        self._insert_token_or_text("\n")
        self._sync_multiline_height()

    def action_send_mid_turn(self) -> None:
        """Forward Alt+M to active screen."""
        if hasattr(self, "screen") and hasattr(self.screen, "action_send_mid_turn"):
            self.screen.action_send_mid_turn()
        elif hasattr(self, "app") and hasattr(self.app, "screen") and hasattr(self.app.screen, "action_send_mid_turn"):
            self.app.screen.action_send_mid_turn()

    def _move_cursor_up(self) -> None:
        lines = self.value.split("\n")
        cur = self.cursor_position
        cur_line = 0
        line_start = 0
        for i, l in enumerate(lines):
            l_len = len(l)
            if line_start <= cur <= line_start + l_len:
                cur_line = i
                break
            line_start += l_len + 1
        col = cur - line_start
        if cur_line > 0:
            prev_line_len = len(lines[cur_line - 1])
            prev_line_start = sum(len(lines[j]) + 1 for j in range(cur_line - 1))
            new_col = min(col, prev_line_len)
            self.cursor_position = prev_line_start + new_col

    def _move_cursor_down(self) -> None:
        lines = self.value.split("\n")
        cur = self.cursor_position
        cur_line = 0
        line_start = 0
        for i, l in enumerate(lines):
            l_len = len(l)
            if line_start <= cur <= line_start + l_len:
                cur_line = i
                break
            line_start += l_len + 1
        col = cur - line_start
        if cur_line < len(lines) - 1:
            next_line_len = len(lines[cur_line + 1])
            next_line_start = sum(len(lines[j]) + 1 for j in range(cur_line + 1))
            new_col = min(col, next_line_len)
            self.cursor_position = next_line_start + new_col

    def render_line(self, y: int) -> Strip:
        if not self.value:
            if y != 0:
                return Strip.blank(self.size.width, self.rich_style)
            return super().render_line(y)

        lines = self.value.split("\n")
        if y >= len(lines):
            return Strip.blank(self.size.width, self.rich_style)

        line_str = lines[y]
        console = self.app.console if hasattr(self, "app") and getattr(self, "app", None) else Console()
        console_options = self.app.console_options if hasattr(self, "app") and getattr(self, "app", None) else console.options
        max_content_width = self.scrollable_content_region.width if hasattr(self, "scrollable_content_region") else self.size.width
        max_w = max(max_content_width, len(line_str) + 10, 80)

        t = Text(line_str, no_wrap=True, overflow="ignore", end="")
        for m in re.finditer(r"\[IMAGE #\d+\]", line_str):
            t.stylize("bold #38bdf8 on #0f2744", m.start(), m.end())
        for m in re.finditer(r"\[\d+ (?:characters|chars|lines)\]", line_str):
            t.stylize("bold #cbd5e1 on #27272a", m.start(), m.end())

        line_start = sum(len(lines[i]) + 1 for i in range(y))
        line_end = line_start + len(line_str)

        if self.has_focus and self._cursor_visible:
            if line_start <= self.cursor_position <= line_end:
                cursor_style = self.get_component_rich_style("input--cursor")
                cursor = self.cursor_position - line_start
                if cursor >= len(line_str):
                    t.pad_right(1)
                t.stylize(cursor_style, cursor, cursor + 1)

        if self.has_focus and not self.selection.is_empty:
            sel_start, sel_end = sorted(self.selection)
            s = max(sel_start, line_start) - line_start
            e = min(sel_end, line_end) - line_start
            if s < e:
                selection_style = self.get_component_rich_style("input--selection")
                t.stylize_before(selection_style, s, e)

        segments = list(console.render(t, console_options.update_width(max_w)))
        strip = Strip(segments)
        scroll_x, _ = self.scroll_offset if hasattr(self, "scroll_offset") else (0, 0)
        strip = strip.crop(scroll_x, scroll_x + max_w)
        return strip.apply_style(self.rich_style)

    def _insert_token_or_text(self, text: str) -> None:
        try:
            if not self.selection.is_empty:
                self.replace(text, *self.selection)
                return
        except Exception:
            pass

        try:
            self.insert_text_at_cursor(text)
        except Exception:
            self.value += text

    def _handle_pasted_text(self, text: str) -> None:
        if not text:
            return
        # Debounce rapid duplicate paste events between terminal bracketed paste and key action
        now = time.monotonic()
        if text == self._last_paste_text and (now - self._last_paste_time) < 0.4:
            return
        self._last_paste_text = text
        self._last_paste_time = now

        lines = text.splitlines()
        # Trigger pasted text block if over 45 characters or multiline
        if len(text) > 45 or len(lines) > 2:
            count = len(text)
            tag = f"[{count} characters]"
            self._pasted_blocks.append(PastedTextBlock(tag=tag, content=text, line_count=count))
            self._insert_token_or_text(tag)
        else:
            # Short text paste
            line_str = " ".join(lines) if len(lines) == 2 else (lines[0] if lines else "")
            self._insert_token_or_text(line_str)
        self._update_suggestions()

    def _handle_pasted_image(self, img: Any) -> None:
        self._image_counter += 1
        tag = f"[IMAGE #{self._image_counter}]"
        try:
            screen_obj = self.screen
        except Exception:
            screen_obj = None
        try:
            app_obj = self.app
        except Exception:
            app_obj = None
        sess_id = getattr(screen_obj, "active_session_id", None) or getattr(app_obj, "active_session_id", None)
        saved_path = save_clipboard_image(img, session_id=sess_id, index=self._image_counter)
        self._pasted_images[tag] = saved_path
        self._insert_token_or_text(tag)
        self._update_suggestions()
        if hasattr(self, "app") and hasattr(self.app, "notify"):
            try:
                w, h = getattr(img, "size", (0, 0))
                self.app.notify(f"Image pasted as {tag} ({w}x{h})", title="Image Attached")
            except Exception:
                pass

    def _on_paste(self, event: events.Paste) -> None:
        event.prevent_default()
        event.stop()
        if getattr(event, "_handled_by_prompt", False):
            return
        event._handled_by_prompt = True
        if event.text:
            self._handle_pasted_text(event.text)
        else:
            img = get_system_clipboard_image()
            if img is not None:
                self._handle_pasted_image(img)

    def on_paste(self, event: events.Paste) -> None:
        self._on_paste(event)

    def action_paste(self) -> None:
        """Paste from system clipboard, checking for image first, then text."""
        img = get_system_clipboard_image()
        if img is not None:
            self._handle_pasted_image(img)
            return

        text = get_system_clipboard_text(getattr(self, "app", None))
        if text:
            self._handle_pasted_text(text)
            return

    def resolve_for_submission(self, text: str) -> tuple[str, str, dict[str, str]]:
        """Resolve prompt text for submission.

        Returns:
            display_text: Preserves neat tokens like [87 characters] and [IMAGE #1] for chat UI.
            expanded_text: Expands [87 characters] to full text and links [IMAGE #1] for the agent.
            attached_images: Map of image token -> saved local image path.
        """
        display_text = text
        expanded_text = text

        blocks_by_tag: dict[str, deque[str]] = {}
        for block in self._pasted_blocks:
            if block.tag not in blocks_by_tag:
                blocks_by_tag[block.tag] = deque()
            blocks_by_tag[block.tag].append(block.content)

        def _replace_block(m: re.Match) -> str:
            token = m.group(0)
            q = blocks_by_tag.get(token)
            if q and len(q) > 0:
                return q.popleft()
            return token

        expanded_text = re.sub(r"\[\d+ (?:characters|chars|lines)\]", _replace_block, expanded_text)

        attached_images: dict[str, str] = {}
        for tag, img_path in list(self._pasted_images.items()):
            if tag in display_text:
                attached_images[tag] = str(img_path)
                expanded_text = expanded_text.replace(tag, f"{tag} (attached image file: {img_path})")

        return display_text, expanded_text, attached_images

    def clear_pasted_data(self) -> None:
        self._pasted_blocks.clear()
        self._pasted_images.clear()
        self._image_counter = 0
        self._last_paste_text = ""
        self._last_paste_time = 0.0

    def apply_completion(self, sel: CommandSuggestion) -> None:
        completion = sel.get_completion()
        box = self.suggestions_box
        if sel.category.lower() == "device" or completion.startswith("@"):
            cur_pos = self.cursor_position
            text_before = self.value[:cur_pos]
            at_idx = text_before.rfind("@")
            if at_idx != -1:
                prefix = self.value[:at_idx]
                suffix = self.value[cur_pos:]
                if completion.endswith(" ") and suffix.startswith(" "):
                    suffix = suffix[1:]
                self.value = prefix + completion + suffix
                self.cursor_position = len(prefix) + len(completion)
                if box:
                    box.hide_suggestions()
                self._sync_multiline_height()
                self.focus()
                return

        self.value = completion
        self.cursor_position = len(self.value)
        if box:
            box.hide_suggestions()
        self._sync_multiline_height()
        self.focus()

    def on_key(self, event: events.Key) -> None:
        if event.key in ("ctrl+enter", "shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.action_new_line()
            return

        if event.key in ("alt+m", "escape+m"):
            event.prevent_default()
            event.stop()
            self.action_send_mid_turn()
            return

        box = self.suggestions_box

        if event.key == "tab":
            if box and box.is_active:
                event.prevent_default()
                event.stop()
                sel = box.get_selected()
                if sel:
                    self.apply_completion(sel)
                    return
            else:
                event.prevent_default()
                event.stop()
                if not self.value:
                    self.value = "/"
                    self.cursor_position = 1
                self._update_suggestions()
                return

        elif event.key == "up":
            if box and box.is_active:
                event.prevent_default()
                event.stop()
                box.move_selection(-1)
                return
            elif "\n" in self.value:
                event.prevent_default()
                event.stop()
                self._move_cursor_up()
                return

        elif event.key == "down":
            if box and box.is_active:
                event.prevent_default()
                event.stop()
                box.move_selection(1)
                return
            elif "\n" in self.value:
                event.prevent_default()
                event.stop()
                self._move_cursor_down()
                return

        elif event.key == "escape":
            if box and box.is_active:
                event.prevent_default()
                event.stop()
                box.hide_suggestions()
                return
            ws = self.screen
            if ws and getattr(ws, "_busy", False) and hasattr(ws, "interrupt_response"):
                event.prevent_default()
                event.stop()
                ws.interrupt_response()
                return

        elif event.key == "enter":
            if box and box.is_active:
                sel = box.get_selected()
                if sel and sel.category.lower() == "device":
                    event.prevent_default()
                    event.stop()
                    self.apply_completion(sel)
                    return
                elif sel and self.value.strip().lower() != sel.value.lower():
                    if not sel.value.endswith(" "):
                        self.value = sel.value
                box.hide_suggestions()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._sync_multiline_height()
        self._update_suggestions()

    def _update_suggestions(self) -> None:
        if not self.suggestions_box:
            return
        matches = get_suggestions(self.value, self.app, cursor_pos=self.cursor_position)
        if matches:
            self.suggestions_box.show_suggestions(matches)
        else:
            self.suggestions_box.hide_suggestions()
