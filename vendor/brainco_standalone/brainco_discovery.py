"""Bounded Windows LAN fallback when the cap's mDNS is unavailable.

A TCP listener is only a candidate. The normal SDK/CRC/32-channel preflight
must still validate it before acquisition. Never send configuration probes.
"""
from concurrent.futures import ThreadPoolExecutor
from ipaddress import ip_interface, ip_network
import json
import logging
import os
import socket
import subprocess

LOGGER = logging.getLogger(__name__)
PORT = 53129
PRIVATE = tuple(ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))


def active_interfaces():
    if os.name != 'nt':
        return []
    # Exclude disconnected/virtual adapters at the source; stale Windows IPv4
    # addresses can otherwise point to entirely unrelated networks.
    command = r"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ErrorActionPreference = 'Stop'
$activeIds = @(Get-NetAdapter -Physical | Where-Object Status -eq 'Up' | Select-Object -ExpandProperty ifIndex)
@((Get-NetIPAddress -AddressFamily IPv4) | Where-Object {
    $_.InterfaceIndex -in $activeIds -and $_.AddressState -eq 'Preferred'
} | Select-Object IPAddress,PrefixLength) | ConvertTo-Json -Compress
"""
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                            capture_output=True, text=True, encoding='utf-8', timeout=12,
                            creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    value = json.loads(result.stdout.strip() or '[]')
    return [value] if isinstance(value, dict) else value


def candidate_addresses(interfaces):
    networks = set()
    local = set()
    for row in interfaces:
        try:
            interface = ip_interface(f"{row['IPAddress']}/{row['PrefixLength']}")
        except (KeyError, ValueError, TypeError):
            continue
        if interface.version != 4:
            continue
        local.add(interface.ip)
        network = interface.network
        # Device/router LANs only, at most 254 hosts per interface. Do not
        # expand a company /21 or a VPN/link-local range into an address sweep.
        if 24 <= network.prefixlen <= 30 and any(network.subnet_of(n) for n in PRIVATE):
            networks.add(network)
    if len(networks) > 4:
        raise RuntimeError('设备候选网段超过 4 个，请手动填写脑电帽 IP/端口。')
    return [str(address) for network in sorted(networks) for address in network.hosts()
            if address not in local]


def port_open(address):
    try:
        with socket.create_connection((address, PORT), timeout=.3):
            return True
    except OSError:
        return False


def discover_lan():
    try:
        addresses = candidate_addresses(active_interfaces())
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RuntimeError('读取本机网络失败，请重试或手动填写脑电帽 IP/端口。') from exc
    if not addresses:
        return None
    with ThreadPoolExecutor(max_workers=64, thread_name_prefix='brainco-discovery') as pool:
        matches = [address for address, found in zip(addresses, pool.map(port_open, addresses)) if found]
    if len(matches) > 1:
        raise RuntimeError('发现多个脑电设备候选地址，请关闭自动发现并选择本机使用的 IP：'
                           + ', '.join(f'{address}:{PORT}' for address in matches))
    if not matches:
        return None
    LOGGER.info('BrainCo LAN candidate %s:%s; protocol verification still required', matches[0], PORT)
    return matches[0], PORT
