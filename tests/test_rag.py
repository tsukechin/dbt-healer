import unittest

from app.rag import extract_error_signals, structured_sql_context


class RagTests(unittest.TestCase):
    def test_extracts_error_symbols(self):
        """Check dbt error text contributes retrieval symbols."""
        signals = extract_error_signals('Database Error: column "cust_id" does not exist')

        self.assertIn("cust_id", signals)

    def test_structured_context_uses_lexical_rag_for_large_sql(self):
        """Check large SQL is reduced to relevant retrieved chunks."""
        filler = "\n".join(f"select {index} as filler_{index}" for index in range(120))
        source = (
            "{{ config(materialized='table') }}\n"
            f"{filler}\n"
            "select cust_id, customer_name from final_customers"
        )

        result = structured_sql_context(
            source,
            {"cust_id"},
            max_chars=800,
        )

        self.assertIn("[RAG lexical windows", result)
        self.assertIn("RELEVANT_WINDOWS", result)
        self.assertIn("cust_id", result)
        self.assertLessEqual(len(result), 850)


if __name__ == "__main__":
    unittest.main()
