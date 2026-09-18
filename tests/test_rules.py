"""Rule tests: one or more assertions per detection the tool advertises.

The negative control (a safe migration producing zero findings) and the
expand/contract migrations live in :mod:`test_fixtures`; this module tests the
rules themselves on small inline migrations.
"""

import unittest

from support import LintTestCase

from sqlmig import rules
from sqlmig import analyzer


class DestructiveStatementTests(LintTestCase):
    def test_drop_column_is_high_risk(self):
        report = self.analyse("ALTER TABLE users DROP COLUMN legacy;")
        finding = self.finding_for(report, "destructive-statement")
        self.assertEqual(finding.risk, "high")
        self.assertIn("users.legacy", finding.message)
        self.assertIn("expand/contract", finding.suggestion)

    def test_drop_table_names_the_table(self):
        report = self.analyse("DROP TABLE accounts;")
        finding = self.finding_for(report, "destructive-statement")
        self.assertIn("DROP TABLE accounts", finding.message)

    def test_drop_table_cascade_is_mentioned(self):
        report = self.analyse("DROP TABLE accounts CASCADE;")
        finding = self.finding_for(report, "destructive-statement")
        self.assertIn("CASCADE", finding.message)

    def test_truncate_is_reported(self):
        report = self.analyse("TRUNCATE sessions;")
        finding = self.finding_for(report, "destructive-statement")
        self.assertEqual(finding.risk, "high")
        self.assertIn("TRUNCATE", finding.message)

    def test_drop_database_is_reported(self):
        report = self.analyse("DROP DATABASE reporting;")
        self.assertTrue(
            any(f.rule_id == "destructive-statement" for f in report.findings)
        )


class IndexConcurrencyTests(LintTestCase):
    def test_create_index_without_concurrently(self):
        report = self.analyse("CREATE INDEX idx_a ON users (email);")
        finding = self.finding_for(report, "index-not-concurrent")
        self.assertEqual(finding.risk, "high")
        self.assertEqual(finding.confidence, "verified")
        self.assertIn("CONCURRENTLY", finding.suggestion)

    def test_create_unique_index_without_concurrently(self):
        report = self.analyse("CREATE UNIQUE INDEX idx_a ON users (email);")
        self.assertIn("index-not-concurrent", self.problem_ids(report))

    def test_create_index_concurrently_is_clean(self):
        report = self.analyse("CREATE INDEX CONCURRENTLY idx_a ON users (email);")
        self.assertNotIn("index-not-concurrent", self.problem_ids(report))
        self.assertIn("create-index-concurrently", self.operation_kinds(report))

    def test_drop_index_without_concurrently(self):
        report = self.analyse("DROP INDEX idx_a;")
        finding = self.finding_for(report, "index-drop-not-concurrent")
        self.assertEqual(finding.risk, "high")
        self.assertIn("idx_a", finding.message)

    def test_drop_index_concurrently_is_clean(self):
        report = self.analyse("DROP INDEX CONCURRENTLY idx_a;")
        self.assertNotIn("index-drop-not-concurrent", self.problem_ids(report))
        self.assertIn("drop-index-concurrently", self.operation_kinds(report))

    def test_index_lock_risk_levels(self):
        plain = self.analyse("CREATE INDEX idx_a ON users (email);")
        self.assertEqual(plain.operations[0].risk, "high")
        concurrent = self.analyse("CREATE INDEX CONCURRENTLY idx_a ON users (email);")
        self.assertEqual(concurrent.operations[0].risk, "medium")


class TypeChangeTests(LintTestCase):
    def test_widening_varchar_within_the_file_is_safe(self):
        report = self.analyse(
            "CREATE TABLE t (note varchar(50));\n"
            "ALTER TABLE t ALTER COLUMN note TYPE varchar(120);"
        )
        finding = self.finding_for(report, "type-safe")
        self.assertEqual(finding.risk, "low")
        self.assertIn("50 -> 120", finding.message)

    def test_text_to_bounded_varchar_rewrites(self):
        report = self.analyse(
            "CREATE TABLE t (note text);\n"
            "ALTER TABLE t ALTER COLUMN note TYPE varchar(80);"
        )
        finding = self.finding_for(report, "type-rewrite")
        self.assertEqual(finding.risk, "high")
        self.assertIn("rewrites every row", finding.message)

    def test_numeric_precision_increase_is_safe(self):
        report = self.analyse(
            "CREATE TABLE t (amount numeric(10,2));\n"
            "ALTER TABLE t ALTER COLUMN amount TYPE numeric(12,2);"
        )
        self.assertIn("type-safe", self.rule_ids(report))

    def test_numeric_precision_decrease_rewrites(self):
        report = self.analyse(
            "CREATE TABLE t (amount numeric(12,2));\n"
            "ALTER TABLE t ALTER COLUMN amount TYPE numeric(10,2);"
        )
        self.assertIn("type-rewrite", self.rule_ids(report))

    def test_unknown_pair_asks_for_review(self):
        report = self.analyse("ALTER TABLE metrics ALTER COLUMN balance TYPE varchar(40);")
        finding = self.finding_for(report, "type-unknown")
        self.assertEqual(finding.risk, "high")
        self.assertIn("pg_cast", finding.suggestion)

    def test_set_data_type_spelling_is_recognised(self):
        report = self.analyse(
            "CREATE TABLE t (n integer);\n"
            "ALTER TABLE t ALTER COLUMN n SET DATA TYPE bigint;"
        )
        self.assertIn("type-rewrite", self.rule_ids(report))

    def test_classifier_reports_direction(self):
        self.assertEqual(analyzer.classify_type_change("varchar(50)", "varchar(120)")[0], "safe")
        self.assertEqual(analyzer.classify_type_change("varchar(50)", "varchar(20)")[0], "unsafe")
        self.assertEqual(analyzer.classify_type_change("text", "text")[0], "safe")
        self.assertEqual(analyzer.classify_type_change("", "text")[0], "unknown")

    def test_safe_type_change_is_not_a_problem(self):
        report = self.analyse(
            "CREATE TABLE t (note varchar(50));\n"
            "ALTER TABLE t ALTER COLUMN note TYPE varchar(120);"
        )
        self.assertNotIn("type-safe", self.problem_ids(report))


class AddColumnTests(LintTestCase):
    def test_not_null_without_default(self):
        report = self.analyse("ALTER TABLE accounts ADD COLUMN email text NOT NULL;")
        finding = self.finding_for(report, "add-column-not-null")
        self.assertEqual(finding.risk, "high")
        self.assertEqual(finding.confidence, "verified")
        self.assertIn("fails", finding.message)

    def test_add_column_if_not_exists_not_null_without_default(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD COLUMN IF NOT EXISTS email text NOT NULL;"
        )
        self.assertIn("add-column-not-null", self.problem_ids(report))

    def test_not_null_with_default_is_not_the_failing_case(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD COLUMN email text NOT NULL DEFAULT '';"
        )
        self.assertNotIn("add-column-not-null", self.problem_ids(report))
        self.assertIn("add-column-default", self.problem_ids(report))

    def test_default_on_older_postgres_is_still_flagged(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD COLUMN created_at timestamptz DEFAULT now();"
        )
        finding = self.finding_for(report, "add-column-default")
        self.assertEqual(finding.risk, "medium")
        self.assertEqual(finding.confidence, "likely")
        self.assertIn("PostgreSQL 11", finding.message)

    def test_nullable_column_is_clean(self):
        report = self.analyse("ALTER TABLE accounts ADD COLUMN email text;")
        self.assertNotIn("add-column-not-null", self.problem_ids(report))
        self.assertNotIn("add-column-default", self.problem_ids(report))
        self.assertIn("add-column-safe", self.rule_ids(report))

    def test_bare_add_column_is_clean_of_column_rules(self):
        # lock-timeout-missing is expected here; no column rule should fire.
        report = self.analyse(
            "SET lock_timeout = '5s';\nALTER TABLE accounts ADD COLUMN email text;"
        )
        self.assertEqual(self.problem_ids(report), [])

    def test_not_null_on_add_constraint_is_not_confused_with_add_column(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD CONSTRAINT nn CHECK (email IS NOT NULL) NOT VALID;"
        )
        self.assertNotIn("add-column-not-null", self.problem_ids(report))


class SetNotNullTests(LintTestCase):
    def test_set_not_null_reports_the_scan(self):
        report = self.analyse("ALTER TABLE accounts ALTER COLUMN nickname SET NOT NULL;")
        finding = self.finding_for(report, "set-not-null")
        self.assertEqual(finding.risk, "high")
        self.assertIn("CHECK", finding.suggestion)

    def test_check_not_valid_pattern_is_suggested(self):
        report = self.analyse("ALTER TABLE accounts ALTER COLUMN nickname SET NOT NULL;")
        finding = self.finding_for(report, "set-not-null")
        self.assertIn("NOT VALID", finding.suggestion)
        self.assertIn("VALIDATE CONSTRAINT", finding.suggestion)

    def test_drop_not_null_is_not_flagged_as_set_not_null(self):
        report = self.analyse("ALTER TABLE accounts ALTER COLUMN nickname DROP NOT NULL;")
        self.assertNotIn("set-not-null", self.problem_ids(report))


class ConstraintTests(LintTestCase):
    def test_foreign_key_without_not_valid(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD CONSTRAINT fk FOREIGN KEY (owner_id) REFERENCES users (id);"
        )
        finding = self.finding_for(report, "constraint-not-valid")
        self.assertEqual(finding.risk, "high")
        self.assertIn("fk", finding.message)

    def test_foreign_key_with_not_valid_is_not_validated_yet(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD CONSTRAINT fk FOREIGN KEY (owner_id) "
            "REFERENCES users (id) NOT VALID;"
        )
        self.assertNotIn("constraint-not-valid", self.problem_ids(report))
        self.assertIn("constraint-not-valid-ok", self.rule_ids(report))
        # The companion finding is the reminder to validate it later.
        self.assertIn("constraint-never-validated", self.problem_ids(report))

    def test_foreign_key_with_not_valid_then_validate_is_clean(self):
        report = self.analyse(
            "SET lock_timeout = '5s';\n"
            "ALTER TABLE accounts ADD CONSTRAINT fk FOREIGN KEY (owner_id) "
            "REFERENCES users (id) NOT VALID;\n"
            "ALTER TABLE accounts VALIDATE CONSTRAINT fk;"
        )
        ids = self.problem_ids(report)
        self.assertNotIn("constraint-never-validated", ids)
        self.assertNotIn("constraint-not-valid", ids)

    def test_unnamed_foreign_key_is_reported(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD FOREIGN KEY (owner_id) REFERENCES users (id);"
        )
        self.assertIn("constraint-not-valid", self.problem_ids(report))

    def test_check_without_not_valid(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD CONSTRAINT c CHECK (balance >= 0);"
        )
        self.assertIn("constraint-not-valid", self.problem_ids(report))

    def test_unique_constraint_points_at_concurrent_index(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD CONSTRAINT uq UNIQUE (email);"
        )
        finding = self.finding_for(report, "constraint-not-valid")
        self.assertIn("CREATE INDEX CONCURRENTLY", finding.suggestion)

    def test_not_valid_without_validate_is_reported(self):
        report = self.analyse(
            "ALTER TABLE accounts ADD CONSTRAINT fk FOREIGN KEY (owner_id) "
            "REFERENCES users (id) NOT VALID;"
        )
        finding = self.finding_for(report, "constraint-never-validated")
        self.assertIn("VALIDATE CONSTRAINT", finding.suggestion)

    def test_validate_in_a_later_migration_is_accepted(self):
        first = self.analyse(
            "ALTER TABLE accounts ADD CONSTRAINT fk FOREIGN KEY (owner_id) "
            "REFERENCES users (id) NOT VALID;",
            path="0001_add_fk.sql",
        )
        self.assertIn("constraint-never-validated", self.problem_ids(first))


class UnboundedDmlTests(LintTestCase):
    def test_update_without_where(self):
        report = self.analyse("UPDATE accounts SET verified = true;")
        finding = self.finding_for(report, "unbounded-dml")
        self.assertEqual(finding.risk, "high")
        self.assertIn("WHERE", finding.message)

    def test_delete_without_where(self):
        report = self.analyse("DELETE FROM sessions;")
        self.assertIn("unbounded-dml", self.problem_ids(report))

    def test_update_with_where_is_not_flagged(self):
        report = self.analyse("UPDATE accounts SET verified = true WHERE id = 1;")
        self.assertNotIn("unbounded-dml", self.problem_ids(report))

    def test_where_inside_a_subquery_still_counts(self):
        report = self.analyse(
            "UPDATE accounts SET x = 1 WHERE id IN (SELECT id FROM t WHERE a = 1);"
        )
        self.assertNotIn("unbounded-dml", self.problem_ids(report))

    def test_cte_delete_without_where(self):
        report = self.analyse(
            "WITH victims AS (SELECT id FROM sessions WHERE stale) DELETE FROM sessions;"
        )
        self.assertIn("unbounded-dml", self.problem_ids(report))

    def test_delete_with_where_is_clean(self):
        report = self.analyse("DELETE FROM sessions WHERE created_at < now() - interval '90 days';")
        self.assertNotIn("unbounded-dml", self.problem_ids(report))


class LockChurnTests(LintTestCase):
    def test_two_alters_on_one_table(self):
        report = self.analyse(
            "ALTER TABLE t ADD COLUMN a int;\n"
            "ALTER TABLE t ADD COLUMN b int;"
        )
        finding = self.finding_for(report, "lock-churn")
        self.assertEqual(finding.risk, "medium")
        self.assertIn("2 times", finding.message)

    def test_alters_on_different_tables_are_not_churn(self):
        report = self.analyse(
            "ALTER TABLE t ADD COLUMN a int;\n"
            "ALTER TABLE u ADD COLUMN b int;"
        )
        self.assertNotIn("lock-churn", self.problem_ids(report))

    def test_one_alter_with_many_actions_is_not_churn(self):
        report = self.analyse(
            "ALTER TABLE t ADD COLUMN a int, ADD COLUMN b int, ALTER COLUMN c SET DEFAULT 1;"
        )
        self.assertNotIn("lock-churn", self.problem_ids(report))

    def test_schema_qualification_is_normalised(self):
        report = self.analyse(
            "ALTER TABLE public.t ADD COLUMN a int;\n"
            "ALTER TABLE public.t ADD COLUMN b int;"
        )
        self.assertIn("lock-churn", self.problem_ids(report))


class LockTimeoutTests(LintTestCase):
    def test_missing_lock_timeout_before_alter(self):
        report = self.analyse("ALTER TABLE t ADD COLUMN a int;")
        finding = self.finding_for(report, "lock-timeout-missing")
        self.assertEqual(finding.risk, "medium")
        self.assertIn("SET lock_timeout", finding.suggestion)

    def test_lock_timeout_present_is_clean(self):
        report = self.analyse(
            "SET lock_timeout = '5s';\nALTER TABLE t ADD COLUMN a int;\nRESET lock_timeout;"
        )
        self.assertNotIn("lock-timeout-missing", self.problem_ids(report))
        self.assertNotIn("lock-timeout-disabled", self.problem_ids(report))

    def test_local_lock_timeout_is_accepted(self):
        report = self.analyse(
            "BEGIN;\nSET LOCAL lock_timeout = '3s';\nALTER TABLE t ADD COLUMN a int;\nCOMMIT;"
        )
        self.assertNotIn("lock-timeout-missing", self.problem_ids(report))

    def test_disabled_lock_timeout_is_reported(self):
        report = self.analyse("SET lock_timeout = 0;\nALTER TABLE t ADD COLUMN a int;")
        finding = self.finding_for(report, "lock-timeout-disabled")
        self.assertIn("disables", finding.message)

    def test_lock_timeout_not_needed_without_locking_statements(self):
        report = self.analyse("CREATE INDEX CONCURRENTLY idx ON t (a);")
        self.assertNotIn("lock-timeout-missing", self.problem_ids(report))


class TransactionTests(LintTestCase):
    def test_concurrent_index_inside_transaction(self):
        report = self.analyse(
            "BEGIN;\nCREATE INDEX CONCURRENTLY idx ON t (a);\nCOMMIT;"
        )
        finding = self.finding_for(report, "transaction-concurrently")
        self.assertEqual(finding.risk, "high")
        self.assertEqual(finding.confidence, "verified")

    def test_concurrent_index_outside_transaction_is_clean(self):
        report = self.analyse("CREATE INDEX CONCURRENTLY idx ON t (a);")
        self.assertNotIn("transaction-concurrently", self.problem_ids(report))

    def test_vacuum_inside_transaction(self):
        report = self.analyse("BEGIN;\nVACUUM ANALYZE t;\nCOMMIT;")
        finding = self.finding_for(report, "transaction-illegal")
        self.assertEqual(finding.risk, "high")

    def test_create_database_inside_transaction(self):
        report = self.analyse("BEGIN;\nCREATE DATABASE reporting;\nCOMMIT;")
        self.assertIn("transaction-illegal", self.problem_ids(report))

    def test_reindex_inside_transaction_is_still_reported(self):
        report = self.analyse("BEGIN;\nREINDEX TABLE t;\nCOMMIT;")
        self.assertIn("reindex-locking", self.problem_ids(report))

    def test_vacuum_outside_transaction_is_clean_of_the_illegal_rule(self):
        report = self.analyse("VACUUM ANALYZE t;")
        self.assertNotIn("transaction-illegal", self.problem_ids(report))

    def test_mixed_concurrently_and_ddl_without_control(self):
        report = self.analyse(
            "CREATE INDEX CONCURRENTLY idx ON t (a);\nALTER TABLE t ADD COLUMN b int;"
        )
        finding = self.finding_for(report, "no-transaction-control")
        self.assertEqual(finding.risk, "medium")
        self.assertIn("Split the file", finding.suggestion)

    def test_explicit_marker_silences_the_transaction_rule(self):
        report = self.analyse(
            "-- migrate: no-transaction\n"
            "CREATE INDEX CONCURRENTLY idx ON t (a);\n"
            "ALTER TABLE t ADD COLUMN b int;"
        )
        self.assertNotIn("no-transaction-control", self.problem_ids(report))

    def test_explicit_begin_and_commit_silences_the_transaction_rule(self):
        report = self.analyse("BEGIN;\nALTER TABLE t ADD COLUMN b int;\nCOMMIT;")
        self.assertNotIn("no-transaction-control", self.problem_ids(report))

    def test_plain_transactional_ddl_needs_no_transaction_control(self):
        report = self.analyse("CREATE TABLE t (a int);")
        self.assertNotIn("no-transaction-control", self.problem_ids(report))

    def test_marker_inside_a_string_is_not_a_declaration(self):
        report = self.analyse(
            "INSERT INTO notes (body) VALUES ('-- migrate: no-transaction');\n"
            "CREATE INDEX CONCURRENTLY idx ON t (a);\n"
            "ALTER TABLE t ADD COLUMN b int;"
        )
        self.assertIn("no-transaction-control", self.problem_ids(report))

    def test_marker_detection_helper(self):
        self.assertTrue(analyzer.declares_non_transactional("-- migrate: no-transaction\nSELECT 1;"))
        self.assertTrue(analyzer.declares_non_transactional("-- disable_ddl_transaction\nSELECT 1;"))
        self.assertFalse(analyzer.declares_non_transactional("SELECT '-- no-transaction';"))


class VacuumAndReindexTests(LintTestCase):
    def test_vacuum_full_is_high_risk(self):
        report = self.analyse("VACUUM FULL accounts;")
        finding = self.finding_for(report, "vacuum-full")
        self.assertEqual(finding.risk, "high")
        self.assertIn("ACCESS EXCLUSIVE", finding.message)

    def test_plain_vacuum_is_not_a_problem(self):
        report = self.analyse("VACUUM ANALYZE accounts;")
        self.assertEqual(self.problem_ids(report), [])

    def test_reindex_table_is_high_risk(self):
        report = self.analyse("REINDEX TABLE accounts;")
        finding = self.finding_for(report, "reindex-locking")
        self.assertEqual(finding.risk, "high")

    def test_reindex_concurrently_is_medium(self):
        report = self.analyse("REINDEX INDEX CONCURRENTLY idx_accounts_email;")
        finding = self.finding_for(report, "reindex-locking")
        self.assertEqual(finding.risk, "medium")
        self.assertIn("SHARE UPDATE EXCLUSIVE", finding.message)


class OperationRiskTests(LintTestCase):
    def test_every_operation_carries_a_risk_and_a_reason(self):
        report = self.analyse(
            "ALTER TABLE t ADD COLUMN a int;\nCREATE INDEX CONCURRENTLY idx ON t (a);"
        )
        self.assertEqual(len(report.operations), 2)
        for op in report.operations:
            self.assertIn(op.risk, rules.RISK_LEVELS)
            self.assertTrue(op.note or op.kind, "operation should describe itself")

    def test_worst_risk_is_the_dangerous_one(self):
        report = self.analyse(
            "ALTER TABLE t ADD COLUMN a int;\nDROP TABLE t;"
        )
        self.assertEqual(report.worst_risk, "high")

    def test_min_risk_filtering(self):
        report = self.analyse(
            "ALTER TABLE t ADD COLUMN a int NOT NULL;\nALTER TABLE t ALTER COLUMN b DROP NOT NULL;"
        )
        high_only = report.filtered("high")
        all_of_them = report.filtered("low")
        self.assertLessEqual(len(high_only), len(all_of_them))
        self.assertTrue(all(f.risk == "high" for f in high_only))

    def test_statement_count_and_lines(self):
        report = self.analyse("SELECT 1;\nSELECT 2;\n")
        self.assertEqual(report.statement_count, 2)
        self.assertEqual(report.total_lines, 3)


class ParserHonestyTests(LintTestCase):
    """Findings must not come from text the SQL parser would not execute."""

    def test_decoys_in_comments_produce_nothing(self):
        report = self.analyse(
            "/*\nDROP TABLE accounts;\nALTER TABLE accounts ALTER COLUMN b TYPE text;\n*/\n"
            "-- DROP COLUMN legacy; CREATE DATABASE reporting;\n"
            "SET lock_timeout = '5s';\n"
            "ALTER TABLE accounts ADD COLUMN ok text;"
        )
        self.assertEqual(self.problem_ids(report), [])

    def test_decoys_in_string_literals_produce_nothing(self):
        report = self.analyse(
            "INSERT INTO audit_log (message)\n"
            "VALUES ('DROP TABLE accounts; ALTER TABLE t ALTER COLUMN c TYPE text;');"
        )
        self.assertEqual(self.problem_ids(report), [])

    def test_decoys_in_function_bodies_produce_nothing(self):
        report = self.analyse(
            "CREATE FUNCTION f() RETURNS trigger AS $$\n"
            "BEGIN\n"
            "    -- DROP TABLE accounts;\n"
            "    UPDATE accounts SET seen = true;\n"
            "    RETURN NEW;\n"
            "END;\n"
            "$$ LANGUAGE plpgsql;"
        )
        self.assertEqual(self.problem_ids(report), [])

    def test_dollar_quote_does_not_hide_the_next_statement(self):
        report = self.analyse(
            "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;\n"
            "DROP TABLE accounts;"
        )
        self.assertIn("destructive-statement", self.problem_ids(report))

    def test_keyword_inside_quoted_identifier_is_not_a_statement(self):
        report = self.analyse('SELECT "delete" FROM accounts;')
        self.assertEqual(self.problem_ids(report), [])

    def test_unterminated_statement_is_reported_as_a_parse_warning(self):
        report = self.analyse("DROP TABLE accounts")
        self.assertTrue(any("not terminated" in err for err in report.parse_errors))


if __name__ == "__main__":
    unittest.main()
