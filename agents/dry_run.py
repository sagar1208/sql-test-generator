"""Exercise the agent pipeline with invoke_bedrock stubbed out, for local testing without any AWS calls."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

AGENT_PATH = Path(__file__).parent / "01_sql_test_agent.py"

FAKE_MODEL_RESPONSE = """1. Table orders_summary, column customer_name: Null check. Since customer_name is \
populated via a LEFT JOIN against raw_customers, a failure is any row where \
customer_name is NULL, meaning an order references a customer_id that doesn't exist \
in raw_customers.

2. Table orders_summary, column order_id: Duplicate check. order_id should be unique \
per order; a failure is any order_id value that appears more than once in the table.

3. Tables orders_summary and raw_orders, column order_id: Referential integrity check. \
Every order_id in orders_summary must have a matching row in the source raw_orders \
table; a failure is any orders_summary row whose order_id is missing from raw_orders.

4. Table orders_summary, column total_amount: Type/range sanity check. total_amount \
should always be a non-negative, plausible currency value; a failure is any row where \
total_amount is negative or exceeds a reasonable upper bound (e.g. 1,000,000).

5. Table orders_summary, column order_status: Value validity check. order_status is \
expected to be one of COMPLETED, SHIPPED, or PENDING per the job's filter; a failure \
is any row with a status outside that set.

6. Table orders_summary, column order_date: Date range validity check. Since the job \
only pulls orders from the last day, order_date should never be in the future; a \
failure is any row where order_date is later than the current date.
"""


def main() -> None:
    """Load the real agent module and run its main() with invoke_bedrock stubbed to a canned response."""
    spec = importlib.util.spec_from_file_location("sql_test_agent", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with patch.object(module, "invoke_bedrock", return_value=FAKE_MODEL_RESPONSE):
        module.main()


if __name__ == "__main__":
    main()
