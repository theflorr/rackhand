"""Modal for selecting model reasoning effort / thinking variant."""

from __future__ import annotations

from typing import Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static
from textual.widgets.option_list import Option

from ui.styles import MenuStatic

EFFORT_LEVELS = [
    ("off", "Off / Minimal", "Standard inference with no extended reasoning tokens (fastest, lowest token cost)."),
    ("low", "Low", "Light reasoning budget for quick diagnostics, simple queries, and standard troubleshooting."),
    ("medium", "Medium (Default)", "Balanced reasoning effort for comprehensive network analysis and configuration synthesis."),
    ("high", "High", "Deep reasoning budget for complex multi-vendor routing, root-cause debugging, and migration planning."),
    ("max", "Max / Extra High", "Maximum reasoning depth for exhaustive architecture verification and critical safety reviews."),
]


class EffortPickerModal(ModalScreen[Optional[str]]):
    """Select model reasoning effort / thinking variant."""

    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("enter", "confirm", "Select", show=False),
    ]

    def __init__(self, current_effort: str = "medium") -> None:
        super().__init__()
        self._current_effort = (current_effort or "medium").lower()
        if self._current_effort == "none":
            self._current_effort = "off"
        elif self._current_effort == "xhigh":
            self._current_effort = "max"
        self._selected_effort = self._current_effort

    def compose(self) -> ComposeResult:
        with Vertical(id="effort-dialog", classes="dialog"):
            yield MenuStatic(Text("REASONING EFFORT / MODEL VARIANT", style="bold white"), classes="title")

            with Horizontal(id="effort-modal-body"):
                with Vertical(id="effort-list-col"):
                    yield OptionList(id="effort-picker-list")

                with VerticalScroll(id="effort-details-col"):
                    yield Static("", id="effort-detail-title")
                    yield Static("", id="effort-detail-desc")

            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="effort-btn-cancel")
                yield Button("Apply Effort", variant="primary", id="effort-btn-select")

    async def on_mount(self) -> None:
        lst = self.query_one("#effort-picker-list", OptionList)
        highlight_idx = 0
        for i, (key, label, desc) in enumerate(EFFORT_LEVELS):
            is_active = (key == self._current_effort)
            dot = "* " if is_active else "  "
            t = Text.assemble(
                (dot, "bold #38bdf8" if is_active else "dim"),
                (f"{label}\n", "bold white" if is_active else "white"),
                (f"   {desc[:48]}...", "dim"),
            )
            lst.add_option(Option(t, id=key))
            if is_active:
                highlight_idx = i

        lst.highlighted = highlight_idx
        self._show_details(self._current_effort)

    def _show_details(self, key: str) -> None:
        key_norm = "off" if key == "none" else ("max" if key == "xhigh" else key)
        found = next((item for item in EFFORT_LEVELS if item[0] == key_norm), EFFORT_LEVELS[2])
        self._selected_effort = found[0]

        t_title = Text.assemble(
            ("Selected Level: ", "dim"),
            (found[1], "bold #38bdf8"),
        )
        self.query_one("#effort-detail-title", Static).update(t_title)

        t_desc = Text()
        t_desc.append(f"\nConfiguration Key: {found[0]}\n\n", style="bold white")
        t_desc.append(f"{found[2]}\n\n", style="white")
        t_desc.append("Saved choice persists across application sessions and applies to all active LLM reasoning engines.", style="dim")
        self.query_one("#effort-detail-desc", Static).update(t_desc)

    @on(OptionList.OptionHighlighted, "#effort-picker-list")
    def _option_highlighted(self, ev: OptionList.OptionHighlighted) -> None:
        if ev.option_id:
            self._show_details(str(ev.option_id))

    @on(OptionList.OptionSelected, "#effort-picker-list")
    def _option_selected(self, ev: OptionList.OptionSelected) -> None:
        if ev.option_id:
            self.dismiss(str(ev.option_id))

    @on(Button.Pressed, "#effort-btn-cancel")
    def _cancel_pressed(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#effort-btn-select")
    def _select_pressed(self) -> None:
        self.dismiss(self._selected_effort)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_confirm(self) -> None:
        self.dismiss(self._selected_effort)
