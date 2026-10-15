# DECISIONS.md

## 1. Indexes

The most important index is the unique index on attendance_logs (emp_code, date). It makes sure there is only one
record per employee per day, and it is what makes punch-in safe when two requests come at the same time.

On employees, emp_code is unique so two employees can never have the same code.

The other indexes are there so the queries do not scan the whole collection:
- employees (department, emp_code): filter by department, sorted by emp_code
- employees (joined_on): headcount in the department summary
- attendance_logs (date, emp_code): the attendance list sort and month ranges for the leaderboard
- attendance_logs (emp_code, punch_in): punch-out finds the latest punch-in quickly
- attendance_logs (status, date, emp_code): the attendance list filtered by status

I decided not to add an index on late_minutes. The leaderboard already filters by month first, so that index would
only make every write slower without helping much.

## 2. Punch-in race

Both requests look up EMP0001 in employees. It exists, so neither gets a 404. Both then call insert_one. There is a
unique index on (emp_code, date), so MongoDB lets only one of them in. The second insert fails with DuplicateKeyError,
and I return 409.

I did not use find-then-insert because both find_one calls can find nothing at the same moment, and then both inserts
succeed and we get a duplicate record.

## 3. Ties

If two employees are tied at the cutoff, both are returned. I use $rank, which gives tied employees the same rank
(1, 2, 3, 3). The limit is applied to the rank, not to the number of rows, so with limit 3 both people ranked 3rd are
included. Tied rows are ordered by emp_code. Cutting at exactly 3 rows would drop one of them for no fair reason.

## 4. Headcount

An employee with zero logs this month has no log documents at all. If I started from logs, that person would never
appear anywhere in the pipeline, so they could not be counted. Starting from employees means every person is in the
pipeline from step one.

Then I $lookup attendance_logs (this month only, grouped into stats). For a zero-log employee the lookup returns an
empty array, and $unwind with preserveNullAndEmptyArrays: true keeps that employee, just with stats missing. Then I
$group by department with headcount: {$sum: 1}, so they are counted.

## 5. One thing I would change for 100x the data

Right now the department summary does one $lookup per employee. With 100x the data that gets slow. I would keep a
small collection with monthly totals per employee and update it on every punch-in, punch-out and correction. Then the
summary just reads these ready totals instead of going through all the logs every time.


