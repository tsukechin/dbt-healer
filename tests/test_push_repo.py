import unittest
from types import SimpleNamespace

from app import push_repo


class PushRepoTests(unittest.TestCase):
    def test_extract_solution_parts_reads_summary(self):
        """Check generated patch summary is parsed with solution content."""
        solution = (
            "<solution>\nselect 1 as id\n</solution>\n"
            "<file>\nmodels/core/customers.sql\n</file>\n"
            "<summary>\nИзменено: исправлен id.\nОшибка: модель customers.\nПричина: падал dbt build.\n</summary>"
        )

        self.assertEqual(
            push_repo.extract_solution_parts(solution),
            [(
                "\nselect 1 as id\n",
                "\nmodels/core/customers.sql\n",
                "\nИзменено: исправлен id.\nОшибка: модель customers.\nПричина: падал dbt build.\n",
            )],
        )

    def test_create_branch_uses_healer_dbt_prefix(self):
        """Check generated branches use the healer/dbt namespace."""
        class Repo:
            def __init__(self):
                self.created_ref = None

            def get_git_ref(self, ref):
                return SimpleNamespace(object=SimpleNamespace(sha="abc123"))

            def create_git_ref(self, ref, sha):
                self.created_ref = (ref, sha)

        repo = Repo()

        branch = push_repo.create_branch(repo, "main")

        self.assertTrue(branch.startswith("healer/dbt-fix-patch-"))
        self.assertEqual(repo.created_ref, (f"refs/heads/{branch}", "abc123"))

    def test_commit_message_includes_change_error_and_reason(self):
        """Check commit messages carry detailed model summary."""
        message = push_repo.commit_message(
            "models/core/customers.sql",
            "Изменено: переименован ключ клиента.\nОшибка: модель customers выбирала cust_idx.\nПричина: тесты ожидают cust_id.",
        )

        self.assertIn("Исправлена dbt-ошибка в models/core/customers.sql", message)
        self.assertIn("Изменено: переименован ключ клиента.", message)
        self.assertIn("Ошибка: модель customers выбирала cust_idx.", message)
        self.assertIn("Причина: тесты ожидают cust_id.", message)

    def test_solution_summaries_use_russian_fallback(self):
        """Check fallback request body text is Russian."""
        body = push_repo.solution_summaries([("select 1", "models/core/customers.sql", "")])

        self.assertIn("Изменено: обновлен файл models/core/customers.sql.", body)
        self.assertNotIn("Changed:", body)


if __name__ == "__main__":
    unittest.main()
