# FortiGate (FortiOS) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
FortiOS is Fortinet's proprietary, high-performance security operating system running a heavily customized, real-time Linux kernel tightly integrated with Fortinet's proprietary Application-Specific Integrated Circuits (ASICs):
* **Network Processor (NPU - e.g. NP6, NP7):** Offloads Layer 2/3/4 packet forwarding, flow routing, NAT, and IPsec cryptographic operations directly in hardware.
* **Content Processor (CP - e.g. CP8, CP9):** Accelerates pattern matching, deep packet inspection (DPI), antivirus, and SSL/TLS cryptographic offloading.
* **Security Processing Unit (SPU / SoC):** System-on-a-Chip architectures for desktop models (e.g. SOC4) unifying CPU, NPU, and CP onto a single silicon die.
* **Virtual Domains (VDOMs):** Logical partitioning of a single physical FortiGate into multiple independent virtual firewalls with dedicated routing tables, firewall policies, administrators, and interfaces.

### Target Platforms & Hardware
* **Entry-Level (Desktop):** FortiGate 40F, 60E/60F, 70F, 80F, 90G.
* **Mid-Range (1RU Rackmount):** FortiGate 100E/100F, 200E/200F, 400F, 600F.
* **High-End & Data Center:** FortiGate 1000F, 1800F, 2600F, 3000F, 4400F, 7000 Series.
* **Virtual Appliances:** FortiGate-VM (VMware ESXi, KVM, Hyper-V, AWS, Azure, GCP, OCI).

---

## 2. CLI Modes, Hierarchies & Command Paradigms

The FortiOS CLI is structured around five primary verbs:
1. **`config`**: Enters a configuration tree. Changes are transactional and commit when exiting with **`end`** (or discard with **`abort`**).
2. **`show`**: Displays non-default configuration elements (what is written to the backup file).
3. **`get`**: Displays both configuration and operational state, including default values.
4. **`diagnose`**: Deep diagnostic probes, hardware registers, session tables, and live packet debugging.
5. **`execute`**: Triggers immediate runtime operations (e.g., ping, reboot, backup, restore, clear).

```
Global EXEC Context (FortiGate #)
  │
  ├── VDOM Context (config vdom -> edit <vdom>)
  │
  ├── config <subsystem> <object>
  │     ├── edit <entry-id / name>
  │     │     ├── set <parameter> <value>
  │     │     ├── unset <parameter>
  │     │     └── next (commit current entry & return to table)
  │     └── end (commit all changes & return to parent)
  │         └── abort (DISCARD all uncommitted changes & exit)
  │
  ├── show <subsystem>
  ├── get <subsystem>
  ├── diagnose <subsystem>
  └── execute <command>
```

### Prompt Patterns & Regex Matching
| Mode / Context | Standard Prompt Example | Regex Pattern |
|---|---|---|
| **Standard Mode** | `FW-HQ # ` | `^[A-Za-z0-9_\-\.]+\s*#\s*$` |
| **Config Tree** | `FW-HQ (interface) # ` | `^[A-Za-z0-9_\-\.]+\s*\([A-Za-z0-9_\-]+\)\s*#\s*$` |
| **VDOM Context** | `FW-HQ (root) # ` or `(global) # ` | `^[A-Za-z0-9_\-\.]+\s*\([A-Za-z0-9_\-]+\)\s*#\s*$` |
| **Config in VDOM** | `FW-HQ (firewall) # ` | `^[A-Za-z0-9_\-\.]+\s*\([A-Za-z0-9_\-]+\)\s*#\s*$` |

---

## 3. Terminal Control & Automation Setup

> [!IMPORTANT]
> FortiOS does not use `terminal length 0`. To disable the `--More--` paging prompt in automation scripts, change the console output setting to `standard`.

```fortios
# Disable paging in FortiOS (equivalent to terminal length 0)
config system console
    set output standard
end

# Re-enable interactive paging (default)
config system console
    set output more
end
```

### Output Pipe Filtering
FortiOS includes a built-in grep filter:
```fortios
<command> | grep [-i] [-v] [-c] [-n] [-e] <string>
# Options:
#  -i : Case-insensitive match
#  -v : Invert match (exclude)
#  -c : Count matching lines
#  -n : Print line numbers
#  -e : Regular expression pattern
```

---

## 4. Configuration Lifecycle & Transaction Rules

### The `edit / set / next / end` Paradigm
```fortios
config system interface
    edit "port1"
        set mode static
        set ip 192.168.1.1 255.255.255.0
        set allowaccess ping https ssh
    next
end
```

### Crucial Exit Commands
* **`next`**: Commits the current table entry and remains within the current `config` block to edit another entry.
* **`end`**: Commits all pending modifications made in the block and exits to the parent level.
* **`abort`**: **DISCARDS all uncommitted changes** made since entering the current block and exits immediately.

### Instant Persistence
Unlike Cisco devices where you must run `write memory` or `copy run start`, **FortiOS automatically commits changes to non-volatile flash storage the instant `end` is executed**.

---

## 5. Comprehensive CLI Command Reference

### A. System Status, Hardware & Performance Telemetry
```fortios
# System release, serial number, firmware build, hostname, license status, uptime
get system status

# System performance status (CPU, memory, active sessions, network bandwidth)
get system performance status

# Real-time top processes by CPU and memory consumption
diagnose sys process top 1 20

# Hardware inventory and physical component status
get hardware status
diagnose hardware deviceinfo nic

# High Availability (HA - A-P / A-A) cluster status and synchronization
get system ha status
diagnose sys ha status
diagnose sys ha checksum cluster

# System resource thresholds and memory health
diagnose hardware sysinfo memory
diagnose hardware sysinfo conserve
```

### B. Interface Status & Physical Networking
```fortios
# List interface IP addresses, link status, and administrative state
get system interface
get system interface physical

# Hardware transceiver optical diagnostics (SFP DDM/DOM: optical Rx/Tx power, temp)
get hardware nic port1
diagnose hardware deviceinfo nic port1

# ARP resolution cache
get system arp
diagnose ip arp list
```

### C. Firewall Policies & Address Objects
```fortios
# Display all configured firewall policies
show firewall policy

# View detailed runtime state and hit counters for firewall policies
diagnose firewall policy hitcount
diagnose firewall policy-lookup 192.168.1.50 8.8.8.8 6 12345 443

# Configured address objects and address groups
show firewall address
show firewall addrgrp
```

### D. Stateful Session Table (Flow Inspection)
```fortios
# Real-time stateful session list
diagnose sys session list

# Filter session table before printing (Crucial on production firewalls)
diagnose sys session filter clear
diagnose sys session filter src 192.168.1.50
diagnose sys session filter dst 8.8.8.8
diagnose sys session filter dport 443
diagnose sys session list

# Clear stateful sessions based on current filter
diagnose sys session clear
```

### E. Live Diagnostics: Sniffer & Debug Flow
FortiOS provides industry-leading CLI packet inspection tools directly in the kernel:

#### 1. Live Packet Sniffer (Tcpdump equivalent)
```fortios
# Syntax: diagnose sniffer packet <interface> '<filter>' <verbosity> <count> <time-format>
# Verbosity:
#  1: print packet headers
#  2: print IP header + payload
#  3: print ethernet header + IP + payload
#  4: print interface name + packet headers
#  6: print packet with full hex/ASCII dump and ethernet header

# Sniff ICMP or port 443 traffic on any interface (first 10 packets)
diagnose sniffer packet any 'icmp or port 443' 4 10 a
```

#### 2. Debug Flow (Internal Packet Trace Engine)
Traces routing decision, policy lookup, SNAT/DNAT evaluation, and hardware offload status:
```fortios
diagnose debug reset
diagnose debug flow filter clear
diagnose debug flow filter saddr 192.168.1.50
diagnose debug flow filter daddr 8.8.8.8
diagnose debug flow show console enable
diagnose debug flow trace start 10
diagnose debug enable

# Disable debug flow after troubleshooting
diagnose debug disable
diagnose debug reset
```

### F. Routing, VRFs & Protocols
```fortios
# IPv4 Active Routing Table
get router info routing-table all
get router info routing-table details

# BGP State
get router info bgp summary
get router info bgp neighbors
get router info bgp neighbors [ip] advertised-routes
get router info bgp neighbors [ip] routes

# OSPF State
get router info ospf neighbor
get router info ospf status
```

### G. IPsec & SSL VPN
```fortios
# IPsec Phase 1 and Phase 2 Tunnel Status
diagnose vpn tunnel list
get vpn ipsec tunnel summary

# SSL VPN active tunnel users and sessions
get vpn ssl monitor
diagnose vpn ssl list
```

### H. System Maintenance & Execution
```fortios
# Test connectivity
execute ping 8.8.8.8
execute ping-options source 192.168.1.1
execute traceroute 8.8.8.8

# Backup and restore configuration files
execute backup config ftp <filename> <ftp-server> <user> <password>
execute backup config usb <filename>

# Reboot or shutdown system
execute reboot
execute shutdown
```

---

## 6. Error Indicators & Parsing Gotchas

| Error Pattern | Cause | Meaning / Action |
|---|---|---|
| `Unknown action 0` | Syntax error | Command entered in incorrect context or invalid parameter. |
| `node_check_object fail! for ...` | Validation | Value violates schema (e.g. invalid IP address or netmask). |
| `entry not found` | Reference error | Target object (interface, address, schedule) does not exist. |
| `value parse error before '...'` | Token error | Typo in string or missing required quotes for spaced names. |
| `The system is in conserve mode!` | RAM exhaustion | Memory reached >82-88%; FortiGate drops new sessions and enters fail-open/closed state. |

### Automation Gotchas on FortiOS
1. **Quoted Strings with Spaces:** Any name containing spaces or special symbols (e.g., `"Sales LAN"`) MUST be enclosed in double quotes.
2. **`set output standard` Required:** Scripts will hang on long outputs (e.g., `show firewall policy`) unless `set output standard` is executed first.
3. **Commit on `end`:** If your automated script loses connectivity before sending `end`, the entire block of changes is silently dropped.
