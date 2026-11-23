# Rackhand

**Agentic network engineering - autonomously**

Rackhand connects LLM agents directly to the device, so you don't have to relay output and commands by hand. Rackhand puts autonomous LLM agents directly on your serial console and SSH sessions. It edits switch and router configs, troubleshoots VLANs and routing protocols, and plans out the exact commands with human approval before touching your hardware.

![Rackhand](screenshot.jpg)

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

![Rackhand](screenshot-agent.png)

---

### Safety & Approvals

* **You approve all changes**: The agent runs `show` and `get` commands on its own, but stops and asks you before editing configs.
* **Automatic backups**: Pulls a copy of `running-config` to your disk before touching anything.
* **Hides passwords**: Masks passwords, PSKs, and SNMP strings (`[REDACTED]`) before sending text to the AI.
* **Blocks dangerous commands**: Catches things like `reload` or `write erase` before they can break your gear.

---

### Specialist Agents

Knows the right syntax, quirks, and save commands for each platform:

* **Cisco**: IOS, IOS-XE, NX-OS (vPC / VXLAN), ASA (`packet-tracer`), FTD.
* **Fortinet**: FortiOS (firewall rules, SD-WAN, VDOMs), FortiSwitch, FortiAP, FortiManager.
* **Auto-Detect**: Reads the device banner on connect and picks the right agent automatically.

---

### Modes & Commands

* **`/plan`**: Read-only mode. Asks questions, checks the setup, and writes a step-by-step plan with rollback steps.
* **`/build`**: Normal mode. Runs commands and applies changes with your approval.
* **`/compact`**: Summarizes the chat so you don't run out of memory on long sessions.
* **`/fork`**: Copies your current session to a new tab so you can test another idea.
* **`/effort`**: Changes how hard thinking models think (`low`, `medium`, `high`).

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
