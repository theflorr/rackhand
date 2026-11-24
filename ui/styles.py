from __future__ import annotations

import difflib
import re
from typing import Optional

from rich.text import Text
from textual.theme import Theme
from textual.widgets import Label, Select, Static

Static.ALLOW_SELECT = False
Label.ALLOW_SELECT = False


class MenuStatic(Static):
    """Static text widget for menus, labels, badges and chrome that should not be text-selectable."""
    ALLOW_SELECT = False


class SelectableStatic(Static):
    """Static text widget for content that the user should be able to select and copy (chat, diffs, etc.)."""
    ALLOW_SELECT = True

from models import ConsoleChunk, DeviceSession, Risk, RISK_LABEL

RISK_STYLE = {Risk.READ: "dim", Risk.CONFIG: "#e0e0e0", Risk.DANGER: "bold #f87171"}
RISK_LABEL = {Risk.READ: "READ-ONLY", Risk.CONFIG: "CHANGES CONFIG", Risk.DANGER: "DANGEROUS"}
_PILL = {
    "ok": "bold #0a0a0a on #d0d0d0",
    "warn": "bold #0a0a0a on #888888",
    "err": "bold #ffffff on #441111",
    "info": "bold #0a0a0a on #a0a0a0",
    "accent": "bold #ffffff on #333333",
    "muted": "#888888 on #1a1a1a",
}


def pill(label: str, kind: str = "muted") -> Text:
    return Text(f" {label} ", style=_PILL.get(kind, _PILL["muted"]))


def sel_value(select: Select) -> Optional[str]:
    """Select value as str, or None when blank (works across Textual versions)."""
    v = select.value
    return v if isinstance(v, str) else None


def highlight_config(text: str) -> Text:
    t = Text(text)
    t.highlight_regex(r"\"[^\"]*\"", "#aaaaaa")
    t.highlight_regex(r"\b\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?\b", "bold white")
    t.highlight_regex(r"\b(?:[0-9a-fA-F]{4}\.){2}[0-9a-fA-F]{4}\b", "white")
    t.highlight_regex(r"(?m)^\s*(?:interface|router|vlan|config|edit|ip access-list|line|hostname|boot|aaa|spanning-tree)\b.*$", "bold white")
    t.highlight_regex(r"\b(?:GigabitEthernet|TenGigabitEthernet|FastEthernet|Ethernet|Loopback|Vlan|Port-channel|mgmt0|Gi|Te|Fa|Lo)\d+(?:/\d+)*(?:\.\d+)?\b", "bold white")
    t.highlight_regex(r"\b(?:shutdown|deny|disable|down|Invalid|administratively down|notconnect|ERROR|FAIL|critical)\b", "bold #f87171")
    t.highlight_regex(r"\b(?:permit|enable|up|connected|active|NORMAL|OK|SUCCESS|trunk|full|a-full)\b", "bold white")
    t.highlight_regex(r"(?m)^\s*[!#%].*$", "dim italic")
    return t


def render_diff(before: str, after: str) -> Text:
    out = Text()
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                 "running-config", "proposed", lineterm="", n=3)
    for ln in lines:
        style = "white" if ln.startswith("+") and not ln.startswith("+++") else \
            "dim" if ln.startswith("-") and not ln.startswith("---") else \
            "bold white" if ln.startswith("@@") else "dim"
        out.append(ln + "\n", style=style)
    return out if out.plain else Text("(no differences)", style="dim")


BANNER_ART = [
    r"██████╗   █████╗   ██████╗ ██╗  ██╗██╗  ██╗ █████╗  ███╗   ██╗██████╗ ",
    r"██╔══██╗ ██╔══██╗ ██╔════╝ ██║ ██╔╝██║  ██║██╔══██╗ ████╗  ██║██╔══██╗",
    r"██████╔╝ ███████║ ██║      █████═╝ ███████║███████║ ██╔██╗ ██║██║  ██║",
    r"██╔══██╗ ██╔══██║ ██║      ██╔═██╗ ██╔══██║██╔══██║ ██║╚██╗██║██║  ██║",
    r"██║  ██║ ██║  ██║ ╚██████╗ ██║ ╚██╗██║  ██║██║  ██║ ██║ ╚████║██████╔╝",
    r"╚═╝  ╚═╝ ╚═╝  ╚═╝  ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═╝  ╚═══╝╚═════╝ ",
]


def build_logo() -> Text:
    t = Text()
    t.append("RACKHAND\n", style="bold white")
    t.append("agentic network configuration", style="dim")
    return t


def build_sidebar_banner() -> Text:
    t = Text()
    t.append("RACKHAND\n", style="bold white")
    t.append("agentic network ops", style="dim #666666")
    return t


class Logo(Static):
    def __init__(self, **kw) -> None:
        super().__init__(build_logo(), **kw)


def fmt_bytes_prompt(chunk: ConsoleChunk) -> Text:
    """Colour a console chunk by who produced it."""
    clean = chunk.text.replace("\r\n", "\n").replace("\r", "")
    if chunk.source == "agent":
        t = Text()
        for ln in clean.splitlines(True):
            t.append("  ")
            t.append_text(highlight_config(ln))
        return t
    return highlight_config(clean)


APP_CSS = """
Screen { background: #0a0a0a; color: #d0d0d0; }
ModalScreen { align: center middle; background: #000000 75%; transition: opacity 150ms in_out_cubic; }

.dialog { width: 90%; height: 95%; max-width: 130; max-height: 96%; background: #111111; border: round #333333; padding: 1 2; }
.dialog.small { width: 90%; max-width: 120; height: 95%; max-height: 96%; border: round #333333; }
.title { height: 1; margin-bottom: 1; color: #e0e0e0; text-style: bold; }
.section { color: #666666; text-style: bold; margin: 1 0 0 0; }
.buttons { height: 1; align-horizontal: right; margin-top: 1; }
.buttons Button { margin-left: 1; min-width: 6; padding: 0 1; height: 1; }
.row { height: auto; }
.col { width: 1fr; height: auto; margin-right: 1; }
.col.wide { width: 3fr; }
.col.narrow { width: 1fr; }
Label { color: #666666; }

/* compact dialog controls */
.dialog Input {
    height: 1;
    border: none;
    background: #161616;
    color: #e0e0e0;
    padding: 0 1;
    margin: 0 0 1 0;
}
.dialog Input:focus {
    background: #202020;
    color: #ffffff;
}
.dialog Select {
    height: 1;
    border: none;
    background: #161616;
    color: #e0e0e0;
    padding: 0;
    margin: 0 0 1 0;
}
.dialog Select SelectCurrent {
    height: 1;
    border: none;
    background: #161616;
    color: #e0e0e0;
    padding: 0 1;
}
.dialog Select:focus SelectCurrent {
    background: #202020;
    color: #ffffff;
}
.dialog Checkbox {
    border: none;
    background: transparent;
    padding: 0;
    height: 1;
    margin: 0 0 1 0;
    color: #888888;
}
.dialog Checkbox:focus {
    border: none;
    background: transparent;
}
.dialog Checkbox:hover {
    color: #ffffff;
}
.dialog Label {
    color: #666666;
    height: 1;
    margin: 0;
}
.dialog Collapsible {
    margin-bottom: 1;
    background: #131313;
    border: none;
    padding: 0;
}
.dialog CollapsibleTitle {
    height: 1;
    padding: 0 1;
    color: #777777;
}
.dialog CollapsibleTitle:hover {
    color: #cccccc;
    background: #1a1a1a;
}
.dialog TextArea {
    height: 4;
    margin-bottom: 1;
    background: #141414;
    border: none;
    color: #d0d0d0;
    padding: 0 1;
}
.dialog TextArea:focus {
    background: #1e1e1e;
}

/* connect dialog - override base .dialog constraints to fill the screen properly */
#cn-dialog {
    width: 94%;
    max-width: 160;
    height: 90%;
    max-height: 56;
    padding: 1 2;
}
#cn-body { height: 1fr; margin-top: 1; }
#prov-body { height: 1fr; }

/* saved devices panel (left) - fixed width, full height via 1fr */
#cn-saved {
    width: 36;
    height: 1fr;
    border: round #282828;
    padding: 0 1;
    margin-right: 2;
}
#saved-list { height: 1fr; border: none; background: #0a0a0a; padding: 0; }
.section { color: #555555; text-style: bold; margin-bottom: 1; padding: 0 0 0 1; }

/* new device button - anchored to bottom of left pane */
.cn-new-btn {
    width: 100%;
    margin: 1 0;
    height: 1;
    background: #161616;
    border: none;
    color: #666666;
}
.cn-new-btn:hover { background: #222222; color: #cccccc; }

/* right pane - outlined, fills remaining horizontal and all vertical space */
#cn-right {
    width: 1fr;
    height: 1fr;
    border: round #282828;
    padding: 0 2;
    margin-left: 2;
}

/* empty state placeholder - centred inside right pane */
#cn-placeholder {
    width: 1fr;
    height: 1fr;
    color: #333333;
    content-align: center middle;
    text-style: italic;
}

/* form - fills right pane when shown */
#cn-form { width: 1fr; height: 1fr; padding: 0; }
#prov-form { width: 1fr; height: 100%; padding-left: 2; }

#agent-dialog { width: 92; height: 32; }
#agent-modal-body { height: 1fr; }
#agent-list-col { width: 38; height: 100%; border-right: solid #1c1c1c; padding: 0 1 0 0; }
#agent-search { height: 1; margin: 0 0 1 0; border: none; background: #141414; padding: 0 1; }
#agent-picker-list { height: 1fr; border: none; background: #0a0a0a; padding: 0; }
#agent-details-col { width: 1fr; height: 100%; padding-left: 2; }
#agent-detail-title { height: auto; text-style: bold; margin-bottom: 1; }
#agent-detail-desc { height: auto; color: #d0d0d0; margin-bottom: 1; }
#agent-detail-body { height: 1fr; color: #888888; }

/* transport tabs */
#transport { height: auto; margin-bottom: 1; }
#transport ContentSwitcher { height: auto; }
#transport TabPane { height: auto; padding: 0 0 1 0; }

/* field labels */
.cn-field-label { color: #777777; margin-bottom: 0; margin-top: 1; height: 1; }

/* row & column helpers */
.cn-row { height: auto; align-vertical: middle; margin-bottom: 0; }
.cn-col { width: 1fr; height: auto; margin-right: 1; }
.cn-col:last-of-type { margin-right: 0; }
.cn-baud-row { align-vertical: bottom; }

/* serial port select */
#ser-port { width: 1fr; }

/* small action buttons beside inputs */
.cn-btn-sm { width: 14; margin-left: 1; height: 1; min-width: 10; }

/* ssh host/port columns */
#cn-ssh-host-col { width: 1fr; height: auto; margin-right: 1; }
#cn-ssh-port-col { width: 12; height: auto; }

/* status line - full width, 1 line below body */
#connect-status { height: 1; width: 100%; padding: 0 1; color: #555555; }

/* footer */
#cn-footer { height: auto; margin-top: 1; align-horizontal: right; }
.cn-btn-cancel { margin-right: 1; }

/* legacy compat for prov fetch button */
#ser-refresh, #p-fetch { width: 12; margin-left: 1; height: 1; }
#p-result { height: auto; min-height: 0; }

/* providers */
#prov-body { height: 1fr; }
#prov-list-col { width: 30; height: 100%; border-right: solid #1c1c1c; padding: 0 1 0 0; }
#prov-search { height: 1; margin: 0 0 1 0; border: none; background: #141414; padding: 0 1; }
#prov-list { height: 1fr; border: none; background: #0a0a0a; padding: 0; }
#prov-form { width: 1fr; height: 100%; padding-left: 2; }

#agent-dialog { width: 92; height: 32; }
#agent-modal-body { height: 1fr; }
#agent-list-col { width: 38; height: 100%; border-right: solid #1c1c1c; padding: 0 1 0 0; }
#agent-search { height: 1; margin: 0 0 1 0; border: none; background: #141414; padding: 0 1; }
#agent-picker-list { height: 1fr; border: none; background: #0a0a0a; padding: 0; }
#agent-details-col { width: 1fr; height: 100%; padding-left: 2; }
#agent-detail-title { height: auto; text-style: bold; margin-bottom: 1; }
#agent-detail-desc { height: auto; color: #d0d0d0; margin-bottom: 1; }
#agent-detail-body { height: 1fr; color: #888888; }

#p-head { height: auto; margin-bottom: 0; }
#p-note { height: auto; margin-bottom: 1; }
#p-detected-row { height: auto; margin-top: 1; margin-bottom: 1; padding: 0 1; background: #141414; border-left: solid #71717a; align-vertical: middle; }
#p-detected-info { width: 1fr; height: auto; }
#p-detected-title { height: 1; color: #ffffff; text-style: bold; }
#p-detected-desc { height: auto; color: #888888; }
#p-import-btn { width: auto; min-width: 16; height: 1; margin-left: 1; }
#p-name-box { height: auto; }
#p-model-row { height: auto; margin-bottom: 1; }
#p-models { width: 1fr; margin-top: 0; margin-bottom: 0; }
#p-fetch { width: 10; margin-left: 1; height: 1; }
#p-custom-model-box { height: auto; margin-top: 0; margin-bottom: 0; }
#p-custom-model-box Input { margin-bottom: 1; }
#p-result { height: auto; min-height: 1; margin-top: 1; }

/* ── workspace settings tab & modal ─────────────────────────────────────────── */
#tab-settings {
    height: 100%;
    width: 100%;
    padding: 1 3;
    background: #0a0a0a;
}

#tab-settings-scroll {
    width: 100%;
    height: 100%;
    padding-bottom: 1;
}

#settings-hdr {
    height: auto;
    width: 100%;
    margin-bottom: 1;
    padding-bottom: 1;
    border-bottom: solid #222222;
}

#settings-title {
    color: #ffffff;
    text-style: bold;
    height: 1;
}

#settings-subtitle {
    color: #666666;
    height: 1;
}

.settings-card {
    height: auto;
    width: 100%;
    background: #111111;
    border: round #262626;
    padding: 1 2;
    margin-bottom: 1;
}

.settings-card:hover {
    border: round #383838;
}

.settings-card-header {
    height: auto;
    margin-bottom: 1;
    padding-bottom: 1;
    border-bottom: solid #1c1c1c;
}

.settings-card-title {
    color: #ffffff;
    text-style: bold;
    height: 1;
}

.settings-card-desc {
    color: #777777;
    height: auto;
}

.settings-item {
    height: auto;
    margin-top: 1;
    margin-bottom: 1;
}

.settings-item:first-of-type {
    margin-top: 0;
}

.settings-field-lbl {
    color: #e2e2e2;
    text-style: bold;
    height: 1;
    margin-bottom: 0;
}

.settings-field-help {
    color: #6a6a6a;
    height: auto;
    margin-top: 0;
    margin-bottom: 0;
}

/* Checkbox item: Indents help text to align directly under checkbox text! */
.settings-checkbox-item {
    height: auto;
    margin-top: 1;
    margin-bottom: 1;
}

.settings-checkbox-item Checkbox {
    height: auto;
    padding: 0;
    margin: 0;
    background: transparent;
    border: none;
}

.settings-checkbox-item Checkbox:focus {
    text-style: bold;
}

.settings-checkbox-item .settings-field-help {
    color: #6a6a6a;
    margin-left: 4;
    margin-top: 0;
    margin-bottom: 0;
    height: auto;
}

.settings-card Select {
    margin-top: 0;
    margin-bottom: 0;
}

.settings-card Input {
    margin-top: 0;
    margin-bottom: 0;
}

.settings-row {
    height: auto;
    layout: horizontal;
    margin-top: 1;
    margin-bottom: 0;
}

.settings-col {
    width: 1fr;
    height: auto;
    margin-right: 2;
}

.settings-col:last-child {
    margin-right: 0;
}

.settings-col .settings-field-help {
    margin-top: 0;
}

.settings-btn-row {
    height: auto;
    layout: horizontal;
    margin-top: 1;
    margin-bottom: 1;
}

.settings-btn-row Button {
    margin-right: 1;
}

#settings,
#settings.small {
    width: 92%;
    max-width: 130;
    height: 95%;
    max-height: 96%;
    background: #111111;
    border: round #2e2e2e;
    padding: 1 2;
}

#settings-scroll {
    height: 1fr;
    padding-right: 1;
    margin-bottom: 1;
}

/* ── usage & limits modal ───────────────────────────────────────────────── */
#usage-dialog {
    width: 92%;
    max-width: 126;
    height: 95%;
    max-height: 96%;
    background: #111111;
    border: round #2e2e2e;
    padding: 1 2;
}

#usage-hdr {
    height: auto;
    width: 100%;
    margin-bottom: 1;
    padding-bottom: 1;
    border-bottom: solid #222222;
}

#usage-title {
    color: #ffffff;
    text-style: bold;
    height: 1;
}

#usage-subtitle {
    color: #888888;
    height: 1;
}

#usage-scroll {
    height: 1fr;
    padding-right: 1;
    margin-bottom: 1;
}

#usage-context-content {
    height: auto;
    padding-top: 1;
}

#usage-windows-content {
    height: auto;
    padding-top: 1;
}

#usage-api-content {
    height: auto;
    padding-top: 1;
}

/* ── approval modal redesign ────────────────────────────────────────────── */
#approval {
    width: 76;
    max-width: 90%;
    height: auto;
    max-height: 85%;
    background: #111111;
    border: round #2e2e2e;
    padding: 1 2;
    overflow-y: auto;
}

#ap-header {
    height: auto;
    margin-bottom: 1;
}

#ap-title {
    color: #ffffff;
    text-style: bold;
    height: auto;
}

#ap-device-row {
    color: #888888;
    height: auto;
    margin-top: 0;
    margin-bottom: 1;
}

#ap-note-box {
    background: #151515;
    border: round #242424;
    padding: 1 1;
    margin-bottom: 1;
    height: auto;
}

#ap-note-lbl {
    color: #777777;
    text-style: bold;
    height: 1;
    margin-bottom: 0;
}

#ap-note-text {
    color: #cccccc;
    height: auto;
}

#ap-rollback-box {
    background: #141111;
    border: round #3d2424;
    padding: 1 1;
    margin-bottom: 1;
    height: auto;
}

#ap-rollback-lbl {
    color: #cc7777;
    text-style: bold;
    height: 1;
    margin-bottom: 0;
}

#ap-rollback-text {
    color: #ddaaaa;
    height: auto;
}

#ap-cmds-lbl {
    color: #777777;
    text-style: bold;
    height: 1;
    margin-top: 0;
    margin-bottom: 0;
}

#ap-cmds {
    height: 7;
    background: #090909;
    border: round #262626;
    color: #e0e0e0;
    margin-bottom: 1;
}

#ap-cmds:focus {
    border: round #444444;
}

#ap-save {
    margin-top: 0;
    margin-bottom: 1;
    color: #aaaaaa;
}

.ap-buttons {
    width: 100%;
    height: 3;
    align: right middle;
    margin-top: 1;
}

#ap-reject {
    min-width: 12;
    height: 3;
    margin-right: 1;
    background: #1a1a1a;
    color: #aaaaaa;
    border: round #2e2e2e;
}

#ap-reject:hover {
    background: #282828;
    color: #ffffff;
}

#ap-apply {
    min-width: 18;
    height: 3;
    background: #202020;
    color: #ffffff;
    text-style: bold;
    border: round #383838;
}

#ap-apply:hover {
    background: #2c2c2c;
    border: round #555555;
    color: #ffffff;
}

.warn { color: #d4d4d8; margin-top: 1; }

/* ── workspace ───────────────────────────────────────────────────────────── */
#ws-body { width: 100%; height: 100%; }

/* sidebar - flush left, banner on row 1 */
#sidebar {
    width: 27;
    height: 100%;
    background: #0c0c0f;
    padding: 0 1;
}

/* draggable divider between sidebar and main */
#sidebar-divider {
    width: 1;
    height: 100%;
    background: #0a0a0a;
    color: #2a2a2a;
    content-align: center middle;
}
#sidebar-divider:hover,
#sidebar-divider.dragging {
    color: #e4e4e7;
    background: #0a0a0a;
}
#sidebar-banner {
    height: auto;
    margin: 1 0 0 0;
    padding: 0;
    border-bottom: solid #1c1c24;
}
#sidebar #sec-devices {
    margin-top: 0;
}
.sidebar-header {
    height: 1;
    margin-top: 1;
    margin-bottom: 0;
    padding: 0;
}
#sidebar .section {
    color: #71717a;
    text-style: bold;
    height: 1;
    margin-top: 1;
    margin-bottom: 0;
    padding: 0;
}
#device-list {
    height: auto;
    max-height: 6;
    background: #111116;
    border: solid #1c1c24;
    padding: 0;
    margin: 0;
}
#session-list {
    height: auto;
    max-height: 8;
    background: #111116;
    border: solid #1c1c24;
    padding: 0;
    margin: 0;
    scrollbar-size: 0 0;
}
.session-row {
    height: 1;
    width: 100%;
    layout: horizontal;
    padding: 0 1;
    background: transparent;
    transition: background 120ms in_out_cubic;
}
.session-row:hover,
.session-row.hovered {
    background: #181824;
}
.session-row.active {
    background: #1e1e2e;
    border-left: solid #e4e4e7;
}
.session-row.active:hover,
.session-row.active.hovered {
    background: #242436;
}
.session-row .session-title {
    width: 1fr;
    height: 1;
    color: #71717a;
}
.session-row.active .session-title {
    color: #ffffff;
    text-style: bold;
}
.sidebar-btn-row {
    width: 100%;
    height: auto;
    margin-top: 0;
    margin-bottom: 0;
}
.sidebar-action-btn {
    width: 100%;
    height: 1;
    margin: 1 0;
    padding: 0 1;
    background: #181822;
    color: #a1a1aa;
    border: none;
    text-align: center;
    text-style: bold;
}
.sidebar-action-btn:hover {
    background: #262636;
    color: #ffffff;
}
.sidebar-action-btn:focus {
    background: #27272a;
    color: #ffffff;
}
#sidebar Button {
    width: auto;
    height: 1;
    margin: 0;
    padding: 0 1;
    background: transparent;
    color: #71717a;
    border: none;
    transition: background 120ms in_out_cubic, color 120ms in_out_cubic;
}
#sidebar Button:hover {
    background: #181818;
    color: #d0d0d0;
}
#sidebar Button.session-del-btn {
    visibility: hidden;
    width: 3;
    min-width: 3;
    height: 1;
    margin: 0;
    padding: 0;
    background: transparent;
    color: #555555;
    border: none;
    text-align: center;
}
#sidebar .session-row.hovered Button.session-del-btn {
    visibility: visible;
}
#sidebar Button.session-del-btn:hover {
    color: #f87171;
    background: #1c1c24;
    text-style: bold;
}

/* main content area */
#main { width: 1fr; height: 100%; }

TabbedContent {
    background: #0a0a0a;
    height: 100%;
}

Tabs Tab {
    transition: background 150ms in_out_cubic, color 150ms in_out_cubic;
}

TabbedContent ContentSwitcher, #main ContentSwitcher {
    height: 1fr;
    width: 100%;
}

TabbedContent TabPane {
    height: 1fr;
    width: 100%;
}

#chat { height: 1fr; padding: 0 1; background: #0a0a0a; }
.bubble { height: auto; margin-bottom: 1; padding: 0 1; }
.bubble.user {
    border-left: thick #444444;
    padding: 0 2;
    background: #0f0f0f;
    margin-top: 1;
    margin-bottom: 1;
}
.bubble.tool-bundle {
    margin-bottom: 1;
}
ToolBundleWidget {
    width: 100%;
    height: auto;
    margin-bottom: 1;
    padding: 0 1;
    background: #111111;
    border: round #262626;
    transition: border 150ms in_out_cubic;
}
ToolBundleWidget:hover {
    border: round #383838;
}
.tool-header-row {
    width: 100%;
    height: 1;
    layout: horizontal;
    align-vertical: middle;
}
.tool-title-static {
    width: 1fr;
    height: 1;
    color: #888888;
}
.btn-tool-toggle {
    width: auto;
    min-width: 11;
    height: 1;
    min-height: 1;
    padding: 0 1;
    background: #1e1e1e;
    color: #999999;
    border: none;
}
.btn-tool-toggle:hover {
    background: #2a2a2a;
    color: #ffffff;
}
.btn-tool-toggle:focus {
    background: #333333;
    color: #ffffff;
}
.tool-compact-body {
    width: 100%;
    height: auto;
    padding: 0 0 0 1;
}
.tool-detail-body {
    width: 100%;
    height: auto;
    display: none;
    margin-top: 1;
    padding-top: 1;
    border-top: solid #222222;
}
ToolBundleWidget.-expanded .tool-detail-body {
    display: block;
}
.bubble.assistant {
    margin-bottom: 1;
}
/* ── modern prompt bar / composer ────────────────────────────────────────── */
CommandSuggestionsBox, #composer-suggestions {
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
CommandSuggestionsBox.-visible, #composer-suggestions.-visible {
    display: block;
}

#composer-container {
    height: auto;
    margin: 0 1 1 1;
}
#composer-queue-bar {
    width: 100%;
    height: auto;
    max-height: 5;
    background: #0f172a;
    border-left: outer #f59e0b;
    padding: 0 1;
    margin-bottom: 1;
    display: none;
}
#composer-queue-bar.-visible {
    display: block;
}
#composer-queue-content {
    width: 1fr;
    height: auto;
    max-height: 5;
    color: #e2e8f0;
}
#composer-queue-hint {
    width: auto;
    height: 1;
    color: #f59e0b;
    padding-left: 1;
}
#composer-box {
    height: auto;
    min-height: 1;
    max-height: 10;
    background: #101318;
    border: none;
    border-left: outer #2a3545;
    padding: 0 1;
    align-vertical: top;
    transition: background 120ms in_out_cubic;
}
#composer-box:focus-within {
    border-left: outer #4a658a;
    background: #121720;
}
#prompt-prefix {
    width: 2;
    height: 1;
    color: #6a82a0;
    text-style: bold;
}
#prompt {
    width: 1fr;
    height: auto;
    min-height: 1;
    max-height: 8;
    border: none;
    background: transparent;
    padding: 0;
    color: #e0e0e0;
}
#prompt:focus {
    border: none;
    background: transparent;
}
#composer-footer {
    height: 1;
    margin-top: 1;
    padding: 0;
    align-vertical: middle;
}
#composer-status {
    width: 1fr;
    height: 1;
    padding: 0 1;
    color: #666666;
}
#composer-actions {
    width: auto;
    height: 1;
}
#composer-actions Button {
    height: 1;
    margin-left: 1;
    padding: 0 1;
    border: none;
}
#btn-model {
    background: #141722;
    color: #8da4c4;
}
#btn-model:hover {
    background: #1c2234;
    color: #c0d4f0;
}
#btn-model:focus {
    background: #1c2234;
    color: #c0d4f0;
}

#agent-select {
    width: 30;
    height: 1;
    border: none;
    background: #141722;
    color: #8da4c4;
    margin-right: 1;
}
#agent-select:focus {
    background: #1c2234;
    color: #c0d4f0;
}
#agent-select SelectCurrent {
    height: 1;
    border: none;
    background: #141722;
    color: #8da4c4;
    padding: 0 1;
}
#agent-select:focus SelectCurrent {
    background: #1c2234;
    color: #c0d4f0;
}
#agent-select SelectOverlay {
    width: 34;
    height: auto;
    max-height: 14;
}

/* ── console tab & remade header ────────────────────────────────────────── */
#tab-console {
    padding: 0;
    height: 1fr;
}

#console-header {
    height: 3;
    width: 100%;
    background: #111111;
    border: round #262626;
    margin: 0 1 1 1;
    padding: 0 1;
    align-vertical: middle;
}

#console-hdr-left {
    width: auto;
    height: 1;
    align-vertical: middle;
}

#console-status-badge {
    width: auto;
    height: 1;
    color: #e4e4e7;
    text-style: bold;
    margin-right: 1;
    padding: 0 1;
    background: #181818;
}

#console-status-badge.-busy {
    color: #d4d4d8;
}

#console-status-badge.-offline {
    color: #666666;
}

#console-device-select {
    width: auto;
    min-width: 32;
    max-width: 48;
    height: 1;
    min-height: 1;
    border: none;
    background: transparent;
    margin: 0;
    padding: 0;
}

#console-device-select SelectCurrent {
    height: 1;
    min-height: 1;
    border: none;
    background: #1a1a1a;
    color: #e4e4e7;
    padding: 0 1;
    text-style: bold;
}

#console-device-select SelectCurrent:hover {
    background: #242424;
    color: #ffffff;
}

#console-device-select:focus SelectCurrent {
    background: #282828;
    color: #ffffff;
}

#console-device-select SelectOverlay {
    width: auto;
    min-width: 38;
    max-width: 52;
    height: auto;
    max-height: 14;
    background: #141414;
    border: round #2e2e2e;
    padding: 0;
}

#console-device-select OptionList {
    background: #141414;
    color: #d4d4d8;
    padding: 0;
}

#console-device-select OptionList:focus {
    background: #141414;
}

#console-device-select OptionList > .option-list--option {
    padding: 0 1;
    color: #a1a1aa;
}

#console-device-select OptionList > .option-list--option-highlighted {
    background: #27272a;
    color: #ffffff;
    text-style: bold;
}

#console-hdr-mid {
    width: 1fr;
    height: 1;
    align: center middle;
    content-align: center middle;
}

#console-device-meta {
    width: 100%;
    height: 1;
    color: #888888;
    content-align: center middle;
}

#console-hdr-right {
    width: auto;
    height: 1;
    align-vertical: middle;
}

#console-hdr-right Button {
    min-width: 6;
    margin-left: 1;
    height: 1;
    min-height: 1;
    background: #1c1c1c;
    color: #cccccc;
    border: none;
    padding: 0 1;
}

#console-hdr-right Button:hover {
    background: #2a2a2a;
    color: #ffffff;
}

#console-hdr-right Button:focus {
    background: #333333;
    color: #ffffff;
}

#console-hdr-right Button:disabled {
    color: #444444;
    background: #141414;
}

#console {
    height: 1fr;
    background: #080808;
    border: round #202020;
    margin: 0 1 1 1;
    padding: 0 1;
}

#console-input-box {
    height: 3;
    margin: 0 1 1 1;
    background: #111111;
    border: round #262626;
    padding: 0 1;
    align-vertical: middle;
}

#console-input-box:focus-within {
    border: round #444444;
    background: #141414;
}

#console-prefix {
    width: auto;
    min-width: 2;
    height: 1;
    color: #aaaaaa;
    text-style: bold;
    margin-right: 1;
}

#console-input {
    width: 1fr;
    height: 1;
    border: none;
    background: transparent;
    padding: 0;
    color: #ffffff;
}

#console-input:focus {
    border: none;
    background: transparent;
}

/* ── home screen - edge-to-edge tiling layout ───────────────────────────── */
HomeScreen { background: #0a0a0a; }

#home {
    width: 100%;
    height: 100%;
    background: #0a0a0a;
}

/* top header bar: logo flush-left, keybinds flush-right */
#home-header {
    height: 2;
    width: 100%;
    background: #0f0f0f;
    border-bottom: tall #1e1e1e;
    padding: 0 2;
    align-vertical: middle;
}
#home-logo {
    width: 1fr;
    height: 2;
    content-align: left middle;
}
#home-nav {
    width: auto;
    height: 2;
    content-align: right middle;
}

/* cards row: fills remaining height */
#cards {
    height: 1fr;
    width: 100%;
}

/* vertical divider between cards */
#home-vdivider {
    width: 1;
    height: 100%;
    background: #1e1e1e;
}

.card {
    width: 1fr;
    height: 100%;
    background: #0d0d0d;
    padding: 2 3;
}
.card:hover { background: #0f0f0f; }
.card Static { height: auto; }

.card-actions { height: 1; margin-top: 2; }
.card-actions Button { margin-right: 1; }

/* bottom strip: status + open workspace */
#home-statusline {
    height: 1;
    width: 100%;
    background: #0d0d0d;
    padding: 0 2;
    border-top: tall #1e1e1e;
    content-align: left middle;
}
#h-open {
    width: 100%;
    height: 1;
    border-top: tall #222222;
}

/* ── buttons ─────────────────────────────────────────────────────────────── */
Button {
    height: 1;
    min-width: 6;
    padding: 0 1;
    margin: 0;
    border: none;
    background: #1a1a1a;
    color: #777777;
    transition: background 150ms in_out_cubic, color 150ms in_out_cubic;
}
Button:hover {
    background: #222222;
    color: #d0d0d0;
}
Button:focus {
    background: #2a2a2a;
    color: #ffffff;
}
Button.-primary {
    background: #242424;
    color: #d0d0d0;
    text-style: bold;
}
Button.-primary:hover {
    background: #2e2e2e;
    color: #ffffff;
}
Button.-success {
    background: #242424;
    color: #e0e0e0;
    text-style: bold;
}
Button.-success:hover {
    background: #2e2e2e;
    color: #ffffff;
}
Button.-error {
    background: #1a1414;
    color: #aa5555;
    text-style: bold;
}
Button.-error:hover {
    background: #281c1c;
    color: #cc7777;
}
Button:disabled {
    background: #111111;
    color: #2a2a2a;
}

/* ── toasts / notifications ──────────────────────────────────────────────── */
ToastRack {
    align: right bottom;
    height: auto;
    width: auto;
}
ToastHolder {
    align-horizontal: right;
    width: auto;
    height: auto;
}
Toast {
    width: auto;
    min-width: 22;
    max-width: 36;
    height: auto;
    padding: 0 1;
    margin: 0 1 1 0;
    background: #141414;
    border: round #383838;
    color: #e0e0e0;
}
Toast:hover {
    background: #1a1a1a;
    border: round #666666;
}
Toast .toast--title {
    display: none;
}
Toast.-information {
    border: round #4a4a4a;
}
Toast.-warning {
    border: round #cc8800;
}
Toast.-error {
    border: round #cc3333;
}
"""


def _rackhand_theme():
    from textual.theme import Theme
    return Theme(
        name="rackhand",
        primary="#888888",
        secondary="#666666",
        accent="#aaaaaa",
        warning="#cc8800",
        error="#cc3333",
        success="#aaaaaa",
        foreground="#d0d0d0",
        background="#0a0a0a",
        surface="#111111",
        panel="#1a1a1a",
        dark=True,
    )


def _rackhand_console_theme():
    from rich.theme import Theme
    return Theme({
        "markdown.h1": "bold #ffffff",
        "markdown.h2": "bold #38bdf8",
        "markdown.h3": "bold #e4e4e7",
        "markdown.h4": "bold #a1a1aa",
        "markdown.h5": "bold #71717a",
        "markdown.h6": "dim #71717a",
        "markdown.block_quote": "italic #a1a1aa",
        "markdown.link": "underline #38bdf8",
        "markdown.link_url": "underline #38bdf8",
        "markdown.list": "#38bdf8",
        "markdown.item.bullet": "bold #a1a1aa",
        "markdown.code": "bold #38bdf8 on #18181b",
        "markdown.code_block": "#e4e4e7 on #111111",
    })



