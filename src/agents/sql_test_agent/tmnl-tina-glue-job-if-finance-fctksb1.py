"""tmnl-tina-glue-job-if-finance-fctksb1

Loads SAP KSB1 cost-centre line items into the finance fact table.

Source  : finance_raw.if_ksb1_extract   (Glue catalog, daily SAP extract)
Lookups : finance_dwh.dim_cost_center, finance_dwh.dim_gl_account
Target  : finance_dwh.fct_ksb1          (Redshift, delete-then-insert)
Control : finance_ctl.etl_watermark

Scheduled daily at 03:15 CET by the tmnl-tina-finance-daily Airflow DAG.

This file is the sample input shipped with the sql_test_agent: it is a realistic
finance job, and it deliberately contains the kinds of defect the agent is asked
to find. It is not a template to copy.
"""

import sys
from datetime import date, timedelta

from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.job import Job
from awsglue.transforms import ApplyMapping, ResolveChoice
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F

ARGS = getResolvedOptions(
    sys.argv,
    [
        "JOB_NAME",
        "redshift_connection",
        "redshift_tempdir",
        "lookback_days",
    ],
)

SPARK_CONTEXT = SparkContext.getOrCreate()
GLUE_CONTEXT = GlueContext(SPARK_CONTEXT)
SPARK = GLUE_CONTEXT.spark_session
JOB = Job(GLUE_CONTEXT)
JOB.init(ARGS["JOB_NAME"], ARGS)

RAW_DATABASE = "finance_raw"
RAW_TABLE = "if_ksb1_extract"
TARGET_TABLE = "finance_dwh.fct_ksb1"
COST_CENTER_DIM = "finance_dwh.dim_cost_center"
GL_ACCOUNT_DIM = "finance_dwh.dim_gl_account"
WATERMARK_TABLE = "finance_ctl.etl_watermark"
STAGING_TABLE = "#stg_fct_ksb1"

LOOKBACK_DAYS = int(ARGS.get("lookback_days") or 7)
WINDOW_START = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
RUN_DATE = date.today().isoformat()


def read_source():
    """The daily SAP extract, straight from the Glue catalog."""
    frame = GLUE_CONTEXT.create_dynamic_frame.from_catalog(
        database=RAW_DATABASE,
        table_name=RAW_TABLE,
        transformation_ctx="read_if_ksb1_extract",
        additional_options={"jobBookmarkKeys": ["posting_date"], "jobBookmarkKeysSortOrder": "asc"},
    )

    # SAP sends numerics as text often enough that a choice column shows up here
    # on maybe one run in twenty.
    frame = ResolveChoice.apply(
        frame=frame,
        choice="make_struct",
        transformation_ctx="resolve_if_ksb1_choices",
    )

    return ApplyMapping.apply(
        frame=frame,
        mappings=[
            ("belnr", "string", "document_number", "string"),
            ("buzei", "string", "document_line", "string"),
            ("kostl", "string", "cost_center", "string"),
            ("hkont", "string", "gl_account", "string"),
            ("budat", "string", "posting_date", "date"),
            ("wtgbtr", "decimal(23,4)", "amount_doc_currency", "decimal(23,4)"),
            ("waers", "string", "currency", "string"),
            ("meins", "string", "unit_of_measure", "string"),
            ("mbgbtr", "decimal(23,4)", "quantity", "decimal(23,4)"),
        ],
        transformation_ctx="map_if_ksb1_extract",
    )


def stage_line_items(source_frame):
    """Flatten the extract into a temp view the SQL step can read."""
    source = source_frame.toDF()

    source = (
        source.withColumn("document_number", F.trim(F.col("document_number")))
        .withColumn("cost_center", F.lpad(F.trim(F.col("cost_center")), 10, "0"))
        .withColumn("gl_account", F.lpad(F.trim(F.col("gl_account")), 10, "0"))
        # Reporting only ever shows two decimals, so the amount is narrowed here.
        .withColumn("amount_eur", F.col("amount_doc_currency").cast("decimal(13,2)"))
        # Quantities are whole units in every extract seen so far.
        .withColumn("quantity_units", F.col("quantity").cast("int"))
        .withColumn("load_date", F.lit(RUN_DATE).cast("date"))
    )

    source.createOrReplaceTempView("raw_ksb1_lines")

    # The same view, declared in SQL, is what the transform below reads.
    SPARK.sql(
        """
        CREATE OR REPLACE TEMPORARY VIEW stg_ksb1_lines AS
        SELECT
            document_number,
            document_line,
            cost_center,
            gl_account,
            posting_date,
            amount_eur,
            quantity_units,
            currency,
            load_date
        FROM raw_ksb1_lines
        WHERE currency = 'EUR'
        """
    )

    return SPARK.table("stg_ksb1_lines")


def build_fact():
    """Join the staged lines to the dimensions and shape the fact rows."""
    return SPARK.sql(
        f"""
        WITH recent_postings AS (
            SELECT *
            FROM stg_ksb1_lines
            WHERE posting_date > DATE '{WINDOW_START}'
        ),
        enriched_postings AS (
            SELECT
                rp.document_number,
                rp.document_line,
                rp.cost_center,
                cc.cost_center_name,
                cc.company_code,
                rp.gl_account,
                ga.gl_account_group,
                rp.posting_date,
                rp.amount_eur,
                rp.quantity_units,
                rp.load_date
            FROM recent_postings rp
            LEFT JOIN {COST_CENTER_DIM} cc
                ON cc.cost_center = rp.cost_center
            LEFT JOIN {GL_ACCOUNT_DIM} ga
                ON ga.gl_account = rp.gl_account
        )
        SELECT
            document_number,
            document_line,
            cost_center,
            cost_center_name,
            company_code,
            gl_account,
            gl_account_group,
            posting_date,
            amount_eur,
            quantity_units,
            load_date
        FROM enriched_postings
        """
    )


def write_fact(fact_df):
    """Stage in Redshift, then replace the rolling window inside one transaction."""
    preactions = "; ".join(
        [
            f"DROP TABLE IF EXISTS {STAGING_TABLE}",
            f"CREATE TEMP TABLE {STAGING_TABLE} (LIKE {TARGET_TABLE})",
        ]
    )

    postactions = "; ".join(
        [
            f"DELETE FROM {TARGET_TABLE} WHERE posting_date >= '{WINDOW_START}'",
            f"INSERT INTO {TARGET_TABLE} SELECT * FROM {STAGING_TABLE}",
            f"DROP TABLE IF EXISTS {STAGING_TABLE}",
            f"INSERT INTO {WATERMARK_TABLE} (table_name, loaded_through, loaded_at) "
            f"VALUES ('fct_ksb1', '{RUN_DATE}', GETDATE())",
        ]
    )

    fact_frame = DynamicFrame.fromDF(fact_df, GLUE_CONTEXT, "fct_ksb1_out")

    GLUE_CONTEXT.write_dynamic_frame.from_options(
        frame=fact_frame,
        connection_type="redshift",
        connection_options={
            "redshiftTmpDir": ARGS["redshift_tempdir"],
            "useConnectionProperties": "true",
            "connectionName": ARGS["redshift_connection"],
            "dbtable": STAGING_TABLE,
            "database": "finance_dwh",
            "preactions": preactions,
            "postactions": postactions,
        },
        transformation_ctx="write_fct_ksb1",
    )


def main():
    source_frame = read_source()
    stage_line_items(source_frame)
    fact_df = build_fact()
    write_fact(fact_df)
    JOB.commit()


if __name__ == "__main__":
    main()
