# FortiManager (FMG) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
FortiManager-OS is Fortinet's centralized network management and policy orchestration operating system. It provides single-pane-of-glass management for enterprise fleets of FortiGate firewalls, FortiSwitches, FortiAPs, and SD-WAN overlays:
* **FGFM Protocol (TCP Port 541):** Managed FortiGates establish an outbound, TLS-encrypted management tunnel over TCP 541 to FortiManager. Configuration pushes, real-time logging, and firmware images traverse this tunnel.
* **Administrative Domains (ADOMs):** Logical partitioning of managed devices into administrative groups by tenant, region, organizational unit, or firmware major version (e.g. 7.0, 7.2, 7.4).
* **Two-Tier Configuration Model (Device DB & Central DB):**
  * **Device Database (CDB):** Local mirror of each managed FortiGate's configuration.
  * **Policy & Objects Database:** Centralized firewall policy packages and shared address objects assigned across multiple firewalls.
  * **Installation / Deployment Engine:** Pushes policy changes from the Central DB into device configurations via audited deployment sessions.
* **High Availability (HA):** Geographic clustering with Primary and Secondary/Backup FortiManager nodes syncing PostgreSQL and CDB data-plane stores.

### Target Platforms & Hardware
* **Hardware Appliances:** FortiManager 200G, 400G, 1000F, 3000G, 3700G.
* **Virtual Appliances:** FortiManager-VM (VMware ESXi, KVM, Hyper-V, AWS, Azure, GCP, OCI).

---

## 2. CLI Modes & Architecture

FortiManager shares the FortiOS CLI structure (`config / show / get / diagnose / execute`) with specialized subsystems for Device Management (`dvm`), Policy Management (`pm2`), and Configuration Databases (`cdb`).

```
FortiManager CLI (FMG-Core #)
  │
  ├── config system <subsystem> (admin, interface, route, ha, adom)
  │
  ├── get system status (version, licenses, hardware)
  ├── diagnose dvm <subsystem> (Device Manager: managed FortiGate fleet, ADOMs, sync)
  ├── diagnose fgfm <subsystem> (FGFM TCP 541 tunnels, keepalives, SSL)
  ├── diagnose cdb <subsystem> (Configuration Database integrity)
  └── execute <action> (backup, restore, reboot, tunnel resets)
```

### Prompt Patterns & Regex Matching
| Mode | Standard Prompt Example | Regex Pattern |
|---|---|---|
| **EXEC Prompt** | `FMG-Primary # ` | `^[A-Za-z0-9_\-\.]+\s*#\s*$` |
| **Config Tree** | `FMG-Primary (interface) # ` | `^[A-Za-z0-9_\-\.]+\s*\([A-Za-z0-9_\-]+\)\s*#\s*$` |

---

## 3. Terminal Control & Environment Preparation

```fortios
# Disable paging for automation scripts
config system console
    set output standard
end
```

---

## 4. Comprehensive CLI Command Reference

### A. System Health, Licensing & Performance
```fortios
# Appliance model, firmware version, serial number, license count (managed devices limit)
get system status

# System resource consumption (CPU, RAM, disk I/O)
get system performance status
diagnose system print df
diagnose system print meminfo

# High Availability (HA) cluster status and node synchronization
get system ha status
diagnose system ha status
diagnose system ha checksum
```

### B. Device Manager (DVM): Fleet Status & Managed Firewalls
```fortios
# List all managed FortiGates (IP, Serial Number, Platform, Firmware, Sync Status, ADOM)
diagnose dvm device list

# Filter managed devices by specific name or serial number
diagnose dvm device list "FGT-Branch-01"

# Check device synchronization status (Synced, Modified, Conflict, Connection Down)
diagnose dvm device list | grep -E "(NAME|conn_status|db_status)"

# View registered Administrative Domains (ADOMs)
diagnose dvm adom list
```

### C. FGFM Management Tunnel Diagnostics (TCP Port 541)
The FGFM protocol connects FortiGates to FortiManager:

```fortios
# Check FGFM daemon status and active listener port (541)
diagnose fgfm status

# Display real-time FGFM connection session list
diagnose fgfm session-list

# Reclaim or reset a stuck FGFM tunnel to a specific FortiGate
execute fgfm reclaim-dev-tunnel [device-name]

# Debug live FGFM communication and tunnel negotiation
diagnose debug application fgfmsd 255
diagnose debug enable
# Disable:
diagnose debug disable
diagnose debug reset
```

### D. Configuration Database (CDB) & Task Monitor
```fortios
# View status of background installation, import, and deployment tasks
diagnose dvm task list
diagnose dvm task list 20

# Check integrity of the Configuration Database
diagnose cdb check all
diagnose cdb check-policy

# Check Policy Manager (PM) daemon status
diagnose pm2 status
```

### E. System Maintenance, Backup & Execution
```fortios
# Connectivity diagnostics
execute ping 192.168.1.1
execute traceroute 192.168.1.1

# Backup FortiManager full database and system configuration
execute backup config ftp <filename> <server> <user> <password>
execute backup config sftp <filename> <server> <user> <password>

# Reboot or shutdown
execute reboot
execute shutdown
```

---

## 5. Error Indicators & Parsing Gotchas

| Error Pattern | Cause | Meaning / Action |
|---|---|---|
| `conn_status: down` | Network / Cert | FortiGate cannot reach FMG over TCP 541, or SSL cert rejected. |
| `db_status: conflict` | Out of sync | Manual CLI changes were made directly on the FortiGate, diverging from FMG database. |
| `Task failed: verify error` | Policy syntax | Push failed because the target FortiGate rejected an object or syntax rule. |
| `Managed device limit reached` | License | Licensed device quota full; cannot discover new firewalls. |

### Parsing Traps on FortiManager
1. **Multi-Column Tables:** Output from `diagnose dvm device list` produces indented key-value blocks per device rather than a single horizontal table. Parsers must group records by `--- Device (name) ---` blocks.
2. **`set output standard` is Essential:** Fleet lists on enterprise FortiManagers can contain hundreds of devices; omitting `set output standard` will freeze the automation worker.
3. **Task Completion Polling:** Policy deployments run asynchronously in the background. Capture the `Task ID` and query `diagnose dvm task list` to confirm `status: completed` or `failed`.
