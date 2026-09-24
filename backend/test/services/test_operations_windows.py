"""看板时间窗与日期区间口径（tasklist 13.3）。不连库、不连网。

口径错了看板会整体偏移一天（北京时间上午 8 点前的提问算到前一天），而这种错在界面上
看不出来——所以把边界单独锁住。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.common.errors import AppError
from app.services.operations import RANGE_7D, RANGE_TODAY, resolve_day_range, resolve_range

TZ = ZoneInfo("Asia/Shanghai")


def _local(moment: datetime) -> datetime:
    return moment.astimezone(TZ)


def test_today_range_starts_at_local_midnight() -> None:
    window = resolve_range(RANGE_TODAY)

    start = _local(window.start)
    assert start.time() == time(0, 0)
    assert start.date() == date.today()
    assert window.days == [date.today()]
    assert start.utcoffset() is None or start.utcoffset().total_seconds() == 8 * 3600


def test_seven_day_range_covers_seven_local_days() -> None:
    window = resolve_range(RANGE_7D)

    assert len(window.days) == 7
    assert window.days[-1] == date.today()
    assert window.days[0] == date.today() - timedelta(days=6)
    assert _local(window.start).date() == window.days[0]
    assert window.start < window.end


def test_utc_boundary_uses_business_timezone() -> None:
    """`start` 必须是业务时区的 00:00 对应的 UTC 时刻，而不是 UTC 的 00:00。"""

    window = resolve_range(RANGE_7D)

    assert window.start == datetime.combine(
        window.days[0], time.min, tzinfo=TZ
    ).astimezone(UTC)


def test_day_range_includes_both_ends() -> None:
    start, end = resolve_day_range(date(2026, 3, 10), date(2026, 3, 10))

    assert start == datetime(2026, 3, 9, 16, 0, tzinfo=UTC)
    assert end == datetime(2026, 3, 10, 16, 0, tzinfo=UTC)


def test_day_range_defaults_to_last_seven_days(monkeypatch) -> None:
    today = date.today()
    start, end = resolve_day_range(None, None)

    assert _local(start).date() == today - timedelta(days=6)
    assert _local(end).date() == today + timedelta(days=1)


def test_day_range_rejects_reversed_input() -> None:
    with pytest.raises(AppError):
        resolve_day_range(date(2026, 3, 10), date(2026, 3, 1))


def test_day_range_end_only_derives_start() -> None:
    start, end = resolve_day_range(None, date(2026, 3, 10))

    assert _local(start).date() == date(2026, 3, 4)
    assert _local(end).date() == date(2026, 3, 11)
