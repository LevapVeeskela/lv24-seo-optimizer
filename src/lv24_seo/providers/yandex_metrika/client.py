"""Yandex Metrika adapter: counter and goal discovery plus ``stat/v1/data`` tables.

The adapter is intentionally thin. It normalises provider rows into stable, flat dictionaries
and reports how complete the answer is (``truncated``, ``sampled``) instead of hiding sampling
behind a number. Metrika's free tier covers every call here, so no paid-provider marker is set.

Limits that shape the contract (see ``docs/data-sources.md``):

* ``/stat/v1/data`` allows 200 requests per 5 minutes; callers are expected to pace themselves.
* ``accuracy="medium"`` is the default because ``full`` is slower and burns the same quota.
* Sampling is exposed through the :data:`~lv24_seo.providers.base.SAMPLED` limitation and the
  ``sampled`` warning, never silently averaged away.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from typing import Any

from lv24_seo.core.errors import AUTH_REQUIRED, INVALID_INPUT, UPSTREAM_UNAVAILABLE, SeoError
from lv24_seo.core.http import HttpClient
from lv24_seo.providers.base import SAMPLED, ProviderResult, data_age_days, now_utc

__all__ = ["YandexMetrikaClient"]

SOURCE = "yandex_metrika"
DEFAULT_BASE_URL = "https://api-metrika.yandex.net"

_LOGIN_NEXT_ACTION = "lv24-seo auth login --provider yandex"
_SESSION_PREFIX = "ym:s:"

Row = dict[str, Any]


class YandexMetrikaClient:
    """Read-only client for the Metrika management and stat APIs.

    The token is validated eagerly: an unconfigured client can never issue an unauthenticated
    call that would only fail later with an opaque 403.
    """

    def __init__(
        self,
        *,
        token: str,
        http: HttpClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        candidate: object = token
        if not isinstance(candidate, str) or not candidate.strip():
            raise SeoError(
                code=AUTH_REQUIRED,
                message="Yandex Metrika OAuth token is not configured",
                next_action=_LOGIN_NEXT_ACTION,
            )
        self._token = candidate.strip()
        self._base_url = base_url.rstrip("/")
        self._http = http if http is not None else HttpClient(base_url=self._base_url)

    def counters(self) -> ProviderResult[Row]:
        """List the counters this token can read.

        Rows carry ``id``, ``name``, ``site`` and ``status`` only; the management payload has
        far more fields and none of them are needed by reports.
        """
        payload = self._get("/management/v1/counters")
        rows: list[Row] = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "site": item.get("site"),
                "status": item.get("status"),
            }
            for item in _payload_rows(payload, "counters")
        ]
        return self._listing(property_id="counters", rows=rows)

    def goals(self, counter: int | str) -> ProviderResult[Row]:
        """List the goals configured for ``counter``."""
        counter_id = _counter_id(counter)
        payload = self._get(f"/management/v1/counter/{counter_id}/goals")
        rows: list[Row] = [
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "type": item.get("type"),
                "is_retargeting": item.get("is_retargeting"),
            }
            for item in _payload_rows(payload, "goals")
        ]
        return self._listing(property_id=counter_id, rows=rows)

    def stat_data(
        self,
        *,
        counter: int | str,
        metrics: Sequence[str],
        date1: date,
        date2: date,
        dimensions: Sequence[str] = (),
        filters: str | None = None,
        accuracy: str = "medium",
        limit: int = 100,
    ) -> ProviderResult[Row]:
        """Read one ``stat/v1/data`` table.

        Row keys are the requested dimension and metric names with the session prefix removed
        (``ym:s:startURLPath`` -> ``startURLPath``, ``ym:s:visits`` -> ``visits``). Every metric
        value is a float. A sampled response carries the ``sampled`` limitation and warning so a
        consumer can label the number instead of presenting it as exact.
        """
        counter_id = _counter_id(counter)
        metric_names = _names(metrics, what="metrics", required=True)
        dimension_names = _names(dimensions, what="dimensions", required=False)
        first = _require_date(date1, name="date1")
        last = _require_date(date2, name="date2")
        if first > last:
            raise SeoError(
                code=INVALID_INPUT,
                message=(
                    f"date1 ({first.isoformat()}) must not be after date2 ({last.isoformat()})"
                ),
                next_action="pass an inclusive date window",
            )
        page_limit = _require_limit(limit)

        form: dict[str, str] = {
            "ids": counter_id,
            "metrics": ",".join(metric_names),
            "dimensions": ",".join(dimension_names),
            "date1": first.isoformat(),
            "date2": last.isoformat(),
            "accuracy": accuracy,
            "limit": str(page_limit),
        }
        if filters is not None and filters.strip():
            form["filters"] = filters.strip()

        payload = self._http.post_json(
            self._url("/stat/v1/data"),
            data=form,
            headers=self._auth_headers(),
        )
        table = _require_mapping(payload)
        rows = _stat_rows(
            table,
            dimension_names=dimension_names,
            metric_names=metric_names,
        )
        total = _total_rows(table)
        sampled = table.get("sampled") is True

        # Metrika reports whole days; the window is complete at 23:59 UTC on ``date2``.
        fetched = now_utc()
        window_end = datetime.combine(last, time(hour=23, minute=59), tzinfo=UTC)
        return ProviderResult(
            source=SOURCE,
            property_id=counter_id,
            fetched_at=fetched,
            data_age_days=data_age_days(end=window_end, fetched_at=fetched),
            rows=rows,
            total=total,
            truncated=total is not None and total > page_limit,
            limitations=(SAMPLED,) if sampled else (),
            warnings=("sampled",) if sampled else (),
        )

    def _get(self, path: str) -> Mapping[str, Any]:
        payload = self._http.get_json(self._url(path), headers=self._auth_headers())
        return _require_mapping(payload)

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"OAuth {self._token}"}

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    def _listing(self, *, property_id: str, rows: list[Row]) -> ProviderResult[Row]:
        # Counter and goal metadata is configuration data, not a reporting window.
        return ProviderResult(
            source=SOURCE,
            property_id=property_id,
            fetched_at=now_utc(),
            data_age_days=0,
            rows=rows,
            total=len(rows),
        )


def _names(values: Sequence[str], *, what: str, required: bool) -> list[str]:
    """Normalise a sequence of Metrika dimension or metric names."""
    if isinstance(values, str):
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{what} must be a sequence of names, not a single string",
            next_action=f"pass {what} as a list, e.g. ['ym:s:visits']",
        )
    names: list[str] = []
    for value in values:
        candidate: object = value
        if not isinstance(candidate, str):
            raise SeoError(
                code=INVALID_INPUT,
                message=f"{what} entries must be strings, got {type(candidate).__name__}",
                next_action=f"pass {what} such as ['ym:s:visits']",
            )
        name = candidate.strip()
        if name:
            names.append(name)
    if required and not names:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"{what} must not be empty",
            next_action=f"pass at least one {what} name",
        )
    return names


def _require_date(value: date | None, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise SeoError(
        code=INVALID_INPUT,
        message=f"{name} is required",
        next_action="pass a datetime.date for the stat window",
    )


def _require_limit(limit: int) -> int:
    candidate: object = limit
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 1:
        raise SeoError(
            code=INVALID_INPUT,
            message=f"limit must be an integer >= 1, got {candidate!r}",
            next_action="pass a page size such as 100",
        )
    return candidate


def _counter_id(counter: int | str) -> str:
    candidate: object = counter
    if isinstance(candidate, bool) or not isinstance(candidate, (int, str)):
        raise SeoError(
            code=INVALID_INPUT,
            message="counter must be an integer or a string id",
            next_action="pass a Metrika counter id",
        )
    counter_id = str(candidate).strip()
    if not counter_id:
        raise SeoError(
            code=INVALID_INPUT,
            message="counter is required",
            next_action="pass a Metrika counter id",
        )
    return counter_id


def _require_mapping(payload: object) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    raise _bad_response()


def _payload_rows(payload: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    raw = payload.get(key)
    if not isinstance(raw, list):
        raise _bad_response()
    rows: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise _bad_response()
        rows.append(item)
    return rows


def _stat_rows(
    payload: Mapping[str, Any],
    *,
    dimension_names: Sequence[str],
    metric_names: Sequence[str],
) -> list[Row]:
    rows: list[Row] = []
    for item in _payload_rows(payload, "data"):
        dimension_values = item.get("dimensions", [])
        metric_values = item.get("metrics", [])
        if not isinstance(dimension_values, list) or not isinstance(metric_values, list):
            raise _bad_response()
        if len(dimension_values) != len(dimension_names):
            raise _bad_response()
        if len(metric_values) != len(metric_names):
            raise _bad_response()

        row: Row = {}
        for name, value in zip(dimension_names, dimension_values, strict=True):
            row[_strip_session_prefix(name)] = _dimension_value(value)
        for name, value in zip(metric_names, metric_values, strict=True):
            row[_strip_session_prefix(name)] = _to_float(value)
        rows.append(row)
    return rows


def _dimension_value(value: object) -> object:
    # Metrika wraps each dimension value as ``{"name": "<text value>", "id": ...}``.
    if isinstance(value, Mapping):
        return value.get("name")
    return value


def _to_float(value: object) -> float:
    candidate: object = value
    if isinstance(candidate, bool):
        raise _bad_response()
    if candidate is None:
        # Metrika omits undefined values as null; reports still expect a numeric cell.
        return 0.0
    if isinstance(candidate, (int, float)):
        return float(candidate)
    if isinstance(candidate, str):
        try:
            return float(candidate)
        except ValueError as exc:
            raise _bad_response() from exc
    raise _bad_response()


def _strip_session_prefix(name: str) -> str:
    return name.removeprefix(_SESSION_PREFIX)


def _total_rows(payload: Mapping[str, Any]) -> int | None:
    value: object = payload.get("total_rows")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _bad_response() -> SeoError:
    return SeoError(
        code=UPSTREAM_UNAVAILABLE,
        message="unexpected Yandex Metrika response",
        retryable=False,
    )
