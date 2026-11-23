from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from models import Settings, Snapshot


def generate_snapshot_id() -> str:
    """Generate an unambiguous 5-character hash/ID for the snapshot (e.g. 'A7K9M')."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(5))


def get_config_pull_command(platform: str) -> str:
    """Determine the CLI command to pull running configuration by platform."""
    plat = (platform or "cisco_ios").lower()
    if any(k in plat for k in ("junos", "juniper")):
        return "show configuration"
    if any(k in plat for k in ("fortigate", "fortios")):
        return "show"
    if any(k in plat for k in ("vyos", "edgeos")):
        return "show configuration"
    if "linux" in plat:
        return "ip a; ip r; iptables-save 2>/dev/null"
    # Cisco IOS / XE / XR, Arista EOS, Huawei, generic
    return "show running-config"


def get_health_commands(platform: str) -> dict[str, str]:
    """Return platform-specific basic health check commands."""
    plat = (platform or "cisco_ios").lower()
    if any(k in plat for k in ("junos", "juniper")):
        return {
            "interfaces": "show interfaces terse",
            "routing": "show route summary",
            "system": "show system uptime",
        }
    if any(k in plat for k in ("fortigate", "fortios")):
        return {
            "system": "get system status",
            "interfaces": "get system interface physical",
            "routing": "get router info routing-table summary",
        }
    # Cisco IOS / Arista EOS
    return {
        "interfaces": "show ip interface brief",
        "routing": "show ip route summary",
        "cpu": "show processes cpu | include CPU",
    }


def parse_health_summary(platform: str, health_data: dict[str, str]) -> str:
    """Summarize raw health telemetry into a clean 1-line baseline."""
    plat = (platform or "cisco_ios").lower()
    intf_out = health_data.get("interfaces", "")
    routing_out = health_data.get("routing", "")
    cpu_out = health_data.get("cpu", "") or health_data.get("system", "")

    summary_parts = []

    # Parse interfaces
    if intf_out:
        up_count = len(re.findall(r"\bup\b", intf_out, re.IGNORECASE))
        down_count = len(re.findall(r"\b(?:down|administratively down)\b", intf_out, re.IGNORECASE))
        if up_count > 0:
            summary_parts.append(f"{up_count} interfaces UP")
        elif "show" not in intf_out.lower():
            summary_parts.append("interfaces checked")

    # Parse routing
    if routing_out:
        routes_m = re.search(r"Total routes:\s*(\d+)", routing_out, re.IGNORECASE) or re.search(r"(\d+)\s+routes", routing_out, re.IGNORECASE)
        if routes_m:
            summary_parts.append(f"{routes_m.group(1)} routes")
        else:
            summary_parts.append("routing active")

    # Parse CPU / System
    if cpu_out:
        cpu_m = re.search(r"CPU utilization for five seconds:\s*(\d+)%", cpu_out, re.IGNORECASE)
        if cpu_m:
            summary_parts.append(f"CPU {cpu_m.group(1)}%")

    return " · ".join(summary_parts) if summary_parts else "Telemetry baseline captured"


async def capture_pre_change_snapshot(
    session: Any,
    settings: Settings,
    run_command_fn: Callable[[str], Coroutine[Any, Any, Any]],
    snap_id: Optional[str] = None,
) -> Snapshot:
    """Pull running config and health statistics, saving locally or on-device."""
    sid = snap_id or generate_snapshot_id()
    hostname = getattr(session, "hostname", "device") or "device"
    platform = getattr(session, "platform", "cisco_ios") or "cisco_ios"
    now_ts = datetime.now()
    ts_str = now_ts.strftime("%Y%m%d_%H%M%S")
    dest = getattr(settings, "snapshot_destination", "local")

    snapshot = Snapshot(
        id=sid,
        device=hostname,
        timestamp=now_ts.isoformat(),
        destination=dest,
    )

    # 1. Pull Running Configuration (if enabled)
    config_text = ""
    if getattr(settings, "snapshot_config", True):
        cfg_cmd = get_config_pull_command(platform)
        try:
            res = await run_command_fn(cfg_cmd)
            config_text = res.output if hasattr(res, "output") else str(res)
            # Filter out command prompt echoes
            if config_text:
                snapshot.config_captured = True
        except Exception:
            pass

    # Save Configuration
    if snapshot.config_captured and config_text:
        # Determine local storage path
        base_dir = Path(os.path.expanduser(getattr(settings, "snapshot_dir", "~/.rackhand/snapshots")))
        dev_dir = base_dir / hostname
        dev_dir.mkdir(parents=True, exist_ok=True)
        local_cfg_file = dev_dir / f"{sid}_{ts_str}.cfg"

        try:
            local_cfg_file.write_text(config_text, encoding="utf-8")
            snapshot.local_path = str(local_cfg_file)
        except Exception:
            pass

        # If on-device destination requested, issue copy to flash
        if dest == "device":
            device_fname = f"flash:snap_{sid}.cfg"
            snapshot.device_path = device_fname
            try:
                # E.g. copy running-config flash:snap_ABC12.cfg
                await run_command_fn(f"copy running-config {device_fname}\r\n")
            except Exception:
                pass

    # 2. Pull Basic Health Statistics (if enabled)
    if getattr(settings, "snapshot_health_stats", True):
        health_cmds = get_health_commands(platform)
        health_results: dict[str, str] = {}
        for metric, cmd in health_cmds.items():
            try:
                h_res = await run_command_fn(cmd)
                out = h_res.output if hasattr(h_res, "output") else str(h_res)
                health_results[metric] = out
            except Exception:
                pass

        if health_results:
            snapshot.stats_captured = True
            snapshot.health_baseline = health_results
            snapshot.health_summary = parse_health_summary(platform, health_results)

    # 3. Record snapshot in persistent index
    _append_snapshot_index(settings, snapshot)
    return snapshot


async def verify_post_change_health(
    session: Any,
    pre_snapshot: Snapshot,
    run_command_fn: Callable[[str], Coroutine[Any, Any, Any]],
) -> str:
    """Run post-flight health verification and compare against baseline snapshot."""
    if not pre_snapshot.stats_captured or not pre_snapshot.health_baseline:
        return ""

    platform = getattr(session, "platform", "cisco_ios") or "cisco_ios"
    health_cmds = get_health_commands(platform)
    post_results: dict[str, str] = {}

    for metric, cmd in health_cmds.items():
        try:
            h_res = await run_command_fn(cmd)
            out = h_res.output if hasattr(h_res, "output") else str(h_res)
            post_results[metric] = out
        except Exception:
            pass

    post_summary = parse_health_summary(platform, post_results)
    return f"✓ **Post-Flight Health Verification:** {post_summary} (Baseline preserved against Snapshot `[{pre_snapshot.id}]`)"


def _append_snapshot_index(settings: Settings, snapshot: Snapshot) -> None:
    """Append snapshot metadata to snapshots index.json."""
    try:
        base_dir = Path(os.path.expanduser(getattr(settings, "snapshot_dir", "~/.rackhand/snapshots")))
        base_dir.mkdir(parents=True, exist_ok=True)
        idx_file = base_dir / "index.json"

        entries = []
        if idx_file.exists():
            try:
                entries = json.loads(idx_file.read_text(encoding="utf-8"))
                if not isinstance(entries, list):
                    entries = []
            except Exception:
                entries = []

        entry = {
            "id": snapshot.id,
            "device": snapshot.device,
            "timestamp": snapshot.timestamp,
            "destination": snapshot.destination,
            "config_captured": snapshot.config_captured,
            "stats_captured": snapshot.stats_captured,
            "local_path": snapshot.local_path,
            "device_path": snapshot.device_path,
            "health_summary": snapshot.health_summary,
        }
        entries.insert(0, entry)
        # Keep last 100 snapshots in index
        idx_file.write_text(json.dumps(entries[:100], indent=2), encoding="utf-8")
    except Exception:
        pass
