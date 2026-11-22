# Cisco NX-OS (Nexus Operating System) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
Cisco NX-OS is a data-center-class, modular network operating system built on a preemptive, multi-threaded Linux microkernel. Its architecture isolates protocol state and device drivers into independent, supervised user-space processes (via `sysmgr`):
* **Stateful Process Restart:** Crashed processes (e.g. OSPF, BGP) restart automatically without dropping hardware data-plane traffic.
* **Hardware Abstraction Layer (HAL / USD):** Translates logical forwarding updates into hardware ASIC tables (Cloud Scale / Merchant Silicon).
* **Virtual Device Contexts (VDCs):** Partitioning a physical chassis into multiple virtual switches with isolated control/data planes (Nexus 7000/7700).
* **Native Structured Output:** Built-in programmatic JSON and XML translation pipes directly from the CLI.

### Target Platforms & Hardware
* **Data Center Leaf / Spine Switches:** Nexus 9300, 9500 (Cloud Scale ASICs).
* **Ultra-Low Latency / High-Density Top-of-Rack:** Nexus 3000, 3100, 3400, 3500 series.
* **Classic Fabric / Modular Core:** Nexus 7000, 7700, 5500, 5600, 6000 series.
* **Fabric Extenders:** Nexus 2000 (FEX) managed remotely by a parent switch.
* **Virtual Appliances:** Nexus 9000v (NX-OSv).

---

## 2. CLI Modes, Hierarchies & Navigation

```
EXEC Mode (#)
  │
  ├── Global Configuration Mode (config)#
  │     ├── Feature Activation (e.g. feature ospf, feature vpc)
  │     ├── Interface Configuration (config-if)#
  │     ├── vPC Domain Configuration (config-vpc-domain)#
  │     └── VRF Configuration (config-vrf)#
  │
  ├── VDC Context (switchto vdc <name>)
  └── Bash Linux Shell (run bash)
```

### Prompt Patterns & Regex Matching
Unlike classic IOS, NX-OS defaults immediately to privileged EXEC mode `#` upon authentication:
| Mode | Standard Prompt | Regex Pattern |
|---|---|---|
| **EXEC Mode** | `N9K-1#` or `N7K-Core#` | `^[A-Za-z0-9_\-\.\/]+#$` |
| **Global Config** | `N9K-1(config)#` | `^[A-Za-z0-9_\-\.\/]+\(config\)#$` |
| **Interface Config** | `N9K-1(config-if)#` | `^[A-Za-z0-9_\-\.\/]+\(config-if\)#$` |
| **vPC Domain Config** | `N9K-1(config-vpc-domain)#` | `^[A-Za-z0-9_\-\.\/]+\(config-.*\)#$` |
| **Bash Shell** | `bash-4.4$ ` or `bash-4.4# ` | `^bash-[0-9\.]+[\$#]\s*$` |

### Key Differences from IOS
* **No Unprivileged User EXEC Mode:** Logging into NX-OS places the user directly at prompt `#` with administrative permissions dictated by RBAC roles (`network-admin`, `network-operator`).
* **Feature-Based Model:** Functionality (OSPF, BGP, vPC, LACP, Interface-VLAN, EVPN) is disabled by default to optimize RAM/CPU footprint. Commands will not exist in the CLI until their feature is explicitly enabled.

---

## 3. Terminal Control & Output Formatting

```cisco
! Disable pagination
terminal length 0

! Disable line wrap truncation
terminal width 511

! Set default CLI output to JSON (Optional - powerful for automation)
terminal output json

! Disable DNS timeouts
configure terminal
 no ip domain-lookup
end
```

### Programmatic Pipe Features (`| json`)
NX-OS can convert almost any CLI output to native JSON:
```cisco
! Raw JSON output
show interface status | json

! Formatted / Pretty JSON output
show ip route | json-pretty

! Filter by XML
show version | xml

! Standard Unix pipes
show interface status | grep Eth1/1
show ip route | egrep "(via|Direct)"
show running-config | section vpc
```

---

## 4. Configuration Lifecycle, Checkpoints & Rollback

### Feature Model (Mandatory Step)
Before configuring protocols or features, enable the feature globally:
```cisco
configure terminal
 feature lldp
 feature lacp
 feature interface-vlan
 feature vpc
 feature ospf
 feature bgp
 feature nv overlay
 feature vn-segment-vlan-based
 feature bash-shell
end
```

### Checkpoints and Atomic Rollback
NX-OS supports native configuration snapshots and atomic rollbacks:
```cisco
! Create an atomic checkpoint of the current running configuration
checkpoint BEFORE_CHANGE description "Pre-maintenance snapshot"

! Display saved checkpoints
show checkpoint summary

! View configuration diff between running-config and a checkpoint
show diff rollback-patch checkpoint BEFORE_CHANGE running-config

! Atomic rollback to a checkpoint
rollback running-config checkpoint BEFORE_CHANGE
```

### Saving Configuration
```cisco
! Save running config to startup config
copy running-config startup-config

! Erase configuration to restore factory defaults
write erase
reload
```

---

## 5. Comprehensive CLI Command Reference

### A. System, Hardware & Environmental Health
```cisco
! System release, BIOS version, chassis uptime, system image
show version

! Hardware modules, serial numbers, ASIC revisions, SFPs
show inventory
show module

! Power supplies, fan trays, temperature sensors, system wattage
show environment
show environment power
show environment temperature
show environment fan

! System CPU utilization (user, kernel, idle per core)
show system resources
show processes cpu sorted

! Process memory consumption
show processes memory

! System clock and NTP synchronization
show clock
show ntp peer-status

! File systems and flash storage
dir bootflash:
dir volatile:
```

### B. Interface Status & Physical Layer
```cisco
! Interface operational state, speed, duplex, VLAN, and type
show interface status
show interface brief

! Detailed interface statistics, drops, discards, CRC errors
show interface Ethernet1/1

! Interface error counters only
show interface counters errors

! Optical transceiver diagnostics (DOM / DDM: Tx/Rx dBm, temp)
show interface Ethernet1/1 transceiver details

! Interface descriptions
show interface description
```

### C. Layer 2: VLANs, Trunking, Spanning Tree & Port-Channels
```cisco
! VLAN table and port associations
show vlan
show vlan brief
show vlan id [vlan-id]

! Switchport mode, 802.1Q trunking, allowed VLANs
show interface trunk
show interface switchport

! Spanning tree protocol status (MSTP / Rapid-PVST+)
show spanning-tree
show spanning-tree summary

! Dynamic MAC address table
show mac address-table
show mac address-table dynamic
show mac address-table count

! Port-Channel (LACP) state
show port-channel summary
show port-channel database
show lacp neighbor

! vPC (Virtual Port Channel) Status & Diagnostics
show vpc
show vpc brief
show vpc consistency-parameters global
show vpc consistency-parameters interface Port-channel10
show vpc peer-keepalive

! Neighbor discovery
show cdp neighbors
show lldp neighbors
```

### D. Layer 3: Routing, VRFs, ARP, BGP & OSPF
```cisco
! Virtual Routing and Forwarding (VRF) instances
show vrf
show vrf all

! IPv4 / IPv6 Routing Tables per VRF
show ip route
show ip route vrf all
show ip route vrf [vrf-name]
show ip route summary

! ARP table
show ip arp
show ip arp vrf all

! OSPF State
show ip ospf
show ip ospf neighbors vrf all
show ip ospf database

! BGP State
show bgp all summary
show bgp sessions
show bgp ipv4 unicast summary vrf all
show bgp ipv4 unicast neighbors [ip] advertised-routes
```

### E. Data Center Overlays: VXLAN & EVPN
```cisco
! Network Virtualization Edge (NVE) interface status
show nve interface nve1
show nve interface nve1 detail

! VNI mapping and VXLAN encapsulation
show nve vni
show nve vni summary

! Remote VTEP peers
show nve peers

! BGP EVPN L2/L3 route exchange
show bgp l2vpn evpn summary
show bgp l2vpn evpn
```

### F. Diagnostics, Logging & Packet Sniffing
```cisco
! System event logs
show logging
show logging last 100

! Ethanalyzer (Built-in Control-Plane Wireshark / TShark Sniffer)
ethanalyzer local interface inband limit-captured-frames 50
ethanalyzer local interface inband display-filter "arp || icmp" limit-captured-frames 20
ethanalyzer local interface inband display-filter "ip.addr == 192.168.1.1" detail

! IP Connectivity
ping [ip-address] vrf [vrf-name]
traceroute [ip-address] vrf [vrf-name]
```

---

## 6. Error Indicators & Parsing Gotchas

| Error Pattern | Cause | Meaning / Action |
|---|---|---|
| `% Invalid command at '^' marker` | Syntax or Feature | Command token is invalid, or its required `feature` is disabled. |
| `% Incomplete command at '^' marker` | Missing arguments | Mandatory positional argument missing. |
| `ERROR: Feature not enabled` | Dependency | Issue `feature <name>` before running protocol commands. |
| `ERROR: Module operation failed` | Hardware / ASIC | Module insertion or ASIC programming failure. |
| `Configuration failed for vPC...` | Inconsistency | Type-1 consistency check failed between vPC peers. |

### Parsing Gotchas on NX-OS
1. **Interface Naming:** NX-OS uses `Ethernet1/1` or `Eth1/1`, not `GigabitEthernet`. Management port is `mgmt0`. Port-channels are `port-channel1` or `Po1`.
2. **VRF Contexts:** Many commands (e.g. `show ip route`, `ping`) default only to VRF `default`. Management traffic is typically isolated under VRF `management` (`show ip route vrf management`, `ping 8.8.8.8 vrf management`).
3. **Structured `| json` Advantage:** When writing automated scrapers for NX-OS, always consider appending `| json`. It bypasses regex text scraping entirely and produces consistent JSON key-value trees.
