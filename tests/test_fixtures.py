"""Fixture-based tests: the negative control, expand/contract, and every
dangerous pattern the tool advertises, on realistic migration files.
"""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

from support import LintTestCase, fixture, ROOT

from sqlmig import analyzer


class NegativeControlTests(LintTestCase):
    """A correctly written migration must produce ZERO findings.

    The safe pair of migrations is checked as a directory, because the second
    file validates the NOT VALID constraint the first one adds -- the two-step
    pattern the tool itself recommends.
    """

    def test_safe_directory_has_no_findings_at_all(self):
        reports = analyzer.analyse_path(fixture("safe"))
        self.assertEqual(len(reports), 2)
        for report in reports:
            self.assertEqual(report.problems, [], f"{report.path} should be clean")
            self.assertIsNone(report.worst_risk)
            # Nothing may be hidden either: every recorded emission is a note.
            self.assertEqual(len(report.findings), len(report.notes))

    def test_safe_first_migration_only_adds_not_valid_constraints(self):
        # Run alone it correctly asks for a later VALIDATE CONSTRAINT.
        report = self.analyse_fixture("safe", "0001_add_email.sql")
        self.assertEqual([f.rule_id for f in report.problems],
                         ["constraint-never-validated"])
        self.assertIn("VALIDATE CONSTRAINT", report.problems[0].suggestion)

    def test_safe_second_migration_is_silent(self):
        report = self.analyse_fixture("safe", "0002_validate_email.sql")
        self.assertEqual(report.findings, [])
        self.assertEqual(report.operations[0].kind, "validate-constraint")

    def test_safe_fixture_still_reports_what_it_checked(self):
        # Silence must not mean "did not look": the operations list must exist.
        report = self.analyse_fixture("safe", "0001_add_email.sql")
        kinds = self.operation_kinds(report)
        self.assertIn("add-column", kinds)
        self.assertIn("create-index-concurrently", kinds)
        self.assertIn("add-constraint", kinds)

    def test_expand_phase_directory_is_clean(self):
        reports = analyzer.analyse_path(fixture("expand_contract"))
        self.assertEqual(len(reports), 3)
        for report in reports:
            self.assertEqual(report.problems, [], f"{report.path} should be clean")
            self.assertIsNone(report.worst_risk)

    def test_expand_phase_alone_asks_for_the_validation_step(self):
        report = self.analyse_fixture("expand_contract", "0004_expand_phase.sql")
        self.assertEqual(
            sorted(f.rule_id for f in report.problems),
            ["constraint-never-validated", "constraint-never-validated"],
        )

    def test_validate_phase_is_clean(self):
        report = self.analyse_fixture("expand_contract", "0006_validate_phase.sql")
        self.assertEqual(report.problems, [])

    def test_expand_contract_files_are_numbered_in_order(self):
        reports = analyzer.analyse_path(fixture("expand_contract"))
        names = [os.path.basename(r.path) for r in reports]
        self.assertEqual(names, sorted(names))
        self.assertIn("0004_expand_phase.sql", names[0])
        self.assertIn("0006_validate_phase.sql", names[-1])


class DangerousFixtureTests(LintTestCase):
    """One assertion per advertised detection, on the same file."""

    def setUp(self):
        self.report = self.analyse_fixture("dangerous", "0007_risky_account_changes.sql")
        self.ids = set(self.rule_ids(self.report))

    def test_drop_column_and_drop_table(self):
        self.assertIn("destructive-statement", self.ids)
        hits = [f for f in self.report.findings if f.rule_id == "destructive-statement"]
        self.assertEqual(len(hits), 2)  # DROP COLUMN at line 20, DROP TABLE at line 28
        lines = sorted(f.line for f in hits)
        self.assertEqual(lines, [20, 28])

    def test_alter_column_type(self):
        self.assertIn("type-unknown", self.ids)

    def test_create_index_without_concurrently(self):
        self.assertIn("index-not-concurrent", self.ids)

    def test_drop_index_without_concurrently(self):
        self.assertIn("index-drop-not-concurrent", self.ids)

    def test_add_column_not_null_without_default(self):
        finding = self.finding_for(self.report, "add-column-not-null")
        self.assertEqual(finding.line, 5)

    def test_add_column_with_default(self):
        self.assertIn("add-column-default", self.ids)

    def test_add_constraint_without_not_valid(self):
        self.assertIn("constraint-not-valid", self.ids)

    def test_set_not_null(self):
        self.assertIn("set-not-null", self.ids)

    def test_unbounded_update_and_delete(self):
        hits = [f for f in self.report.findings if f.rule_id == "unbounded-dml"]
        self.assertEqual(len(hits), 2)

    def test_multiple_alters_on_one_table(self):
        self.assertIn("lock-churn", self.ids)

    def test_no_transaction_control(self):
        self.assertIn("no-transaction-control", self.ids)

    def test_create_database_warning(self):
        self.assertIn("transaction-illegal-risk", self.ids)

    def test_reindex(self):
        self.assertIn("reindex-locking", self.ids)

    def test_vacuum_full(self):
        self.assertIn("vacuum-full", self.ids)

    def test_lock_timeout_missing(self):
        self.assertIn("lock-timeout-missing", self.ids)

    def test_every_finding_has_a_message_and_a_rewrite(self):
        self.assertGreater(len(self.report.problems), 10)
        for finding in self.report.problems:
            self.assertTrue(finding.message, f"{finding.rule_id} needs a reason")
            self.assertTrue(finding.suggestion, f"{finding.rule_id} needs a safer rewrite")
            self.assertGreater(finding.line, 0)

    def test_every_finding_names_a_confidence(self):
        allowed = {"verified", "likely", "judgement"}
        for finding in self.report.problems:
            self.assertIn(finding.confidence, allowed)

    def test_worst_risk_is_high(self):
        self.assertEqual(self.report.worst_risk, "high")

    def test_high_risk_filter_keeps_only_high(self):
        high = self.report.filtered("high")
        self.assertTrue(high)
        self.assertTrue(all(f.risk == "high" for f in high))

    def test_lower_threshold_reports_more(self):
        self.assertGreaterEqual(
            len(self.report.filtered("low")), len(self.report.filtered("medium"))
        )


class TransactionFixtureTests(LintTestCase):
    def setUp(self):
        self.report = self.analyse_fixture("transaction", "0008_transaction_mistakes.sql")

    def test_concurrently_inside_transaction(self):
        finding = self.finding_for(self.report, "transaction-concurrently")
        self.assertEqual(finding.risk, "high")

    def test_vacuum_inside_transaction_is_illegal(self):
        finding = self.finding_for(self.report, "transaction-illegal")
        self.assertEqual(finding.line, 13)

    def test_reindex_inside_transaction(self):
        self.assertIn("reindex-locking", self.problem_ids(self.report))

    def test_no_spurious_transaction_control_finding(self):
        # The file says BEGIN/COMMIT explicitly, so that rule must stay quiet.
        self.assertNotIn("no-transaction-control", self.problem_ids(self.report))


class CommentsFixtureTests(LintTestCase):
    """Everything dangerous in this fixture is inside a comment or a string."""

    def setUp(self):
        self.report = self.analyse_fixture("comments", "0009_comments_and_strings.sql")

    def test_no_findings_from_comments_or_literals(self):
        self.assertEqual(self.report.findings, [])

    def test_no_data_loss_findings(self):
        self.assertNotIn("destructive-statement", self.rule_ids(self.report))

    def test_no_unbounded_dml_from_dollar_quoted_bodies(self):
        self.assertNotIn("unbounded-dml", self.rule_ids(self.report))

    def test_function_bodies_are_not_parsed_as_migrations(self):
        self.assertNotIn("add-column-not-null", self.rule_ids(self.report))
        self.assertNotIn("reindex-locking", self.rule_ids(self.report))
        self.assertNotIn("vacuum-full", self.rule_ids(self.report))


class TypeFixturesTests(LintTestCase):
    def test_only_the_narrowing_change_is_high_risk(self):
        report = self.analyse_fixture("types", "0011_type_changes.sql")
        rewrites = [f for f in report.findings if f.rule_id == "type-rewrite"]
        self.assertEqual(len(rewrites), 1)
        self.assertEqual(rewrites[0].line, 13)
        safe = [f for f in report.findings if f.rule_id == "type-safe"]
        self.assertEqual(len(safe), 2)


class EdgeFixtureTests(LintTestCase):
    def setUp(self):
        self.report = self.analyse_fixture("edge", "0010_edge_cases.sql")

    def test_unterminated_statement_warns(self):
        self.assertTrue(any("not terminated" in e for e in self.report.parse_errors))

    def test_lock_timeout_zero_is_reported(self):
        self.assertIn("lock-timeout-disabled", self.problem_ids(self.report))

    def test_drop_database_is_reported(self):
        self.assertIn("destructive-statement", self.problem_ids(self.report))

    def test_nested_block_comment_does_not_hide_the_next_statement(self):
        finding = self.finding_for(self.report, "destructive-statement")
        self.assertIn("DROP DATABASE", finding.message)

    def test_bounded_update_is_not_unbounded(self):
        self.assertNotIn("unbounded-dml", self.problem_ids(self.report))


class OrderCheckTests(LintTestCase):
    def setUp(self):
        self.report = analyzer.check_order(fixture("order"))

    def test_duplicate_number_is_high_risk(self):
        finding = self.finding_for(self.report, "migration-order-duplicate")
        self.assertEqual(finding.risk, "high")
        self.assertIn("0002_add_accounts.sql", finding.statement)
        self.assertIn("0002_add_sessions.sql", finding.statement)

    def test_gap_is_reported(self):
        finding = self.finding_for(self.report, "migration-order-gap")
        self.assertEqual(finding.risk, "low")

    def test_unnumbered_file_is_reported(self):
        finding = self.finding_for(self.report, "migration-order-unnumbered")
        self.assertIn("hotfix_indexes.sql", finding.statement)

    def test_order_check_on_a_clean_directory_is_silent(self):
        report = analyzer.check_order(fixture("expand_contract"))
        self.assertEqual(report.findings, [])

    def test_order_check_on_a_single_file_asks_for_a_directory(self):
        report = analyzer.check_order(fixture("safe", "0001_add_email.sql"))
        self.assertIn("migration-order-single-file", [f.rule_id for f in report.findings])


class DirectoryRunTests(LintTestCase):
    def test_directory_run_returns_one_report_per_file(self):
        reports = analyzer.analyse_path(fixture("dangerous"))
        self.assertEqual(len(reports), 1)

    def test_directory_run_on_order_fixture_analyses_every_sql_file(self):
        reports = analyzer.analyse_path(fixture("order"))
        self.assertEqual(len(reports), 5)

    def test_directory_run_ignores_non_sql_files(self):
        reports = analyzer.analyse_path(fixture("safe"))
        self.assertTrue(all(r.path.endswith(".sql") for r in reports))


if __name__ == "__main__":
    unittest.main()
