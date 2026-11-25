from __future__ import annotations

import asyncio
import re
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, Checkbox, Collapsible, Input, Label, OptionList, Select, Static, TextArea,
)
from textual.widgets.option_list import Option

from backend import Backend
from models import (
    ApiScheme, AuthStyle, AUTH_LABELS, ProviderConfig, ProviderSpec,
    PROVIDERS, PROVIDER_INDEX, PROVIDER_MODELS, SCHEME_LABELS, TestResult,
)
from subscription_detector import DetectedSubscription, detect_all_subscriptions_sync
from ui.styles import MenuStatic, pill, sel_value


AUTH_OPTIONS = [(AUTH_LABELS[a], a.value) for a in AuthStyle]
SCHEME_OPTIONS = [(SCHEME_LABELS[s], s.value) for s in ApiScheme]


def _parse_pairs(text: str, sep: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in text.splitlines():
        if sep in ln and not ln.strip().startswith("#"):
            k, v = ln.split(sep, 1)
            if k.strip():
                out[k.strip()] = v.strip()
    return out


def _fmt_pairs(d: dict[str, str], sep: str) -> str:
    return "\n".join(f"{k}{sep}{v}" for k, v in d.items())


def _clean_privacy_text(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r'[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+', '', str(text))
    cleaned = re.sub(r'\(\s*\)', '', cleaned)
    cleaned = re.sub(r'\s+-\s*$', '', cleaned)
    return re.sub(r'\s{2,}', ' ', cleaned).strip()


def _int(text: str, default: int = 0) -> int:
    try:
        return int(text.strip())
    except ValueError:
        return default


class ProvidersScreen(ModalScreen[Optional[ProviderConfig]]):
    """Select or configure an LLM provider or local subscription."""
    BINDINGS = [
        Binding("escape", "cancel", "Close"),
        Binding("ctrl+s", "save_key", "Save", show=False),
        Binding("ctrl+t", "test_key", "Test", show=False),
    ]

    def __init__(self, active_key: Optional[str] = None) -> None:
        super().__init__()
        self._configs: dict[str, ProviderConfig] = {}
        self._cur: Optional[ProviderConfig] = None
        self._active_key = active_key
        self._custom_n = 0
        self._detected: list[DetectedSubscription] = []
        self._detected_by_spec: dict[str, DetectedSubscription] = {}
        self._detected_by_id: dict[str, DetectedSubscription] = {}
        try:
            fast_subs = detect_all_subscriptions_sync()
            self._apply_detected(fast_subs)
        except Exception:
            pass

    def _apply_detected(self, subs: list[DetectedSubscription]) -> None:
        self._detected = subs
        self._detected_by_spec = {s.spec_id: s for s in subs}
        self._detected_by_id = {s.id: s for s in subs}

    # ---- layout ----
    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="dialog"):
            yield MenuStatic(Text("MODEL PROVIDERS", style="bold white"), classes="title")
            with Horizontal(id="prov-body"):
                with Vertical(id="prov-list-col"):
                    yield Input(placeholder="Filter providers...", id="prov-search")
                    yield OptionList(id="prov-list")

                with VerticalScroll(id="prov-form"):
                    yield MenuStatic("", id="p-head")
                    yield MenuStatic("", id="p-note")

                    with Horizontal(id="p-detected-row"):
                        with Vertical(id="p-detected-info"):
                            yield Static("", id="p-detected-title")
                            yield Static("", id="p-detected-desc")
                        yield Button("Use credentials", id="p-import-btn")

                    with Vertical(id="p-name-box"):
                        yield Label("Display name")
                        yield Input(placeholder="Custom endpoint name", id="p-name")

                    yield Label("Model")
                    with Horizontal(id="p-model-row", classes="row"):
                        yield Select([], prompt="Select model...", id="p-models", allow_blank=True)
                        yield Button("Query", id="p-fetch")
                    with Horizontal(id="p-custom-model-box"):
                        yield Input(placeholder="Custom model identifier", id="p-model")

                    yield Label("Base URL")
                    yield Input(placeholder="https://api.example.com/v1", id="p-url")

                    yield Label("API key / token", id="p-key-label")
                    with Horizontal(classes="row"):
                        yield Input(placeholder="Key or token", password=True, id="p-key")
                        yield Checkbox("Show", False, id="p-show")

                    with Horizontal(classes="row"):
                        with Vertical(classes="col"):
                            yield Label("API scheme")
                            yield Select(SCHEME_OPTIONS, value=ApiScheme.OPENAI_CHAT.value, allow_blank=False, id="p-scheme")
                        with Vertical(classes="col"):
                            yield Label("Auth style")
                            yield Select(AUTH_OPTIONS, value=AuthStyle.BEARER.value, allow_blank=False, id="p-auth")
                        with Vertical(classes="col", id="p-auth-name-box"):
                            yield Label("Header name")
                            yield Input(placeholder="X-My-Key", id="p-auth-name")

                    with Collapsible(title="Advanced parameters", collapsed=True):
                        yield Label("Headers (Name: value)")
                        yield TextArea("", id="p-headers")
                        yield Label("Parameters (key=value)")
                        yield TextArea("", id="p-extras")
                        with Horizontal(classes="row"):
                            with Vertical(classes="col"):
                                yield Label("Timeout (s)")
                                yield Input("120", id="p-timeout", type="integer")
                            with Vertical(classes="col"):
                                yield Label("Context window")
                                yield Input("", placeholder="auto", id="p-ctx", type="integer")
                            with Vertical(classes="col"):
                                yield Label("Max output")
                                yield Input("", placeholder="auto", id="p-maxout", type="integer")
                            with Vertical(classes="col"):
                                yield Label("Temperature")
                                yield Input("0.2", id="p-temp")
                        yield Label("CA bundle")
                        yield Input(placeholder="Path to CA bundle (optional)", id="p-ca")
                        yield Checkbox("Verify TLS", True, id="p-tls")
                        yield Checkbox("Tool calling (required for agent)", True, id="p-tools")
                        yield Checkbox("Streaming", True, id="p-stream")

                    yield MenuStatic("", id="p-result")

            with Horizontal(classes="buttons"):
                yield Button("Delete", variant="error", id="p-delete")
                yield Button("Scan system", id="p-scan")
                yield Button("Close", id="p-close")
                yield Button("Test", id="p-test")
                yield Button("Save", id="p-save")
                yield Button("Use model", variant="primary", id="p-use")

    async def on_mount(self) -> None:
        self._configs = await self.app.backend.load_provider_configs()  # type: ignore[attr-defined]
        self._rebuild_list("")
        self.run_worker(self._async_scan_subscriptions(), group="scan_subs")

        start = self._active_key
        if not start:
            if self._detected:
                start = self._detected[0].id
            elif "anthropic" in self._configs:
                start = "anthropic"
            elif self._configs:
                start = next(iter(self._configs.keys()))
            else:
                start = "anthropic"
        self._load(start)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_save_key(self) -> None:
        self._save(then_use=False)

    def action_test_key(self) -> None:
        self._test(self._read_form())

    @on(Button.Pressed, "#p-close")
    def _close(self) -> None:
        self.dismiss(None)

    # ---- scanning ----
    async def _async_scan_subscriptions(self) -> None:
        try:
            subs = await self.app.backend.detect_subscriptions()  # type: ignore[attr-defined]
            if not self.is_mounted:
                return
            self._apply_detected(subs)
            self._rebuild_list(self.query_one("#prov-search", Input).value)
        except Exception:
            pass

    @on(Button.Pressed, "#p-scan")
    def _scan_pressed(self) -> None:
        self.run_worker(self._do_scan_notify(), group="scan_subs")

    async def _do_scan_notify(self) -> None:
        status = self.query_one("#p-result", Static)
        status.update(Text("Scanning system for local credentials...", style="dim"))
        try:
            subs = await self.app.backend.detect_subscriptions()  # type: ignore[attr-defined]
            if not self.is_mounted:
                return
            self._apply_detected(subs)
            self._rebuild_list(self.query_one("#prov-search", Input).value)
            status.update(Text(f"Scan complete: {len(subs)} subscription(s) found", style="white"))
        except Exception as exc:
            status.update(Text(f"Scan failed: {exc}", style="bold #f87171"))

    # ---- list ----
    def _rebuild_list(self, query: str) -> None:
        lst = self.query_one("#prov-list", OptionList)
        lst.clear_options()
        q = query.strip().lower()
        seen_ids: set[str] = set()

        # 1. Detected subscriptions
        if self._detected:
            lst.add_option(Option(Text(" LOCAL SUBSCRIPTIONS", style="bold #555555"), disabled=True))
            for sub in self._detected:
                if q and q not in sub.name.lower() and q not in sub.service.lower():
                    continue
                seen_ids.add(sub.id)
                seen_ids.add(sub.spec_id)
                # Do not show email in left pane
                if "antigravity" in sub.id:
                    sub_txt = "active session"
                elif "claude" in sub.id:
                    sub_txt = "claude code"
                elif "opencode" in sub.id:
                    sub_txt = "opencode auth"
                elif "copilot" in sub.id:
                    sub_txt = "github session"
                elif "ollama" in sub.id or "lmstudio" in sub.id:
                    sub_txt = "local engine"
                else:
                    sub_txt = "detected session"
                lst.add_option(Option(Text.assemble(
                    (f" {sub.name}\n", "white"),
                    (f" {sub_txt}", "dim"),
                ), id=sub.id))

        # 2. Configured
        has_cfg_header = False
        for k, cfg in self._configs.items():
            if k in seen_ids:
                continue
            if q and q not in cfg.name.lower() and q not in (cfg.model or "").lower():
                continue
            if not has_cfg_header:
                lst.add_option(Option(Text(" CONFIGURED", style="bold #555555"), disabled=True))
                has_cfg_header = True
            seen_ids.add(k)
            if cfg.spec_id:
                seen_ids.add(cfg.spec_id)
            sub_txt = cfg.model or "configured"
            lst.add_option(Option(Text.assemble(
                (f" {cfg.name}\n", "white"),
                (f" {sub_txt[:26]}", "dim"),
            ), id=k))

        # 3. Catalog
        cur_grp = None
        for spec in PROVIDERS:
            if spec.id in seen_ids:
                continue
            if q and q not in spec.name.lower() and q not in spec.id:
                continue
            if spec.group != cur_grp:
                cur_grp = spec.group
                lst.add_option(Option(Text(f" {cur_grp.upper()}", style="bold #555555"), disabled=True))

            seen_ids.add(spec.id)
            sub_txt = spec.key_env or "built-in"
            lst.add_option(Option(Text.assemble(
                (f" {spec.name}\n", "white"),
                (f" {sub_txt[:26]}", "dim"),
            ), id=spec.id))

        # 4. Custom
        lst.add_option(Option(Text(" CUSTOM", style="bold #555555"), disabled=True))
        lst.add_option(Option(Text(" + Add endpoint...", style="dim"), id="__new__"))

    @on(Input.Changed, "#prov-search")
    def _search_changed(self, ev: Input.Changed) -> None:
        self._rebuild_list(ev.value)

    @on(OptionList.OptionSelected, "#prov-list")
    def _picked(self, ev: OptionList.OptionSelected) -> None:
        opt_id = str(ev.option_id)
        if opt_id == "__new__":
            self._new_custom()
        else:
            self._load(opt_id)

    # ---- form load / save ----
    def _load(self, key: str) -> None:
        q = self.query_one
        cfg: Optional[ProviderConfig] = None
        detected_match: Optional[DetectedSubscription] = None

        if key in self._detected_by_id:
            detected_match = self._detected_by_id[key]
            if detected_match.spec_id in self._configs:
                cfg = self._configs[detected_match.spec_id]
                if detected_match.api_key and not cfg.api_key:
                    cfg.api_key = detected_match.api_key
                if detected_match.auth == AuthStyle.BEARER or (isinstance(cfg.api_key, str) and cfg.api_key.startswith("sk-ant-oat")):
                    cfg.auth = AuthStyle.BEARER
            else:
                cfg = detected_match.to_provider_config()
        else:
            cfg = self._configs.get(key)
            detected_match = self._detected_by_spec.get(key)

        spec = PROVIDER_INDEX.get(cfg.spec_id if cfg and cfg.spec_id else key)

        if cfg is None:
            if detected_match:
                cfg = detected_match.to_provider_config()
            elif spec:
                cfg = ProviderConfig(key=spec.id, name=spec.name, spec_id=spec.id, scheme=spec.scheme,
                                     base_url=spec.base_url, auth=spec.auth, supports_tools=spec.tools,
                                     supports_streaming=spec.streaming)
            else:
                return

        # Ensure sk-ant-oat tokens use BEARER auth style
        if isinstance(cfg.api_key, str) and (cfg.api_key.startswith("sk-ant-oat") or (cfg.spec_id == "anthropic" and cfg.api_key.startswith("ey"))):
            cfg.auth = AuthStyle.BEARER

        self._cur = cfg
        is_custom = cfg.spec_id is None

        # Title line
        head = Text.assemble(
            (cfg.name or "Custom endpoint", "bold white"),
            "  ",
            pill(SCHEME_LABELS.get(cfg.scheme, cfg.scheme.value), "muted"),
        )
        if detected_match:
            head.append("  ")
            head.append(pill("local subscription", "primary"))
        elif cfg.key in self._configs and cfg.api_key:
            head.append("  ")
            head.append(pill("configured", "success"))

        q("#p-head", Static).update(head)
        note = (spec.note if spec else "") or (f"API key env var: {spec.key_env}" if spec and spec.key_env else "")
        q("#p-note", Static).update(Text(note, style="dim italic") if note else "")

        # Detected subscription block
        det_row = q("#p-detected-row")
        if detected_match:
            det_row.display = True
            q("#p-detected-title", Static).update(_clean_privacy_text(f"Local subscription: {detected_match.service}"))
            q("#p-detected-desc", Static).update(_clean_privacy_text(f"{detected_match.source_label} - {detected_match.account_info}"))
        else:
            det_row.display = False

        q("#p-name-box").display = is_custom
        q("#p-name", Input).value = cfg.name
        q("#p-scheme", Select).value = cfg.scheme.value
        q("#p-url", Input).value = cfg.base_url

        key_val = cfg.api_key if isinstance(cfg.api_key, str) else (cfg.api_key.get("access_token", "") if isinstance(cfg.api_key, dict) else "")
        q("#p-key", Input).value = str(key_val or "")
        q("#p-key", Input).password = True
        q("#p-show", Checkbox).value = False
        q("#p-auth", Select).value = cfg.auth.value
        q("#p-auth-name", Input).value = cfg.auth_header_name
        self._sync_auth_visibility(cfg.auth)

        # Model options
        preset_models = PROVIDER_MODELS.get(cfg.spec_id or key, [])
        options: list[tuple[str, str]] = list(preset_models)

        if detected_match and detected_match.recommended_model:
            rec = detected_match.recommended_model
            if not any(opt[1] == rec for opt in options):
                options.insert(0, (rec, rec))

        cur_model = (cfg.model or (detected_match.recommended_model if detected_match else "")).strip()
        has_match = any(opt[1] == cur_model for opt in options)
        if cur_model and not has_match:
            options.insert(0, (cur_model, cur_model))
            has_match = True

        options.append(("Custom model id...", "__custom__"))

        sel_models = q("#p-models", Select)
        sel_models.set_options(options)

        if cur_model and has_match:
            sel_models.value = cur_model
            q("#p-custom-model-box").display = False
            q("#p-model", Input).value = cur_model
        elif options and options[0][1] != "__custom__":
            sel_models.value = options[0][1]
            q("#p-custom-model-box").display = False
            q("#p-model", Input).value = options[0][1]
        else:
            sel_models.value = "__custom__"
            q("#p-custom-model-box").display = True
            q("#p-model", Input).value = cur_model

        q("#p-headers", TextArea).text = _fmt_pairs(cfg.headers, ": ")
        q("#p-extras", TextArea).text = _fmt_pairs(cfg.extras, "=")
        q("#p-timeout", Input).value = str(cfg.timeout_s)
        q("#p-ctx", Input).value = str(cfg.context_window) if cfg.context_window else ""
        q("#p-maxout", Input).value = str(cfg.max_output) if cfg.max_output else ""
        q("#p-temp", Input).value = str(cfg.temperature)
        q("#p-ca", Input).value = cfg.ca_bundle
        q("#p-tls", Checkbox).value = cfg.verify_tls
        q("#p-tools", Checkbox).value = cfg.supports_tools
        q("#p-stream", Checkbox).value = cfg.supports_streaming

        q("#p-delete", Button).display = is_custom or cfg.key in self._configs
        q("#p-result", Static).update("")
        self.run_worker(self._async_fetch_live_models(cfg.key, cfg), group="live_models", exclusive=True)

    @on(Button.Pressed, "#p-import-btn")
    def _import_detected_pressed(self) -> None:
        if not self._cur:
            return
        key = self._cur.key
        det = self._detected_by_id.get(key) or self._detected_by_spec.get(key) or self._detected_by_spec.get(self._cur.spec_id or "")
        if not det:
            return

        q = self.query_one
        q("#p-url", Input).value = det.base_url
        key_val = det.api_key if isinstance(det.api_key, str) else (det.api_key.get("access_token", "") if isinstance(det.api_key, dict) else "")
        q("#p-key", Input).value = str(key_val or "")
        q("#p-scheme", Select).value = det.scheme.value
        q("#p-auth", Select).value = det.auth.value

        if det.recommended_model:
            q("#p-model", Input).value = det.recommended_model
            sel = q("#p-models", Select)
            opts = list(sel._options)
            if not any(opt[1] == det.recommended_model for opt in opts):
                opts.insert(0, (det.recommended_model, det.recommended_model))
                sel.set_options(opts)
            sel.value = det.recommended_model
            q("#p-custom-model-box").display = False

        q("#p-result", Static).update(Text(f"Loaded credentials from {det.service}", style="white"))

    async def _async_fetch_live_models(self, key: str, cfg: ProviderConfig) -> None:
        try:
            live_models = await self.app.backend.list_models(cfg)
            if not self.is_mounted or not self._cur or self._cur.key != key or not live_models:
                return

            sel = self.query_one("#p-models", Select)
            current_choice = sel_value(sel) or cfg.model or ""

            options: list[tuple[str, str]] = [(m, m) for m in live_models]
            has_match = any(opt[1] == current_choice for opt in options)

            if current_choice and not has_match and current_choice != "__custom__":
                options.insert(0, (current_choice, current_choice))
                has_match = True

            options.append(("Custom model id...", "__custom__"))
            sel.set_options(options)

            if current_choice and has_match:
                sel.value = current_choice
            elif options and options[0][1] != "__custom__":
                sel.value = options[0][1]
                self.query_one("#p-model", Input).value = options[0][1]
        except Exception:
            pass

    def _new_custom(self) -> None:
        self._custom_n += 1
        key = f"custom:{self._custom_n}"
        cfg = ProviderConfig(key=key, name=f"Custom {self._custom_n}", spec_id=None,
                             scheme=ApiScheme.OPENAI_CHAT, base_url="http://localhost:8000/v1",
                             auth=AuthStyle.BEARER)
        self._configs[key] = cfg
        self._rebuild_list(self.query_one("#prov-search", Input).value)
        self._load(key)

    def _sync_auth_visibility(self, auth: AuthStyle) -> None:
        q = self.query_one
        show_key = auth is not AuthStyle.NONE and auth is not AuthStyle.AWS_SIGV4
        q("#p-key-label").display = show_key
        q("#p-key").display = show_key
        q("#p-show").display = show_key
        q("#p-auth-name-box").display = auth is AuthStyle.CUSTOM_HEADER

    @on(Select.Changed, "#p-auth")
    def _auth_changed(self, ev: Select.Changed) -> None:
        if isinstance(ev.value, str):
            self._sync_auth_visibility(AuthStyle(ev.value))

    @on(Checkbox.Changed, "#p-show")
    def _toggle_key(self, ev: Checkbox.Changed) -> None:
        self.query_one("#p-key", Input).password = not ev.value

    def _read_form(self) -> ProviderConfig:
        q = self.query_one
        cur = self._cur or ProviderConfig(key="tmp")
        try:
            temp = float(q("#p-temp", Input).value or 0.2)
        except ValueError:
            temp = 0.2
        api_key_str = str(q("#p-key", Input).value or "").strip()
        auth_style_val = AuthStyle(sel_value(q("#p-auth", Select)) or cur.auth.value)
        if api_key_str.startswith("sk-ant-oat"):
            auth_style_val = AuthStyle.BEARER
        return ProviderConfig(
            key=cur.key,
            name=q("#p-name", Input).value.strip() if cur.spec_id is None else cur.name,
            spec_id=cur.spec_id,
            scheme=ApiScheme(sel_value(q("#p-scheme", Select)) or cur.scheme.value),
            base_url=q("#p-url", Input).value.strip(),
            api_key=api_key_str,
            auth=auth_style_val,
            auth_header_name=q("#p-auth-name", Input).value.strip(),
            model=(
                q("#p-model", Input).value.strip()
                if (sel_value(q("#p-models", Select)) in ("__custom__", None, ""))
                else (sel_value(q("#p-models", Select)) or "")
            ),
            headers=_parse_pairs(q("#p-headers", TextArea).text, ":"),
            extras=_parse_pairs(q("#p-extras", TextArea).text, "="),
            verify_tls=q("#p-tls", Checkbox).value,
            ca_bundle=q("#p-ca", Input).value.strip(),
            timeout_s=_int(q("#p-timeout", Input).value, 120),
            context_window=_int(q("#p-ctx", Input).value, 0),
            max_output=_int(q("#p-maxout", Input).value, 0),
            temperature=temp,
            supports_tools=q("#p-tools", Checkbox).value,
            supports_streaming=q("#p-stream", Checkbox).value,
        )

    # ---- actions ----
    @on(Button.Pressed, "#p-fetch")
    def _fetch_pressed(self) -> None:
        self._fetch(self._read_form())

    @work(exclusive=True, group="fetch")
    async def _fetch(self, cfg: ProviderConfig) -> None:
        status = self.query_one("#p-result", Static)
        status.update(Text("Querying endpoint for models...", style="dim"))
        try:
            models = await self.app.backend.list_models(cfg)  # type: ignore[attr-defined]
            if self.is_mounted:
                sel = self.query_one("#p-models", Select)
                options: list[tuple[str, str]] = [(m, m) for m in models]
                options.append(("Custom model id...", "__custom__"))
                sel.set_options(options)
                if models:
                    sel.value = models[0]
                    self.query_one("#p-custom-model-box").display = False
                    self.query_one("#p-model", Input).value = models[0]
                status.update(Text(f"Found {len(models)} models", style="white"))
        except Exception as exc:
            if self.is_mounted:
                status.update(Text(f"Query error: {exc}", style="bold #f87171"))

    @on(Select.Changed, "#p-models")
    def _model_picked(self, ev: Select.Changed) -> None:
        if ev.value and ev.value is not Select.BLANK and ev.value != Select.NULL:
            val = str(ev.value)
            if val == "__custom__":
                self.query_one("#p-custom-model-box").display = True
                self.query_one("#p-model", Input).focus()
            else:
                self.query_one("#p-custom-model-box").display = False
                self.query_one("#p-model", Input).value = val

    @on(Button.Pressed, "#p-test")
    def _test_pressed(self) -> None:
        self._test(self._read_form())

    @work(exclusive=True, group="test")
    async def _test(self, cfg: ProviderConfig) -> None:
        status = self.query_one("#p-result", Static)
        status.update(Text("Testing connection...", style="dim"))
        res: TestResult = await self.app.backend.test_provider(cfg)  # type: ignore[attr-defined]
        if not self.is_mounted:
            return
        if res.ok:
            tool_note = "" if res.tools_ok is None else (" - tools supported" if res.tools_ok
                                                         else " - warning: tools not supported")
            status.update(Text(f"Connected ({res.latency_ms} ms){tool_note}", style="white"))
        else:
            status.update(Text(f"Connection failed: {res.message}", style="bold #f87171"))

    @on(Button.Pressed, "#p-save")
    def _save_pressed(self) -> None:
        self._save(then_use=False)

    @on(Button.Pressed, "#p-use")
    def _use_pressed(self) -> None:
        model_val = sel_value(self.query_one("#p-models", Select))
        if model_val == "__custom__" or not model_val:
            model_val = self.query_one("#p-model", Input).value.strip()
        if not model_val:
            self.notify("Choose or enter a model id first.", severity="error")
            return
        self._save(then_use=True)

    def _save(self, then_use: bool) -> None:
        cfg = self._read_form()
        self._cur = cfg
        self._configs[cfg.key] = cfg
        self.run_worker(self._do_save(cfg, then_use), group="save")

    async def _do_save(self, cfg: ProviderConfig, then_use: bool) -> None:
        await self.app.backend.save_provider_config(cfg)  # type: ignore[attr-defined]
        self._rebuild_list(self.query_one("#prov-search", Input).value)
        self.notify(f"Saved {cfg.name}", title="Provider")
        if then_use:
            self.dismiss(cfg)

    @on(Button.Pressed, "#p-delete")
    def _delete_pressed(self) -> None:
        self._delete()

    @work(exclusive=True, group="delete")
    async def _delete(self) -> None:
        if not self._cur:
            return
        key = self._cur.key
        await self.app.backend.delete_provider_config(key)  # type: ignore[attr-defined]
        self._configs.pop(key, None)
        self._rebuild_list(self.query_one("#prov-search", Input).value)
        self._load("anthropic")
        self.notify("Removed saved settings", title="Provider")


THEMES = ["rackhand", "tokyo-night", "nord", "gruvbox", "dracula", "catppuccin-mocha", "textual-dark"]
