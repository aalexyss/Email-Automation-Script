Bulk Email Sender (Python + SMTP)

Script for sending bulk personalized emails via SMTP. Reads recipients from CSV, uses HTML/TXT templates with placeholders, supports attachments, rate-limit, retry and logs. Tested with Gmail (App Password) and ABV.

---------------------------------------------------

# Requirements

Python 3.10+
SMTP access of the chosen provider
Gmail: 2FA + App Password enabled

---------------------------------------------------

# Installation

python3 -m venv .venv
source .venv/bin/activate

python3 -m pip install --upgrade pip
pip install -r requirements.txt

---------------------------------------------------

# Configuration (config.env)

! Fill in one block according to the provider. Do not add comments on the same line. !

# 1) Gmail
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURE=starttls
SMTP_USER=client@gmail.com
SMTP_PASS=APP_PASSWORD
FROM_NAME=Client Name
FROM_EMAIL=client@gmail.com
REPLY_TO=client@gmail.com
RATE_LIMIT_PER_MIN=15
MAX_RETRIES=3
ATTACHMENT_PATH=

# 2) ABV.bg
SMTP_HOST=smtp.abv.bg
SMTP_PORT=465
SMTP_SECURE=ssl
SMTP_USER=client@abv.bg
SMTP_PASS=ACCOUNT_PASSWORD
FROM_NAME=Client Name
FROM_EMAIL=client@abv.bg
REPLY_TO=client@abv.bg
RATE_LIMIT_PER_MIN=10
MAX_RETRIES=3
ATTACHMENT_PATH=

----------------------------------------------------

# Format of recipients.csv

At least the email column. Additional fields (e.g. name, company) are used only if present in the templates/subject.

email,name,company
john.doe@example.com,John,Acme
jane@sample.org,Jane,Sample Ltd

If a field is missing in a row, fallbacks from the script (name=there, company="") are used, except for email, which is required.

----------------------------------------------------

# Templates

- email_template.txt (plain text):

    Hi ${name},

    This is a quick note from ${company}.


- email_template.html:

    <!doctype html>
    <html>
    <body>
    <p>Hi <strong>${name}</strong>,</p>
    <p>This is a quick note from ${company}.</p>
    </body>
    </html>

----------------------------------------------------

# Launch

python3 send_emails.py
The script loads recipients.csv, customizes subject/content, and sends.
Logs are kept in logs/send_YYYYmmdd_HHMMSS.log.

----------------------------------------------------

# Rate-limit and Retry

RATE_LIMIT_PER_MIN controls the rate (e.g. 10–20/min for personal emails).
MAX_RETRIES – retries on temporary errors (with backoff).

-----------------------------------------------------


# Logs

Each run creates a file in logs/ with the format send_YYYYMMDD_HHMMSS.log.
Sample final line:
    Done. Sent: 123 | Failed: 4 | Skipped(dry-run): 125 | Log: logs/send_2025...


-----------------------------------------------------

# Helpful tips
    - DRY_RUN is true by default – change it to false when you are ready.
    - 587 + starttls is the most common setting; for SSL it is typically port 465.
    - If you are getting Permission denied/auth errors – check 2FA/“App password” in the mail.
    - If the CSV does not have a required column or is empty, the script will not run.
