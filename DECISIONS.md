# DECISIONS.md

1. **Indexes.**
   On `employees`: unique `emp_code` (guarantees business key uniqueness; O(1) lookups), `(department, emp_code)` (covers filtered employee listing and sorting), and `joined_on` (headcount date filters).
   On `attendance_logs`: unique `(emp_code, date)` (guarantees natural key uniqueness; prevents concurrent duplicate punch-ins), `(date, -1), (emp_code, 1)` (powers default attendance listing and sorting without in-memory sort), `(emp_code, 1), (punch_in, -1)` (O(1) punch-out lookup), and `(date, 1), (late_minutes, -1)` (covers leaderboard aggregation).
   *Rejected*: Considered `(emp_code, date, punch_out)` for punch-outs, but rejected because `(emp_code, date)` is already unique, keeping index overhead minimal.

2. **Punch-in race.**
   Both concurrent requests validate input, compute the identical IST attendance date, and attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` arbitrates atomically at the database engine level. Exactly one insert succeeds and returns HTTP 201. The concurrent request encounters a `DuplicateKeyError` (code 11000), which our handler intercepts and returns as HTTP 409 Conflict.

3. **Ties.**
   We apply standard competition ranking via MongoDB `$setWindowFields` using the `$rank` operator (producing 1, 2, 2, 4) and filter `rank <= limit` *after* ranking. Both tied employees share the cutoff rank and satisfy `rank <= limit`, so both are returned (the response may exceed `limit` items). Within tied ranks, rows are deterministically ordered by `emp_code` ascending.

4. **Headcount.**
   The aggregation pipeline originates on `employees` (matching `joined_on <= period_end`), then performs a `$lookup` into `attendance_logs` with `preserveNullAndEmptyArrays: True`. Department headcount is calculated as `$size` over `$addToSet: "$emp_code"`. Employees with zero attendance documents are preserved in the stream and counted.

5. **One thing you would change for 100x the data.**
   At 10M+ attendance records, calculating monthly aggregates and trends on demand strains the database. I would introduce incremental materialized rollups or pre-aggregated daily summaries via MongoDB Change Streams, allowing analytics endpoints to serve O(1) reads rather than scanning raw logs.
