"""Shared helpers for the sql-migration-lint test suite (standard library only)."""

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def fixture(*parts):
    """Absolute path to a fixture file or directory."""
    return os.path.join(FIXTURES, *parts)


class LintTestCase(unittest.TestCase):
    """Base class with small helpers over the analyzer."""

    def analyse(self, sql, path="<test>.sql"):
        from sqlmig import analyzer
        return analyzer.analyse_text(sql, path)

    def analyse_fixture(self, *parts):
        from sqlmig import analyzer
        return analyzer.analyse_file(fixture(*parts))

    def rule_ids(self, report):
        return [f.rule_id for f in report.findings]

    def problem_ids(self, report):
        return [f.rule_id for f in report.problems]

    def finding_for(self, report, rule_id):
        for finding in report.findings:
            if finding.rule_id == rule_id:
                return finding
        self.fail(
            f"no finding with rule {rule_id!r}; got {sorted(set(self.rule_ids(report)))}"
        )

    def operation_kinds(self, report):
        return [op.kind for op in report.operations]

    def assertNoProblems(self, report, msg=None):
        self.assertEqual(
            self.problem_ids(report), [],
            msg or "expected no findings, got: "
                   + "; ".join(f"{f.rule_id}@line{f.line}" for f in report.problems),
        )
