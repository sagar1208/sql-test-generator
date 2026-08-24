-- Glue/Airflow-style batch job: populate orders_summary from raw orders + customers
INSERT INTO orders_summary (order_id, customer_id, customer_name, order_date, total_amount, order_status)
SELECT
    o.order_id,
    c.customer_id,
    c.customer_name,
    o.order_date,
    o.total_amount,
    o.order_status
FROM raw_orders o
LEFT JOIN raw_customers c
    ON o.customer_id = c.customer_id
WHERE o.order_date >= CURRENT_DATE - INTERVAL '1' DAY
  AND o.order_status IN ('COMPLETED', 'SHIPPED', 'PENDING');
