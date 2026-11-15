"""The outbound gateway: every connection Kurai opens to the internet goes through here.

One proxy (SOCKS5, SOCKS5 with names resolved by the proxy, or HTTP), an optional DNS server, and a checkbox per
subsystem. With its checkbox on, a subsystem never falls back to a direct connection: when the proxy is down, it fails
with a clear error. No other module creates HTTP clients for the internet (local services on the compose network are
reached with `local_session`).
"""

import ipaddress
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp_socks import ProxyConnector

from kurai import settings


@dataclass(frozen=True)
class Route:
    """How one subsystem reaches the internet right now."""

    proxy: str | None  # full proxy URL with credentials, None: direct
    dns: str | None  # "ip[:port]" of the DNS server to use, None: the system's

    @property
    def via_proxy(self) -> bool:
        return self.proxy is not None


def _with_credentials(url: str, user: str, password: str) -> str:
    if not user:
        return url
    parts = urlsplit(url)
    netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return parts._replace(netloc=netloc).geturl()


def route_from(values: dict[str, Any], subsystem: str) -> Route:
    if subsystem not in settings.outbound_subsystems():
        raise KeyError(subsystem)
    dns = (values.get("outbound.dns") or "").strip() or None
    proxy = (values.get("outbound.proxy") or "").strip()
    if not proxy or not values.get(f"outbound.via.{subsystem}"):
        return Route(None, dns)
    return Route(_with_credentials(proxy, values.get("outbound.user", ""), values.get("outbound.password", "")), dns)


def parse_dns(dns: str) -> tuple[str, int | None]:
    """"10.8.0.1", "10.8.0.1:5353", "fd00::1", "[fd00::1]:53" -> (address, port or None). Only IP addresses: a name
    would need a DNS server to find the DNS server."""
    dns = dns.strip()
    if dns.startswith("["):
        host, _, rest = dns[1:].partition("]")
        port = rest.removeprefix(":") or None
    elif dns.count(":") == 1:
        host, port = dns.split(":")
    else:
        host, port = dns, None
    ipaddress.ip_address(host)  # ValueError for a name or garbage
    return host, int(port) if port else None


def _resolver(dns: str) -> aiohttp.abc.AbstractResolver:
    host, port = parse_dns(dns)
    kwargs: dict[str, Any] = {"nameservers": [host]}
    if port:
        kwargs.update(udp_port=port, tcp_port=port)
    # no fallback to the system's resolver: with a VPN's DNS server, going around it would leave the tunnel
    return aiohttp.AsyncResolver(**kwargs)


def session_for_route(route: Route, timeout: float = 60) -> aiohttp.ClientSession:
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    resolver = _resolver(route.dns) if route.dns else None
    if route.proxy:
        scheme = urlsplit(route.proxy).scheme
        if scheme.startswith("socks"):
            # socks5h: the proxy resolves names (rdns); socks5: they're resolved here
            url = route.proxy.replace("socks5h://", "socks5://", 1)
            connector = ProxyConnector.from_url(url, rdns=scheme == "socks5h", resolver=resolver)
            return aiohttp.ClientSession(connector=connector, timeout=client_timeout)
        connector = aiohttp.TCPConnector(resolver=resolver) if resolver else None
        return aiohttp.ClientSession(connector=connector, timeout=client_timeout, proxy=route.proxy)
    connector = aiohttp.TCPConnector(resolver=resolver) if resolver else None
    return aiohttp.ClientSession(connector=connector, timeout=client_timeout)


async def session_for(db, subsystem: str, timeout: float = 60) -> aiohttp.ClientSession:
    """A session for one subsystem, by the current settings. Close it (async with)."""
    return session_for_route(route_from(await settings.get_all(db), subsystem), timeout)


def local_session(timeout: float = 60) -> aiohttp.ClientSession:
    """For the services next to Kurai (Navidrome, the local models, the apps of plugins): never proxied."""
    return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout))


def is_local_url(url: str) -> bool:
    """Local model servers and the compose network are reached directly; only internet hosts use the gateway."""
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost" or "." not in host or host.endswith((".local", ".lan", ".internal")):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False
