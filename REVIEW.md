# REVIEW.md

List every defect found in the starter `app/main.py`. For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `GET /health` | Always returns `{"status": "ok"}` without actually pinging MongoDB — a dead database would still look healthy | Hit the endpoint after stopping the DB; it still returns 200 | Added `client.admin.command("ping")` inside the handler; catches `Exception` and raises `HTTPException(503, detail="MongoDB unavailable")` per openapi spec |
| 2 | `GET /employees` pagination — `skip` calculation | `skip = page * page_size` is off by one: page 1 skips the first page of results | `GET /employees?page=1&page_size=5` returns records 6-10 instead of 1-5 | Changed to `skip = (page - 1) * page_size` |
| 3 | `GET /employees` — `total` count ignores filters | Total always counted all employees regardless of `department` or `search` filter | Filter by department; `total` is larger than the filtered result set | Passed the same filter `query` dict to `count_documents()` |
| 4 | Punch-in — no race-condition protection | Two simultaneous punch-in requests for same employee both succeed, creating a duplicate log | Send two concurrent POST `/attendance/punch-in` with same `emp_code` | Wrapped insert in `try/except DuplicateKeyError` and return HTTP 409; relies on unique index on `(emp_code, date)` |
| 5 | `compute_late_minutes` — wrong base for `diff_min` | Starter measured diff from punch time alone without converting punch_in to IST shift_start datetime on the correct date | Employee on overnight shift would get wrong late_minutes | Re-built `shift_start` as a proper `datetime` in IST on `att_date`, then subtracted |
| 6 | `get_attendance_date` — overnight shift not handled | Starter always used `punch_ist.date()` as the attendance date, never checking overnight shifts | Night-shift employee punching in at 02:00 IST gets today's date instead of yesterday's | Added overnight detection: if `shift_end <= shift_start` and IST punch time is before `shift_end`, subtract one day |
| 7 | `compute_work_hours` — used Python's `round()` (banker's rounding) | `round(0.125, 2)` → 0.12 instead of 0.13 due to banker's rounding | Work hours ending in exactly 0.5 at second decimal place are rounded the wrong way | Replaced with `round_half_up()` using `Decimal` + `ROUND_HALF_UP` |
| 8 | `GET /attendance` — missing `status` filter | Starter ignored the `status` query parameter; all records returned regardless | `GET /attendance?status=ABSENT` returns all statuses | Added `if status: query["status"] = status` before the DB query |
| 9 | `PATCH /attendance/{emp_code}/{date}` — no audit history | Regularize endpoint updated the record but never stored what changed or who changed it | Check `history` field after a regularize — it is empty or missing | On every update, appended a `history` entry with `{at, changes: {field: {from, to}}}` to the document |
| 10 | Analytics endpoints — missing entirely | `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{dept}/trend`, `GET /admin/explain/{endpoint}` were not implemented | Hitting any analytics route returns 404 | Implemented all five endpoints from scratch per `openapi.yaml` contract |
| 11 | `GET /analytics/leaderboard/late` — no tie-breaking / competition ranking | Not implemented in starter; even a naive implementation would skip the "1,2,2,4" competition ranking required by openapi.yaml | Two employees tied at 3rd place; one is silently dropped | After DB aggregation, compute competition rank in Python: same score → same rank, next rank skips |
| 12 | `GET /analytics/departments/summary` — headcount misses zero-log employees | A naive `$group` on `attendance_logs` would not count employees with no logs that month | Department has 3 employees but only 2 logged attendance; headcount shows 2 | Started pipeline from `employees` collection with `$lookup` to attendance, so every employee joined before month-end is counted |

## Items examined and confirmed NOT a defect

- **`shift_start` / `shift_end` defaults (`"09:30"` / `"18:30"`)** — These are sensible application defaults; the schema allows custom values per employee, and the defaults match the sample data.
- **`sample_seed.py` inserting raw dicts** — This is a utility script, not part of the API. It purposely bypasses Pydantic validation for speed; acceptable for seeding.
- **`requirements.txt` dependencies** — Confirmed that dependencies are aligned with application needs; synchronous `pymongo` with standard `fastapi`, `uvicorn`, `pydantic`, and `python-dotenv` are all that is required.
