#!/usr/bin/env python3
"""
vdcm_update_ip_map.py — Zabbix macro auto-updater for Synamedia vDCM SNMP trap channel name mapping.

Reads the 'vdcm.snmp.ip.map' item value from each vDCM host and automatically
updates the {$SNMP.IP.MAP} host macro so SNMP trap alarms display channel names
instead of raw TS IP addresses.

Usage:
    python3 vdcm_update_ip_map.py

Recommended cron (run every 5 minutes):
    */5 * * * * /usr/bin/python3 /etc/zabbix/scripts/vdcm_update_ip_map.py >> /var/log/vdcm_ip_map.log 2>&1

Requirements:
    pip install requests
"""

import json
import re
import sys
import ssl
import urllib.request

# ── Configuration ──────────────────────────────────────────────────────────────
ZABBIX_URL  = "https://zabbix-01.svc.litv.tv"       # Zabbix frontend URL (no trailing slash)
ZBX_USER    = "Admin"                               # Zabbix username (used when API_TOKEN is empty)
ZBX_PASS    = "w@JGoP9&x75Gt%yf"                   # Zabbix password
API_TOKEN   = ""                                    # Zabbix API token (leave empty to use user/pass)
ITEM_KEY    = "vdcm.snmp.ip.map"                    # Item key that holds the IP map JSON
MACRO_NAME  = "{$SNMP.IP.MAP}"                      # Host macro to create/update
MACRO_MAX   = 2048                                  # Zabbix hostmacro.value column limit
# ───────────────────────────────────────────────────────────────────────────────


def zabbix_api(method: str, params: dict) -> object:
    """Send a JSON-RPC request to the Zabbix API and return the result."""
    payload = {
        "jsonrpc": "2.0",
        "method":  method,
        "params":  params,
        "id":      1,
    }
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {_get_auth_token()}",
    }
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(
            f"{ZABBIX_URL}/api_jsonrpc.php",
            data=json.dumps(payload).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            result = json.load(resp)
    except Exception as exc:
        print(f"[ERROR] Zabbix API request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if "error" in result:
        print(f"[ERROR] Zabbix API error: {result['error']}", file=sys.stderr)
        sys.exit(1)
    return result["result"]


_session_token = None  # type: str | None

def _get_auth_token() -> str:
    """Return API token from config, or login with user/pass to get session token."""
    global _session_token
    if API_TOKEN and API_TOKEN not in ("", "your_api_token_here"):
        return API_TOKEN
    if _session_token:
        return _session_token
    payload = {"jsonrpc":"2.0","method":"user.login","params":{"username":ZBX_USER,"password":ZBX_PASS},"id":1}
    ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
    try:
        req = urllib.request.Request(f"{ZABBIX_URL}/api_jsonrpc.php",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            result = json.load(r)
        if "error" in result:
            print(f"[ERROR] Login failed: {result['error']}", file=sys.stderr); sys.exit(1)
        _session_token = result["result"]
    except Exception as exc:
        print(f"[ERROR] Login request failed: {exc}", file=sys.stderr); sys.exit(1)
    return _session_token


def get_ip_map_items() -> list:
    """Return all monitored items with key vdcm.snmp.ip.map, including host info and latest value."""
    return zabbix_api("item.get", {
        "output":      ["itemid", "hostid", "lastvalue"],
        "filter":      {"key_": ITEM_KEY},
        "selectHosts": ["hostid", "host"],
        "monitored":   True,
    })


def get_existing_macro(hostid: str):
    """Return the hostmacroid of {$SNMP.IP.MAP} on this host, or None if not set."""
    macros = zabbix_api("usermacro.get", {
        "output":  ["hostmacroid", "value"],
        "hostids": hostid,
        "filter":  {"macro": MACRO_NAME},
    })
    return macros[0] if macros else None


def set_macro(hostid: str, value: str, existing) -> None:
    """Create or update the {$SNMP.IP.MAP} host macro."""
    if existing:
        zabbix_api("usermacro.update", {
            "hostmacroid": existing["hostmacroid"],
            "value":   value,
        })
    else:
        zabbix_api("usermacro.create", {
            "hostid": hostid,
            "macro":  MACRO_NAME,
            "value":  value,
        })


def _clean_value(v: str) -> str:
    v = re.sub(r"<[^>]+>", "", v or "").strip()
    m = re.match(r"^Service0?\d+\s*\(([^)]+)\)$", v)
    if m:
        v = m.group(1).strip()
    return v


def _prepare_map(raw_map: dict) -> dict:
    """Clean values and keep only keys the trap parser actually uses (multicast IPs and :port fallbacks)."""
    out = {}
    for k, v in raw_map.items():
        cv = _clean_value(str(v))
        if not cv:
            continue
        if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", k) or re.match(r"^:\d+$", k):
            out[k] = cv
    return out


def _fit_map(m: dict, max_bytes: int) -> dict:
    """Drop entries whose value is duplicated ≥3× until JSON fits max_bytes."""
    m = dict(m)
    while True:
        s = json.dumps(m, ensure_ascii=False, separators=(",", ":"))
        if len(s) <= max_bytes:
            return m
        vcount: dict = {}
        for v in m.values():
            vcount[v] = vcount.get(v, 0) + 1
        drop_key = next((k for k, v in m.items() if vcount[v] >= 3), None)
        if drop_key is None:
            return m
        del m[drop_key]


def main() -> None:
    items = get_ip_map_items()
    if not items:
        print(f"No items found with key '{ITEM_KEY}' — check template import")
        return

    for item in items:
        hostid   = item["hostid"]
        hostname = item["hosts"][0]["host"] if item.get("hosts") else f"hostid={hostid}"
        raw      = item.get("lastvalue", "")

        try:
            ip_map = json.loads(raw)
            if not isinstance(ip_map, dict) or not ip_map:
                print(f"[SKIP]  {hostname}: empty or invalid IP map value")
                continue
        except json.JSONDecodeError:
            print(f"[SKIP]  {hostname}: cannot parse JSON — '{raw[:80]}'")
            continue

        cleaned = _prepare_map(ip_map)
        fitted  = _fit_map(cleaned, MACRO_MAX)
        new_value = json.dumps(fitted, ensure_ascii=False, separators=(",", ":"))
        if len(new_value) > MACRO_MAX:
            print(f"[SKIP]  {hostname}: cleaned map still {len(new_value)} chars > {MACRO_MAX}")
            continue

        existing = get_existing_macro(hostid)
        if existing and existing.get("value") == new_value:
            print(f"[OK]    {hostname}: {MACRO_NAME} already up to date ({len(fitted)} entries)")
            continue

        set_macro(hostid, new_value, existing)
        action = "updated" if existing else "created"
        dropped = len(cleaned) - len(fitted)
        note = f" (dropped {dropped} duplicate entries to fit {MACRO_MAX})" if dropped else ""
        print(f"[UPD]   {hostname}: {MACRO_NAME} {action} with {len(fitted)} entries{note}")


if __name__ == "__main__":
    main()
