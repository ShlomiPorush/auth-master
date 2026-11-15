"""The passkey address: the one origin that passkeys are created and used at.

WebAuthn binds every passkey to a relying party ID (RP ID), here the host of this origin,
and the browser writes the page's exact origin into every response.  The library compares
that origin with the expected one as text, so the address is kept in one canonical form:
``scheme://host[:port]``, host in lowercase ASCII, without a default port.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

MAX_ORIGIN_LEN = 300
_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_DEFAULT_PORTS = {"https": 443, "http": 80}


class InvalidOrigin(ValueError):
    pass


def canonical_origin(value: str) -> str:
    """The canonical form of a passkey address, or InvalidOrigin with a message for people."""
    if not isinstance(value, str):
        raise InvalidOrigin("Enter the address as text, such as https://auth.example.com.")
    text = value.strip()
    if not text:
        raise InvalidOrigin("Enter an address, such as https://auth.example.com.")
    if len(text) > MAX_ORIGIN_LEN:
        raise InvalidOrigin(f"The address can have at most {MAX_ORIGIN_LEN} characters.")
    if any(ch.isspace() for ch in text) or "?" in text or "#" in text or "\\" in text:
        raise InvalidOrigin("Enter only the address, such as https://auth.example.com, without a query or anchor.")
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise InvalidOrigin("This is not a valid address. Use the form https://auth.example.com.") from None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        raise InvalidOrigin("The address must start with https://.")
    if "@" in parts.netloc:
        raise InvalidOrigin("The address cannot contain a user name or password.")
    if parts.path not in ("", "/"):
        raise InvalidOrigin("Enter only the address, without a path. Passkeys work for the whole host.")
    if "[" in parts.netloc:
        raise InvalidOrigin("Passkeys need a domain name, not an IP address.")
    host = parts.hostname or ""
    if not host:
        raise InvalidOrigin("The address has no host name.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise InvalidOrigin("Passkeys need a domain name, not an IP address.")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise InvalidOrigin("The host name is not a valid domain name.") from None
    labels = host.split(".")
    if len(host) > 253 or not all(_LABEL.match(label) for label in labels):
        raise InvalidOrigin("The host name is not a valid domain name.")
    if labels[-1].isdigit():
        # Such as 10.1, which browsers read as an IPv4 address.
        raise InvalidOrigin("Passkeys need a domain name, not an IP address.")
    if scheme == "http" and host != "localhost":
        raise InvalidOrigin("Passkeys need HTTPS. Use https://, or http://localhost on this computer.")
    if port == 0:
        raise InvalidOrigin("The port must be a number from 1 to 65535.")
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def rp_id_of(origin: str) -> str:
    """The RP ID of a canonical origin: its host."""
    return urlsplit(origin).hostname or ""
