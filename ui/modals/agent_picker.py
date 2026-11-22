"""Agent selection modal for choosing specialized device engineering agents."""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static
from textual.widgets.option_list import Option

from agents import AGENTS, AGENT_INDEX, AgentSpec, get_agent
from ui.styles import MenuStatic


class AgentPickerModal(ModalScreen[Optional[str]]):
    """Select a specialist device agent."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("enter", "confirm", "Select", show=False),
    ]

    def __init__(self, current_agent_id: str = "auto") -> None:
        super().__init__()
        self._current_agent_id = current_agent_id
        self._selected_agent_id = current_agent_id

    def compose(self) -> ComposeResult:
        with Vertical(id="agent-dialog", classes="dialog"):
            yield MenuStatic(Text("SPECIALIST NETWORK AGENTS", style="bold white"), classes="title")

            with Horizontal(id="agent-modal-body"):
                # Left pane: agent list with search filter
                with Vertical(id="agent-list-col"):
                    yield Input(placeholder="Filter specialist agents...", id="agent-search")
                    yield OptionList(id="agent-picker-list")

                # Right pane: agent details and architecture preview
                with VerticalScroll(id="agent-details-col"):
                    yield Static("", id="agent-detail-title")
                    yield Static("", id="agent-detail-desc")
                    yield Static("", id="agent-detail-body")

            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="agent-btn-cancel")
                yield Button("Select Agent", variant="primary", id="agent-btn-select")

    async def on_mount(self) -> None:
        self._rebuild_list("")
        self._show_details(self._current_agent_id)

    def _rebuild_list(self, query: str) -> None:
        lst = self.query_one("#agent-picker-list", OptionList)
        lst.clear_options()
        q = query.strip().lower()

        highlight_idx = 0
        idx = 0
        for agent in AGENTS:
            if q and q not in agent.name.lower() and q not in agent.description.lower() and q not in agent.id.lower():
                continue

            is_active = agent.id == self._current_agent_id
            marker = "● " if is_active else "  "

            opt_text = Text.assemble(
                (marker, "bold #38bdf8" if is_active else "dim"),
                (f"{agent.badge} ", "bold #f59e0b" if "auto" in agent.id else "bold cyan"),
                (f"{agent.name}\n", "bold white"),
                (f"   {agent.description[:52]}...", "dim"),
            )
            lst.add_option(Option(opt_text, id=agent.id))
            if agent.id == self._selected_agent_id:
                highlight_idx = idx
            idx += 1

        if lst.option_count > 0:
            lst.highlighted = highlight_idx

    def _show_details(self, agent_id: str) -> None:
        agent = get_agent(agent_id) or AGENT_INDEX.get("auto")
        if not agent:
            return

        self._selected_agent_id = agent.id

        title_text = Text.assemble(
            (f"{agent.badge} ", "bold #f59e0b" if "auto" in agent.id else "bold cyan"),
            (agent.name, "bold white"),
            (f"  [{agent.vendor.upper()}]", "dim italic"),
        )
        self.query_one("#agent-detail-title", Static).update(title_text)

        desc_text = Text(f"\n{agent.description}\n", style="white")
        self.query_one("#agent-detail-desc", Static).update(desc_text)

        # Build detailed capabilities block
        preview_lines = []
        for line in agent.system_prompt.splitlines():
            line_str = line.strip()
            if line_str.startswith(("- `show", "- `get", "- `cfg", "- `diagnose", "- `monitor", "###")):
                preview_lines.append(line_str)
            if len(preview_lines) >= 18:
                break

        body_text = Text.assemble(
            ("Domain Knowledge & Capabilities:\n", "bold #8da4c4"),
            ("\n".join(preview_lines) if preview_lines else "Specialized operational heuristics loaded.", "dim"),
        )
        self.query_one("#agent-detail-body", Static).update(body_text)

    @on(Input.Changed, "#agent-search")
    def _search_changed(self, ev: Input.Changed) -> None:
        self._rebuild_list(ev.value)

    @on(OptionList.OptionHighlighted, "#agent-picker-list")
    def _option_highlighted(self, ev: OptionList.OptionHighlighted) -> None:
        if ev.option_id:
            self._show_details(str(ev.option_id))

    @on(OptionList.OptionSelected, "#agent-picker-list")
    def _option_selected(self, ev: OptionList.OptionSelected) -> None:
        if ev.option_id:
            self.dismiss(str(ev.option_id))

    @on(Button.Pressed, "#agent-btn-cancel")
    def _cancel_pressed(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#agent-btn-select")
    def _select_pressed(self) -> None:
        self.dismiss(self._selected_agent_id)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        self.dismiss(self._selected_agent_id)
