# Cisco IOS-XE Operating System CLI Reference

## 1. Operating System & Platform Architecture

### Overview
Cisco IOS-XE is a modular, Linux-based network operating system that separates the control plane, data plane, and management plane. Unlike classic monolithic IOS, IOS-XE runs a hardened Linux kernel with individual user-space processes:
* **IOS Daemon (IOSd):** Runs the classic Cisco IOS feature set and CLI as a user-space daemon.
* **Forwarding Engine Driver (FED):** Programs the hardware ASIC pipeline (e.g., Cisco UADP / Silicon One).
* **Chassis Manager / Platform Daemons:** Manage fans, power supplies, transceivers, and environmental monitoring independently of protocol routing.
* **Programmability Subsystem:** Built-in Python runtime, Guest Shell (LXC Linux container), NETCONF/RESTCONF yang engines.

### Target Platforms & Hardware
* **Enterprise Access/Aggregation Switches:** Catalyst 9200, 9300, 9400, 9500, 9600 series; legacy Catalyst 3650, 3850.
* **Edge / Branch Routers:** Catalyst 8200, 8300, 8500 Edge Platforms; ISR 4200, 4300, 4400 series; ASR 1000 series (ASR 1001-X, 1002-HX, 1006-X).
* **Virtual Appliances:** Catalyst 8000v, CSR 1000v.

---

## 2. CLI Modes, Hierarchies & Navigation

IOS-XE retains full backward compatibility with the classic IOS CLI syntax, with additional modes for Linux containers, platform diagnostics, and programmability.

```
User EXEC Mode (>)
  │
  └── Privileged EXEC Mode (#)
        │
        ├── Global Configuration Mode (config)#
        │     ├── Interface Configuration (config-if)#
        │     ├── App-Hosting / IOx Configuration (config-app-hosting)#
        │     ├── NETCONF / Telemetry Config (config)#
        │     └── Line / Router Config (config-line)# / (config-router)#
        │
        ├── Guestshell / Linux Shell (bash)
        └── ROMmon Mode (rommon 1 >)
```

### Prompt Patterns & Regex Matching
| Mode | Standard Prompt | Regex Pattern |
|---|---|---|
| **User EXEC** | `Cat9300>` or `C8300>` | `^[A-Za-z0-9_\-\.\/]+>$` |
| **Privileged EXEC** | `Cat9300#` or `C8300#` | `^[A-Za-z0-9_\-\.\/]+#$` |
| **Global Config** | `Cat9300(config)#` | `^[A-Za-z0-9_\-\.\/]+\(config\)#$` |
| **Interface Config** | `Cat9300(config-if)#` | `^[A-Za-z0-9_\-\.\/]+\(config-if\)#$` |
| **StackWise-Virtual**| `Cat9500(config-stackwise-virtual)#` | `^[A-Za-z0-9_\-\.\/]+\(config-.*\)#$` |
| **Guestshell** | `[guestshell@cat9300 ~]$` | `^\[.*@.*\][\$#]\s*$` |
| **ROMmon** | `switch:` or `rommon 1 >` | `^(?:switch:|rommon\s*\d+\s*>)$` |

### Mode Transitions
* **User -> Privileged:** `enable`
* **Privileged -> User:** `disable`
* **Privileged -> Global Config:** `configure terminal`
* **Privileged -> Guest Shell (Bash):** `guestshell run bash` or `guestshell`
* **Exit Guest Shell:** `exit` (returns to IOS-XE EXEC)
* **Exit Any Config Mode:** `end` or `Ctrl+Z`

---

## 3. Terminal Control & Automation Preparation

Execute at the beginning of an automated session:

```cisco
! Disable interactive pagination
terminal length 0

! Disable terminal width wrapping
terminal width 0
! Or:
terminal width 511

! Disable DNS name lookups for typos
configure terminal
 no ip domain lookup
end

! Set console/VTY session timeout and logging synchronization
configure terminal
 line vty 0 15
  exec-timeout 0 0
  logging synchronous
 line con 0
  exec-timeout 0 0
  logging synchronous
end
```

---

## 4. Configuration Lifecycle, Rollback & Boot Modes

### Immediate Running Configuration
Changes take effect immediately upon typing in `(config)#`.
```cisco
! Save running config to NVRAM
write memory
copy running-config startup-config

! Erase configuration and return to factory state
write erase
reload
```

### Configuration Rollback & Archive (Rescue Feature)
IOS-XE features an enterprise archive and rollback system:
```cisco
! Enable configuration archiving
configure terminal
 archive
  path bootflash:archive-config
  maximum 14
  write-memory
 end

! View configuration rollback points
show archive

! Compare running configuration against a saved checkpoint
show archive config differences nvram:startup-config system:running-config

! Atomic rollback to a previous checkpoint without rebooting
configure replace bootflash:archive-config-1 force
```

### Install Mode vs. Bundle Mode
* **Bundle Mode:** Boots a single monolithic `.bin` file directly. (Slower, higher memory usage, deprecated on modern Cat9k).
* **Install Mode (Recommended):** Expands packages onto `bootflash:` and loads via bootloader packages.conf.
```cisco
! Check current running mode
show version | include Mode

! Upgrading in Install Mode (Standard 17.x workflow)
install add file bootflash:cat9k_iosxe.17.09.04a.SPA.bin activate commit
! Clean up obsolete packages
install remove inactive
```

---

## 5. Output Filtering & Enhanced Pipes

IOS-XE supports advanced CLI regex and pipe features:
```cisco
show <command> | include <regex>
show <command> | exclude <regex>
show <command> | begin <regex>
show <command> | section <regex>
show <command> | count <regex>
show <command> | format <spec>
! Send output to flash or TFTP/SCP/SFTP
show running-config | redirect bootflash:backup.cfg
```

---

## 6. Comprehensive CLI Command Reference

### A. System, Hardware & Platform Telemetry
```cisco
! System release, uptime, package hash, boot mode
show version

! Detailed chassis inventory, modular cards, power supplies, SFPs
show inventory

! Power supplies, fans, temperature sensors, system health
show environment all
show environment power
show environment temperature

! Detailed CPU utilization per core and per Linux thread
show processes cpu sorted | exclude 0.00%
show processes cpu platform sorted

! Dynamic memory pool usage across IOSd and Linux control plane
show processes memory sorted
show platform software status control-processor brief

! Switch stack state (StackWise-480 / StackWise-1T)
show switch
show switch detail
show switch stack-bandwidth
show switch stack-ports

! StackWise Virtual (SVL on Cat9500 / 9600)
show stackwise-virtual
show stackwise-virtual link
show stackwise-virtual dual-active-detection

! Cisco Smart Licensing status (SL / SLE / CSL)
show license status
show license summary
show license usage

! Flash file system contents
dir bootflash:
dir crashinfo:
```

### B. Interface Status & Physical Layer
```cisco
! Interface IP, Line and Protocol operational state
show ip interface brief
show ipv6 interface brief

! Switchport port table (Speed, Duplex, Vlan, State)
show interfaces status

! Detailed interface statistics, drops, MTU, duplex, CRC errors
show interfaces [interface-id]

! Optic transceiver diagnostics (DDM/DOM optical Rx/Tx power, temperature)
show interfaces [interface-id] transceiver detail

! PoE allocation per port and power consumption
show power inline
show power inline detail

! Interface description mapping
show interfaces description
```

### C. Layer 2: VLANs, Trunking, STP, MAC & Neighbors
```cisco
! VLAN table and interface membership
show vlan brief
show vlan id [vlan-id]

! 802.1Q trunking configuration and allowed VLANs
show interfaces trunk
show interfaces [interface-id] switchport

! Spanning Tree Protocol status (Rapid-PVST+ or MST)
show spanning-tree
show spanning-tree summary
show spanning-tree root
show spanning-tree detail

! Dynamic MAC address table
show mac address-table
show mac address-table dynamic
show mac address-table interface [interface-id]

! EtherChannel (LACP) state
show etherchannel summary
show etherchannel port-channel
show lacp neighbor

! LLDP / CDP neighbor tables
show cdp neighbors detail
show lldp neighbors detail
```

### D. Layer 3: Routing, VRFs, ARP, Protocols
```cisco
! VRF instances
show vrf
show ip vrf

! IPv4 / IPv6 Routing Tables
show ip route
show ip route vrf [vrf-name]
show ip route summary
show ipv6 route

! ARP table
show ip arp
show ip arp vrf [vrf-name]

! Routing protocol state
show ip protocols
show ip ospf neighbor
show ip ospf database
show ip bgp summary
show ip bgp all summary
show ip bgp neighbors [ip-address] advertised-routes
show ip eigrp neighbors
```

### E. Programmability, Telemetry & Security
```cisco
! NETCONF / RESTCONF service status
show netconf-yang status
show netconf-yang statistics
show platform software yang-management process

! Model-driven telemetry subscriptions
show telemetry ietf subscription all

! Guest shell container status
show app-hosting list
show app-hosting detail

! ACL configurations and rule matches
show access-lists
show ip access-lists

! 802.1X / MAB Authentication Manager sessions (TrustSec / ISE)
show authentication sessions
show authentication sessions interface [interface-id] details
```

### F. Packet Tracing & Advanced Diagnostics
```cisco
! Embedded Packet Capture (EPC) on IOS-XE
monitor capture CAP interface GigabitEthernet1/0/1 both
monitor capture CAP buffer size 10
monitor capture CAP match any
monitor capture CAP start
! Wait for traffic...
monitor capture CAP stop
show monitor capture CAP buffer dump
no monitor capture CAP

! Datapath Packet Tracer (Hardware ASIC flow tracing on routers/switches)
debug platform condition ipv4 192.168.1.1/32 both
debug platform condition start
debug platform packet-trace packet 1024
debug platform packet-trace copy packet input both
show platform packet-trace summary
show platform packet-trace packet all
debug platform condition stop

! Logging buffers
show logging
```

---

## 7. Error Indicators & Parsing Gotchas

| Error Pattern | Cause | Meaning / Action |
|---|---|---|
| `% Invalid input detected at '^' marker.` | Syntax error | Unrecognized keyword or invalid token at the caret. |
| `% Incomplete command.` | Missing parameters | Further positional arguments required. |
| `% Ambiguous command:` | Multiple matches | CLI prefix matches more than one command keyword. |
| `% Configuration mode locked by...` | Concurrency lock | Another session has opened configuration transaction. |
| `% Software install operation in progress` | Package lock | An `install` command is currently modifying flash packages. |
| `% License violation...` | Smart License | Feature requires unallocated entitlement; evaluate grace period. |

### Parsing Traps on IOS-XE
1. **Multi-Core / Process Tables:** `show processes cpu` output contains multi-line headers with per-core percentages (`Core 0: CPU utilization for five seconds: 2%...`).
2. **GigabitEthernet vs. TenGigabitEthernet vs. TwentyFiveGigE:** Modern Catalyst switches use diverse naming conventions (`GigabitEthernet1/0/1`, `TenGigabitEthernet1/0/1`, `TwentyFiveGigE1/0/1`, `AppGigabitEthernet1/0/1`). Always extract port names using flexible regex `^[A-Za-z]+[0-9\/]+`.
3. **Install Mode Warnings:** During boot, `install` mode triggers lengthy notifications that can disrupt expected banner tokens. Wait for prompt stabilization before sending characters.
