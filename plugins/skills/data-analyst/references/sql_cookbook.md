# SQLite cookbook for `dataset_query`

Tables are the loaded dataset names; columns are snake_case. One `SELECT` (CTEs and window functions are fine).

## Aggregates and top-N

```sql
SELECT region, COUNT(*) AS orders, ROUND(SUM(amount), 2) AS revenue, ROUND(AVG(amount), 2) AS avg_order
FROM sales GROUP BY region ORDER BY revenue DESC LIMIT 10;
```

Share of total:

```sql
SELECT region, SUM(amount) AS revenue,
       ROUND(100.0 * SUM(amount) / (SELECT SUM(amount) FROM sales), 1) AS pct
FROM sales GROUP BY region ORDER BY revenue DESC;
```

## Time series (dates are TEXT, ISO `YYYY-MM-DD`)

```sql
SELECT strftime('%Y-%m', order_date) AS month, SUM(amount) AS revenue
FROM sales GROUP BY month ORDER BY month;
```

Month-over-month change with a window function:

```sql
WITH m AS (SELECT strftime('%Y-%m', order_date) AS month, SUM(amount) AS revenue FROM sales GROUP BY month)
SELECT month, revenue,
       ROUND(100.0 * (revenue - LAG(revenue) OVER (ORDER BY month)) / LAG(revenue) OVER (ORDER BY month), 1) AS mom_pct
FROM m ORDER BY month;
```

## Quality checks

| Goal | Query |
|---|---|
| Null share | `SELECT ROUND(100.0*SUM(col IS NULL)/COUNT(*),1) FROM t` |
| Duplicate keys | `SELECT id, COUNT(*) c FROM t GROUP BY id HAVING c > 1 ORDER BY c DESC` |
| Orphans after join | `SELECT COUNT(*) FROM a LEFT JOIN b ON a.k=b.k WHERE b.k IS NULL` |
| Distinct categories | `SELECT col, COUNT(*) FROM t GROUP BY col ORDER BY 2 DESC` |

## Outliers (IQR rule)

```sql
WITH ranked AS (
  SELECT amount, NTILE(4) OVER (ORDER BY amount) q FROM sales WHERE amount IS NOT NULL),
bounds AS (
  SELECT MAX(CASE WHEN q=1 THEN amount END) AS q1, MAX(CASE WHEN q=3 THEN amount END) AS q3 FROM ranked)
SELECT s.* FROM sales s, bounds b
WHERE s.amount > b.q3 + 1.5*(b.q3-b.q1) OR s.amount < b.q1 - 1.5*(b.q3-b.q1) LIMIT 50;
```

## Pitfalls

- Integer division: use `100.0 * a / b`. `AVG` ignores NULLs; `COUNT(col)` ignores NULLs, `COUNT(*)` does not.
- Text sorting is lexicographic: cast (`CAST(col AS INTEGER)`) when a numeric-looking column is TEXT.
- Median: no built-in; use `dataset_profile` (median per numeric column) or `ORDER BY col LIMIT 1 OFFSET (SELECT COUNT(*)/2 FROM t)`.
- Always `LIMIT` exploratory queries; the tool returns at most 200 rows.
