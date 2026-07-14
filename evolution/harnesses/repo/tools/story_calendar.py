"""StoryCalendar — 虚构历法 → datetime 映射（可进化要素）。

Graphiti 的 valid_at 需要真实的 datetime，但中文小说用虚构历法
（科幻用公元纪年 2087年2月15日，玄幻用建元二十年冬）。
本模块把故事内时间文本转成 Graphiti 可用的 reference_time。

双模式（需求决策 6）：
  - 公元纪年（默认）：直接解析 '2087年2月15日' → datetime
  - 虚构历法：配 epoch（历法起点对应的真实 datetime）+ 年份映射，
    '建元二十年冬' → epoch + 20年 + 冬季偏移

为什么放 harness tools（可进化）：
  虚构历法的解析规则因作品而异（每个玄幻世界的历法不同），
  evolution agent 需要能调整解析逻辑。

设计原则：
  解析失败不崩——返回 epoch 默认值 + 记 warning。
  不是所有事件都有精确时间（有些只写"三年前"），强制解析会丢事件。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class StoryCalendar:
    """故事历法映射器。

    Attributes:
        mode: 'gregorian'（公元纪年）或 'fictional'（虚构历法）
        epoch: 虚构历法的纪元起点（对应的真实 datetime）。
               公元模式下不用。默认 2000-01-01。
        year_span_days: 虚构历法一年对应多少天（默认 365.25）。
        chinese_num_map: 中文数字映射（一=1, 二=2, ..., 二十=20 等）。
    """
    mode: str = "gregorian"
    epoch: datetime = field(default_factory=lambda: datetime(2000, 1, 1))
    year_span_days: float = 365.25

    # 中文数字（基础 + 十位组合）
    _CN_DIGITS = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    }

    def to_datetime(self, story_time: str) -> datetime:
        """把故事内时间文本转成 datetime。

        解析失败返回 epoch（不崩，记 warning）。
        """
        if not story_time or not story_time.strip():
            return self.epoch

        story_time = story_time.strip()

        if self.mode == "gregorian":
            dt = self._parse_gregorian(story_time)
        else:
            dt = self._parse_fictional(story_time)

        if dt is None:
            logger.warning("StoryCalendar 解析失败，回退到 epoch：%s", story_time)
            return self.epoch
        return dt

    # ------------------------------------------------------------------
    # 公元纪年解析：'2087年2月15日' / '2087-02-15' / '2087年12月'
    # ------------------------------------------------------------------

    # 匹配 '2087年2月15日' 或 '2087年12月'
    _GREGORIAN_YMD = re.compile(r"(\d{2,4})年(\d{1,2})月(\d{1,2})日")
    _GREGORIAN_YM = re.compile(r"(\d{2,4})年(\d{1,2})月")
    _GREGORIAN_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")

    def _parse_gregorian(self, text: str) -> datetime | None:
        # 完整年月日：2087年2月15日
        m = self._GREGORIAN_YMD.search(text)
        if m:
            return self._safe_datetime(int(m[1]), int(m[2]), int(m[3]))

        # ISO 格式：2087-02-15
        m = self._GREGORIAN_ISO.search(text)
        if m:
            return self._safe_datetime(int(m[1]), int(m[2]), int(m[3]))

        # 年月：2087年2月（取该月1日）
        m = self._GREGORIAN_YM.search(text)
        if m:
            return self._safe_datetime(int(m[1]), int(m[2]), 1)

        return None

    def _safe_datetime(self, year: int, month: int, day: int) -> datetime | None:
        """构造 datetime，参数非法时返回 None（不抛异常）。"""
        try:
            # 年份补全：两位数年份补 2000（如 '87年' → 2087）
            if year < 100:
                year += 2000
            return datetime(year, month, day)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # 虚构历法解析：'建元二十年冬' / '第三百零五章' / '三年前'
    # ------------------------------------------------------------------

    # 匹配 'XX年' 前的中文数字（如 '二十年' '二十年冬' '三百年前'）
    _FICTIONAL_YEAR = re.compile(r"([零〇一二三四五六七八九十百千]+)年")
    # 季节映射（冬/春/夏/秋 → 月份偏移）
    _SEASON_MAP = {"春": 3, "夏": 6, "秋": 9, "冬": 12}
    _SEASON_PATTERN = re.compile(r"[春夏秋冬]")

    def _parse_fictional(self, text: str) -> datetime | None:
        """虚构历法解析。

        策略：
          1. 提取中文年份数字（如 '二十年' → 20）
          2. 加季节偏移（如 '冬' → 12月）
          3. epoch + 年数 * year_span_days + 季节月偏移

        解析不了纯数字年份时，尝试匹配 '三年前'/'十年后' 这类相对时间。
        """
        # 绝对年份：'建元二十年' → 20 年
        year_match = self._FICTIONAL_YEAR.search(text)
        if year_match:
            years = self._cn_to_int(year_match.group(1))
            if years is not None:
                # 季节偏移
                season_match = self._SEASON_PATTERN.search(text)
                month_offset = 0
                if season_match:
                    season = season_match.group()
                    month_offset = self._SEASON_MAP.get(season, 0)

                result = self.epoch + timedelta(days=years * self.year_span_days)
                if month_offset:
                    result = result.replace(month=month_offset)  # 可能 ValueError
                return result

        # 相对时间：'三年前' / '十年后'
        rel_match = re.search(r"([零〇一二三四五六七八九十百千]+)年[前后]", text)
        if rel_match:
            years = self._cn_to_int(rel_match.group(1))
            if years is not None:
                direction = -1 if "前" in text else 1
                return self.epoch + timedelta(
                    days=direction * years * self.year_span_days
                )

        return None

    def _cn_to_int(self, cn: str) -> int | None:
        """中文数字转整数。支持 十/百（如 '二十'→20, '三百零五'→305）。

        无法解析返回 None。
        """
        if not cn:
            return None

        # 纯数字直接转
        if cn.isdigit():
            return int(cn)

        total = 0
        current = 0
        for ch in cn:
            if ch not in self._CN_DIGITS:
                return None
            val = self._CN_DIGITS[ch]
            if val >= 10:  # 十/百/千：乘数位
                if current == 0:
                    current = 1
                if ch == "十":
                    total += current * 10
                elif ch == "百":
                    total += current * 100
                # 注：千位暂不支持（故事里极少用千年级别）
                current = 0
            else:
                current = val
        total += current
        return total if total > 0 else None


__all__ = ["StoryCalendar"]
