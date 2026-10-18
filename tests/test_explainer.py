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


class TypingAndList(unittest.TestCase):
    CODE = "const sprint = await plan();\nfor (const task of sprint) {\n  build(task);\n}"

    def test_good_typing_and_list_have_no_problems(self):
        self.assertEqual(problems_of("typing", {"text": self.CODE, "title": "sprint.ts"}), [])
        self.assertEqual(problems_of("typing", {"text": "x"}), [])
        self.assertEqual(problems_of("list", {"items": ["a", "b"]}), [])
        self.assertEqual(problems_of("list", {"title": "T", "items": ["a", "b", "c"], "from": "left"}), [])

    def test_a_long_line_of_code_is_wrapped_so_the_type_can_stay_big_and_a_short_one_is_left_alone(self):
        java = 'var url = new URL("http://www.oracle.com/");'
        wrapped = explainer.wrap_code(java)
        self.assertEqual(wrapped, 'var url = new URL(\n  "http://www.oracle.com/");', "broken after the bracket, the rest indented")
        for code in (java, "    return some_function(argument_one, argument_two)", "x = " + "a" * 70, "if (a) { go(); } else { stop(); }" * 2):
            with self.subTest(code=code[:20]):
                w = explainer.wrap_code(code)
                self.assertTrue(all(len(x) <= explainer.WRAP_COLS for x in w.split("\n")), w)
                self.assertEqual("".join(w.split()), "".join(code.split()), "nothing is lost or added but whitespace")
        for same in ("short", ".container {\n  display: flex;\n}", "with open(f) as g:\n    x = g.read()"):
            self.assertEqual(explainer.wrap_code(same), same)
        deep = " " * 40 + "a line far too deep to wrap usefully but long enough to be asked"
        self.assertEqual(explainer.wrap_code(deep), deep, "and it does not loop")

    def test_the_wrapped_code_is_what_the_card_types_and_times(self):
        seg = explainer.segment({"_card": "typing", "typing": {"text": 'var url = new URL("http://www.oracle.com/");'}})
        self.assertEqual(seg["code"], explainer.wrap_code('var url = new URL("http://www.oracle.com/");'))
        self.assertIn("\n", seg["code"])

    def test_typing_problems_are_said_in_plain_words(self):
        for bad, why in (
            ("x", "an object like"),
            ({}, "typing.text"),
            ({"text": "   "}, "typing.text"),
            ({"text": "a\n" * 9}, "at most 8 lines"),
            ({"text": "x" * 81}, "at most 80 characters a line"),
            ({"text": "\n".join(["word " * 13] * 5)}, "once long lines are wrapped"),
            ({"text": "x", "title": ""}, "typing.title"),
            ({"text": "x", "title": "y" * 31}, "typing.title"),
            ({"text": "x", "colour": 1}, "unknown field"),
        ):
            with self.subTest(bad=bad):
                self.assertTrue(any(why in p for p in problems_of("typing", bad)), problems_of("typing", bad))

    def test_list_problems_are_said_in_plain_words(self):
        for bad, why in (
            ({"items": ["only"]}, "2 to 6 items"),
            ({"items": ["a"] * 7}, "2 to 6 items"),
            ({"items": ["a", "x" * 51]}, "1 to 50 characters"),
            ({"items": ["a", "b"], "title": ""}, "list.title"),
            ({"items": ["a", "b"], "from": "up"}, "right or left"),
            ({"items": ["a", "b"], "speed": 2}, "unknown field"),
        ):
            with self.subTest(bad=bad):
                self.assertTrue(any(why in p for p in problems_of("list", bad)), problems_of("list", bad))

    def test_typing_shows_the_text_in_growing_steps_ending_on_all_of_it(self):
        cuts = explainer.type_cuts(self.CODE)
        self.assertEqual((cuts[0], cuts[-1]), (0, len(self.CODE)))
        self.assertEqual(cuts, sorted(set(cuts)))
        self.assertLessEqual(len(cuts), explainer.TYPE_STEPS + 1)
        self.assertEqual(explainer.type_cuts("ab"), [0, 1, 2], "a short text steps one character at a time")
        self.assertLessEqual(len(explainer.type_cuts("x" * 400)), explainer.TYPE_STEPS + 1, "never more pictures than the cap")

    def test_the_typing_is_done_a_second_before_the_end_however_long_the_piece_is_held(self):
        for d in (3.0, 6.0, 12.0):
            start, T = explainer.type_window(d)
            self.assertAlmostEqual(start + T, d - 1.0, places=2)

    def test_the_default_length_grows_with_the_text_and_is_capped(self):
        seg = lambda t, **k: explainer.segment({"_card": "typing", "typing": {"text": t}, **k})
        self.assertGreater(seg("x" * 120)["duration"], seg("x" * 10)["duration"] + 2)
        self.assertLessEqual(seg("x" * 300)["duration"], 0.6 + 7.0 + 1.0 + 0.01)
        self.assertEqual(seg("x" * 10, hold=11)["duration"], 11.0)
        self.assertEqual(explainer.segment({"_card": "typing", "typing": {"text": "a = 1\nb = 2", "title": "x.py"}})["text"], "x.py")
        self.assertEqual(explainer.segment({"_card": "typing", "typing": {"text": "a = 1\nb = 2"}})["text"], "a = 1")

    def test_a_tick_belongs_at_the_start_of_each_typed_line_and_at_each_row(self):
        starts = explainer.line_starts(self.CODE)
        self.assertEqual(len(starts), 4)
        seg = {"kind": "typing", "code": self.CODE, "duration": 6.0}
        ev = explainer.events(seg)
        self.assertEqual(len(ev), 4)
        self.assertEqual(ev, sorted(ev))
        self.assertAlmostEqual(ev[0], explainer.TYPE_START, places=2)
        rows = {"kind": "list", "items": ["a", "b", "c"], "duration": 4.0}
        self.assertEqual(explainer.events(rows), explainer.times(3, 4.0))


class FlowAndVersus(unittest.TestCase):
    SIDE = {"label": "Dulu", "items": ["Manual"]}

    def test_good_flow_and_versus_have_no_problems(self):
        self.assertEqual(problems_of("flow", {"steps": ["Kod", "Binaan"]}), [])
        self.assertEqual(problems_of("flow", {"title": "T", "steps": list("abcde")}), [])
        self.assertEqual(problems_of("versus", {"before": self.SIDE, "after": {"label": "Kini", "items": ["Auto"]}}), [])
        self.assertEqual(problems_of("versus", {"title": "T", "before": self.SIDE, "after": self.SIDE}), [])

    def test_flow_problems_are_said_in_plain_words(self):
        for bad, why in (
            ("x", "an object like"),
            ({"steps": ["only"]}, "2 to 5 steps"),
            ({"steps": list("abcdef")}, "2 to 5 steps"),
            ({"steps": ["a", "x" * 19]}, "1 to 18 characters"),
            ({"steps": ["a", 3]}, "1 to 18 characters"),
            ({"title": "", "steps": ["a", "b"]}, "flow.title"),
            ({"steps": ["a", "b"], "colour": 1}, "unknown field"),
        ):
            with self.subTest(bad=bad):
                self.assertTrue(any(why in p for p in problems_of("flow", bad)), problems_of("flow", bad))

    def test_versus_problems_are_said_in_plain_words(self):
        ok = {"label": "L", "items": ["a"]}
        for bad, why in (
            ({"after": ok}, "versus.before"),
            ({"before": ok}, "versus.after"),
            ({"before": "x", "after": ok}, "versus.before: an object"),
            ({"before": {"items": ["a"]}, "after": ok}, "before.label"),
            ({"before": {"label": "x" * 21, "items": ["a"]}, "after": ok}, "before.label"),
            ({"before": {"label": "L", "items": []}, "after": ok}, "1 to 4 items"),
            ({"before": {"label": "L", "items": list("abcde")}, "after": ok}, "1 to 4 items"),
            ({"before": {"label": "L", "items": ["x" * 31]}, "after": ok}, "1 to 30 characters"),
            ({"before": {"label": "L", "items": ["a"], "icon": 1}, "after": ok}, "unknown field"),
            ({"title": "", "before": ok, "after": ok}, "versus.title"),
        ):
            with self.subTest(bad=bad):
                self.assertTrue(any(why in p for p in problems_of("versus", bad)), problems_of("versus", bad))

    def test_the_whole_pipeline_is_drawn_first_and_then_each_box_lights_in_turn(self):
        drawn, lit = explainer.flow_times(4, 4.7)
        self.assertEqual(drawn, sorted(drawn))
        self.assertLess(drawn[-1], lit[0], "the pipeline is seen whole before it runs")
        self.assertEqual(lit, sorted(set(lit)))
        self.assertLessEqual(lit[-1], 4.7 - 1.0 + 0.01, "the last box has about a second lit before the piece ends")
        drawn5, lit5 = explainer.flow_times(5, 2.5)
        self.assertLess(lit5[-1], 2.5, "a short piece squeezes the run and never overruns")

    def test_the_before_is_shown_first_then_dims_as_the_after_begins(self):
        before, after, dim = explainer.versus_times(3, 3, 6.1)
        self.assertEqual(before, sorted(set(before)))
        self.assertLess(before[-1], after[0], "all the before comes first")
        self.assertLess(before[-1], dim, "the before is complete when it dims")
        self.assertLess(dim, after[0], "the dim starts just before the first after item")
        self.assertLessEqual(after[-1] + 1.0, 6.1 + 0.01, "a second to read the last item")

    def test_defaults_text_and_events(self):
        flow = explainer.segment({"_card": "flow", "flow": {"steps": ["Kod", "Binaan", "Ujian"]}})
        self.assertEqual((flow["text"], flow["title"]), ("Kod → Binaan → Ujian", ""))
        self.assertEqual(explainer.segment({"_card": "flow", "flow": {"title": "T", "steps": ["a", "b"]}})["text"], "T")
        self.assertEqual(explainer.segment({"_card": "flow", "flow": {"steps": ["a", "b"]}, "hold": 7})["duration"], 7.0)
        vs = explainer.segment({"_card": "versus", "versus": {"before": {"label": "Dulu", "items": [" a "]}, "after": {"label": "Kini", "items": ["b", "c"]}}})
        self.assertEqual((vs["text"], vs["before"]["items"], vs["after"]["items"]), ("Dulu → Kini", ["a"], ["b", "c"]))
        self.assertEqual(len(explainer.events(flow)), 3, "a tick as each box lights")
        self.assertEqual(len(explainer.events(vs)), 3, "a tick as each item arrives")


if __name__ == "__main__":
    unittest.main()
