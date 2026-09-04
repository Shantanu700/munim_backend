import base64
import binascii

from django.db.models import Q
from django.utils.dateparse import parse_datetime
from rest_framework.pagination import PageNumberPagination


class DefaultPagination(PageNumberPagination):
    page_size = 10        
    page_query_param = "page"
    page_size_query_param = "page_size"
    max_page_size = 50


class KeysetPagination:
    """Infinite scroll, positioned by the last row seen rather than by an offset.

    Not PageNumberPagination, and not DRF's CursorPagination either.

    `page=2` is wrong for a list that grows at the head: an order placed between
    two scrolls shifts every offset by one, so the reader sees a row twice and
    never sees another at all. That is tolerable on the audit table, which pages
    on a gapless `seq`, and not tolerable here.

    DRF's CursorPagination encodes position from `ordering[0]` only and resolves
    ties with an in-page offset counter -- the same defect, narrowed to rows
    sharing a timestamp. The pair below is strictly ordered, so a cursor names
    exactly one row.

    Deliberately not a DRF BasePagination subclass: the views here build their
    own envelope (see `envelope`) because `total` is conditional in a way DRF's
    paginate/get_paginated_response split has nowhere to express.
    """

    page_size = 20
    max_page_size = 100
    cursor_query_param = "cursor"
    page_size_query_param = "page_size"

    ordering = ("-authorized_at", "-id")

    def get_page_size(self, request) -> int:
        raw = request.query_params.get(self.page_size_query_param)
        if not raw:
            return self.page_size
        try:
            size = int(raw)
        except (TypeError, ValueError):
            return self.page_size
        return max(1, min(size, self.max_page_size))

    def encode(self, obj) -> str:
        raw = f"{obj.authorized_at.isoformat()}|{obj.pk}".encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def decode(self, raw: str | None):
        """A cursor as (timestamp, pk), or None if it is not one.

        Every failure path returns None -- a cursor is an opaque token the
        client got from us, so a garbled one means a truncated URL or a stale
        bookmark. Answering with the first page is right; a 500 is not.
        """
        if not raw:
            return None
        try:
            padded = raw + "=" * (-len(raw) % 4)
            stamp, _, pk = base64.urlsafe_b64decode(padded).decode().rpartition("|")
            when = parse_datetime(stamp)
            if when is None:
                return None
            return when, int(pk)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return None

    def paginate(self, queryset, request):
        """One page, plus whether another exists.

        Returns (rows, has_more). Fetches page_size + 1 rows and discards the
        extra: one row is a cheaper way to learn "is there more" than a second
        COUNT query, and unlike a count it cannot disagree with the page.
        """
        size = self.get_page_size(request)
        queryset = queryset.order_by(*self.ordering)

        if position := self.decode(request.query_params.get(self.cursor_query_param)):
            when, pk = position
            queryset = queryset.filter(
                Q(authorized_at__lt=when) | Q(authorized_at=when, pk__lt=pk))

        rows = list(queryset[:size + 1])
        has_more = len(rows) > size
        return rows[:size], has_more

    def envelope(self, rows, serialized, has_more, *, total=None) -> dict:
        """The response body.

        Key names are deliberately unlike DRF's next/previous/count so nothing
        mistakes this for the envelope the page-numbered endpoints return.

        `total` is the caller's to supply, and only for a request that arrived
        without a cursor: that is the one request whose answer needs "Showing
        140 of 147", and a COUNT(*) on every scroll tick is pure waste. On
        later pages it is null, meaning "unchanged since the first page".
        """
        return {
            "results": serialized,
            "next_cursor": self.encode(rows[-1]) if (rows and has_more) else None,
            "has_more": has_more,
            "total": total,
        }
