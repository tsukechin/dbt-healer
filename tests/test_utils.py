import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import utils


class DbtLogParsingTests(unittest.TestCase):
    def setUp(self):
        """Store original utils config."""
        self.original_config = utils.config

    def tearDown(self):
        """Restore original utils config."""
        utils.config = self.original_config

    def test_extracts_explicit_model_path_from_dbt_error(self):
        """Check explicit model path is extracted from dbt error."""
        log = "Database Error in model customers (models/core/customers.sql)"

        self.assertEqual(
            utils.get_error_files_from_dbt_log(log),
            ["models/core/customers.sql"],
        )

    def test_extracts_macro_path_from_dbt_error(self):
        """Check macro path is extracted from dbt error."""
        log = "Compilation Error in macro cents_to_dollars (macros/money.sql)"

        self.assertEqual(
            utils.get_error_files_from_dbt_log(log),
            ["macros/money.sql"],
        )

    def test_resolves_model_name_from_manifest(self):
        """Check model name resolves through manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            project_path = repo_root / "jaffle_shop"
            manifest_path = project_path / "target" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "model.jaffle_shop.customers": {
                                "resource_type": "model",
                                "name": "customers",
                                "alias": "customers",
                                "original_file_path": "models/core/customers.sql",
                            }
                        },
                        "macros": {},
                    }
                ),
                encoding="utf-8",
            )
            utils.config = SimpleNamespace(dbt_project_name="jaffle_shop", repo_root=repo_root)

            self.assertEqual(
                utils.get_error_files_from_dbt_log("Database Error in model customers"),
                ["models/core/customers.sql"],
            )

    def test_test_failure_resolves_tested_model_from_manifest(self):
        """Check test failure resolves tested model through manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            project_path = repo_root / "shops_dwh"
            manifest_path = project_path / "target" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "model.shops_dwh.mart_customer_360": {
                                "resource_type": "model",
                                "name": "mart_customer_360",
                                "alias": "mart_customer_360",
                                "original_file_path": "models/mart/mart_customer_360.sql",
                            },
                            "test.shops_dwh.unique_mart_customer_360_cust_id.abc123": {
                                "resource_type": "test",
                                "name": "unique_mart_customer_360_cust_id",
                                "original_file_path": "models/mart/_mart_layer_doc.yml",
                                "depends_on": {
                                    "nodes": ["model.shops_dwh.mart_customer_360"]
                                },
                            },
                        },
                        "macros": {},
                    }
                ),
                encoding="utf-8",
            )
            utils.config = SimpleNamespace(dbt_project_name="shops_dwh", repo_root=repo_root)

            log = "Failure in test unique_mart_customer_360_cust_id (models/mart/_mart_layer_doc.yml)"

            self.assertEqual(
                utils.get_error_files_from_dbt_log(log),
                ["models/mart/mart_customer_360.sql"],
            )

    def test_relationship_test_resolves_from_model_when_from_field_fails(self):
        """Check relationships source column errors resolve to checked model."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            project_path = repo_root / "shops_dwh"
            manifest_path = project_path / "target" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "model.shops_dwh.core_shopping_mall": {
                                "resource_type": "model",
                                "name": "core_shopping_mall",
                                "alias": "core_shopping_mall",
                                "original_file_path": "models/core/core_shopping_mall.sql",
                            },
                            "model.shops_dwh.core_invoices": {
                                "resource_type": "model",
                                "name": "core_invoices",
                                "alias": "core_invoices",
                                "original_file_path": "models/core/core_invoices.sql",
                            },
                            "test.shops_dwh.relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_.abc123": {
                                "resource_type": "test",
                                "name": "relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_",
                                "original_file_path": "models/core/_core_layer_doc.yml",
                                "column_name": "sm_id",
                                "test_metadata": {
                                    "name": "relationships",
                                    "kwargs": {
                                        "column_name": "sm_id",
                                        "field": "sm_id",
                                        "to": "ref('core_shopping_mall')",
                                    },
                                },
                                "depends_on": {
                                    "nodes": [
                                        "model.shops_dwh.core_shopping_mall",
                                        "model.shops_dwh.core_invoices",
                                    ]
                                },
                            },
                        },
                        "macros": {},
                    }
                ),
                encoding="utf-8",
            )
            utils.config = SimpleNamespace(dbt_project_name="shops_dwh", repo_root=repo_root)

            log = (
                "Failure in test relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_ "
                "(models/core/_core_layer_doc.yml)\n"
                "Database Error in test relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_ "
                "(models/core/_core_layer_doc.yml)\n"
                "  column \"sm_id\" does not exist\n"
                "  LINE 15:     select sm_id as from_field\n"
                "  HINT:  Perhaps you meant to reference the column \"core_invoices.sm_idd\"."
            )

            self.assertEqual(
                utils.get_error_files_from_dbt_log(log),
                ["models/core/core_invoices.sql"],
            )

    def test_relationship_test_resolves_to_model_when_to_field_fails(self):
        """Check relationships target column errors resolve to referenced model."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            project_path = repo_root / "shops_dwh"
            manifest_path = project_path / "target" / "manifest.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "model.shops_dwh.core_shopping_mall": {
                                "resource_type": "model",
                                "name": "core_shopping_mall",
                                "alias": "core_shopping_mall",
                                "original_file_path": "models/core/core_shopping_mall.sql",
                            },
                            "model.shops_dwh.core_invoices": {
                                "resource_type": "model",
                                "name": "core_invoices",
                                "alias": "core_invoices",
                                "original_file_path": "models/core/core_invoices.sql",
                            },
                            "test.shops_dwh.relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_.abc123": {
                                "resource_type": "test",
                                "name": "relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_",
                                "original_file_path": "models/core/_core_layer_doc.yml",
                                "column_name": "sm_id",
                                "test_metadata": {
                                    "name": "relationships",
                                    "kwargs": {
                                        "column_name": "sm_id",
                                        "field": "sm_id",
                                        "to": "ref('core_shopping_mall')",
                                    },
                                },
                                "depends_on": {
                                    "nodes": [
                                        "model.shops_dwh.core_invoices",
                                        "model.shops_dwh.core_shopping_mall",
                                    ]
                                },
                            },
                        },
                        "macros": {},
                    }
                ),
                encoding="utf-8",
            )
            utils.config = SimpleNamespace(dbt_project_name="shops_dwh", repo_root=repo_root)

            log = (
                "Failure in test relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_ "
                "(models/core/_core_layer_doc.yml)\n"
                "Database Error in test relationships_core_invoices_sm_id__sm_id__ref_core_shopping_mall_ "
                "(models/core/_core_layer_doc.yml)\n"
                "  column \"sm_id\" does not exist\n"
                "  LINE 29:     select sm_id as to_field"
            )

            self.assertEqual(
                utils.get_error_files_from_dbt_log(log),
                ["models/core/core_shopping_mall.sql"],
            )

    def test_test_failure_infers_model_from_test_name_without_manifest(self):
        """Check test failure infers model name without manifest."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            project_path = repo_root / "shops_dwh"
            model_path = project_path / "models" / "mart" / "mart_customer_360.sql"
            model_path.parent.mkdir(parents=True)
            model_path.write_text("select 1 as cust_id", encoding="utf-8")
            utils.config = SimpleNamespace(dbt_project_name="shops_dwh", repo_root=repo_root)

            log = "Failure in test unique_mart_customer_360_cust_id (models/mart/_mart_layer_doc.yml)"

            self.assertEqual(
                utils.get_error_files_from_dbt_log(log),
                ["models/mart/mart_customer_360.sql"],
            )

    def test_unresolved_test_failure_does_not_fall_back_to_yaml_doc(self):
        """Check unresolved test failure does not use yaml doc path."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            project_path = repo_root / "shops_dwh"
            project_path.mkdir(parents=True)
            utils.config = SimpleNamespace(dbt_project_name="shops_dwh", repo_root=repo_root)

            log = "Failure in test unique_mart_customer_360_cust_id (models/mart/_mart_layer_doc.yml)"

            self.assertEqual(utils.get_error_files_from_dbt_log(log), [])

    def test_clean_log_removes_ansi_escape_sequences(self):
        """Check ANSI escape sequences are ignored in dbt log."""
        log = "\x1b[31mDatabase Error in model customers (models/core/customers.sql)\x1b[0m"

        self.assertEqual(
            utils.get_error_files_from_dbt_log(log),
            ["models/core/customers.sql"],
        )

    def test_context_log_reads_uploaded_payload(self):
        """Check uploaded CI dbt log is used as the error context."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            uploaded = repo_root / "logs" / "payload_dbt.log"
            local = repo_root / "shops_dwh" / "logs" / "dbt.log"
            uploaded.parent.mkdir(parents=True)
            local.parent.mkdir(parents=True)
            uploaded.write_text("uploaded error", encoding="utf-8")
            local.write_text("local error", encoding="utf-8")
            utils.config = SimpleNamespace(uploaded_dbt_log=uploaded, dbt_log=local)

            self.assertEqual(utils.get_context_log(), "uploaded error")

    def test_context_log_falls_back_to_local_dbt_log(self):
        """Check local dbt log is used when no uploaded payload exists."""
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            uploaded = repo_root / "logs" / "payload_dbt.log"
            local = repo_root / "shops_dwh" / "logs" / "dbt.log"
            local.parent.mkdir(parents=True)
            local.write_text("local error", encoding="utf-8")
            utils.config = SimpleNamespace(uploaded_dbt_log=uploaded, dbt_log=local)

            self.assertEqual(utils.get_context_log(), "local error")

    def test_clone_repo_from_ci_fetches_feature_branch_before_checkout(self):
        """Check feature branch commits can be checked out from a shallow clone."""
        with tempfile.TemporaryDirectory() as tmp:
            remote = Path(tmp) / "remote"
            subprocess.run(["git", "init", "--initial-branch=main", remote], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=remote, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=remote, check=True)
            (remote / "shops_dwh").mkdir()
            (remote / "shops_dwh" / "model.sql").write_text("select 1 as id\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=remote, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=remote, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "feature/fix"], cwd=remote, check=True, capture_output=True)
            (remote / "shops_dwh" / "model.sql").write_text("select 2 as id\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "feature"], cwd=remote, check=True, capture_output=True)
            commit_hash = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=remote,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "main"], cwd=remote, check=True, capture_output=True)

            workdir = Path(tmp) / "workdir"
            utils.config = SimpleNamespace(
                base_branch="main",
                github_token="",
                git_platform="GitHub",
                github_name="acme",
            )

            with (
                patch.object(utils.Path, "home", return_value=workdir),
                patch.object(utils, "prepare_dbt_metadata"),
            ):
                utils.clone_repo_from_ci(
                    remote.as_posix(),
                    commit_hash,
                    "shops_dwh",
                    run_id="run-1",
                    branch_name="feature/fix",
                )

            cloned_model = workdir / ".failedrepo" / "remote" / "run-1" / "shops_dwh" / "model.sql"
            self.assertEqual(cloned_model.read_text(encoding="utf-8"), "select 2 as id\n")


if __name__ == "__main__":
    unittest.main()
