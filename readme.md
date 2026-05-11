# Easyscout 🏀

AI-powered basketball scouting reports. Generate professional, coach-ready scouting reports on demand with smart caching, credit-based usage, and a personal report library.

**Live:** [easyscout.xyz](https://easyscout.xyz)

---

## Features

- **AI-Generated Reports** — Professional scouting reports powered by OpenAI
- **Smart Caching** — Cached reports cost 0 credits (first request costs 1 credit)
- **Personal Library** — User-isolated report storage with PostgreSQL
- **Credit System** — Credit-based usage with transaction ledger
- **Secure Auth** — Supabase authentication with JWT tokens
- **Dev Mode** — Local testing with credit grants (no payment setup required)

---

## Tech Stack

- **Backend:** Flask (Python 3.10+)
- **AI:** OpenAI API (GPT-5.x)
- **Database:** PostgreSQL 17+ (Supabase or self-hosted)
- **Auth:** Supabase Auth
- **Payments:** Stripe (production only, not required for local dev)
- **Frontend:** HTML, Tailwind CSS, Vanilla JS
- **Hosting:** Render

---

## Project Structure

```
easyscout/
├── app.py                 # Flask application & API routes
├── auth.py                # Authentication helpers
├── db.py                  # PostgreSQL data layer
├── db_schema.sql          # Complete database schema
├── run_server.py          # Development server entry point
├── requirements.txt       # Python dependencies
├── .env.example           # Environment variables template
├── tailwind.config.js     # Tailwind CSS configuration
├── services/
│   ├── scout.py           # Scouting report generation
│   ├── reports.py         # Report API endpoints
│   ├── analytics.py       # Analytics HTTP routes
│   └── ...                # Other services
├── utils/
│   ├── analytics.py       # PostHog SDK wrapper
│   ├── name_matching.py   # Name comparison primitives
│   ├── similarity_matching.py # Report fuzzy search
│   ├── embeddings.py      # Vector math & similarity
│   ├── cost_pricing.py    # Model pricing lookup
│   ├── payload_handler.py # Report data enrichment
│   ├── prompts.py         # Prompt loading
│   ├── metrics.py         # Instrumentation
│   └── ...                # Other utilities
├── templates/             # Jinja2 HTML templates
├── static/                # CSS, JS, assets
└── prompts/
    └── scout_instructions.example.txt
```

---

## Getting Started

### Prerequisites

- Python 3.10+
- PostgreSQL 17+ (local or Supabase)
- OpenAI API key
- Supabase project (for auth and user management)
- Stripe account (optional, production only)

**Note:** Email features (Mailjet SMTP) are not required for local development. Supabase auth works out of the box with your project credentials.

### Local Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/yourusername/easyscout.git
   cd easyscout
   ```

2. **Create virtual environment**
   ```bash
   python -m venv .venv
   ```

   Activate it before installing dependencies or running the app so `python` and `pip`
   resolve to the environment-local interpreter:

   ```bash
   # macOS / Linux
   source .venv/bin/activate
   ```

   ```powershell
   # Windows PowerShell
   .\.venv\Scripts\Activate.ps1
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
   python run_server.py
   ```

   If you prefer not to activate the environment, run the environment's interpreter directly:

   ```powershell
   .\.venv\Scripts\python.exe run_server.py
   ```

   For local development with automatic reload on code changes:

   ```bash
   python run_server.py --reload
   ```

   If you are editing `static/tailwind.input.css`, run the Tailwind watcher in a second terminal:

   ```bash
   npm run watch:css
   ```

   `--reload` picks up Python and Jinja template changes automatically. CSS and browser-side
   JavaScript still require a browser refresh, and Tailwind source changes require the watcher.
   
   App runs at: http://127.0.0.1:5000

### Database Schema

Requires **PostgreSQL 17+**. Run the schema file to create all tables (reports, credits, cost tracking, embeddings, metrics):

```bash
# Using psql
psql -h <host> -U <user> -d <db> -f db_schema.sql

# Or with DATABASE_URL
export DATABASE_URL="postgresql://user:pass@host:5432/dbname"  # PowerShell: $env:DATABASE_URL="..."
python migrations/apply_migration.py ../db_schema.sql
```

**Note:** For local development, use `DEV_TOOLS=1` mode to grant yourself credits for testing without Stripe setup.

---

## Environment Variables

See [.env.example](.env.example) for full list.

### Required for Local Development

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | OpenAI API key from [platform.openai.com](https://platform.openai.com) |
| `OPENAI_MODEL` | Model name (e.g., `gpt-5.2`) |
| `DATABASE_URL` | PostgreSQL connection string |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anonymous key |
| `APP_BASE_URL` | Local: `http://localhost:5000` |
| `DEV_TOOLS` | Set to `1` for development mode |

### Optional for Local Development

| Variable | Description |
|----------|-------------|
| `ENABLE_OPENAI` | Enable OpenAI integration (1 or 0) |
| `SENTRY_DSN` | Sentry error tracking DSN |
| `SENTRY_ENV` | Sentry environment (e.g., `development`) |
| `SENTRY_TRACES_SAMPLE_RATE` | Sentry traces sample rate (0-1) |

### Production Only

| Variable | Description |
|----------|-------------|
| `STRIPE_SECRET_KEY` | Stripe secret key |
| `STRIPE_PUBLISHABLE_KEY` | Stripe publishable key |
| `STRIPE_WEBHOOK_SECRET` | Stripe webhook signing secret |

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

### Development Only
- `POST /api/dev/grant_credits` — Grant credits (requires `DEV_TOOLS=1`)

---

## Development

### Grant Credits (Dev Mode)

Instead of using Stripe, contributors can grant themselves credits for testing:

```javascript
const token = (await window.sb.auth.getSession()).data.session.access_token;
fetch('/api/dev/grant_credits', {
  method: 'POST',
  headers: { 
    'Authorization': 'Bearer ' + token, 
    'Content-Type': 'application/json' 
  },
  body: JSON.stringify({ amount: 10 })
}).then(r => r.json()).then(console.log);
```

### Run with Debug Mode

```bash
export DEV_TOOLS=1  # Windows: set DEV_TOOLS=1
python run_server.py --reload
```

---

## License

MIT License - feel free to use for your own projects.

---

## Contributing

Contributions are welcome! Here's how to get started:

### Setup
1. Follow the [Getting Started](#getting-started) section above
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Make your changes and test locally with `DEV_TOOLS=1`

### Testing Your Changes
- Test the scouting flow end-to-end
- Use `DEV_TOOLS=1` to grant credits for testing
- Check the browser console and server logs for errors

### Areas to Contribute
- **Frontend:** Improve UI/UX (templates, static/)
- **Backend:** Add new API features or optimize existing ones (app.py, services/)
- **Database:** Improve schema or add new tables (db.py, db_schema.sql)
- **Utilities:** Add new helper functions or improve existing ones (utils/)
- **Docs:** Improve documentation and examples

### Submitting Changes
1. Commit with clear messages: `git commit -m 'Add feature: description'`
2. Push to your branch: `git push origin feature/your-feature`
3. Open a Pull Request with a description of your changes

### Code Style
- Use clear, descriptive variable names
- Follow PEP 8 for Python
- Add comments for complex logic
- Keep functions focused and testable

---

**Questions?** Open an issue or reach out!