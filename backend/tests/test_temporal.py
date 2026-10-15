import unittest
from datetime import date

from app.temporal import elapsed_years_answer


class ElapsedYearsTests(unittest.TestCase):
    def test_answers_from_explicit_founding_year_as_an_approximation(self):
        answer = elapsed_years_answer(
            "截至目前学校建校多少年了？",
            ["- **创办时间**：2003 年"],
            date(2026, 10, 9),
        )

        self.assertIsNotNone(answer)
        self.assertIn("建校约 23 年", answer)
        self.assertIn("[来源1]", answer)
        self.assertIn("无法判断该年周年日是否已过", answer)
        self.assertNotIn("已满", answer)

    def test_does_not_answer_when_duration_or_event_is_not_requested(self):
        passage = ["- **创办时间**：2003 年"]

        self.assertIsNone(elapsed_years_answer("学校哪年创办？", passage, date(2026, 10, 9)))
        self.assertIsNone(elapsed_years_answer("学校有多少学生？", passage, date(2026, 10, 9)))

    def test_does_not_guess_when_labeled_year_is_missing_or_ambiguous(self):
        self.assertIsNone(
            elapsed_years_answer("学校建校多少年？", ["学校于2003年创办。"], date(2026, 10, 9))
        )
        self.assertIsNone(
            elapsed_years_answer(
                "学校建校多少年？",
                ["创办时间：2003年", "成立时间：2004年"],
                date(2026, 10, 9),
            )
        )


if __name__ == "__main__":
    unittest.main()
