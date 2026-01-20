# Easyscout 🏀

AI-powered basketball scouting reports. Generate professional, coach-ready scouting reports on demand with smart caching, credit-based usage, and a personal report library.

**Live:** [easyscout.xyz](https://easyscout.xyz)

---

## Features

- **AI-Generated Reports** — Professional scouting reports powered by OpenAI
- **Smart Caching** — Cached reports cost 0 credits (first request costs 1 credit)
- **Personal Library** — User-isolated report storage with PostgreSQL
- **Credit System** — Stripe-powered credit wallet with ledger-based accounting
- **Secure Auth** — Supabase authentication with JWT tokens

---

## Tech Stack

- **Backend:** Flask (Python 3.10+)
- **AI:** OpenAI API (GPT-4.5+)
- **Database:** PostgreSQL (Supabase) + SQLite (local cache)
- **Auth:** Supabase Auth
- **Payments:** Stripe
- **Frontend:** HTML, Tailwind CSS, Vanilla JS
- **Hosting:** Render

---

## Project Structure

```
easyscout/
├── app.py                 # Flask application & API routes
├── auth.py                # Authentication helpers
├── db.py                  # SQLite cache layer
├── db_pg.py              # PostgreSQL data layer
├── requirements.txt       # Python dependencies
├── .env.example          # Environment variables template
├── services/
│   └── scout.py          # Scouting report generation
├── utils/
│   ├── prompts.py        # Prompt loading (supports Render secret files)
│   ├── embeddings.py     # Name matching & fuzzy search
│   └── ...               # Other utilities
├── templates/            # Jinja2 HTML templates
├── static/               # CSS, JS, assets
└── prompts/
    └── scout_instructions.example.txt
```

---

## Getting Started

### Prerequisites

- Python 3.10+
- PostgreSQL database (or Supabase)
- OpenAI API key
- Stripe account (for payments)
- Supabase project (for auth)

### Local Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/easyscout.git
   cd easyscout
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**
   ```bash
   pip install -r requirements.txt
   ```

4. **Set up environment variables**
   ```bash
   cp .env.example .env
   # Edit .env with your actual credentials
   ```

5. **Create the prompt file**
   ```bash
   # Copy example and customize
   cp prompts/scout_instructions.example.txt prompts/scout_instructions.txt
   # Edit prompts/scout_instructions.txt with your actual prompt
   ```

6. **Run the application**
   ```bash
   python app.py
   ```
   
   App runs at: http://127.0.0.1:5000

---

## Environment Variables

See [.env.example](.env.example) for full list. Key variables:

| Variable | Description | Required |
|----------|-------------|----------|
| `SECRET_KEY` | Flask secret key (generate random string) | Yes |
| `OPENAI_API_KEY` | OpenAI API key | Yes |
| `OPENAI_MODEL` | Model name (e.g., `gpt-4.5`) | Yes |
| `DATABASE_URL` | PostgreSQL connection string | Yes |
| `SUPABASE_URL` | Supabase project URL | Yes |
| `SUPABASE_ANON_KEY` | Supabase anonymous key | Yes |
| `STRIPE_SECRET_KEY` | Stripe secret key | Yes |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret | Yes |
| `APP_BASE_URL` | Production URL (e.g., `https://easyscout.xyz`) | Yes |
| `SENTRY_DSN` | Sentry error tracking DSN | No |
| `DEV_TOOLS` | Set to `1` for development mode | No |

---

## Deployment (Render)

### 1. Push to GitHub
```bash
git add .
git commit -m "Ready for production"
git push origin main
```

### 2. Create Render Web Service
- Connect your GitHub repository
- Environment: Python 3
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn app:app` (add gunicorn to requirements.txt)

### 3. Configure Environment Variables
Add all variables from `.env.example` to Render's environment settings.

### 4. Upload Secret File (Prompt)
- Go to **Environment** → **Secret Files**
- Filename: `scout_instructions.txt`
- Paste your prompt content
- Add environment variable: `RENDER_SECRET_FILE_PATH=scout_instructions.txt`

### 5. Configure Database
Use Render's PostgreSQL addon or link your Supabase database URL.

### 6. Set Custom Domain
- Add your domain in Render settings
- Update `APP_BASE_URL` environment variable

---

## API Endpoints

### Public Routes
- `GET /` — Landing page
- `GET /app` — Main application (requires auth)
- `GET /privacy` — Privacy policy

### API Routes (Authenticated)
- `POST /api/scout` — Generate or retrieve scouting report
- `GET /api/reports` — List user's saved reports
- `GET /api/credits` — Get current credit balance
- `POST /api/render_md` — Render markdown to HTML

### Webhooks
- `POST /webhooks/stripe` — Stripe payment webhooks

### Development Only
- `POST /api/dev/grant_credits` — Grant credits (requires `DEV_TOOLS=1`)

---

## Development

### Testing Stripe Locally
```bash
stripe listen --forward-to localhost:5000/webhooks/stripe
```

### Grant Credits (Dev Mode)
```javascript
const token = (await window.sb.auth.getSession()).data.session.access_token;
fetch('/api/dev/grant_credits', {
  method: 'POST',
  headers: { 
    'Authorization': 'Bearer ' + token, 
    'Content-Type': 'application/json' 
  },
  body: JSON.stringify({ amount: 100 })
}).then(r => r.json()).then(console.log);
```

### Run with Debug Mode
```bash
export DEV_TOOLS=1  # Windows: set DEV_TOOLS=1
python app.py
```

---

## Caching Logic

Reports are deduplicated using a deterministic query key:

```python
query_key = json.dumps({
    "player": normalized_player_name,
    "team": team or "",
    "league": league or ""
}, sort_keys=True)
```

Database constraint on `(user_id, query_key)` ensures:
- No duplicate reports per user
- No double charging for same query
- Safe retries on network failures

**Cost:**
- First generation: 1 credit
- Cached retrieval: 0 credits
- Force refresh: 1 credit

---

## Security Features

- **HTTPS Only** — Secure cookies with HSTS headers
- **XSS Protection** — Content Security Policy headers
- **CSRF Protection** — SameSite cookies
- **SQL Injection Safe** — Parameterized queries only
- **Secret Management** — Environment variables + Render secret files
- **Error Tracking** — Sentry integration (optional)

---

## License

MIT License - feel free to use for your own projects.

---

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

**Questions?** Open an issue or reach out!