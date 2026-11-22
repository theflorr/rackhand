# FortiSwitch (FortiSwitchOS) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
FortiSwitchOS is Fortinet's dedicated switching operating system designed for enterprise access, aggregation, and campus/data center networks. It shares the FortiOS command philosophy (`config / show / get / diagnose / execute`) and runs in two distinct deployment architectures:
1. **FortiLink Mode (Centralized / Managed):** The switch is discovered, authorized, and configured centrally from a FortiGate firewall over a dedicated FortiLink trunk (using CAPWAP tunnels and proprietary 802.3ad link aggregation). In this mode, configuration is pushed from the FortiGate, while the local FortiSwitch CLI is used for physical layer verification, diagnostics, and hardware telemetry.
2. **Standalone Mode:** The switch functions as an independent, fully autonomous Layer 2/3 switch configured entirely through its local CLI and Web UI.

### Target Platforms & Hardware
* **Entry Access (1G / Desktop / Fanless):** FortiSwitch 108E, 108F, 124E, 124F (with PoE/PoE+ options).
* **Campus & Wiring Closet (1G / Multi-Gig):** FortiSwitch 148E, 148F, 248E, 424E, 448E (PoE+ / 802.3bt UPoE).
* **Aggregation & Data Center Core (10G / 25G / 40G / 100G):** FortiSwitch 1024D, 1048E, 2048E, 3032D, 3032E.
* **Industrial & Ruggedized:** FortiSwitch Rugged (FRS) series for harsh environments.

---

## 2. CLI Modes & Command Paradigms

```
FortiSwitch Global CLI (FSW-Floor1 #)
  │
  ├── config switch <subsystem> (e.g. vlan, physical-port, trunk, stp)
  │     ├── edit <port / entry>
  │     │     ├── set <param> <value>
  │     │     └── next
  │     └── end (commits changes)
  │
  ├── get switch <subsystem> (operational tables, VLANs, MACs, optics)
  ├── diagnose switch <subsystem> (ASIC state, internal registers, PoE telemetry)
  └── execute <command> (ping, reboot, factory reset, backup)
```

### Prompt Patterns & Regex Matching
| Mode | Standard Prompt | Regex Pattern |
|---|---|---|
| **EXEC Prompt** | `FSW-Floor1 # ` | `^[A-Za-z0-9_\-\.]+\s*#\s*$` |
| **Config Mode** | `FSW-Floor1 (physical-port) # ` | `^[A-Za-z0-9_\-\.]+\s*\([A-Za-z0-9_\-]+\)\s*#\s*$` |

---

## 3. Terminal Control & Environment Preparation

Like FortiOS, FortiSwitch requires setting the console output to standard to prevent screen-paging halts (`--More--`) during automated scripting:

```fortios
# Disable paging for automation
config system console
    set output standard
end
```

---

## 4. Comprehensive CLI Command Reference

### A. System, Hardware & Environmental Status
```fortios
# System version, serial number, uptime, firmware build
get system status

# System performance (CPU, memory utilization)
get system performance status

# Environmental sensors (temperature, fan speed, power supply status)
get system status
diagnose hardware sysinfo
diagnose switch physical-port temperature
```

### B. Physical Port Status & SFP Transceiver Optics
```fortios
# Link state, speed, duplex, and port description across all ports
get switch physical-port status
get switch physical-port summary

# Detailed statistics on a specific port (bytes in/out, CRC errors, discards)
diagnose switch physical-port status port1

# SFP / SFP+ / QSFP optical transceiver diagnostics (DDM/DOM: Tx/Rx dBm optical power, temp)
get switch modules status
diagnose switch modules detail port24
diagnose switch modules detail port28
```

### C. Layer 2: VLANs, Trunking, Port-Channels (LAG) & MAC Table
```fortios
# VLAN configuration and member interfaces
get switch vlan
show switch vlan

# MAC address forwarding table
get switch mac-address-table
diagnose switch mac-address-table list
diagnose switch mac-address-table list | grep "00:11:22"

# Trunk (Link Aggregation / LACP) status and active member ports
get switch trunk
diagnose switch trunk status
diagnose switch trunk list

# Spanning Tree Protocol (STP / RSTP / MSTP) root bridge and port roles
get switch stp root
get switch stp status
diagnose switch stp root
```

### D. Multi-Chassis Link Aggregation (MCLAG - Core Redundancy)
```fortios
# MCLAG peer synchronization and health status
get switch mclag peer-status
get switch mclag icl-status

# MCLAG interface states
diagnose switch mclag status
diagnose switch mclag peer
```

### E. Power over Ethernet (PoE / PoE+ / 802.3bt)
```fortios
# Overall PoE power budget, consumption, and available wattage
get switch poe inline
get switch poe status

# Per-port PoE power draw and classification
diagnose switch poe status
diagnose switch poe port-status port1
```

### F. LLDP, Neighbor Discovery & FortiLink Management
```fortios
# LLDP neighbor summary (detected phones, access points, endpoints)
get switch lldp neighbors-summary
get switch lldp neighbors-detail

# FortiLink Controller connection status (when managed by FortiGate)
get system fortilink
diagnose switch-controller status
```

### G. System Actions & Diagnostics
```fortios
# Network connectivity
execute ping 192.168.1.1
execute traceroute 192.168.1.1

# Cable Diagnostics (TDR test for shorts, opens, distance to fault)
diagnose switch physical-port cable-diag port1

# Reboot or reset
execute reboot
execute factoryreset
```

---

## 5. Error Indicators & Parsing Gotchas

| Error Pattern | Cause | Meaning / Action |
|---|---|---|
| `Unknown action 0` | Syntax error | Invalid command or missing sub-level. |
| `FortiLink managed switch: change not permitted` | Managed lock | Switch is under FortiLink control; port config must be done on the FortiGate. |
| `Entry already exists` | Duplicate | Attempted to assign an existing VLAN ID or trunk name. |

### Parsing Gotchas on FortiSwitch
1. **Managed vs. Standalone Conflicts:** If managed by FortiGate over FortiLink, direct manual changes to VLANs on ports will trigger a synchronization warning and will be overwritten by the FortiGate's Switch Controller.
2. **Port Numbering:** Ports are typically named `port1`, `port2`, etc. Fiber ports are often `port25-port28` or `port49-port52` depending on switch density.
