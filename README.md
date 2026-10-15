# Mass TempMail Account Registrar

Registers batches of accounts on a target site using temporary emails from 1secmail. Automatically accepts terms checkboxes, fills forms, fetches OTP, and completes registration.

## Local run

```bash
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --host 0.0.0.0 --port 8000
```

## Railway deploy

1. Connect this repo to Railway.
2. Railway will detect Python. Set start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
3. For Playwright browsers, use this Dockerfile (recommended):

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.48.0-jammy
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

Playwright + Chromium needs enough RAM (2GB+ recommended).

## Usage

Paste the registration URL. The tool generates temp emails, random passwords, checks terms boxes, submits, polls for OTP, and finishes.

Selectors are defaults; override in the form if the site uses non-standard fields.
