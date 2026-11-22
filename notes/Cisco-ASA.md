# Cisco ASA (Adaptive Security Appliance) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
Cisco ASA Software is a stateful inspection firewall operating system descended from the Cisco PIX architecture, running as an optimized data-plane engine (`lina`) on top of a hardened Linux microkernel.
* **Stateful Flow Engine:** Every packet is checked against the stateful connection table (`conn`) and XLATE (translation) table.
* **Security Levels (0-100):** Logical interfaces have an assigned security level. By default, traffic initiated from higher security levels to lower security levels is permitted, while traffic initiated from lower to higher is denied unless permitted by an Access Control List.
* **Single vs. Multiple Context Mode:** Supports virtual firewalls (Security Contexts) with partitioned interfaces, routing, and policy tables governed by a System Execution space.
* **Routed vs. Transparent Mode:** Operates as a Layer 3 routing hop or as a Layer 2 bump-in-the-wire transparent firewall.

### Target Platforms & Hardware
* **Hardware Appliances:** ASA 5505, 5510, 5520, 5540, 5550; ASA 5500-X Series (5506-X, 5508-X, 5516-X, 5525-X, 5545-X, 5555-X, 5585-X).
* **Firepower Running ASA Image:** Firepower 1000, 2100, 3100, 4100, 9300 appliances running ASA image.
* **Virtual Appliances:** Cisco ASAv (VMware ESXi, KVM, AWS, Azure, GCP).

---

## 2. CLI Modes, Hierarchies & Navigation

```
User EXEC Mode (ciscoasa>)
  │
  └── Privileged EXEC Mode (ciscoasa#)
        │
        ├── Global Configuration Mode (ciscoasa(config)#)
        │     ├── Interface Configuration (config-if)#
        │     ├── Network / Service Object Configuration (config-network-object)#
        │     └── Crypto / VPN Policy Configuration (config-tunnel-ipsec)#
        │
        └── Multi-Context Switching:
              ├── changeto system
              └── changeto context <context-name>
```

### Prompt Patterns & Regex Matching
| Mode | Standard Prompt | Regex Pattern |
|---|---|---|
| **User EXEC** | `ciscoasa>` | `^[A-Za-z0-9_\-\.\/]+>$` |
| **Privileged EXEC** | `ciscoasa#` | `^[A-Za-z0-9_\-\.\/]+#$` |
| **Global Config** | `ciscoasa(config)#` | `^[A-Za-z0-9_\-\.\/]+\(config\)#$` |
| **Interface Config** | `ciscoasa(config-if)#` | `^[A-Za-z0-9_\-\.\/]+\(config-if\)#$` |
| **System Admin (Multi)**| `ciscoasa/admin#` or `ciscoasa(config-sys)#` | `^[A-Za-z0-9_\-\.\/]+[\/\(].*#$` |
| **Sub-Context** | `ciscoasa/CUSTOMER_A#` | `^[A-Za-z0-9_\-\.\/]+\/[A-Za-z0-9_\-]+#$` |

### Critical Interface Concept: `nameif`
Unlike routers where commands attach to physical interfaces (`GigabitEthernet0/1`), the ASA assigns a logical **`nameif`** (e.g. `inside`, `outside`, `dmz`). Once defined, almost all routing, NAT, ACL, and diagnostic commands bind to the **nameif**, not the hardware port!

```cisco
interface GigabitEthernet1/1
 nameif outside
 security-level 0
 ip address 203.0.113.2 255.255.255.0
 no shutdown
```

---

## 3. Terminal Control & Pagination (Critical ASA Gotcha)

> [!IMPORTANT]
> The ASA does NOT use `terminal length 0`. Using `terminal length 0` will produce an error (`% Invalid input detected at '^' marker`). The ASA uses `terminal pager 0` or `no pager`.

```cisco
! Disable interactive pagination on ASA
terminal pager 0
! Or alternatively:
no pager

! Disable terminal width wrapping
terminal width 511

! Disable domain lookups on command typos
no ip domain-lookup
```

---

## 4. Configuration Lifecycle & Persistence

The ASA uses the immediate running-config mutation model:
```cisco
! Save running configuration to flash startup-config
write memory
! Or:
copy running-config startup-config

! Display running configuration
show running-config

! Erase configuration
write erase
reload
```

---

## 5. Comprehensive CLI Command Reference

### A. System, Hardware & Failover (HA) Telemetry
```cisco
! Appliance model, ASA software version, uptime, RAM, serial number, activation keys
show version

! Hardware component inventory, SFP modules
show inventory

! Environment telemetry (fans, power supplies, temperature)
show environment

! CPU and memory utilization
show cpu usage
show memory

! Active High Availability / Failover state (Active/Standby or Active/Active)
show failover
show failover state
show failover history

! Active system buffers and crash logs
show logging
show crashinfo
```

### B. Interface Status & Layer 2/3 Physical State
```cisco
! Summary of logical interfaces (nameif), IP addresses, and physical port state
show ip address
show interface ip brief

! Detailed interface statistics, packet counts, errors, collisions, drops
show interface [interface-id]

! Detailed operational switchport or nameif status
show interface [nameif]
```

### C. Stateful Connection Table & NAT Translations
```cisco
! Active stateful connection table (TCP, UDP, ICMP)
show conn
show conn detail
show conn count
show conn address [ip-address]
show conn port [port-number]

! Network Address Translation (NAT / PAT) active translation sessions
show xlate
show xlate count
show xlate detail

! Configured NAT rules (Auto-NAT / Manual Twice-NAT) and hit counters
show nat
show nat detail

! Clearing stuck connections or translation tables
clear conn address [ip-address]
clear xlate
```

### D. Security Policies, ACLs & Drop Statistics
```cisco
! Configured access-lists, rule elements, and hit counters
show access-list
show access-list [acl-name]
show access-list [acl-name] brief

! Accelerated Security Path (ASP) drop counters (Crucial for packet drops)
show asp drop
show asp drop frame
show asp drop flow

! Inspecting service policies and application inspection engines (HTTP, DNS, SIP)
show service-policy
show service-policy inspect
```

### E. Packet-Tracer: Diagnostic Simulation Engine
The ASA features a built-in simulation engine that traces a virtual packet through the entire internal pipeline (Interface input -> ACL -> Route -> NAT -> Connection Creation -> ASP Drop):

```cisco
! Syntax:
! packet-tracer input <ingress-nameif> <protocol> <src-ip> <src-port> <dst-ip> <dst-port> detailed

! Example: Simulating HTTPS traffic from inside host to outside server
packet-tracer input inside tcp 192.168.1.50 49152 8.8.8.8 443 detailed

! Example: Simulating ICMP Ping
packet-tracer input inside icmp 192.168.1.50 8 0 8.8.8.8 detailed
```
* The bottom line indicates: `Action: ALLOW` or `Action: DROP (reason)`.

### F. VPN (IPsec, IKEv2 & AnyConnect SSL)
```cisco
! VPN session database summary and user sessions
show vpn-sessiondb
show vpn-sessiondb anyconnect
show vpn-sessiondb l2l
show vpn-sessiondb summary

! IKEv1 / IKEv2 Security Associations
show crypto isakmp sa
show crypto ikev2 sa
show crypto ikev2 sa detail

! IPsec Phase 2 Security Associations (encaps/decaps counters)
show crypto ipsec sa
show crypto ipsec sa detail
```

### G. Layer 3 Routing & ARP
```cisco
! Routing Table
show route
show route summary

! ARP table
show arp
```

### H. Packet Capture (Built-in PCAP Capture Engine)
```cisco
! Define and start a packet capture on the ASA
access-list CAP_ACL extended permit ip host 192.168.1.50 any
access-list CAP_ACL extended permit ip any host 192.168.1.50
capture MYCAP access-list CAP_ACL interface inside buffer 1000000

! View captured packet summary
show capture MYCAP
show capture MYCAP dump
show capture MYCAP detail

! Stop and remove capture
no capture MYCAP
no access-list CAP_ACL
```

---

## 6. Error Indicators & Parsing Gotchas

| Error Pattern | Cause | Meaning / Action |
|---|---|---|
| `% Invalid input detected at '^' marker` | Syntax error | Invalid command syntax (e.g. trying `terminal length 0`). |
| `ERROR: Interface ... has no nameif` | Missing nameif | Attempted to assign IP or policy to unnamed interface. |
| `ERROR: packet-tracer failed...` | Bad parameter | Ingress nameif or protocol parameter invalid. |
| `[Failover] Switchover failed` | HA error | Standby unit rejected transition due to interface sync failure. |

### Parsing Gotchas on Cisco ASA
1. **`terminal pager 0` Mandatory:** Any script attempting `terminal length 0` will fail. Always use `terminal pager 0`.
2. **`nameif` Case Sensitivity:** Interface names (e.g. `inside`, `Outside`, `DMZ`) are case-sensitive in many ASA versions.
3. **Double Prompts in Multi-Context:** Prompts display the context: `ciscoasa/admin#` or `ciscoasa/context1#`. Ensure regex patterns handle slashes.
