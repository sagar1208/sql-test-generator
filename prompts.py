"""Prompt templates for the three-pass reasoning pipeline.

Pass 1: Understand - Analyze the SQL query to identify its structure and intent.
Pass 2: Generate - Create plain-English data quality test cases.
Pass 3: Self-Critique - Validate and refine the generated test cases.
"""

UNDERSTAND_PROMPT = """Analyze the following SQL query and describe its key components in plain English.

Identify:
1. The target table(s) being written to
2. Key columns and their purposes
3. Source table(s) being read from
4. Main transformations (joins, aggregations, filters, case statements)
5. Business logic being implemented

SQL Query:
{sql}

Context (optional):
{context}

Provide a concise summary of what this query does, without suggesting test cases yet."""

GENERATE_PROMPT = """Based on this SQL query and its analysis, generate 3-6 plain-English data quality test cases.

For each test case, describe:
- The table and column(s) being tested
- The specific risk or condition being checked (null checks, duplicates, referential integrity, row-count sanity, type mismatches, date range validity)
- What would count as a test failure

Do NOT write any SQL code. Return only plain English descriptions.

SQL Query:
{sql}

Context (optional):
{context}

Query Analysis:
{query_analysis}

Generate the test cases as a numbered list."""

SELF_CRITIQUE_PROMPT = """Review these generated data quality test cases and refine them if needed.

Evaluate whether:
1. Each test case is specific to the given query (not generic boilerplate)
2. The test cases cover structural risks (schema, null/type issues)
3. The test cases cover referential integrity risks (joins, lookups)
4. The test cases cover business logic risks (calculations, thresholds, row counts)
5. All test cases are described in plain English with no SQL code

If improvements are needed, refine the test cases. Otherwise, return them as-is.

Original Test Cases:
{generated_cases}

SQL Query (for reference):
{sql}

Return the final refined test cases as a numbered list in plain English."""
