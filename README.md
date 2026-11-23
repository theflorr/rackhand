# Rackhand

**Agentic network engineering - autonomously**

Rackhand connects LLM agents directly to the device, so you don't have to relay output and commands by hand. Rackhand puts autonomous LLM agents directly on your serial console and SSH sessions. It edits switch and router configs, troubleshoots VLANs and routing protocols, and plans out the exact commands with human approval before touching your hardware.

![Rackhand Terminal UI](screenshot.jpg)

---

### Download

Grab the standalone Windows executable from **[GitHub Releases](https://github.com/theflorr/rackhand/releases)**.  
No Python installation or dependencies required: download `rackhand.exe` and run it.

---

### What it does

* **Direct Serial & SSH**: Plugs straight into USB-to-serial console cables (with automatic COM port and baud detection) or network SSH sessions.
* **Human-in-the-Loop Safety**: The agent audits and investigates read-only first. If it suggests changes, it presents an approval card with the exact commands and reasoning. You review, edit, or reject before anything gets sent to the CLI.
* **Network Baseline Knowledge**: Pre-loaded with operational playbooks for Cisco (IOS, IOS-XE, IOS-XR, NX-OS, ASA) and Fortinet (FortiOS, FortiSwitch, FortiAP).
* **Multi-Provider AI**: Supports Anthropic Claude, OpenAI, Google Antigravity / Gemini, DeepSeek, OpenRouter, or local Ollama instances.
* **Clean Dark TUI**: Built on Textual with split live console streaming, agent chat, and side-by-side session management.

![Rackhand Multi-Session Interface and OS Detection](screenshot-agent.jpg)

---

### Safety & Approvals

* **Staged Changes**: Read-only diagnostics run autonomously. Config changes pause for your review, edit, or approval.
* **Pre-Flight Backups**: Automatically snapshots `running-config` to local disk before applying changes.
* **Secret Redaction**: Passwords, PSKs, and SNMP strings are masked (`[REDACTED]`) in-flight before hitting the LLM.
* **Risk Gatekeeper**: Flags destructive commands (`reload`, `write erase`, interface wipes) before execution.

---

### Specialist Agents

Pre-loaded with platform syntax, pagination handling, and commit mechanics:

* **Cisco**: IOS, IOS-XE, NX-OS (vPC / VXLAN), ASA (`packet-tracer`), FTD.
* **Fortinet**: FortiOS (firewall policies, SD-WAN, VDOMs), FortiSwitch, FortiAP, FortiManager.
* **Auto-Detect**: Reads device banner on connect to pick the right specialist automatically.

---

### Modes & Commands

* **`/plan`**: Read-only mode. Explores topology and drafts a 4-phase rollout & rollback plan.
* **`/build`**: Execution mode. Stages commands and verifies live hardware state.
* **`/compact`**: Compresses conversation into a checkpoint to free token context.
* **`/fork`**: Clones active state into a new tab to test alternate configs safely.
* **`/effort`**: Sets reasoning effort (`low`, `medium`, `high`) on thinking models.

---

### Running from source

If you prefer running with Python:

```bash
git clone https://github.com/theflorr/rackhand.git
cd rackhand
pip install -e .
python app.py
```

### Compiling

Rackhand compiles into a standalone, single-file `.exe` powered by **PyApp** (Rust + embedded Python 3.12 runtime and uv):

```bash
python compile.py
```

---

### License

Distributed under the [MIT License](LICENSE).
