# DECISIONS.md

1. **Indexes.**
   I added a unique index on `employees.emp_code` for lookup and creation safety, and a unique compound index on `attendance_logs(emp_code, date)` to enforce one punch-in per employee per day. For query performance, I indexed `(date DESC, emp_code ASC)` for paginated attendance listings, `(emp_code, date ASC)` for employee monthly history, and `(date, late_minutes DESC)` for leaderboard aggregation. I considered indexing `department` alone on logs, but rejected it because department queries consistently filter by date ranges first.

2. **Punch-in race.**
   If two identical punch-in requests hit concurrently, both pass initial validation, but the compound unique index `(emp_code, date)` ensures MongoDB atomically inserts only one. The second insert raises a `DuplicateKeyError`, which the handler catches and returns HTTP 409 Conflict without leaving duplicate logs.

3. **Ties.**
   I use standard competition ranking (1, 2, 2, 4) in Python after grouping. If a tie occurs at the `limit` cutoff, all tied employees sharing that rank are included, so the response may exceed `limit` to avoid arbitrarily excluding anyone with identical performance.

4. **Headcount.**
   The aggregation pipeline starts from `employees` filtering `joined_on <= last_day`, then `$lookup` joins `attendance_logs` with `preserveNullAndEmptyArrays: True`. This ensures every active employee is counted in department headcount even with zero monthly logs.

5. **One thing I would change for 100x data.**
   I would replace live `$lookup` and `$group` aggregations with incremental pre-aggregated daily summaries updated via MongoDB Change Streams or background jobs, keeping leaderboard and trend query latencies under 50ms at scale.
