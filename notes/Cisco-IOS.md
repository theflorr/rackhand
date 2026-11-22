# Cisco IOS (Internetwork Operating System) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
Cisco IOS is Cisco's classic, monolithic operating system that historically powered the vast majority of enterprise routing and switching platforms. In classic IOS, the operating system, system drivers, network protocols, and management applications run in a single shared memory space under a single run-to-completion scheduler (non-preemptive multitasking).

### Target Platforms & Hardware
* **Switches:** Cisco Catalyst 2960, 2960-S, 2960-X, 2960-C, 3560, 3560-X, 3750, 3750-X, 3560-CX.
* **Routers:** Cisco 1800, 1900, 2800, 2900, 3800, 3900, 7200, 7600 Integrated Services Routers (ISR G1 / ISR G2).
* **Architecture:** Monolithic ELF/binary image stored in flash memory (`flash:` or `bootflash:`), decompressed into RAM upon bootstrap.

---

## 2. CLI Modes, Hierarchies & Navigation

### Mode Hierarchy
Classic Cisco IOS uses a hierarchical, stateful command-line interface. Commands available depend strictly on the active mode:

```
User EXEC Mode (>)
  │
  └── Privileged EXEC Mode (#)
        │
        └── Global Configuration Mode (config)#
              │
              ├── Interface Configuration (config-if)#
              ├── Sub-Interface Configuration (config-subif)#
              ├── Line Configuration (config-line)#
              ├── Router Configuration (config-router)#
              ├── VLAN Database / Configuration (config-vlan)#
              └── ACL Configuration (config-ext-nacl)# / (config-std-nacl)#
```

### Prompt Patterns & Regex Matching
| Mode | Standard Prompt | Regex Pattern |
|---|---|---|
| **User EXEC** | `Switch>` or `Router>` | `^[A-Za-z0-9_\-\.\/]+>$` |
| **Privileged EXEC** | `Switch#` or `Router#` | `^[A-Za-z0-9_\-\.\/]+#$` |
| **Global Config** | `Switch(config)#` | `^[A-Za-z0-9_\-\.\/]+\(config\)#$` |
| **Interface Config** | `Switch(config-if)#` | `^[A-Za-z0-9_\-\.\/]+\(config-if\)#$` |
| **Sub-Interface** | `Switch(config-subif)#` | `^[A-Za-z0-9_\-\.\/]+\(config-subif\)#$` |
| **VLAN Config** | `Switch(config-vlan)#` | `^[A-Za-z0-9_\-\.\/]+\(config-vlan\)#$` |
| **Line Config** | `Switch(config-line)#` | `^[A-Za-z0-9_\-\.\/]+\(config-line\)#$` |
| **Router Config** | `Switch(config-router)#` | `^[A-Za-z0-9_\-\.\/]+\(config-router\)#$` |
| **ROMmon (Recovery)** | `switch:` or `rommon 1 >` | `^(?:switch:|rommon\s*\d+\s*>)$` |

### Mode Transitions
* **User -> Privileged:** `enable` (prompts for password if configured).
* **Privileged -> User:** `disable`.
* **Privileged -> Global Config:** `configure terminal` (abbreviated `conf t`).
* **Global Config -> Sub-mode:**
  * Interface: `interface FastEthernet 0/1` or `interface GigabitEthernet 0/1`
  * Line: `line vty 0 4` or `line con 0`
  * Routing: `router ospf 1` or `router bgp 65000`
  * VLAN: `vlan 10`
* **Sub-mode -> Global Config:** `exit`
* **Any Config Mode -> Privileged EXEC:** `end` or keystroke `Ctrl+Z`
* **Exit Session:** `quit` or `exit` from EXEC mode.

---

## 3. Terminal Control & Environment Preparation

Before executing programmatic commands or collecting bulk telemetry over automated sessions (Serial or SSH), issue these preparation commands:

```cisco
! Disable interactive pagination (prevent --More-- prompts)
terminal length 0

! Disable terminal line wrap calculation
terminal width 0
! Or standard max width:
terminal width 511

! Prevent long CLI DNS timeouts when mistyping commands in privileged mode
configure terminal
 no ip domain-lookup
! In older switches:
 no ip domain lookup
end

! Set console/VTY session timeout to infinite during troubleshooting (Optional)
configure terminal
 line con 0
  exec-timeout 0 0
  logging synchronous
 line vty 0 15
  exec-timeout 0 0
  logging synchronous
end
```

### Paging Behavior
* Paging Prompt: `--More--`
* Advance one page: `Space`
* Advance one line: `Return` / `Enter`
* Abort output: `q` or `Ctrl+C`

---

## 4. Configuration Lifecycle & Persistence

Classic IOS operates on an **immediate running-state** model. Every command entered in configuration mode immediately takes effect on the system state.

### Key Memory Locations
* **Running Configuration:** Stored in volatile RAM (`system:running-config`). Lost upon reboot unless saved.
* **Startup Configuration:** Stored in non-volatile NVRAM (`nvram:startup-config`). Read during boot.

### Persistence Commands
```cisco
! Save running config to NVRAM (classic)
write memory
! Or abbreviated:
wr

! Canonical IOS syntax
copy running-config startup-config
! Abbreviated:
copy run start

! Overwrite running config with startup config (revert changes)
copy startup-config running-config

! Wipe NVRAM configuration to reset device to factory defaults
write erase
! Or:
erase startup-config
reload
```

### Confirmation Handlers in Automation
When running `copy run start` or `reload`, IOS asks for confirmation:
```
Destination filename [startup-config]? <CR>
Proceed with reload? [confirm] <CR>
```
Automation engines must detect `\[.*\]\s*$` and send a carriage return `\r`.

---

## 5. Output Filtering & Pipe Commands

IOS supports Unix-like piping on `show` commands:
```cisco
show <command> | include <regex>    ! Filter lines matching pattern (case-sensitive)
show <command> | exclude <regex>    ! Filter lines NOT matching pattern
show <command> | begin <regex>      ! Begin printing from first matching line
show <command> | section <regex>    ! Display block/stanza starting with header (IOS 12.3T+)
show <command> | count <regex>      ! Output line count matching pattern
show <command> | redirect <url>     ! Send output to flash: or tftp:
show <command> | append <url>       ! Append output to file
```

---

## 6. Comprehensive CLI Command Reference

### A. System Information & Telemetry
```cisco
! Hardware model, serial number, IOS image path, uptime, configuration register
show version

! Detailed modular chassis/SFP hardware inventory and PID/VID/SN
show inventory

! Power supplies, fan status, temperature sensors
show env all
show environment
show environment power
show environment temperature

! CPU utilization over 5s, 1m, 5m and thread breakdown
show processes cpu
show processes cpu sorted | exclude 0.00%

! Memory pools (Processor, I/O) and allocations
show processes memory sorted

! Switch stack member roles, priority, state (StackWise/FlexStack)
show switch
show switch detail
show switch stack-ports

! System clock, NTP synchronization, timezone
show clock
show ntp status
show ntp associations

! Flash file system contents and directory listing
dir
dir flash:
dir nvram:
show file systems
```

### B. Interface Status & Layer 1/2 Physical State
```cisco
! Quick summary of interface IP addresses and Line/Protocol status
show ip interface brief

! Switchport operational state (Speed, Duplex, Vlan, Status: connected/notconnect/err-disabled)
show interfaces status

! Full interface counters (MTU, bandwidth, input/output errors, CRC, drops, collisions)
show interfaces [interface-id]

! Error counters only (CRC, frame, overrun, collisions, giants, runts)
show interfaces [interface-id] counters errors

! Transceiver optic diagnostics (SFP DOM / DDM: Tx/Rx dBm optical power, temp, voltage)
show interfaces [interface-id] transceiver detail

! PoE power draw, allocation per port, power inline budget
show power inline
show power inline [interface-id]

! Interface descriptions
show interfaces description
```

### C. Layer 2: VLANs, Trunking, STP, MAC Tables & Discovery
```cisco
! VLAN table, names, operational state, ports assigned
show vlan brief
show vlan id [vlan-id]

! 802.1Q trunking operational mode, encapsulation, allowed VLANs, pruning
show interfaces trunk
show interfaces [interface-id] switchport

! Spanning Tree Protocol (PVST+ / Rapid-PVST / MST)
show spanning-tree
show spanning-tree summary
show spanning-tree vlan [vlan-id]
show spanning-tree detail

! Dynamic and static MAC address table
show mac address-table
! On older 12.2 IOS releases:
show mac-address-table
show mac address-table dynamic
show mac address-table interface [interface-id]
show mac address-table vlan [vlan-id]
show mac address-table count

! EtherChannel / LACP / PAgP Port-Channel status
show etherchannel summary
show etherchannel port-channel
show lacp neighbor

! Neighbor discovery (Cisco Discovery Protocol & LLDP)
show cdp neighbors
show cdp neighbors detail
show lldp neighbors
show lldp neighbors detail
```

### D. Layer 3: Routing, ARP, Protocols
```cisco
! IPv4 Routing Table
show ip route
show ip route summary
show ip route connected
show ip route static
show ip route ospf
show ip route bgp

! ARP resolution cache
show ip arp
show ip arp [interface-id]
show ip arp [ip-address]

! Active routing protocols, process IDs, router ID, advertised networks
show ip protocols

! OSPF State
show ip ospf
show ip ospf neighbor
show ip ospf neighbor detail
show ip ospf interface brief
show ip ospf database

! BGP State
show ip bgp summary
show ip bgp
show ip bgp neighbors [ip-address] advertised-routes
show ip bgp neighbors [ip-address] routes
```

### E. Security, ACLs & Device Access
```cisco
! Configured access lists and hit counters
show access-lists
show ip access-lists

! Active VTY / Console sessions
show users
show line

! Port security status and violation counters
show port-security
show port-security interface [interface-id]
show port-security address

! 802.1X / MAB Authentication Manager sessions
show authentication sessions
show dot1x all

! DHCP Snooping & ARP Inspection (DAI) binding table
show ip dhcp snooping
show ip dhcp snooping binding
show ip arp inspection
show ip arp inspection vlan [vlan-id]
```

### F. Diagnostics, Logging & Packet Monitoring
```cisco
! System buffer log messages
show logging

! Real-time debug toggles (Caution: avoid unmetered debugs on production)
debug ip packet
debug ip ospf events
undebug all          ! Turn off all debugs
no debug all         ! Identical to undebug all

! Embedded Packet Capture (EPC) on supported IOS releases
monitor capture buffer BUF size 1024 max-size 1518 linear
monitor capture point ip cef POINT GigabitEthernet0/1 both
monitor capture point associate POINT BUF
monitor capture point start POINT
monitor capture point stop POINT
show monitor capture buffer BUF dump
no monitor capture point ip cef POINT GigabitEthernet0/1 both
no monitor capture buffer BUF

! ICMP Connectivity Verification
ping [ip-address]
ping ip [ip-address] repeat 100 timeout 1 size 1400
traceroute [ip-address]
```

---

## 7. Error Indicators & Parsing Gotchas

### Common CLI Errors
| Output Pattern | Cause | Meaning |
|---|---|---|
| `% Invalid input detected at '^' marker.` | Syntax error | Character at caret `^` is unparsed or illegal in current mode. |
| `% Incomplete command.` | Missing arguments | Command requires more parameters before pressing Enter. |
| `% Ambiguous command: "sh int"` | Ambiguity | Multiple valid commands match this prefix. |
| `% Bad IP address or host name` | DNS / Typo | Entered token was evaluated as an unresolved hostname. |
| `% Authorization failed.` | AAA / Privilege | User account lacks privilege level (1-15) for command. |
| `% Command rejected: ...` | Hardware constraint | Feature unsupported on this port type or VLAN limit reached. |
| `Line has invalid autocommand ...` | Config error | Autocommand configured on line failed to execute. |

### Banner & Prompt Traps
1. **MOTD / Login Banners:** Devices often display extensive text banners ending in characters like `#` or `>` before the real prompt. Automated parsers must match only lines ending strictly with `<prompt-name>[>#]`.
2. **Double Prompt Echo on Serial Ports:** When sending `\r\n` over serial, IOS frequently echoes the current prompt twice (`SW1>\r\nSW1>`). Parsers must strip repeated trailing prompts.
3. **`--More--` Line Clipping:** When scraping screens, terminal output with length > 0 pauses after 24 lines, appending `--More--`. Always execute `terminal length 0` first.
4. **Command Echo:** By default, IOS consoles echo back the command sent before printing output. Strip the first line of the buffer if it matches the sent command.
