"""Bonjour / DNS-SD advertisement so the printer shows up on the network."""

import logging
import os
import re
import socket

import ifaddr
from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

log = logging.getLogger("mdns")

# Container bridges and VPNs (Tailscale, WireGuard, ZeroTier, OpenVPN) aren't reachable from the LAN.
_SKIP_ADAPTERS = ("docker", "br-", "veth", "virbr", "lo", "tailscale", "wg", "tun", "tap", "zt")
SUBTYPES = ("_universal._sub._ipp._tcp.local.", "_print._sub._ipp._tcp.local.")


def local_ipv4_addresses():
    ips = []
    for adapter in ifaddr.get_adapters():
        if adapter.nice_name.startswith(_SKIP_ADAPTERS):
            continue
        for ip in adapter.ips:
            if ip.is_IPv4 and not ip.ip.startswith(("127.", "169.254.")):
                ips.append(ip.ip)
    return ips


def lan_ipv4_address():
    """The address LAN devices should use: ADVERTISE_IP, else the default-route interface's address."""
    if ip := os.environ.get("ADVERTISE_IP"):
        return ip
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this just picks the outbound interface
            return s.getsockname()[0]
        except OSError:
            pass
    ips = local_ipv4_addresses()
    return ips[0] if ips else None


class Advertiser:
    def __init__(self, name, port, txt, raw_port=0):
        self.name = name
        self.port = port
        self.raw_port = raw_port
        self.txt = txt
        self.zc = None
        self.infos = []

    async def start(self):
        lan_ip = lan_ipv4_address()
        if not lan_ip:
            log.warning("no LAN IPv4 address found; skipping mDNS advertisement")
            return
        addresses = [lan_ip]
        host = re.sub(r"[^a-z0-9-]+", "-", self.name.lower()).strip("-") or "fake-printer"
        self.zc = AsyncZeroconf(interfaces=[lan_ip], ip_version=IPVersion.V4Only)
        packed = [socket.inet_aton(a) for a in addresses]
        info = ServiceInfo("_ipp._tcp.local.", f"{self.name}._ipp._tcp.local.", addresses=packed,
                           port=self.port, properties=self.txt, server=f"{host}.local.")
        await self.zc.async_register_service(info, allow_name_change=True)
        self.infos.append(info)

        if self.raw_port:
            # Same identity (UUID, ty, usb_MFG/MDL) as the IPP record, like real printers, so CUPS
            # treats both as one printer; the higher priority makes it prefer the IPP record.
            raw_txt = {k: v for k, v in self.txt.items() if k not in ("rp", "URF", "kind")}
            raw_txt.update(priority="75",
                           pdl="application/pdf,application/postscript,application/vnd.zebra-zpl,text/plain")
            raw = ServiceInfo("_pdl-datastream._tcp.local.", f"{self.name}._pdl-datastream._tcp.local.",
                              addresses=packed, port=self.raw_port, server=info.server, properties=raw_txt)
            await self.zc.async_register_service(raw, allow_name_change=True)
            self.infos.append(raw)

        # python-zeroconf has no subtype API. Indexing a copy of the service under each
        # subtype makes it answer PTR queries for them, which AirPrint (iOS) browses for.
        try:
            for subtype in SUBTYPES:
                sub = ServiceInfo(subtype, info.name, addresses=packed, port=self.port,
                                  properties=self.txt, server=info.server)
                self.zc.zeroconf.registry.types.setdefault(subtype, {})[sub.key] = sub
        except Exception as e:
            log.warning("could not advertise mDNS subtypes (AirPrint discovery may not work): %s", e)
        log.info("advertising '%s' via mDNS on %s port %d", self.name, ", ".join(addresses), self.port)

    async def stop(self):
        if self.zc:
            for subtype in SUBTYPES:
                self.zc.zeroconf.registry.types.pop(subtype, None)
            for info in self.infos:
                await self.zc.async_unregister_service(info)
            await self.zc.async_close()
