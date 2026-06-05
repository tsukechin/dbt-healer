import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app import context, utils


class DbtContextTests(unittest.TestCase):
    def setUp(self):
        """Store original utils config and log reader."""
        self.original_config = utils.config
        self.original_get_context_log = utils.get_context_log
        self.original_structured_sql_context = context.structured_sql_context
        utils.get_context_log = lambda: (
            'Failure in test unique_mart_customer_360_cust_id '
            '(models/mart/_mart_layer_doc.yml)\n'
            'column "cust_id" is not unique\n'
            'compiled code at target/compiled/shops_dwh/models/mart/mart_customer_360.sql'
        )

    def tearDown(self):
        """Restore original utils state."""
        utils.config = self.original_config
        utils.get_context_log = self.original_get_context_log
        context.structured_sql_context = self.original_structured_sql_context

    def _write_project(self, tmp: str) -> Path:
        repo_root = Path(tmp)
        project_path = repo_root / "shops_dwh"
        files = {
            "models/mart/mart_customer_360.sql": "select cust_id from {{ ref('stg_customers') }}",
            "models/stg/stg_customers.sql": "select cust_id from {{ ref('raw_customers') }}",
            "models/stg/raw_customers.sql": "select 1 as raw_customer_key",
            "models/mart/customer_orders.sql": "select cust_id from {{ ref('mart_customer_360') }}",
            "models/mart/order_metrics.sql": "select order_id from {{ ref('customer_orders') }}",
            "models/mart/_mart_layer_doc.yml": "models:\n  - name: mart_customer_360\n    columns:\n      - name: cust_id",
            "models/core/core_invoices.sql": (
                "with prepared_invoices as (\n"
                "    select s.*, s.campaign_name as campaign_reporting_key\n"
                "    from {{ ref('stg_invoices') }} as s\n"
                ")\n"
                "select campaign_reporting_key as campaign_id from prepared_invoices"
            ),
            "models/core/core_campaign.sql": (
                "select {{ generate_surrogate_key(['s.campaign_code']) }} as campaign_id\n"
                "from {{ ref('stg_campaigns') }} as s"
            ),
            "models/mart/mart_invoice_rollup.sql": "select campaign_id from {{ ref('core_invoices') }}",
            "models/core/_core_layer_doc.yml": (
                "models:\n"
                "  - name: core_invoices\n"
                "    columns:\n"
                "      - name: campaign_id\n"
                "        data_tests:\n"
                "          - relationships:\n"
                "              arguments:\n"
                "                to: ref('core_campaign')\n"
                "                field: campaign_id"
            ),
            "target/compiled/shops_dwh/models/mart/mart_customer_360.sql": "select cust_id from stg_customers",
            "target/compiled/shops_dwh/models/core/_core_layer_doc.yml/relationships_core_invoices.sql": (
                "select campaign_id from core_invoices where campaign_id not in (select campaign_id from core_campaign)"
            ),
        }
        for path, content in files.items():
            file_path = project_path / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")

        manifest_path = project_path / "target" / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "nodes": {
                        "model.shops_dwh.mart_customer_360": {
                            "resource_type": "model",
                            "name": "mart_customer_360",
                            "original_file_path": "models/mart/mart_customer_360.sql",
                            "depends_on": {"nodes": ["model.shops_dwh.stg_customers"]},
                        },
                        "model.shops_dwh.stg_customers": {
                            "resource_type": "model",
                            "name": "stg_customers",
                            "original_file_path": "models/stg/stg_customers.sql",
                            "depends_on": {"nodes": ["model.shops_dwh.raw_customers"]},
                        },
                        "model.shops_dwh.raw_customers": {
                            "resource_type": "model",
                            "name": "raw_customers",
                            "original_file_path": "models/stg/raw_customers.sql",
                            "depends_on": {"nodes": []},
                        },
                        "model.shops_dwh.customer_orders": {
                            "resource_type": "model",
                            "name": "customer_orders",
                            "original_file_path": "models/mart/customer_orders.sql",
                            "depends_on": {"nodes": ["model.shops_dwh.mart_customer_360"]},
                        },
                        "model.shops_dwh.order_metrics": {
                            "resource_type": "model",
                            "name": "order_metrics",
                            "original_file_path": "models/mart/order_metrics.sql",
                            "depends_on": {"nodes": ["model.shops_dwh.customer_orders"]},
                        },
                        "model.shops_dwh.core_invoices": {
                            "resource_type": "model",
                            "name": "core_invoices",
                            "original_file_path": "models/core/core_invoices.sql",
                            "depends_on": {"nodes": []},
                        },
                        "model.shops_dwh.core_campaign": {
                            "resource_type": "model",
                            "name": "core_campaign",
                            "original_file_path": "models/core/core_campaign.sql",
                            "depends_on": {"nodes": []},
                        },
                        "model.shops_dwh.mart_invoice_rollup": {
                            "resource_type": "model",
                            "name": "mart_invoice_rollup",
                            "original_file_path": "models/mart/mart_invoice_rollup.sql",
                            "depends_on": {"nodes": ["model.shops_dwh.core_invoices"]},
                        },
                        "test.shops_dwh.relationships_core_invoices_campaign_id__campaign_id__ref_core_campaign_.abc": {
                            "resource_type": "test",
                            "name": "relationships_core_invoices_campaign_id__campaign_id__ref_core_campaign_",
                            "original_file_path": "models/core/_core_layer_doc.yml",
                            "column_name": "campaign_id",
                            "test_metadata": {
                                "name": "relationships",
                                "kwargs": {
                                    "column_name": "campaign_id",
                                    "field": "campaign_id",
                                    "to": "ref('core_campaign')",
                                },
                            },
                            "depends_on": {
                                "nodes": [
                                    "model.shops_dwh.core_invoices",
                                    "model.shops_dwh.core_campaign",
                                ]
                            },
                        },
                    },
                    "child_map": {
                        "model.shops_dwh.mart_customer_360": ["model.shops_dwh.customer_orders"],
                        "model.shops_dwh.stg_customers": ["model.shops_dwh.mart_customer_360"],
                        "model.shops_dwh.raw_customers": ["model.shops_dwh.stg_customers"],
                        "model.shops_dwh.customer_orders": ["model.shops_dwh.order_metrics"],
                        "model.shops_dwh.order_metrics": [],
                        "model.shops_dwh.core_invoices": ["model.shops_dwh.mart_invoice_rollup"],
                        "model.shops_dwh.core_campaign": [],
                        "model.shops_dwh.mart_invoice_rollup": [],
                    },
                    "macros": {},
                }
            ),
            encoding="utf-8",
        )
        utils.config = SimpleNamespace(
            dbt_project_name="shops_dwh",
            repo_root=repo_root,
            base_branch="master",
            ai_provider="Ollama",
        )
        return project_path

    def test_diagnostic_context_keeps_one_relevant_upstream_model(self):
        """Check diagnostic context keeps only nearest relevant parent."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_project(tmp)

            result = context.parse_lineage_models("models/mart/mart_customer_360.sql")
            text = "\n".join(result.values())

            self.assertIn('name="stg_customers"', text)
            self.assertNotIn('name="raw_customers"', text)
            self.assertNotIn("customer_orders", text)

    def test_impact_context_keeps_one_relevant_downstream_model(self):
        """Check impact context keeps only nearest relevant child."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_project(tmp)

            result = context.get_impact_context("models/mart/mart_customer_360.sql")

            self.assertIn('name="customer_orders"', result)
            self.assertIn('depth="1"', result)
            self.assertNotIn('name="order_metrics"', result)
            self.assertNotIn('depth="2"', result)
            self.assertNotIn('name="stg_customers"', result)

    def test_file_context_contains_primary_error_model_and_compiled_sql(self):
        """Check primary prompt context includes source and compiled SQL."""
        with tempfile.TemporaryDirectory() as tmp:
            project_path = self._write_project(tmp)

            result = context.get_file_context(str(project_path / "models/mart/mart_customer_360.sql"))

            self.assertIn("<PRIMARY_ERROR_MODEL", result)
            self.assertIn("SOURCE OF models/mart/mart_customer_360.sql", result)
            self.assertIn("<DIAGNOSTIC_CONTEXT>", result)
            self.assertIn("<SCHEMA_DEFINITION path=\"models/mart/_mart_layer_doc.yml\">", result)
            self.assertIn("<COMPILED_SQL path=", result)
            self.assertIn("<IMPACT_CONTEXT>", result)
            self.assertIn('name="customer_orders"', result)
            self.assertNotIn('name="order_metrics"', result)

    def test_syntax_error_keeps_primary_model_only(self):
        """Check local syntax errors do not pull lineage context."""
        with tempfile.TemporaryDirectory() as tmp:
            project_path = self._write_project(tmp)
            utils.get_context_log = lambda: (
                "Database Error in model mart_customer_360 (models/mart/mart_customer_360.sql)\n"
                'syntax error at or near "from"'
            )

            result = context.get_file_context(str(project_path / "models/mart/mart_customer_360.sql"))

            self.assertIn("<PRIMARY_ERROR_MODEL", result)
            self.assertIn("NO_DIAGNOSTIC_CONTEXT", result)
            self.assertIn("NO_IMPACT_CONTEXT", result)
            self.assertNotIn('name="stg_customers"', result)
            self.assertNotIn('name="customer_orders"', result)

    def test_column_error_allows_lineage_context(self):
        """Check column errors still pull diagnostic lineage."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_project(tmp)
            utils.get_context_log = lambda: (
                "Database Error in model mart_customer_360 (models/mart/mart_customer_360.sql)\n"
                'column "cust_id" does not exist'
            )

            result = "\n".join(context.parse_lineage_models("models/mart/mart_customer_360.sql").values())

            self.assertIn('name="stg_customers"', result)

    def test_relationship_test_includes_related_parent_model(self):
        """Check relationship tests include the referenced model as diagnostic context."""
        with tempfile.TemporaryDirectory() as tmp:
            project_path = self._write_project(tmp)
            utils.get_context_log = lambda: (
                "Failure in test relationships_core_invoices_campaign_id__campaign_id__ref_core_campaign_ "
                "(models/core/_core_layer_doc.yml)\n"
                "Database Error in test relationships_core_invoices_campaign_id__campaign_id__ref_core_campaign_ "
                "(models/core/_core_layer_doc.yml)\n"
                "column \"campaign_id\" does not exist\n"
                "LINE 15:     select campaign_id as from_field\n"
                "compiled code at target/compiled/shops_dwh/models/core/_core_layer_doc.yml/"
                "relationships_core_invoices.sql"
            )

            result = context.get_file_context(str(project_path / "models/core/_core_layer_doc.yml"))

            self.assertIn('path="models/core/core_invoices.sql"', result)
            self.assertIn("<DBT_TEST_FAILURE>", result)
            self.assertIn("<RELATIONSHIP_TEST_CONTEXT>", result)
            self.assertIn("error_side: from_field", result)
            self.assertIn("from_model: core_invoices", result)
            self.assertIn("to_model: core_campaign", result)
            self.assertIn('RELATED_TEST_MODEL name="core_campaign"', result)
            self.assertIn("generate_surrogate_key", result)
            self.assertIn("campaign_reporting_key", result)
            self.assertIn("NO_IMPACT_CONTEXT", result)
            self.assertNotIn("mart_invoice_rollup", result)

    def test_additional_context_shrinks_for_every_provider(self):
        """Check additional context uses compact SQL context for every provider."""
        with tempfile.TemporaryDirectory() as tmp:
            self._write_project(tmp)
            context.structured_sql_context = lambda *args, **kwargs: "SHRUNK"

            utils.config.ai_provider = "DeepSeek API"
            result = "\n".join(context.parse_lineage_models("models/mart/mart_customer_360.sql").values())

            self.assertIn("SHRUNK", result)

            utils.config.ai_provider = "Ollama"
            result = "\n".join(context.parse_lineage_models("models/mart/mart_customer_360.sql").values())

            self.assertIn("SHRUNK", result)


if __name__ == "__main__":
    unittest.main()
