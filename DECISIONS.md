# DECISIONS.md

## 1. Indexes
The most important is the unique index on attendance_logs (emp_code, date): one record per employee per day, and it
makes punch-in safe when two requests come at once. emp_code is also unique on employees.

The others stop full collection scans: employees (department, emp_code) and (joined_on); attendance_logs
(date, emp_code) for the list and month ranges, (emp_code, punch_in) for punch-out, and (status, date, emp_code) for
the status filter.

I rejected an index on late_minutes: the leaderboard filters by month first, so it would only slow down writes.

## 2. Punch-in race
Both requests call insert_one. The unique index on (emp_code, date) lets only one in; the other fails with
DuplicateKeyError and gets 409. I avoided find-then-insert because both finds can see "nothing" at the same moment,
and then both inserts succeed.

## 3. Ties
Both are returned. $rank gives tied employees the same rank (1, 2, 3, 3), and limit is applied to the rank, not to the
number of rows. Tied rows are ordered by emp_code.

## 4. Headcount
I start from employees, not logs, so a person with no logs is still in the pipeline. Their $lookup returns an empty
array, $unwind with preserveNullAndEmptyArrays keeps them, and $group with headcount: {$sum: 1} counts them.

## 5. 100x the data
The department summary does one $lookup per employee. I would keep a small collection of monthly totals per employee,
updated on every punch and correction, so the summary reads ready totals instead of all the logs.
