# Company Research Web App

AI-powered company enrichment tool built with FastAPI + Groq.

## Local Run

```bash
pip install -r requirements.txt
export GROQ_API_KEY=your-key-here
uvicorn main:app --reload
```
Open http://localhost:8000
Website live link : https://relu-webapp.onrender.com/

## Deploy to Railway

1. Push this folder to a GitHub repo
2. Go to railway.app → New Project → Deploy from GitHub
3. Select your repo
4. Add environment variable: `GROQ_API_KEY = your-key-here`
5. Railway auto-deploys → copy the public URL

## API Endpoints

- `POST /enrich` — `{ "url": "https://example.com", "website_name": "Example" }`
- `GET /results` — returns all enriched companies
