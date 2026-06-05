import httpcore
import unittest

from app.providers import (
    final_solution,
    is_valid_solution,
    retry_request,
    source_paths,
)


FILE_CONTEXT = "SOURCE OF models/core/customers.sql: select 1 as id\nFILE DIFF: NO_DIFF"


class ProviderOutputTests(unittest.TestCase):
    def test_source_paths_are_extracted_from_context(self):
        """Check source paths are extracted from context."""
        self.assertEqual(source_paths(FILE_CONTEXT), ["models/core/customers.sql"])

    def test_valid_solution_must_use_source_context_path(self):
        """Check valid solution uses source context path."""
        response = (
            "<solution>\nselect 2 as id\n</solution>\n"
            "<file>\nmodels/core/customers.sql\n</file>\n"
            "<summary>\nИзменено: выбрана константа id.\nОшибка: модель customers.\nПричина: восстановлен ожидаемый id.\n</summary>"
        )

        self.assertTrue(is_valid_solution(response, FILE_CONTEXT))

    def test_real_solution_requires_summary(self):
        """Check real fixes must include commit summary details."""
        response = (
            "<solution>\nselect 2 as id\n</solution>\n"
            "<file>\nmodels/core/customers.sql\n</file>"
        )

        self.assertFalse(is_valid_solution(response, FILE_CONTEXT))

    def test_solution_with_unknown_path_is_invalid(self):
        """Check solution with unknown path is rejected."""
        response = (
            "<solution>\nselect 2 as id\n</solution>\n"
            "<file>\nmodels/other.sql\n</file>"
        )

        self.assertFalse(is_valid_solution(response, FILE_CONTEXT))

    def test_prose_response_falls_back_to_no_fix(self):
        """Check prose response falls back to NO_FIX."""
        result = final_solution(FILE_CONTEXT, "The error is probably in a SQL model.")

        self.assertEqual(
            result,
            "<solution>NO_FIX</solution>\n<file>\nmodels/core/customers.sql\n</file>",
        )

    def test_retry_can_repair_bad_response(self):
        """Check retry callback can repair bad response."""
        repaired = (
            "<solution>NO_FIX</solution>\n"
            "<file>\nmodels/core/customers.sql\n</file>"
        )

        self.assertEqual(
            final_solution(FILE_CONTEXT, "plain prose", retry=lambda _: repaired),
            repaired,
        )

    def test_retry_request_retries_transient_errors(self):
        """Check transient errors are retried."""
        calls = {"count": 0}

        def flaky_call():
            """Fail once before returning success."""
            calls["count"] += 1
            if calls["count"] == 1:
                raise httpcore.ReadError("temporary read error")
            return "ok"

        self.assertEqual(retry_request(flaky_call, attempts=2), "ok")
        self.assertEqual(calls["count"], 2)


if __name__ == "__main__":
    unittest.main()
