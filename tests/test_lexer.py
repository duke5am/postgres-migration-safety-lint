"""Lexer tests: the parsing-honesty guarantee.

A regex-based linter reports `DROP TABLE` inside a comment, a string literal or a
`CREATE FUNCTION` body. These tests prove this one does not, by checking both the
token stream and the resulting findings.
"""

import unittest

from support import LintTestCase

from sqlmig.lexer import tokenize, split_statements, leading_verb


def word_tokens(sql):
    """Only the keyword/identifier tokens, so keyword checks are meaningful."""
    return [t for t in tokenize(sql) if t.kind == "word"]


def words(sql):
    return [t.normalized for t in word_tokens(sql)]


def code_tokens(sql):
    return [t for t in tokenize(sql) if t.kind not in ("comment",)]


class TokenizerTests(LintTestCase):
    def kinds(self, sql):
        return [(t.kind, t.text) for t in code_tokens(sql)]

    def test_line_comments_are_skipped(self):
        self.assertNotIn("DROP", words("SELECT 1; -- DROP TABLE users;\nSELECT 2;"))
        self.assertEqual(words("SELECT 1; -- DROP TABLE users;\nSELECT 2;"),
                         ["SELECT", "SELECT"])

    def test_block_comments_are_skipped(self):
        sql = "/* DROP TABLE users; */ SELECT 1;"
        self.assertEqual(words(sql), ["SELECT"])
        self.assertEqual(len([t for t in tokenize(sql) if t.kind == "comment"]), 1)

    def test_block_comments_nest(self):
        sql = "/* outer /* DROP TABLE inner; */ still a comment */ SELECT 1;"
        self.assertEqual(words(sql), ["SELECT"])
        comments = [t for t in tokenize(sql) if t.kind == "comment"]
        self.assertEqual(len(comments), 1)
        self.assertIn("DROP TABLE inner", comments[0].text)

    def test_string_literal_is_one_token(self):
        sql = "INSERT INTO t VALUES ('a; DROP TABLE users;');"
        self.assertIn(("string", "'a; DROP TABLE users;'"), self.kinds(sql))
        self.assertNotIn("DROP", words(sql))

    def test_doubled_quote_inside_string_literal(self):
        sql = "INSERT INTO t VALUES ('it''s; DROP TABLE users;');"
        strings = [t for t in tokenize(sql) if t.kind == "string"]
        self.assertEqual(len(strings), 1)
        self.assertEqual(strings[0].text, "'it''s; DROP TABLE users;'")
        self.assertNotIn("DROP", words(sql))

    def test_escape_string_prefix(self):
        sql = r"INSERT INTO t VALUES (E'it\'s; DROP TABLE users;');"
        self.assertNotIn("DROP", words(sql))
        strings = [t for t in tokenize(sql) if t.kind == "string"]
        self.assertEqual(len(strings), 1)

    def test_quoted_identifier_is_not_a_keyword(self):
        sql = 'SELECT "drop" FROM t;'
        self.assertIn(("quoted_ident", '"drop"'), self.kinds(sql))
        self.assertNotIn("DROP", words(sql))

    def test_dollar_quoted_body_is_one_token(self):
        sql = "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; DROP TABLE users; $$ LANGUAGE sql;"
        strings = [t for t in tokenize(sql) if t.kind == "string"]
        self.assertEqual(len(strings), 1)
        self.assertIn("DROP TABLE users;", strings[0].text)
        self.assertNotIn("DROP", words(sql))

    def test_named_dollar_quote_tag(self):
        sql = "CREATE FUNCTION f() RETURNS int AS $body$ DROP TABLE users; $body$ LANGUAGE sql;"
        strings = [t for t in tokenize(sql) if t.kind == "string"]
        self.assertEqual(len(strings), 1)
        self.assertEqual(strings[0].text, "$body$ DROP TABLE users; $body$")

    def test_two_dollar_quotes_do_not_merge(self):
        sql = ("CREATE FUNCTION a() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;\n"
               "CREATE FUNCTION b() RETURNS int AS $$ SELECT 2; $$ LANGUAGE sql;")
        self.assertEqual(len(split_statements(sql)), 2)

    def test_number_token_does_not_eat_following_words(self):
        sql = "SELECT 10, 2;"
        self.assertEqual([t.text for t in tokenize(sql) if t.kind == "number"], ["10", "2"])
        self.assertEqual(words(sql), ["SELECT"])

    def test_line_numbers_are_one_based(self):
        tokens = word_tokens("SELECT 1;\n\nDROP TABLE t;")
        self.assertEqual(tokens[0].line, 1)
        self.assertEqual(tokens[1].line, 3)

    def test_cast_operator_is_single_token(self):
        self.assertIn(("operator", "::"), self.kinds("SELECT balance::numeric(12,2) FROM t;"))


class StatementSplitTests(LintTestCase):
    def test_split_on_top_level_semicolons(self):
        self.assertEqual(len(split_statements("SELECT 1; SELECT 2; SELECT 3;")), 3)

    def test_semicolon_inside_parentheses_does_not_split(self):
        self.assertEqual(len(split_statements("CREATE TABLE t (a int); /* ( ; ) */ SELECT 1;")), 2)

    def test_semicolon_inside_string_does_not_split(self):
        self.assertEqual(len(split_statements("SELECT ';'; SELECT 2;")), 2)

    def test_unterminated_statement_is_flagged(self):
        statements = split_statements("SELECT 1;\nDROP TABLE t")
        self.assertEqual(len(statements), 2)
        self.assertTrue(statements[0].terminated)
        self.assertFalse(statements[1].terminated)

    def test_statement_lines_span_multiple_lines(self):
        statements = split_statements(
            "ALTER TABLE t\n    ADD COLUMN a int,\n    ADD COLUMN b int;"
        )
        self.assertEqual(statements[0].start_line, 1)
        self.assertEqual(statements[0].end_line, 3)

    def test_comment_only_tail_is_not_a_statement(self):
        self.assertEqual(len(split_statements("SELECT 1;\n-- trailing note\n")), 1)


class LeadingVerbTests(LintTestCase):
    def verb(self, sql):
        return split_statements(sql)[0].verb

    def test_plain_verb(self):
        self.assertEqual(self.verb("SELECT 1;"), "SELECT")

    def test_with_clause_returns_real_verb(self):
        self.assertEqual(
            self.verb(
                "WITH recent AS (SELECT id FROM t) DELETE FROM t "
                "WHERE id IN (SELECT id FROM recent);"
            ),
            "DELETE",
        )

    def test_with_clause_update(self):
        self.assertEqual(self.verb("WITH x AS (SELECT 1) UPDATE t SET a = 1;"), "UPDATE")

    def test_leading_comment_is_ignored(self):
        self.assertEqual(self.verb("-- a comment\nVACUUM ANALYZE t;"), "VACUUM")

    def test_drop_returns_drop(self):
        self.assertEqual(self.verb("DROP TABLE t;"), "DROP")


if __name__ == "__main__":
    unittest.main()
