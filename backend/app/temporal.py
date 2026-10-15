"""Deterministic answers for elapsed-year questions with explicit source dates."""
import re
from datetime import date


_DURATION_QUESTION = re.compile(r"(?:多少年|几年|多久|多少周年|几周年)")
_FOUNDING_EVENT_QUESTION = re.compile(r"(?:建校|创办|创建|创立|成立|设立)")
_LABELED_YEAR = re.compile(
    r"(?:建校|创办|创建|创立|成立|设立)(?:时间|年份)\*{0,2}\s*[：:]\s*(\d{4})\s*年?"
)


def elapsed_years_answer(
    question: str,
    passages: list[str],
    current_date: date,
) -> str | None:
    """Answer founding-duration questions only when one labeled year is explicit."""
    if not _DURATION_QUESTION.search(question) or not _FOUNDING_EVENT_QUESTION.search(question):
        return None

    matches: set[tuple[int, int]] = set()
    for source_index, passage in enumerate(passages, start=1):
        for year_text in _LABELED_YEAR.findall(passage):
            year = int(year_text)
            if 1800 <= year <= current_date.year:
                matches.add((year, source_index))

    years = {year for year, _ in matches}
    if len(years) != 1:
        return None

    year = years.pop()
    source_index = next(index for matched_year, index in matches if matched_year == year)
    elapsed = current_date.year - year
    return (
        f"资料记载，学校创办于 {year} 年 [来源{source_index}]。"
        f"按当前年份计算：{current_date.year} − {year} = {elapsed}，"
        f"建校约 {elapsed} 年 [来源{source_index}]。"
        "资料未提供具体月日，因此无法判断该年周年日是否已过。"
    )
