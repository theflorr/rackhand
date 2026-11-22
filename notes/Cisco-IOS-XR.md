# Cisco IOS-XR (Carrier-Grade Operating System) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
Cisco IOS-XR is a carrier-grade, highly modular network operating system designed for service provider backbones, core routing, and large-scale metro/WAN networks. 
* **Microkernel Architecture:** Historically built on QNX Neutrino; modern 64-bit releases (eXR / IOS-XR 7) run a hardened 64-bit Linux kernel (Yocto).
* **Distributed Multiprocessing:** Control processes are distributed across Route Processors (RP/RSP) and Line Cards (LC). A crash in one protocol daemon (e.g., BGP) restarts in milliseconds without impacting hardware forwarding.
* **Two-Stage Configuration Engine:** Configuration edits NEVER apply immediately to the running system. Edits populate an isolated candidate configuration buffer that must be validated, diffed, and atomically committed.
* **Calvados / Admin Plane:** A distinct system administration layer managing chassis hardware, cooling, fabric, and VM orchestration.

### Target Platforms & Hardware
* **Core & Edge Routers:** Cisco ASR 9000 series (9001, 9901, 9006, 9010, 9904, 9912, 9922).
* **Mass-Scale Infrastructure:** Cisco 8000 Series (Silicon One ASICs).
* **Carrier Ethernet & Aggregation:** NCS 5500, NCS 540, NCS 560 series.
* **Virtual Appliances:** Cisco XRv, XRv9000.

---

## 2. CLI Modes, Prompts & Navigation

IOS-XR prompts encode the physical hardware location of the active CPU: `RP/Slot/Module/CPU:Hostname#`.

```
User/Privileged EXEC Mode (RP/0/RP0/CPU0:Router#)
  │
  ├── Candidate Configuration Mode (RP/0/RP0/CPU0:Router(config)#)
  │     ├── Interface Configuration (config-if)#
  │     ├── Route Policy Language (config-rpl)#
  │     └── Router Protocol Configuration (config-bgp)#
  │
  ├── Calvados / System Admin Mode (sysadmin-vm:0_RP0# or (admin)#)
  └── Linux Host / Bash Shell (bash)
```

### Prompt Patterns & Regex Matching
| Mode | Standard Prompt Example | Regex Pattern |
|---|---|---|
| **EXEC Mode** | `RP/0/RSP0/CPU0:ASR9K#` | `^RP\/[0-9A-Za-z_\/]+:[A-Za-z0-9_\-\.\/]+#$` |
| **Config Mode** | `RP/0/RSP0/CPU0:ASR9K(config)#` | `^RP\/[0-9A-Za-z_\/]+:[A-Za-z0-9_\-\.\/]+\(config.*\)#$` |
| **Admin EXEC** | `sysadmin-vm:0_RP0#` or `...:ASR9K(admin)#` | `^(?:sysadmin-[A-Za-z0-9_:]+|.*\(admin\))#$` |
| **Bash Shell** | `[xr-vm_node0_RP0_CPU0:~]$ ` | `^\[.*@?.*\][\$#]\s*$` |

### 4-Tuple Interface Naming Notation
Interfaces on IOS-XR are designated by a four-tuple coordinate: `Rack/Slot/Module/Port`:
* `GigabitEthernet0/0/0/1`
* `TenGigE0/1/0/0`
* `HundredGigE0/2/0/0`
* `FourHundredGigE0/0/0/0`
* Aggregated links: `Bundle-Ether1`

---

## 3. Terminal Control & Session Setup

```cisco
! Disable interactive pagination
terminal length 0

! Disable terminal width truncation
terminal width 511

! Abort command without confirmation
abort

! Set console/VTY session timeout
configure
 line default
  exec-timeout 0 0
 commit
```

---

## 4. Two-Stage Configuration & Commit Paradigm

### Workflow
1. Enter configuration mode: `configure` or `configure exclusive` (locks candidate configuration against other users).
2. Input changes (stored in private candidate configuration buffer).
3. Inspect pending diff: `show commit changes diff`.
4. Validate changes: `validate`.
5. Atomic Commit: `commit`.
6. Discard changes without applying: `abort`.

```cisco
! Enter candidate configuration
configure terminal

! View pending uncommitted changes in unified diff format
show configuration
show commit changes diff

! Commit candidate configuration with a descriptive label
commit comment "Configuring BGP peer 10.0.0.1"

! Safe Commit with Confirmation (Reverts automatically if disconnected)
commit confirmed 5
! Once connectivity is verified:
commit

! Discard pending changes and exit config mode
abort
```

### Rollback Engine (Commit History)
Every successful commit in IOS-XR generates an incremental Commit ID (e.g. `1000000024`):
```cisco
! View commit history and comments
show configuration commit list
show configuration commit list 10 detail

! Inspect configuration diff introduced by a specific commit
show configuration commit changes [commit-id]

! Roll back to an earlier commit ID
rollback configuration to [commit-id]

! Roll back the last N commits
rollback configuration last 1

! Replace entire running config with a clean backup file
commit replace
```

---

## 5. Comprehensive CLI Command Reference

### A. System, Hardware & Platform State
```cisco
! Software version, active packages, eXR build
show version

! Line card, fabric card, and RP state (IOS-XR status: IOS XR RUN / OPERATIONAL)
show platform

! Hardware inventory, serial numbers, optical transceivers
show inventory
admin show inventory

! Environmental telemetry (fans, power, temperature)
admin show environment
admin show environment power

! CPU utilization across processes and line cards
show processes cpu
show processes cpu location 0/RP0/CPU0
show processes cpu location 0/0/CPU0

! Memory consumption per node
show memory summary location all
show memory summary location 0/RP0/CPU0

! System log buffer
show logging
```

### B. Interface Status & Physical Layer
```cisco
! Interface operational status, IP addresses, MTU
show ip interface brief
show ipv4 interface brief
show interfaces summary

! Detailed interface statistics, bit rate, packet drops, errors
show interfaces TenGigE0/0/0/0

! Brief interface descriptions
show interfaces description

! Transceiver optic diagnostics (Tx/Rx dBm optical levels, DOM)
show controllers TenGigE0/0/0/0 phy
show controllers HundredGigE0/1/0/0 ddm

! Bundle-Ethernet / LACP operational state
show bundle Bundle-Ether1
show bundle reasons
show lacp bundle-ether 1
```

### C. Layer 3: Routing, VRFs, ARP, BGP & RPL
```cisco
! VRF instances
show vrf all

! IPv4 / IPv6 Routing Tables
show route
show route vrf [vrf-name]
show route summary

! ARP cache
show arp
show arp vrf all

! Route Policy Language (RPL) inspection
show rpl route-policy
show rpl route-policy [policy-name]

! BGP State
show bgp summary
show bgp all summary
show bgp vrf all summary
show bgp ipv4 unicast
show bgp ipv4 unicast summary
show bgp ipv4 unicast neighbors [ip] advertised-routes
show bgp ipv4 unicast neighbors [ip] routes

! IS-IS / OSPF State
show isis neighbors
show isis adjacency
show isis database
show ospf neighbor
show ospf database
```

### D. MPLS, Segment Routing & EVPN
```cisco
! MPLS forwarding table (LFIB)
show mpls forwarding
show mpls ldp neighbor

! Segment Routing (SR-MPLS / SRv6)
show segment-routing mapping-server
show segment-routing traffic-eng policy
show isis segment-routing label-table

! EVPN L2VPN status
show evpn evi
show evpn mac
show evpn internal-label
```

### E. Diagnostics & Packet Monitoring
```cisco
! Connectivity testing
ping [ip-address]
traceroute [ip-address]

! Access-lists and hit counters
show access-lists
show access-lists ipv4 [acl-name]

! Process restart (recover hung daemon without reboot)
process restart bgp location 0/RP0/CPU0
process restart ospf location 0/RP0/CPU0
```

---

## 6. Error Indicators & Parsing Gotchas

| Error Pattern | Cause | Meaning / Action |
|---|---|---|
| `Failed to commit one or more configuration items` | Semantic error | The candidate configuration contains conflicting or invalid directives. |
| `% Configuration failed validation:` | Validation error | A semantic dependency (e.g., referenced route-policy does not exist) failed. |
| `Configuration database is locked by user:` | Lock contention | Another user has opened `configure exclusive`. |
| `% Incomplete command at '^' marker` | Syntax error | Positional argument missing. |
| `Node 0/0/CPU0 is not accessible` | Card down | The targeted Line Card is powered down or in boot state. |

### Parsing Gotchas on IOS-XR
1. **Uncommitted Changes Ignored:** Issuing configuration commands without `commit` leaves them in candidate memory. When scripting, you MUST verify the return value of `commit`.
2. **Prompts Change with Configuration Sub-levels:**
   * Global: `RP/0/RP0/CPU0:Router(config)#`
   * BGP: `RP/0/RP0/CPU0:Router(config-bgp)#`
   * Route Policy: `RP/0/RP0/CPU0:Router(config-rpl)#`
3. **Commit ID Tracking:** After a `commit`, capture the generated commit string (`Commit 1000000045 successfully executed`) to enable deterministic rollbacks.
