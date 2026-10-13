import sys
import unittest
from pathlib import Path


class NoShadowing(unittest.TestCase):
    def test_no_file_in_the_package_is_named_after_a_standard_library_module(self):
        """Python puts the current folder first on its path. A file named inspect.py or secrets.py inside umcares/ then
        replaces the real module for ANY Python process started in that folder, e.g. dataclasses doing `import inspect`.
        That once broke the SekerinShotto tool for every session opened here."""
        pkg = Path(__file__).resolve().parent.parent / "umcares"
        clash = sorted(p.stem for p in pkg.glob("*.py") if p.stem in sys.stdlib_module_names)
        self.assertEqual(clash, [], f"rename these: they shadow the standard library: {clash}")

    def test_the_standard_library_wins_when_python_starts_inside_the_package_folder(self):
        import subprocess
        pkg = Path(__file__).resolve().parent.parent / "umcares"
        r = subprocess.run([sys.executable, "-c", "import inspect, secrets, dataclasses; print(inspect.__file__, secrets.__file__)"],
                           cwd=pkg, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("umcares", r.stdout.replace(str(pkg.parent), ""), r.stdout)
        self.assertNotIn(str(pkg), r.stdout)


if __name__ == "__main__":
    unittest.main()
