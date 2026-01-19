stripe listen --forward-to localhost:5000/webhooks/stripe

# HoopScout 🏀

HoopScout is an **AI-powered basketball scouting app** that generates **coach-ready scouting reports** on demand.  
Each user gets a **personal report library**, **smart caching**, and a **credit-based usage model**.

---

## ✨ Key Features

- 🧠 AI-generated basketball scouting reports
- 💾 Smart caching (cached reports cost **0 credits**)
- 📚 Personal report library per user
- 💳 Credit wallet with ledger-based accounting
- 🔐 User-isolated data (PostgreSQL-backed)
- ⚡ Fast, deterministic query matching

---

## 🧱 Tech Stack

- **Backend:** Flask (Python)
- **AI:** OpenAI API
- **Database:** PostgreSQL (Supabase-compatible)
- **Frontend:** HTML, Tailwind CSS, Vanilla JS
- **Auth:** JWT / Supabase-ready

---

## 📂 Project Structure

hoopscout/
├── app.py # Flask API
├── db_pg.py # PostgreSQL data layer
├── scout.py # Scouting + LLM logic
├── prompts.py # Prompt templates
├── templates/ # HTML templates
├── static/ # JS & CSS
└── README.md

---

## ⚙️ Environment Variables
```
env
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-5.2
DATABASE_URL=postgresql://user:password@localhost:5432/hoopscout
```
---

## 🚀 Running Locally
```
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```
App runs at: http://127.0.0.1:5000
---

## 🔌 API Highlights

`POST /api/scout`
Generates or loads a scouting report.

- Cached report → 0 credits
- New report → -1 credit
- refresh=true → force regeneration

`GET /api/reports`
List saved reports (sidebar library).

`GET /api/credits`
Returns current credit balance.
---

## 🧠 Caching Logic
Reports are uniquely identified by a canonical query key:

`json.dumps(query_obj, sort_keys=True)`

Database constraint
`json.dumps(query_obj, sort_keys=True)`

This guarantees:
- No duplicate reports
- No double charging
- Safe retries