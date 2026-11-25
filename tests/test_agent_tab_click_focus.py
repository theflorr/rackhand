import pytest
from textual.app import App
from textual.widgets import Select

from backend import RealBackend
from models import ProviderConfig, Settings
from ui.screens.workspace import WorkspaceScreen
from ui.styles import APP_CSS
from ui.suggestions import PromptInput


class _MockApp(App):
    CSS = APP_CSS

    def __init__(self) -> None:
        super().__init__()
        self.sessions = []
        self.active = None
        self.active_id = None
        self.provider = ProviderConfig(key="test", name="Test", model="test-model")
        self.settings = Settings()
        self.backend = RealBackend()

    def on_mount(self) -> None:
        self.push_screen(WorkspaceScreen())


@pytest.mark.asyncio
async def test_clicking_agent_tab_space_focuses_prompt():
    app = _MockApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        prompt = screen.query_one("#prompt", PromptInput)
        agent_select = screen.query_one("#agent-select", Select)

        # Move focus away from prompt first
        agent_select.focus()
        await pilot.pause()
        assert app.focused is agent_select

        # Clicking empty space in #chat should focus #prompt
        await pilot.click("#chat")
        await pilot.pause()
        assert app.focused is prompt

        # Move focus away again, then click #composer-status
        agent_select.focus()
        await pilot.pause()
        assert app.focused is agent_select

        await pilot.click("#composer-status")
        await pilot.pause()
        assert app.focused is prompt

        # Move focus away again, then click #prompt-prefix
        agent_select.focus()
        await pilot.pause()
        assert app.focused is agent_select

        await pilot.click("#prompt-prefix")
        await pilot.pause()
        assert app.focused is prompt


@pytest.mark.asyncio
async def test_clicking_interactive_controls_does_not_steal_focus_to_prompt():
    app = _MockApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        screen = app.screen
        prompt = screen.query_one("#prompt", PromptInput)
        agent_select = screen.query_one("#agent-select", Select)

        # Clicking #agent-select opens/focuses Select, not #prompt
        await pilot.click("#agent-select")
        await pilot.pause()
        assert app.focused is not prompt
        # Close select overlay if open
        await pilot.press("escape")
        await pilot.pause()

        # Clicking #btn-model (can_focus=False) while focus is on #agent-select should not jump focus to #prompt
        agent_select.focus()
        await pilot.pause()
        await pilot.click("#btn-model")
        await pilot.pause()
        assert app.focused is not prompt
