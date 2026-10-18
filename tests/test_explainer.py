"""Explainer cards (checklist, progress): the recipe's checks, the timing the renderer works out, and what a real render puts on screen."""
import unittest

from umcares import explainer


def problems_of(kind, value, **extra):
    out = []
    explainer.check(value, kind, "clips[0]", out)
    return out


class Recipe(unittest.TestCase):
    def test_a_good_checklist_and_progress_have_no_problems(self):
        self.assertEqual(problems_of("checklist", {"title": "Tools", "items": ["Board", "Git"]}), [])
        self.assertEqual(problems_of("progress", {"label": "Done", "to": 100, "unit": "%"}), [])
        self.assertEqual(problems_of("progress", {"label": "Users", "from": 120, "to": 480}), [])

    def test_checklist_problems_are_said_in_plain_words(self):
        for bad, why in (
            ("x", "an object like"),
            ({"items": ["a", "b"]}, "title"),
            ({"title": "T", "items": ["a"]}, "2 to 6 items"),
            ({"title": "T", "items": ["a"] * 7}, "2 to 6 items"),
            ({"title": "T", "items": ["a", 3]}, "each item"),
            ({"title": "T", "items": ["a", "x" * 41]}, "each item"),
            ({"title": "T", "items": ["a", "b"], "colour": "red"}, "unknown field"),
        ):
            with self.subTest(bad=bad):
                self.assertTrue(any(why in p for p in problems_of("checklist", bad)), problems_of("checklist", bad))

    def test_progress_problems_are_said_in_plain_words(self):
        for bad, why in (
            ({"to": 5}, "label"),
            ({"label": "L"}, "progress.to"),
            ({"label": "L", "to": -1}, "progress.to"),
            ({"label": "L", "to": True}, "progress.to"),
            ({"label": "L", "to": 5, "from": -2}, "progress.from"),
            ({"label": "L", "to": 5, "unit": "percent"}, "unit"),
            ({"label": "L", "to": 5, "from": 5}, "different one"),
            ({"label": "L", "to": 120, "unit": "%"}, "up to 100"),
            ({"label": "L", "to": 5, "speed": 2}, "unknown field"),
        ):
            with self.subTest(bad=bad):
                self.assertTrue(any(why in p for p in problems_of("progress", bad)), problems_of("progress", bad))


class Timing(unittest.TestCase):
    def test_items_arrive_one_after_another_and_the_last_has_a_second_to_be_read(self):
        t = explainer.times(4, 4.5)
        self.assertEqual(t, sorted(t))
        self.assertEqual(len(set(t)), 4, "no two at once")
        self.assertGreaterEqual(t[0], 0.2)
        self.assertLessEqual(t[-1], 4.5 - 0.9 + 0.001, "the last item has about a second on screen before the piece ends")

    def test_a_short_piece_squeezes_the_arrivals_and_never_overruns(self):
        t = explainer.times(6, 2.5)
        self.assertEqual(len(set(t)), 6)
        self.assertLess(t[-1], 2.5)

    def test_the_default_length_grows_with_the_number_of_items(self):
        short = explainer.segment({"_card": "checklist", "checklist": {"title": "T", "items": ["a", "b"]}})["duration"]
        long = explainer.segment({"_card": "checklist", "checklist": {"title": "T", "items": list("abcdef")}})["duration"]
        self.assertGreater(long, short + 2)
        held = explainer.segment({"_card": "checklist", "checklist": {"title": "T", "items": ["a", "b"]}, "hold": 9})["duration"]
        self.assertEqual(held, 9.0, "hold is the person's to set")


class Count(unittest.TestCase):
    def test_the_number_starts_at_from_ends_exactly_at_to_and_never_goes_backwards(self):
        for frm, to, d in ((0, 100, 3.4), (120, 480, 3.0), (10, 12, 3.0), (0, 7, 2.5), (5, 900, 4.0)):
            with self.subTest(frm=frm, to=to):
                steps = explainer.count_steps(frm, to, d)
                values = [v for v, _, _ in steps]
                self.assertEqual(values[0], frm)
                self.assertEqual(values[-1], to, "the last picture is exactly the goal")
                self.assertEqual(values, sorted(values))
                self.assertEqual(len(values), len(set(values)), "no value twice in a row")
                self.assertLessEqual(len(steps), explainer.COUNT_STEPS + 1, "each is a picture the renderer must draw")

    def test_the_pictures_tile_the_whole_piece_with_no_gap_and_the_goal_is_held_to_the_end(self):
        steps = explainer.count_steps(0, 100, 3.4)
        self.assertEqual(steps[0][1], 0.0)
        for (_, _, end), (_, start, _) in zip(steps, steps[1:]):
            self.assertAlmostEqual(end, start, places=3)
        self.assertEqual(steps[-1][2], 3.4)

    def test_the_goal_lands_exactly_when_the_bar_is_full(self):
        start, T = explainer.count_window(3.4)
        self.assertAlmostEqual(explainer.count_steps(0, 100, 3.4)[-1][1], start + T, places=2)

    def test_it_is_eased_slow_at_the_ends_fast_in_the_middle(self):
        steps = explainer.count_steps(0, 100, 3.4)
        first, middle = steps[2][0] - steps[1][0], steps[len(steps) // 2][0] - steps[len(steps) // 2 - 1][0]
        self.assertLess(first, middle)

    def test_whole_numbers_stay_whole_and_decimals_show_one_place(self):
        self.assertTrue(all(float(v).is_integer() for v, _, _ in explainer.count_steps(0, 100, 3.4)))
        self.assertEqual(explainer.fmt(5.0), "5")
        self.assertEqual(explainer.fmt(4.5), "4.5")

    def test_ticks_follow_the_items_and_a_count_lands_once(self):
        seg = {"kind": "checklist", "items": ["a", "b", "c"], "duration": 4.0}
        self.assertEqual(len(explainer.events(seg)), 3)
        self.assertEqual(explainer.events(seg), [round(t + explainer.TICK_DELAY, 3) for t in explainer.times(3, 4.0)])
        self.assertEqual(len(explainer.events({"kind": "progress", "duration": 3.4})), 1)


if __name__ == "__main__":
    unittest.main()
