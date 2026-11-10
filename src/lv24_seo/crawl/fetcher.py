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

import math
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from types import TracebackType
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


@dataclass(slots=True)
class RobotsPolicy:
    """Rules that apply to this crawler, already resolved per user-agent."""

    disallow: list[str]
    allow: list[str]
    sitemaps: list[str]
    crawl_delay: float | None = None


@dataclass(frozen=True, slots=True)
class _Group:
    agents: tuple[str, ...]
    rules: tuple[tuple[str, str], ...]


class Fetcher:
    """Synchronous page fetcher with manual redirects and a body size cap."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = _DEFAULT_TIMEOUT,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        transport: httpx.BaseTransport | None = None,
        max_bytes: int = _DEFAULT_MAX_BYTES,
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

        if transport is None:
            # See the module docstring: many VPS hosts are IPv4-only.
            transport = httpx.HTTPTransport(local_address="0.0.0.0")  # noqa: S104

        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": user_agent},
            transport=transport,
            follow_redirects=False,
        )

    def fetch(self, url: str) -> FetchResult:
        """GET ``url`` and never raise for network, size or redirect failures."""
        started = time.perf_counter()
        chain = [url]
        current = url

        initial = _http_url(url)
        if initial is None:
            return self._failure(url, current, chain, "invalid_url", started)
        visited = {str(initial)}

        try:
            while True:
                with self._client.stream("GET", current) as response:
                    if response.is_redirect:
                        target = _resolve_redirect(current, response.headers["Location"])
                        if target is None:
                            return self._failure(url, current, chain, "invalid_url", started)
                        key = str(target)
                        if key in visited:
                            return self._failure(url, current, chain, "redirect_loop", started)
                        if len(chain) - 1 >= self._max_redirects:
                            return self._failure(url, current, chain, "too_many_redirects", started)

                        visited.add(key)
                        current = key
                        chain.append(current)
                        continue

                    body = self._read_body(response)
                    if body is None:
                        return self._failure(url, current, chain, "too_large", started)
                    return FetchResult(
                        url=url,
                        status=response.status_code,
                        final_url=current,
                        headers=dict(response.headers),
                        body=body,
                        elapsed_ms=_elapsed_ms(started),
                        redirect_chain=list(chain),
                    )
        except httpx.TimeoutException:
            return self._failure(url, current, chain, "timeout", started)
        except httpx.InvalidURL:
            return self._failure(url, current, chain, "invalid_url", started)
        except httpx.HTTPError:
            return self._failure(url, current, chain, "connect_error", started)

    def fetch_robots(self, base_url: str) -> RobotsPolicy:
        """Load ``{base_url}/robots.txt``; missing or broken robots allows everything."""
        robots_url = f"{base_url.strip().rstrip('/')}/robots.txt"
        result = self.fetch(robots_url)
        if result.status != 200:
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

    def _read_body(self, response: httpx.Response) -> bytes | None:
        """Return the body, or ``None`` when it exceeds ``max_bytes``."""
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_bytes:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

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

    The longest matching pattern wins; ``Allow`` wins ties. ``*`` matches any sequence
    and a trailing ``$`` anchors the pattern to the end of the URL. An empty policy or
    a path that matches no rule is allowed. A ``Disallow: /`` blocks every path, as
    required by RFC 9309 §2.2.2; only an empty ``Disallow:`` value is ignored.
    """
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
    return _rule_regex(pattern).match(path) is not None


@lru_cache(maxsize=512)
def _rule_regex(pattern: str) -> re.Pattern[str]:
    """Translate a robots pattern (``*`` and trailing ``$``) into a regex."""
    end_anchor = pattern.endswith("$")
    body = pattern[:-1] if end_anchor else pattern
    translated = "".join(".*" if char == "*" else re.escape(char) for char in body)
    return re.compile(f"^{translated}{'$' if end_anchor else ''}")


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


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0
