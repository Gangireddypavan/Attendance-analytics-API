# REVIEW.md

Defects found in the starter `app/main.py`, with how each was noticed and fixed.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees`: `skip = page * page_size` | Off by one page: page starts at 1, so page 1 skipped the first 20 rows | `GET /employees` returned `items: []` with `total: 6` | `skip = (page - 1) * page_size` |
| 2 | `list_employees`: `count_documents({})` | `total` ignored the department filter (R10) | `?department=Sales` still reported `total: 6` | `count_documents(q)` with the same filter |
| 3 | `list_employees` | No sort; contract says sorted by `emp_code` ascending, so pages could overlap or skip rows | Order follows insertion order, not emp_code | `.sort("emp_code", 1)` |
| 4 | `list_employees`, `list_attendance` | `page` / `page_size` not validated (R10: page >= 1, page_size <= 100) | `?page=0` or `?page_size=100000` returned 200 | `Query(1, ge=1)`, `Query(20, ge=1, le=100)` -> 422 |
| 5 | `MongoClient(...)` | Returns naive datetimes, so UTC vs IST is ambiguous and arithmetic mixes naive/aware values | `punch_in.tzinfo is None`; comparing with an aware datetime raises TypeError | `tz_aware=True`; all instants handled as UTC-aware |
| 6 | all responses | Instants returned as datetimes/ISO strings; contract requires epoch milliseconds | `created_at: "2026-01-05T04:30:00"` instead of `1767587400000` | `to_ms()` at the API boundary (`employee_out`, `record_out`, `history_out`) |
| 7 | `create_employee`: `datetime.now()` | Server-local naive time stored as if it were UTC | On an IST machine `created_at` is 5h30m (19,800,000 ms) off | `datetime.now(timezone.utc)` |
| 8 | `create_employee`: `find_one` then `insert_one` | Check-then-insert race and no unique index: two parallel creates both succeed | Two concurrent POSTs with the same emp_code -> two 201s | Unique index on `emp_code`; insert and map `DuplicateKeyError` -> 409 |
| 9 | `EmployeeIn` | No validation of emp_code format, email, HH:MM times, `shift_start == shift_end`, or the date | `email: "abc"` or `shift_start == shift_end` returned 201 | Field patterns/lengths from openapi.yaml + model validator -> 422. Patterns use `[0-9]`, not `\d`: Pydantic's regex `\d` also matches Unicode digits, so `EMP١٢٣٤` would pass |
| 10 | `health` | Always returned 200, even when MongoDB is unreachable | Stop the DB / wrong URI: `/health` still `ok` | `ping` command; 503 on failure |
| 11 | (startup) | No indexes created at all | Explain shows COLLSCAN; uniqueness not enforced | `ensure_indexes()` in the FastAPI lifespan (idempotent) |
| 12 | `compute_late_minutes` | Shift start built from `punch_in.replace(hour=...)` on a UTC/naive value, so the shift is compared in the wrong time zone | Punch at 09:45 IST (04:15 UTC) vs shift 09:30 -> nonsense result | Build shift start as an IST datetime on the attendance date (`shift_start_at`) |
| 13 | `compute_late_minutes`: `minutes > 10` | Grace check on truncated minutes instead of seconds; R2 says strictly more than 10:00 | 09:40:59 gives 10 > 10 = False -> 0, but should be 10 | `seconds > 600`, then `seconds // 60` |
| 14 | `compute_late_minutes` / `punch_in` | Overnight shifts not handled: a 00:30 punch for a 22:00 shift belongs to the previous day (R1) | Night-shift record dated the wrong day; late minutes negative/zero | `attendance_date()` moves early-morning punches to the previous day; shift start on that day |
| 15 | `compute_work_hours`: `round(x, 2)` | Python `round` is banker's rounding on a binary float, not half-up (R4) | 4h07m30s = 14850 s = 4.125 h -> `round` gives 4.12, half-up gives 4.13 | `Decimal` division + `quantize(ROUND_HALF_UP)` |
| 16 | `compute_overtime` | Ignores the 30-minute minimum (R3) | Out at 18:35 for an 18:30 shift -> 5, should be 0 | Return minutes only when >= 30 |
| 17 | `compute_overtime` | Overnight shift end is not moved to the next day; uses naive `fromisoformat` date | 22:00-06:00 shift, out 06:40 -> huge overtime (vs same-day 06:00) | `shift_end_at()` adds one day when the shift is overnight |
| 18 | `punch_in`: `find_one(emp)` unused check | Unknown employee -> `emp["shift_start"]` on `None` -> 500 | Punch in with `EMP9999` -> 500 Internal Server Error | `get_employee_or_404()` |
| 19 | `punch_in`: `datetime.fromtimestamp(ms/1000)` | Uses the server's local time zone and keeps fractional seconds (R1 says truncate) | Date flips on servers in other zones; stored 09:40:00.900 | `from_ms()` -> UTC, whole seconds; date from IST |
| 20 | `PunchInIn.punched_at` | Accepts seconds, floats, negative numbers; strings partly coerced | `punched_at: 1783312500` (seconds) accepted as 1970 | Strict int with range 1e11..4102444800000 -> 422 |
| 21 | `PunchInIn.status` | Any string accepted, including ABSENT/LEAVE (no endpoint may create those) | `status: "FOO"` -> 201 | `Literal["PRESENT","WFH","ON_DUTY"]` -> 422 |
| 22 | `punch_in`: `find_one` then `insert_one` | Same check-then-insert race: parallel punch-ins both create a record | Two simultaneous punch-ins -> two 201s | Unique index on `(emp_code, date)`; `DuplicateKeyError` -> 409 |
| 23 | `punch_in` / `list_attendance` response | Leaks the internal ObjectId as `id`; datetimes not epoch ms; legacy docs missing `half_day`/`history` | Response has `"id": "66a..."` | `record_out()` builds the documented fields only, with defaults |
| 24 | `list_attendance` | Loads the whole collection into Python, then sorts and slices (too slow at 100k docs) | Memory/latency grow with collection size | Server-side `sort/skip/limit` + `count_documents`, backed by indexes |
| 25 | `list_attendance` | Wrong sort (date only, emp_code tie-break missing); no validation of dates, `date_from > date_to`, or status | `date_from=2026-08-01&date_to=2026-07-01` -> 200 | Sort `date` desc, `emp_code` asc; 422 on bad input |
| 26 | `punch_in`: `d = ts.date().isoformat()` | Attendance date taken from the server-local date of the punch, not the IST date (R1) | On a UTC server, a punch at 02:00 IST (20:30 UTC the day before) is filed under the previous day | `attendance_date()` converts to IST first (and applies the overnight rule) |
| 27 | `punch_in`: `if body.punched_at` | Truthiness check: `0` is treated as "not sent" and silently replaced by the current time | `punched_at: 0` -> 201 with "now" instead of 422 | `is not None` check, and `0` is now rejected by the range validation (row 20) |
| 28 | `MongoClient(...)` | Default 30 s server-selection timeout: with the DB down every request (and `/health`) hangs ~30 s before failing | Stop MongoDB, call `/health`: answer after 30 s | `serverSelectionTimeoutMS=3000`, so `/health` returns 503 quickly |
## Looked at and decided were not defects
- `load_dotenv()` with default `override=False` already lets real environment variables win over `.env`.
- `IST = timezone(timedelta(hours=5, minutes=30))` is correct: India has no daylight saving time.
- The `department` filter is an exact, case-sensitive match, which is what the contract asks for.
- `insert_one` adds `_id` to the dict it is given; popping it before returning (as the starter did) is fine. I now build
  responses field by field instead.
- Endpoints are plain `def` functions: FastAPI runs them in a thread pool, so parallel requests are served concurrently
  with the synchronous PyMongo client (which is thread-safe).
- `status_code=201` on `POST /employees` and `POST /attendance/punch-in` matches the contract.
- `compute_overtime` taking `max(0, ...)` handled early punch-outs correctly; only the 30-minute minimum and overnight
  end were wrong.
