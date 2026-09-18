"""CLI tests: exit codes, JSON shape, --min-risk, --no-color, usage errors.

The CLI is exercised in-process through ``main(argv)`` so the tests stay fast,
plus one subprocess run to prove the documented command line works end to end.
"""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr

from support import LintTestCase, fixture, ROOT

sys.path.insert(0, ROOT)

import sql_migration_lint as cli  # noqa: E402


def run_main(argv):
    """Run the CLI in-process and capture stdout, stderr and the exit code."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # argparse --version / usage errors
            code = exc.code if isinstance(exc.code, int) else 2
    return code, out.getvalue(), err.getvalue()


class ExitCodeTests(LintTestCase):
    def test_zero_when_nothing_at_or_above_threshold(self):
        code, out, _ = run_main([fixture("safe"), "--no-color"])
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("no findings", out.lower())

    def test_one_when_findings_present(self):
        code, _, _ = run_main([fixture("dangerous", "0007_risky_account_changes.sql"), "--no-color"])
        self.assertEqual(code, cli.EXIT_FINDINGS)

    def test_two_for_missing_path(self):
        code, _, err = run_main([fixture("does-not-exist.sql")])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no such file", err)

    def test_two_for_directory_without_sql(self):
        code, _, err = run_main([fixture()])
        # tests/fixtures itself contains only sub-directories, no .sql files
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("no .sql files", err)

    def test_two_for_bad_min_risk(self):
        code, _, err = run_main([fixture("safe"), "--min-risk", "urgent"])
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("--min-risk", err)

    def test_two_for_unknown_flag(self):
        code, _, err = run_main([fixture("safe"), "--nope"])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_high_threshold_exits_zero_on_medium_only_file(self):
        # 0010_edge_cases has only medium findings at the top level
        code, out, _ = run_main(
            [fixture("edge", "0010_edge_cases.sql"), "--min-risk", "high", "--no-color"]
        )
        self.assertEqual(code, cli.EXIT_FINDINGS)  # it does contain two high findings

    def test_low_threshold_on_safe_directory(self):
        code, _, _ = run_main([fixture("expand_contract"), "--min-risk", "low", "--no-color"])
        self.assertEqual(code, cli.EXIT_OK)


class OutputTests(LintTestCase):
    def test_text_output_names_the_file_and_the_rule(self):
        code, out, _ = run_main([fixture("dangerous", "0007_risky_account_changes.sql"), "--no-color"])
        self.assertIn("0007_risky_account_changes.sql", out)
        self.assertIn("index-not-concurrent", out)
        self.assertIn("safer:", out)

    def test_no_color_strips_ansi(self):
        _, out, _ = run_main([fixture("dangerous", "0007_risky_account_changes.sql"), "--no-color"])
        self.assertNotIn("\033[", out)

    def test_lock_risk_summary_can_be_hidden(self):
        _, with_summary, _ = run_main(
            [fixture("safe"), "--no-color"]
        )
        _, without, _ = run_main(
            [fixture("safe"), "--no-color", "--no-lock-summary"]
        )
        self.assertIn("lock risk by statement", with_summary)
        self.assertNotIn("lock risk by statement", without)

    def test_min_risk_medium_hides_low_findings(self):
        _, out, _ = run_main(
            [fixture("dangerous", "0007_risky_account_changes.sql"),
             "--no-color", "--min-risk", "medium"]
        )
        self.assertNotIn("[LOW]", out)

    def test_json_output_is_valid_json(self):
        code, out, _ = run_main(
            [fixture("dangerous", "0007_risky_account_changes.sql"), "--json"]
        )
        payload = json.loads(out)
        self.assertEqual(payload["tool"], "sql-migration-lint")
        self.assertEqual(payload["threshold"], "low")
        self.assertEqual(len(payload["files"]), 1)
        self.assertGreater(payload["totals"]["findings"], 10)

    def test_json_findings_carry_rule_risk_and_suggestion(self):
        _, out, _ = run_main([fixture("dangerous", "0007_risky_account_changes.sql"), "--json"])
        payload = json.loads(out)
        findings = payload["files"][0]["findings"]
        self.assertTrue(all({"rule", "risk", "suggestion", "message", "line"} <= set(f)
                            for f in findings))

    def test_json_threshold_filters_findings(self):
        _, out, _ = run_main(
            [fixture("dangerous", "0007_risky_account_changes.sql"), "--json",
             "--min-risk", "high"]
        )
        payload = json.loads(out)
        self.assertTrue(all(f["risk"] == "high" for f in payload["files"][0]["findings"]))

    def test_json_on_safe_file_reports_zero_findings(self):
        code, out, _ = run_main([fixture("safe"), "--json"])
        payload = json.loads(out)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(payload["totals"]["findings"], 0)
        self.assertEqual(payload["files"][0]["findings"], [])

    def test_check_order_reports_duplicate_in_text_output(self):
        code, out, _ = run_main([fixture("order"), "--check-order", "--no-color",
                                 "--min-risk", "high"])
        self.assertEqual(code, cli.EXIT_FINDINGS)
        self.assertIn("duplicate migration number", out)

    def test_check_order_in_json(self):
        _, out, _ = run_main([fixture("order"), "--check-order", "--json"])
        payload = json.loads(out)
        rules = [f["rule"] for entry in payload["files"] for f in entry["findings"]]
        self.assertIn("migration-order-duplicate", rules)

    def test_version_flag(self):
        code, out, _ = run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("sql-migration-lint", out)

    def test_help_mentions_exit_codes(self):
        code, out, _ = run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("exit codes", out.lower())


class SubprocessTests(LintTestCase):
    """One real subprocess run, exactly as the README documents it."""

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(ROOT, "sql_migration_lint.py"), *args],
            capture_output=True, text=True, cwd=ROOT, timeout=60,
        )

    def test_safe_file_exits_zero(self):
        proc = self.run_cli(fixture("safe"), "--no-color")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no findings", proc.stdout.lower())

    def test_dangerous_file_exits_one(self):
        proc = self.run_cli(fixture("dangerous", "0007_risky_account_changes.sql"), "--no-color")
        self.assertEqual(proc.returncode, 1)

    def test_missing_file_exits_two(self):
        proc = self.run_cli(fixture("nope.sql"))
        self.assertEqual(proc.returncode, 2)

    def test_json_is_parseable_from_a_subprocess(self):
        proc = self.run_cli(fixture("expand_contract"), "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["totals"]["findings"], 0)


if __name__ == "__main__":
    unittest.main()
