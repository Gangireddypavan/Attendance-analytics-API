# Employee Attendance & Analytics API

FastAPI + MongoDB. All application code is in `app/main.py`.

## Run
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or put them in a local .env
uvicorn app.main:app --port 8000
```
- `MONGO_URI` / `MONGO_DB` are read from the environment; a `.env` file is loaded too, but real environment variables win.
- All indexes are created at startup (idempotent), so there are no manual steps.
- `GET /health` returns 200 only when MongoDB answers a ping, otherwise 503. Interactive docs: `/docs`.

## Notes
- Instants are epoch milliseconds in the API and BSON dates (UTC) in MongoDB. Incoming instants are truncated to whole
  seconds (R1).
- Half-up rounding: Python `Decimal` for `work_hours` and `attendance_pct`. Inside pipelines I use Decimal128
  `floor(x * 10^n + 0.5) / 10^n`, because MongoDB's `$round` is banker's rounding.
- Every analytics number that comes from attendance data is computed by an aggregation pipeline. Only the employee's
  `working_days` (Mon–Fri between two dates) is plain calendar arithmetic in Python.
- Business-rule 422s (e.g. punch-out before punch-in, a PATCH that changes nothing) use the same `{"detail": [...]}`
  shape as FastAPI's validation errors.
- Concurrency: punch-in relies on the unique `(emp_code, date)` index. Punch-out is a conditional update on
  `punch_out: null`. PATCH is an optimistic update (the record must be unchanged since it was read); the loser gets 409.
- Interpretations:
  - `moving_avg_7d` is the mean of the already-rounded daily `attendance_rate` values.
  - "Range exceeds 92 days" means more than 92 days counting both `from` and `to`.
  - In PATCH, sending `null` for `status` / `punch_in` / `punch_out` is a 422; leaving the field out means "unchanged".

## Testing
Tested locally (MongoDB 8.0) against a seeded dataset of awkward cases: no-log employees, mid-month and future joiners,
weekend records, an overnight shift, legacy records, leaderboard ties at the cutoff, logs of unknown employees, and
rounding ties. The parallel-request tests gave exactly one 201 per punch-in and one 200 per punch-out. On a 100k-record
dataset, every endpoint answered in about 20-210 ms, and `/admin/explain/*` showed IXSCAN and no COLLSCAN for all five
endpoints.

## Missing
Nothing known.
