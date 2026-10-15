# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (one employee per code; lookups and `$lookup` joins), `(department,
   emp_code)` (department filter + sort, trend), `joined_on` (summary headcount). `attendance_logs`: unique
   `(emp_code, date)` (one record per day, race-safe punch-in, monthly summary), `(date -1, emp_code 1)` (list sort,
   month ranges for the leaderboard), `(emp_code, punch_in -1)` (punch-out finds the latest punch-in),
   `(status, date -1, emp_code 1)` (status filter). I rejected an index on `late_minutes`: the leaderboard is already
   narrowed by month, so it would only slow down writes.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one`. The unique index lets
   exactly one insert succeed; the other gets `DuplicateKeyError`, which I return as 409. There is no find-then-insert.

3. **Ties.** `$rank` gives tied employees the same rank, and I filter `rank <= limit` after ranking, so both are
   returned (more rows than `limit`), ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (joined by month end) and `$lookup`s their logs. `$unwind` keeps
   empty arrays, so an employee with no logs still adds 1 to headcount and 0 to everything else.

5. **100x data.** Keep a per-employee monthly totals collection updated on every write, so the summary stops running one
   `$lookup` per employee.
