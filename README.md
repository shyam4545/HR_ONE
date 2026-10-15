# Employee Attendance & Analytics API

Solution for the HROne Engineering Assignment.

## How to run

1. **Environment & Dependencies**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. **Configuration**
   Set `MONGO_URI` and `MONGO_DB` via environment variables or a local `.env` file:
   ```env
   MONGO_URI=mongodb+srv://<username>:<password>@cluster.mongodb.net/?retryWrites=true&w=majority
   MONGO_DB=attendance_db
   ```

3. **Seed Sample Data (Optional)**
   ```bash
   python sample_seed.py
   ```

4. **Run the API**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   Interactive Swagger docs are accessible at `http://localhost:8000/docs`.

## Anything we should know

- **Single-File Implementation**: All application code lives inside `app/main.py`.
- **Database Indexes**: Indexes on `employees` and `attendance_logs` are created idempotently at startup.
- **R1-R10 Compliance**: Strictly implements business rules R1 through R10 including IST timezone handling, overnight shifts, competition ranking ties on the leaderboard, zero-log employee headcount, and half-up decimal rounding.
- **Documentation**:
  - `REVIEW.md` documents all 12 defects found in the starter code with root causes and fixes.
  - `DECISIONS.md` answers all 5 design questions on indexes, race conditions, ties, headcount, and 100x data scaling.
