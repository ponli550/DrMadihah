"""`umcares complete` — completion candidates for the shell."""
import unittest

from umcares import complete


class Subcommands(unittest.TestCase):
    def test_empty_tokens_lists_subcommands(self):
        cands = complete.complete_candidates([])
        self.assertIn("render", cands)
        self.assertIn("ingest", cands)
        self.assertNotIn("complete", cands)

    def test_partial_subcommand_filters(self):
        cands = complete.complete_candidates(["re"])
        self.assertEqual(cands, ["recipe", "remote", "render"])

    def test_global_option_prefix(self):
        cands = complete.complete_candidates(["--tra"])
        self.assertIn("--transport", cands)


class Options(unittest.TestCase):
    def test_subcommand_flags(self):
        cands = complete.complete_candidates(["render", "--"])
        self.assertIn("--file", cands)
        self.assertIn("--force", cands)
        self.assertIn("--from", cands)

    def test_flag_choices(self):
        cands = complete.complete_candidates(["render", "--from"])
        self.assertEqual(cands, sorted(set(complete.complete_candidates(["render", "--from"]))))

    def test_transport_choices_offered_with_equals(self):
        cands = complete.complete_candidates(["--transport"])
        for c in ("--transport", "--transport=auto", "--transport=ssh"):
            self.assertIn(c, cands)


class Values(unittest.TestCase):
    def test_render_file_points_at_recipes(self):
        self.assertEqual(complete.complete_candidates(["render", "--file", ""]),
                         ["__RECIPES__"])
        self.assertEqual(complete.complete_candidates(["render", "--file", "v1"]),
                         ["__RECIPES__"])

    def test_verify_file_points_at_recipes(self):
        self.assertEqual(complete.complete_candidates(["verify", "--file", ""]),
                         ["__RECIPES__"])

    def test_ingest_csv_points_at_files(self):
        self.assertEqual(complete.complete_candidates(["ingest", "--csv", ""]),
                         ["__FILES__"])

    def test_push_local_points_at_files(self):
        self.assertEqual(complete.complete_candidates(["push", ""]),
                         ["__FILES__"])

    def test_equals_form(self):
        cands = complete.complete_candidates(["render", "--from=bu"])
        self.assertEqual(cands, ["--from=build"])


class Positionals(unittest.TestCase):
    def test_recipe_action_choices(self):
        cands = complete.complete_candidates(["recipe", ""])
        for c in ("example", "validate", "resolve"):
            self.assertIn(c, cands)

    def test_config_action_choices(self):
        cands = complete.complete_candidates(["config", ""])
        for c in ("sections", "show", "set"):
            self.assertIn(c, cands)

    def test_positional_consumed_then_options_only(self):
        cands = complete.complete_candidates(["recipe", "validate", ""])
        self.assertIn("--file", cands)
        self.assertNotIn("example", cands)


if __name__ == "__main__":
    unittest.main()