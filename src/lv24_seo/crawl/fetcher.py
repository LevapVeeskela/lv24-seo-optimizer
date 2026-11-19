"""HTTP fetcher and RFC 9309 robots.txt policy for the crawler.

Design notes
------------
* Redirects are followed manually so that every hop lands in
  :attr:`~lv24_seo.crawl.models.FetchResult.redirect_chain`. A loop or a chain longer
  than ``max_redirects`` becomes a typed ``error`` instead of an exception: one broken
  URL must never abort a whole crawl.
* Bodies are streamed. Once ``max_bytes`` is exceeded the transfer is abandoned and the
  result is ``error="too_large"``; the crawler never buffers an unbounded page.
* Network failures are data, not exceptions: :meth:`Fetcher.fetch` always returns a
  :class:`FetchResult` with ``status == 0`` and a short stable ``error`` code.
* IPv4 is forced with ``httpx.HTTPTransport(local_address="0.0.0.0")`` for the same
  reason as in :mod:`lv24_seo.core.http`: some VPS images have no IPv6 route, and a host
  that publishes both ``A`` and ``AAAA`` records may otherwise resolve to an unreachable
  IPv6 address first.
* robots.txt follows RFC 9309: the most specific matching group is used (the longest
  user-agent token wins, ``*`` is only a fallback), rules are compared by longest match,
  and ``Allow`` wins a tie. ``*`` and ``$`` wildcards are supported.
"""

from __future__ import annotations

import ipaddress
import math
import re
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from itertools import pairwise
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpx

from lv24_seo.core.errors import INVALID_INPUT, SeoError
from lv24_seo.crawl.models import FetchResult

DEFAULT_USER_AGENT = "lv24-seo/0.1 (+https://github.com/LevapVeeskela/lv24-seo-optimizer)"

_DEFAULT_TIMEOUT = 20.0
_DEFAULT_MAX_REDIRECTS = 5
_DEFAULT_MAX_BYTES = 5_000_000
_ALLOWED_SCHEMES = frozenset({"http", "https"})
_RULE_FIELDS = frozenset({"disallow", "allow", "crawl-delay"})

# Rate limiting: one bounded retry that honours Retry-After. A crawler that hammers a 429 is
# both rude and likely to be blocked for longer than the header asked for.
_MAX_RATE_LIMIT_RETRIES = 1
_DEFAULT_RATE_LIMIT_DELAY = 1.0
_MAX_RETRY_AFTER_SECONDS = 30.0

# robots.txt patterns are attacker-controlled. Matching is linear (no regex backtracking) and
# the pattern is capped so a hostile file cannot burn CPU: consecutive ``*`` collapse to one
# and the body is truncated at a fixed length. Real robots rules are far shorter than the cap.
_MAX_ROBOTS_PATTERN_LENGTH = 1024
_MAX_ROBOTS_PATH_LENGTH = 4096


@dataclass(slots=True)
class RobotsPolicy:
    """Rules that apply to this crawler, already resolved per user-agent."""

    disallow: list[str]
    allow: list[str]
    sitemaps: list[str]
    crawl_delay: float | None = None
    deferred: bool = False


@dataclass(frozen=True, slots=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[tuple[str, str], ...]


class Fetcher:
    """Synchronous page fetcher with manual redirects and a body size cap.

    Outbound policy: every requested URL — the seed and each redirect hop — is
    resolved and its addresses must be public. Loopback, private, link-local,
    multicast, reserved and unspecified addresses (IPv4 and IPv6, including the
    cloud metadata address ``169.254.169.254``) are refused with
    ``error="blocked_private"`` unless ``allow_private`` is set explicitly for
    local-site audits. The default transport connects to the validated IP,
    preserving the original HTTP Host and TLS SNI, so connection setup cannot
    resolve the hostname again to a different address. Custom injected transports
    are trusted adapters: they retain the original URL and must enforce their own
    connection-address policy. Pass a custom ``resolver`` in tests to avoid real
    DNS; it receives a hostname and returns a list of IP strings.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = _DEFAULT_TIMEOUT,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        transport: httpx.BaseTransport | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        allow_private: bool = False,
        resolver: Callable[[str], list[str]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not math.isfinite(timeout) or timeout <= 0:
            raise SeoError(
                code=INVALID_INPUT,
                message="timeout must be a finite number > 0",
                next_action="pass a positive timeout in seconds",
            )
        if max_redirects < 0:
            raise SeoError(
                code=INVALID_INPUT,
                message="max_redirects must be >= 0",
                next_action="pass 0 or a positive redirect budget",
            )
        if max_bytes <= 0:
            raise SeoError(
                code=INVALID_INPUT,
                message="max_bytes must be > 0",
                next_action="pass a positive body size limit",
            )

        self._user_agent = user_agent
        self._max_redirects = max_redirects
        self._max_bytes = max_bytes
        self._allow_private = allow_private
        self._resolver = resolver or _resolve_host
        self._sleep = sleep
        self._request_count = 0
        self._last_end: float | None = None
        self.politeness_delay: float = 0.0
        self._pin_addresses = transport is None

        if transport is None:
            # See the module docstring: many VPS hosts are IPv4-only.
            # The pool sees pinned IP origins. Do not reuse a TLS connection for
            # a different hostname sharing that IP (and therefore different SNI).
            transport = httpx.HTTPTransport(
                local_address="0.0.0.0",  # noqa: S104
                limits=httpx.Limits(max_keepalive_connections=0),
            )

        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": user_agent},
            transport=transport,
            follow_redirects=False,
        )

    @property
    def request_count(self) -> int:
        """HTTP requests issued so far (page fetches, robots.txt, redirect hops)."""
        return self._request_count

    @property
    def max_redirects(self) -> int:
        """Redirect hops followed per page before ``too_many_redirects``."""
        return self._max_redirects

    def fetch(
        self,
        url: str,
        *,
        allow_url: Callable[[str], bool] | None = None,
        max_bytes: int | None = None,
    ) -> FetchResult:
        """GET ``url`` and never raise for network, size or redirect failures.

        ``max_bytes`` overrides the instance cap for one request. Sitemaps use the larger
        :data:`~lv24_seo.crawl.sitemap.MAX_BYTES` budget while ordinary pages keep the
        default.
        """
        limit = self._max_bytes if max_bytes is None else _positive_limit(max_bytes)
        started = time.perf_counter()
        fetched_at = datetime.now(UTC).isoformat()
        chain = [url]
        current = url
        rate_limit_retries = 0

        initial = _http_url(url)
        if initial is None:
            return self._failure(url, current, chain, "invalid_url", started)
        if allow_url is not None and not allow_url(str(initial)):
            return self._failure(url, current, chain, "robots_blocked", started)
        address = self._outbound_address(initial)
        if address is None:
            return self._failure(url, current, chain, "blocked_private", started)
        visited = {str(initial)}

        try:
            while True:
                self._pace()
                self._request_count += 1
                target_url = httpx.URL(current)
                request_url = (
                    target_url.copy_with(host=address) if self._pin_addresses else target_url
                )
                headers = {"Host": target_url.netloc.decode("ascii")}
                extensions = {"sni_hostname": target_url.host}
                with self._client.stream(
                    "GET", request_url, headers=headers, extensions=extensions
                ) as response:
                    if response.status_code == 429 and rate_limit_retries < _MAX_RATE_LIMIT_RETRIES:
                        # Honour Retry-After instead of treating a throttle as a page answer.
                        # The response is closed by the context manager; the retry re-enters.
                        rate_limit_retries += 1
                        self._last_end = time.perf_counter()
                        self._sleep(_retry_after_seconds(response.headers.get("Retry-After")))
                        continue
                    if response.is_redirect:
                        target = _resolve_redirect(current, response.headers["Location"])
                        if target is None:
                            return self._failure(url, current, chain, "invalid_url", started)
                        if allow_url is not None and not allow_url(str(target)):
                            return self._failure(
                                url, str(target), [*chain, str(target)], "robots_blocked", started
                            )
                        address = self._outbound_address(target)
                        if address is None:
                            return self._failure(url, current, chain, "blocked_private", started)
                        key = str(target)
                        if key in visited:
                            return self._failure(url, current, chain, "redirect_loop", started)
                        if len(chain) - 1 >= self._max_redirects:
                            return self._failure(url, current, chain, "too_many_redirects", started)

                        visited.add(key)
                        current = key
                        chain.append(current)
                        self._last_end = time.perf_counter()
                        continue

                    # Capture TLS facts before the body is consumed: httpcore clears the
                    # SSL object's peer certificate once the stream is closed.
                    network_stream = response.extensions.get("network_stream")
                    ssl_object = (
                        network_stream.get_extra_info("ssl_object")
                        if network_stream is not None
                        else None
                    )
                    tls = _tls_info(ssl_object)
                    body = self._read_body(response, limit)
                    if body is None:
                        return self._failure(url, current, chain, "too_large", started)
                    self._last_end = time.perf_counter()
                    return FetchResult(
                        url=url,
                        status=response.status_code,
                        final_url=current,
                        headers=dict(response.headers),
                        body=body,
                        elapsed_ms=_elapsed_ms(started),
                        redirect_chain=list(chain),
                        downgraded=_is_downgrade(chain),
                        tls=tls,
                        set_cookie=response.headers.get_list("set-cookie"),
                        fetched_at=fetched_at,
                    )
        except httpx.TimeoutException:
            return self._failure(url, current, chain, "timeout", started)
        except httpx.InvalidURL:
            return self._failure(url, current, chain, "invalid_url", started)
        except httpx.HTTPError:
            return self._failure(url, current, chain, "connect_error", started)
        finally:
            self._last_end = time.perf_counter()

    def fetch_robots(self, base_url: str) -> RobotsPolicy:
        """Load ``{base_url}/robots.txt``.

        A missing robots.txt (404/other 4xx) allows everything. A 5xx status, a ``429``
        throttle or a network-level failure *defers* the crawl (RFC 9309 §2.3.1: an unavailable
        robots.txt must not fail open), reported via ``RobotsPolicy.deferred``.
        """
        robots_url = f"{base_url.strip().rstrip('/')}/robots.txt"
        result = self.fetch(robots_url)
        if result.status != 200:
            if (
                result.status == 429
                or result.status >= 500
                or (result.status == 0 and result.error != "blocked_private")
            ):
                return RobotsPolicy(disallow=["/"], allow=[], sitemaps=[], deferred=True)
            return RobotsPolicy(disallow=[], allow=[], sitemaps=[])
        text = result.body.decode("utf-8", errors="replace")
        return parse_robots(text, user_agent=self._user_agent)

    def close(self) -> None:
        """Close the underlying connection pool. Safe to call more than once."""
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def _read_body(self, response: httpx.Response, max_bytes: int) -> bytes | None:
        """Return the body, or ``None`` when it exceeds ``max_bytes``."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def _pace(self) -> None:
        """Honor ``politeness_delay`` between requests (robots Crawl-delay)."""
        if self.politeness_delay <= 0:
            return
        if self._last_end is None:
            return
        wait = self.politeness_delay - (time.perf_counter() - self._last_end)
        if wait > 0:
            self._sleep(wait)

    def _outbound_address(self, target: httpx.URL) -> str | None:
        """Return a validated connection address, never a hostname requiring DNS."""
        if self._allow_private:
            return target.host
        host = target.host or ""
        try:
            addresses = self._resolver(host)
        except OSError:
            return None
        if not addresses:
            return None
        approved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw.strip())
            except ValueError:
                return None
            if not address.is_global or address.is_multicast:
                return None
            approved.append(address)
        # Prefer IPv4 for the default IPv4-bound transport.
        approved.sort(key=lambda item: item.version)
        return str(approved[0])

    def _failure(
        self,
        url: str,
        final_url: str,
        chain: list[str],
        error: str,
        started: float,
    ) -> FetchResult:
        return FetchResult(
            url=url,
            status=0,
            final_url=final_url,
            elapsed_ms=_elapsed_ms(started),
            redirect_chain=list(chain),
            error=error,
            fetched_at=datetime.now(UTC).isoformat(),
        )


def parse_robots(text: str, *, user_agent: str = DEFAULT_USER_AGENT) -> RobotsPolicy:
    """Parse robots.txt and pick the rules that apply to ``user_agent``.

    Comments are stripped, an empty ``Disallow:`` means "allow everything", and
    ``Sitemap`` records are collected globally (they are not group-scoped per RFC 9309).
    """
    groups, sitemaps = _parse_groups(text)
    selected = _select_groups(groups, user_agent)

    disallow: list[str] = []
    allow: list[str] = []
    crawl_delay: float | None = None

    for group in selected:
        for field, value in group.rules:
            if not value:
                continue
            if field == "disallow":
                disallow.append(value)
            elif field == "allow":
                allow.append(value)
            elif field == "crawl-delay" and crawl_delay is None:
                crawl_delay = _parse_delay(value)

    return RobotsPolicy(
        disallow=_unique(disallow),
        allow=_unique(allow),
        sitemaps=_unique(sitemaps),
        crawl_delay=crawl_delay,
    )


def is_allowed(policy: RobotsPolicy, path: str) -> bool:
    """Return whether ``path`` may be crawled under ``policy`` (RFC 9309 matching).

    A ``deferred`` policy (robots.txt unreachable with 5xx/network error) allows
    nothing until robots.txt can be fetched: failing open would crawl a site whose
    owner may have disallowed everything. The longest matching pattern wins;
    ``Allow`` wins ties. ``*`` and a trailing ``$`` anchor are supported. An empty
    policy or a path that matches no rule is allowed. A ``Disallow: /`` blocks
    every path, as required by RFC 9309 §2.2.2; only an empty ``Disallow:`` value
    is ignored.
    """
    if policy.deferred:
        return False
    target = _rule_path(path)
    best_length = -1
    allowed = True

    for pattern in policy.disallow:
        if pattern and _matches(pattern, target) and len(pattern) > best_length:
            best_length = len(pattern)
            allowed = False

    for pattern in policy.allow:
        if not pattern or not _matches(pattern, target):
            continue
        length = len(pattern)
        if length > best_length or (length == best_length and not allowed):
            best_length = length
            allowed = True

    return allowed


def _parse_groups(text: str) -> tuple[list[_Group], list[str]]:
    groups: list[_Group] = []
    sitemaps: list[str] = []
    agents: list[str] = []
    rules: list[tuple[str, str]] = []

    for raw_line in text.lstrip("\ufeff").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        field, separator, value = line.partition(":")
        if not separator:
            continue
        name = field.strip().lower()
        payload = value.strip()

        if name == "user-agent":
            if rules:
                groups.append(_Group(agents=tuple(agents), rules=tuple(rules)))
                agents = []
                rules = []
            if payload:
                agents.append(payload)
            continue

        if name == "sitemap":
            if payload:
                sitemaps.append(payload)
            continue

        if name in _RULE_FIELDS and agents:
            rules.append((name, payload))

    if rules:
        groups.append(_Group(agents=tuple(agents), rules=tuple(rules)))

    return groups, sitemaps


def _select_groups(groups: list[_Group], user_agent: str) -> list[_Group]:
    """Pick groups for ``user_agent``: longest matching token, else ``*``."""
    agent = user_agent.lower()
    specific: list[tuple[int, _Group]] = []
    wildcard: list[_Group] = []

    for group in groups:
        matches = [name for name in group.agents if name != "*" and name.lower() in agent]
        if matches:
            specific.append((max(len(name) for name in matches), group))
        elif "*" in group.agents:
            wildcard.append(group)

    if not specific:
        return wildcard

    best = max(length for length, _ in specific)
    return [group for length, group in specific if length == best]


def _parse_delay(value: str) -> float | None:
    try:
        delay = float(value)
    except ValueError:
        return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _rule_path(raw: str) -> str:
    """Normalise a path or absolute URL to the path+query that robots rules match."""
    candidate = raw.strip()
    if "://" in candidate:
        try:
            parts = urlsplit(candidate)
        except ValueError:
            return candidate
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        return path
    if not candidate:
        return "/"
    if not candidate.startswith("/"):
        return f"/{candidate}"
    return candidate


def _matches(pattern: str, path: str) -> bool:
    body, anchored = _robots_pattern(pattern)
    return _glob_match(body, path[:_MAX_ROBOTS_PATH_LENGTH], anchored=anchored)


@lru_cache(maxsize=512)
def _robots_pattern(pattern: str) -> tuple[str, bool]:
    """Normalise a robots pattern for the linear matcher; ``(body, anchored)``.

    ``$`` anchors the end. Consecutive ``*`` are equivalent in glob semantics and collapse to
    one, and the body is truncated to :data:`_MAX_ROBOTS_PATTERN_LENGTH`: both cuts can only
    broaden a prefix rule, so an oversized hostile pattern cannot fail open.
    """
    end_anchor = pattern.endswith("$")
    body = pattern[:-1] if end_anchor else pattern
    collapsed = re.sub(r"\*+", "*", body)
    return collapsed[:_MAX_ROBOTS_PATTERN_LENGTH], end_anchor


def _glob_match(pattern: str, text: str, *, anchored: bool) -> bool:
    """Glob match with ``*`` only; the pattern is a prefix match unless ``anchored``.

    Implemented as an ordered search for the literal segments between wildcards, so the cost
    is ``O(segments * len(text))`` at C speed and never the exponential backtracking a
    ``.*``-per-star regex suffers on a hostile pattern. Greedy earliest matches are optimal
    for ``*``-only patterns because wildcards cannot overlap.
    """
    segments = pattern.split("*")
    position = 0
    if not pattern.startswith("*"):
        head = segments[0]
        if not text.startswith(head):
            return False
        position = len(head)
        segments = segments[1:]
    if not segments:
        # No wildcard at all: the head matched as a prefix; an anchor requires the full text.
        return not anchored or len(text) == position
    for segment in segments[:-1]:
        found = text.find(segment, position)
        if found < 0:
            return False
        position = found + len(segment)
    tail = segments[-1]
    if anchored:
        start = len(text) - len(tail)
        return start >= position and text.endswith(tail)
    return text.find(tail, position) >= 0


def _retry_after_seconds(value: str | None) -> float:
    """Delay requested by ``Retry-After`` (seconds or HTTP-date), bounded and defaulted."""
    if value is None:
        return _DEFAULT_RATE_LIMIT_DELAY
    raw = value.strip()
    if not raw:
        return _DEFAULT_RATE_LIMIT_DELAY
    try:
        seconds = float(raw)
    except ValueError:
        seconds = None
    if seconds is None:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return _DEFAULT_RATE_LIMIT_DELAY
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = when.timestamp() - time.time()
    if not math.isfinite(seconds):
        return _DEFAULT_RATE_LIMIT_DELAY
    return min(max(0.0, seconds), _MAX_RETRY_AFTER_SECONDS)


def _http_url(url: str) -> httpx.URL | None:
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.host:
        return None
    return parsed


def _resolve_redirect(current: str, location: str) -> httpx.URL | None:
    base = _http_url(current)
    if base is None:
        return None
    try:
        target = base.join(location)
    except (httpx.InvalidURL, TypeError, ValueError):
        return None
    if target.scheme not in _ALLOWED_SCHEMES or not target.host:
        return None
    return target


def _resolve_host(host: str) -> list[str]:
    """Resolve ``host`` to IP strings; raises :class:`OSError` when unresolvable."""
    infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    addresses = [str(info[4][0]) for info in infos if info[4] and info[4][0]]
    return addresses


def _is_downgrade(chain: list[str]) -> bool:
    """Return True when the chain steps down from HTTPS to plain HTTP."""
    schemes = [urlsplit(url).scheme.lower() for url in chain]
    return any(first == "https" and second == "http" for first, second in pairwise(schemes))


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _tls_info(ssl_object: object) -> dict[str, Any] | None:
    """Return certificate facts from an ``ssl.SSLObject``, or None for plain HTTP.

    The fetcher only records data the socket already negotiated; it never opens a second
    connection. A certificate that cannot be inspected is omitted instead of failing the
    crawl, and the TLS rule then reports ``tls_unknown`` from the missing value.
    """
    if ssl_object is None:
        return None
    try:
        certificate = ssl_object.getpeercert() or {}  # type: ignore[attr-defined]
        cipher = ssl_object.cipher()  # type: ignore[attr-defined]
        not_after = certificate.get("notAfter")
        not_after_iso: str | None = None
        if isinstance(not_after, str):
            try:
                not_after_iso = datetime.fromtimestamp(
                    ssl.cert_time_to_seconds(not_after), UTC
                ).isoformat()
            except (ValueError, OverflowError):
                not_after_iso = None
        return {
            "version": ssl_object.version(),  # type: ignore[attr-defined]
            "cipher": cipher[0] if isinstance(cipher, tuple) and cipher else None,
            "not_after": not_after_iso,
            "issuer": _certificate_name(certificate.get("issuer")),
            "subject": _certificate_name(certificate.get("subject")),
            "san": _certificate_san(certificate.get("subjectAltName")),
        }
    except Exception:
        return None


def _certificate_name(value: object) -> dict[str, str]:
    if not isinstance(value, (tuple, list)):
        return {}
    names: dict[str, str] = {}
    for relative_name in value:
        if not isinstance(relative_name, (tuple, list)):
            continue
        for pair in relative_name:
            if isinstance(pair, (tuple, list)) and len(pair) == 2 and isinstance(pair[0], str):
                names[pair[0]] = str(pair[1])
    return names


def _certificate_san(value: object) -> list[str]:
    if not isinstance(value, (tuple, list)):
        return []
    return [
        str(entry[1]) for entry in value if isinstance(entry, (tuple, list)) and len(entry) == 2
    ]


def _positive_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SeoError(
            code=INVALID_INPUT,
            message="max_bytes must be > 0",
            next_action="pass a positive body size limit",
        )
    return value
