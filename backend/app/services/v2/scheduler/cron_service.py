"""Validated five-field cron parsing and timezone-aware next-fire calculation."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class CronService:
    """Parse standard five-field cron expressions and find deterministic fire times.

    Daylight-saving behavior is explicit: nonexistent local times are skipped and
    repeated local times use the first occurrence (fold=0).
    """

    FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

    def parse_cron(self, expression: str) -> dict:
        if not isinstance(expression, str):
            raise ValueError("Cron 表达式必须是字符串")
        parts = expression.strip().split()
        if len(parts) != 5:
            raise ValueError(f"无效的五段式 cron 表达式：{expression}")
        for field, (minimum, maximum) in zip(parts, self.FIELD_RANGES):
            self._parse_field(field, minimum, maximum)
        return {
            "minute": parts[0],
            "hour": parts[1],
            "day_of_month": parts[2],
            "month_of_year": parts[3],
            "day_of_week": parts[4],
        }

    def validate_cron(self, expression: str) -> bool:
        try:
            self.parse_cron(expression)
            return True
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _parse_field(field: str, minimum: int, maximum: int) -> set[int]:
        values: set[int] = set()
        if not field:
            raise ValueError("Cron 字段不能为空")
        for item in field.split(","):
            if not item:
                raise ValueError("Cron 列表中存在空字段")
            if "/" in item:
                if item.count("/") != 1:
                    raise ValueError("Cron 步长格式无效")
                base, raw_step = item.split("/", 1)
                if not raw_step.isdigit() or int(raw_step) < 1:
                    raise ValueError("Cron 步长必须是正整数")
                step = int(raw_step)
            else:
                base, step = item, 1

            if base == "*":
                start, end = minimum, maximum
            elif "-" in base:
                if base.count("-") != 1:
                    raise ValueError("Cron 范围格式无效")
                raw_start, raw_end = base.split("-", 1)
                if not raw_start.isdigit() or not raw_end.isdigit():
                    raise ValueError("Cron 范围必须使用整数")
                start, end = int(raw_start), int(raw_end)
                if start < minimum or start > maximum or end < minimum or end > maximum:
                    raise ValueError("Cron 值超出字段范围")
                if end < start:
                    raise ValueError("Cron 范围不能倒序")
            elif base.isdigit():
                start = int(base)
                if start < minimum or start > maximum:
                    raise ValueError("Cron 值超出字段范围")
                end = maximum if "/" in item else start
            else:
                raise ValueError("Cron 字段只支持数字、逗号、范围、星号和步长")

            values.update(range(start, end + 1, step))
        if not values:
            raise ValueError("Cron 字段没有有效取值")
        # Cron permits both 0 and 7 to mean Sunday.
        if maximum == 7 and 7 in values:
            values.remove(7)
            values.add(0)
        return values

    @staticmethod
    def _matches_day(parts: list[str], candidate: date) -> bool:
        dom_values = CronService._parse_field(parts[2], 1, 31)
        dow_values = CronService._parse_field(parts[4], 0, 7)
        dom_matches = candidate.day in dom_values
        cron_weekday = (candidate.weekday() + 1) % 7
        dow_matches = cron_weekday in dow_values
        dom_is_wild = parts[2] == "*"
        dow_is_wild = parts[4] == "*"
        if dom_is_wild or dow_is_wild:
            return dom_matches and dow_matches
        # Standard cron treats both restricted day fields as alternatives.
        return dom_matches or dow_matches

    def next_fire_utc(self, expression: str, timezone_name: str, after: datetime) -> datetime:
        parts = expression.strip().split() if isinstance(expression, str) else []
        if len(parts) != 5:
            self.parse_cron(expression)
        parsed = self.parse_cron(expression)
        parts = [parsed[key] for key in ("minute", "hour", "day_of_month", "month_of_year", "day_of_week")]
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError(f"未知时区：{timezone_name}") from exc
        after_utc = after.replace(tzinfo=timezone.utc) if after.tzinfo is None else after.astimezone(timezone.utc)
        local_start = after_utc.astimezone(zone)
        minutes = sorted(self._parse_field(parts[0], 0, 59))
        hours = sorted(self._parse_field(parts[1], 0, 23))
        months = self._parse_field(parts[3], 1, 12)

        # Iterate calendar days and only the configured hour/minute combinations.
        # Eight years covers leap-day schedules while keeping invalid schedules bounded.
        for offset in range(366 * 8):
            day = local_start.date() + timedelta(days=offset)
            if day.month not in months or not self._matches_day(parts, day):
                continue
            for hour in hours:
                for minute in minutes:
                    local_naive = datetime.combine(day, time(hour=hour, minute=minute))
                    local_aware = local_naive.replace(tzinfo=zone, fold=0)
                    candidate_utc = local_aware.astimezone(timezone.utc)
                    round_trip = candidate_utc.astimezone(zone).replace(tzinfo=None)
                    if round_trip != local_naive or candidate_utc <= after_utc:
                        continue
                    return candidate_utc
        raise ValueError("未来八年内找不到符合此 cron 的有效触发时间")

    def schedule_connection_sync(self, connection_id: str, cron_expr: str) -> dict:
        self.parse_cron(cron_expr)
        return {
            "connection_id": connection_id,
            "cron": cron_expr.strip(),
            "status": "unregistered",
            "message": "Connection 定时同步尚未接入持久调度器",
        }

    def schedule_pipeline_run(self, pipeline_id: str, cron_expr: str) -> dict:
        self.parse_cron(cron_expr)
        return {
            "pipeline_id": pipeline_id,
            "cron": cron_expr.strip(),
            "status": "unregistered",
            "message": "请通过 Pipeline Schedule API 创建持久调度资源",
        }

    def describe_cron(self, expression: str) -> str:
        if not self.validate_cron(expression):
            return "无效的 cron 表达式"
        parts = expression.strip().split()
        minute, hour, dom, month, dow = parts
        if expression.strip() == "* * * * *":
            return "每分钟"
        if minute.startswith("*/"):
            return f"每 {minute[2:]} 分钟"
        if minute == "0" and dom == "*" and month == "*" and dow == "*":
            if hour == "*":
                return "每小时整点"
            if hour == "8":
                return "每天 08:00"
            return f"每天 {hour}:00"
        return f"按计划执行 ({expression.strip()})"
