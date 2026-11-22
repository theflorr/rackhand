# FortiAP (Fortinet Wireless Access Point) CLI Reference

## 1. Operating System & Platform Architecture

### Overview
FortiAP-OS is Fortinet's specialized wireless access point firmware designed for enterprise Wi-Fi environments. FortiAPs are engineered primarily to operate as **thin access points** managed centrally via the **CAPWAP (Control and Provisioning of Wireless Access Points)** protocol (UDP ports 5246 Control, 5247 Data):
* **Centralized Controller Mode (Standard):** The FortiAP establishes a secure DTLS-encrypted CAPWAP tunnel to a FortiGate firewall acting as the Wireless LAN Controller (WLC) or to **FortiLAN Cloud**. All SSIDs, VLAN tags, radio profiles, transmit power, and security keys are pushed from the controller.
* **Local CLI Functionality:** The local FortiAP CLI (accessed via Serial Console RJ-45 or local SSH) is used for:
  * Static IP bootstrapping and AC (Access Controller) discovery configuration.
  * CAPWAP tunnel inspection, state verification, and keepalive monitoring.
  * Radio RF telemetry (noise floor, channel utilization, client RSSI/SNR).
  * Firmware TFTP recovery and hardware diagnostics.

### Target Platforms & Hardware
* **Wi-Fi 7 (802.11be):** FortiAP 441K, 443K.
* **Wi-Fi 6 / 6E (802.11ax):** FortiAP 231F, 233F, 431F, 433F, 831F; FortiAP-U (Universal) 432F.
* **Wi-Fi 5 (802.11ac Wave 2):** FortiAP 221E, 223E, 421E, 423E.
* **Outdoor & Industrial:** FortiAP 234F, 432F (Weatherproof IP67 enclosures).

---

## 2. CLI Modes & Command Paradigms

FortiAP firmware exposes two command styles depending on hardware generation and firmware version:
1. **Classic Low-Level Bootstrapping Commands (`cfg` engine):** Used on all hardware generations for direct NVRAM controller bootstrapping.
2. **Modern FortiOS-Style Interactive Commands (`get / diagnose / execute`):** Implemented in FortiAP 7.x firmware.

### Prompt Patterns & Regex Matching
| Environment | Standard Prompt Example | Regex Pattern |
|---|---|---|
| **FortiAP CLI** | `FAP231F-1 # ` or `FortiAP # ` | `^[A-Za-z0-9_\-\.]+\s*#\s*$` |
| **Bootloader (U-Boot)** | `FortiAP-231F # ` | `^FortiAP.*#\s*$` |

---

## 3. Controller Bootstrapping (`cfg` Parameter Engine)

The `cfg` command writes parameters directly to the AP's flash configuration block.

### Setting Controller IP Discovery
```bash
# View current NVRAM bootstrapping configuration
cfg -c

# Set Access Controller (FortiGate) IP addresses manually
cfg -a AC_IPADDR_1=192.168.1.99
cfg -a AC_IPADDR_2=192.168.1.100

# Set static IP addressing on the AP (if DHCP is unavailable)
cfg -a ADDR_MODE=STATIC
cfg -a AP_IPADDR=192.168.1.50
cfg -a AP_NETMASK=255.255.255.0
cfg -a IPGW=192.168.1.1
cfg -a AP_DNS=8.8.8.8

# Switch back to dynamic DHCP addressing
cfg -a ADDR_MODE=DHCP

# Set CAPWAP discovery type (0=Static/Broadcast, 1=DHCP Option 138, 2=DNS, 3=Multicast)
cfg -a AC_DISCOVERY_TYPE=0

# Save changes to non-volatile flash (MANDATORY)
cfg -s
```
> [!IMPORTANT]
> You must run `cfg -s` after executing `cfg -a` commands. If `cfg -s` is omitted, the configuration will revert upon reboot!

---

## 4. Comprehensive CLI Command Reference

### A. System & Hardware Telemetry
```fortios
# Appliance model, firmware version, serial number, uptime, MAC address
fap-get-status
get system status

# CPU, RAM, and system resource utilization
get system performance
cat /proc/meminfo

# Ethernet link status, speed, duplex, and MTU
get system interface
ifconfig eth0
```

### B. CAPWAP Tunnel & Wireless Controller Status
```fortios
# Comprehensive CAPWAP tunnel state and WLC controller connectivity
cw_diag -c status

# View registered Virtual Access Points (VAPs / SSIDs active on radios)
cw_diag -c vap

# View connected wireless stations (clients) associated with the AP
cw_diag -c sta

# View CAPWAP configuration pushed from FortiGate
cw_diag -c config
```

### C. Radio Frequency (RF), Channels & Client Telemetry
```fortios
# List active radios (Radio 1: 2.4GHz, Radio 2: 5GHz, Radio 3: Scanning/6GHz)
wlan_diag status

# Inspect connected client RSSI, SNR, TX/RX rate, and MAC addresses
wlan_diag sta
wlan_diag sta -v

# Real-time RF channel interference, noise floor, and spectral utilization
wlan_diag radio
wlan_diag chnl_util
```

### D. Advanced Troubleshooting & Real-Time Debugging
```fortios
# Enable live CAPWAP discovery and handshake debugging
wdbg capwap 3
# Disable debugging
wdbg capwap 0

# Monitor wireless association, authentication, and 802.11 frames
wdbg wpa 3
wdbg 80211 3
```

### E. System Maintenance & Recovery
```bash
# Soft reboot the access point
reboot

# Restore access point to factory default configuration
factoryreset

# Ping network gateway or controller
ping 192.168.1.1
```

---

## 5. Error Indicators & Parsing Gotchas

| Output Signature | Cause | Meaning / Action |
|---|---|---|
| `AC Discovery: Discovering...` | Network failure | AP cannot reach controller via broadcast, DHCP 138, or static IP. |
| `DTLS session handshake failed` | Certificate error | Time mismatch between FortiGate and FortiAP, or untrusted cert. |
| `CW_STATE_RUN` | Normal operation | CAPWAP tunnel is established and operational. |
| `Not authorized` | Controller pending | FortiGate sees the AP, but it has not been accepted/authorized in GUI. |

### Parsing Traps on FortiAP
1. **`cfg -s` is Required:** Unlike FortiOS where `end` persists configuration, the `cfg` engine requires an explicit `cfg -s` command.
2. **Double Shells:** Some FAP models boot into a restricted CLI shell. Type `sh` or `bash` if privileged root debugging is required.
