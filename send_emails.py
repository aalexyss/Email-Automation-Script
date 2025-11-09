from datetime import datetime
from string import Template
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Optional
from email_validator import validate_email, EmailNotValidError
from dotenv import load_dotenv
import smtplib, socket, csv, mimetypes, os, random, time, ssl, logging, re

FALLBACKS = {
    "name": "there",
    "company": "",
}

# ---------- Setup ----------
load_dotenv("config.env")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_SECURE = os.getenv("SMTP_SECURE", "starttls").lower()  # starttls | ssl
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

FROM_NAME = os.getenv("FROM_NAME", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)
REPLY_TO = os.getenv("REPLY_TO", FROM_EMAIL)

SUBJECT_TMPL = os.getenv("SUBJECT", "Hello ${name}")
ATTACHMENT_PATH = os.getenv("ATTACHMENT_PATH", "").strip() or None

RATE_LIMIT_PER_MIN = max(1, int(os.getenv("RATE_LIMIT_PER_MIN", "60")))
MAX_RETRIES = max(1, int(os.getenv("MAX_RETRIES", "3")))

RECIPIENTS_CSV = "recipients.csv"
HTML_TMPL_PATH = "email_template.html"
TEXT_TMPL_PATH = "email_template.txt"

DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
SUPPRESSIONS_FILE = os.getenv("SUPPRESSIONS_FILE", "suppressions.txt")
JITTER = 0.2

UNSUBSCRIBE_MAILTO = os.getenv("UNSUBSCRIBE_MAILTO", "")
UNSUBSCRIBE_URL = os.getenv("UNSUBSCRIBE_URL", "")


# Logging
os.makedirs("logs", exist_ok=True)
log_name = datetime.now().strftime("logs/send_%Y%m%d_%H%M%S.log")
logging.basicConfig(
    filename=log_name,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
logging.getLogger().addHandler(console)


# ---------- Helpers ----------
def read_template(path: str) -> Template:
    with open(path, "r", encoding="utf-8") as f:
        return Template(f.read())


def extract_placeholders(*tmpls: Template) -> set[str]:
    pat = re.compile(r"\$\{([a-zA-Z_]\w*)\}")
    keys = set()
    for t in tmpls:
        s = t.template if isinstance(t, Template) else str(t)
        keys.update(pat.findall(s))
    return keys


def ensure_required_fields(
    row: dict, used_keys: set[str], i: int, total: int
) -> tuple[bool, str]:
    """
    Връща (ok, reason). Валидира само полетата, които реално се ползват в шаблоните.
    """
    # винаги изискваме email (ще го нормализираме отделно)
    required = {"email"} | (used_keys & {"name", "company"})
    for key in required - {"email"}:
        val = (row.get(key) or "").strip()
        if not val:
            fb = FALLBACKS.get(key, None)
            if fb is None:
                return False, f"[{i}/{total}] Missing required field '{key}'. Skipping."
            row[key] = fb  # попълваме fallback
            logging.warning(f"[{i}/{total}] Missing '{key}', using fallback '{fb}'.")
    return True, ""


def load_recipients(csv_path: str):
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            out = {}
            for k, v in row.items():
                if k is None:
                    continue
                key = k.strip().lower()
                out[key] = (v or "").strip()
            if not any(out.values()):
                continue
            yield out


def attach_file(msg: EmailMessage, file_path: str):
    ctype, encoding = mimetypes.guess_type(file_path)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)

    with open(file_path, "rb") as f:
        data = f.read()
    filename = os.path.basename(file_path)
    msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


def normalize_email(addr: str) -> str | None:
    """
    Return normalize email or None, if unvalid.
    """
    try:
        v = validate_email(addr, check_deliverability=True)
        return v.normalized
    except EmailNotValidError as e:
        logging.warning(f"Invalid email '{addr}': {e}")
        return None


def csv_preflight(csv_path: str, required=("email",)) -> bool:
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
    except FileNotFoundError:
        logging.warning(f"CSV '{csv_path}' not found. Aborting.")
        return False

    if not headers:
        logging.warning(f"CSV '{csv_path}' is empty or missing header row. Aborting.")
        return False

    norm = [(h or "").strip().lower() for h in headers]
    missing = [c for c in required if c not in norm]
    if missing:
        logging.warning(
            f"CSV '{csv_path}' is missing required column(s): {', '.join(missing)}. Aborting."
        )
        return False

    return True


def smtp_config_preflight() -> bool:
    """
    Checks: field availability, port/secure validity, host resolution,
    TLS capabilities and optionally login (without sending email).
    In case of problems, logs WARNING and returns False to stop cleanly.
    """
    errors = []

    # 1) Availability of mandatory
    missing = []
    if not SMTP_HOST:
        missing.append("SMTP_HOST")
    if not SMTP_PORT:
        missing.append("SMTP_PORT")
    if not SMTP_USER:
        missing.append("SMTP_USER")
    if SMTP_PASS is None or SMTP_PASS == "":
        missing.append("SMTP_PASS")
    if not FROM_EMAIL:
        missing.append("FROM_EMAIL")
    if missing:
        errors.append(f"Missing SMTP config: {', '.join(missing)}")

    # 2) Port
    try:
        port = int(SMTP_PORT)
        if not (1 <= port <= 65535):
            errors.append(f"Invalid SMTP_PORT '{SMTP_PORT}' (must be 1..65535)")
    except Exception:
        errors.append(f"Invalid SMTP_PORT '{SMTP_PORT}' (not an integer)")

    # 3) SECURE mode
    secure = (SMTP_SECURE or "").lower()
    if secure not in {"starttls", "ssl", "none"}:
        errors.append(
            f"Unsupported SMTP_SECURE='{SMTP_SECURE}' (use starttls|ssl|none)"
        )

    # 4) Validate FROM/REPLY-TO emails
    from_norm = None
    if FROM_EMAIL:
        try:
            v = validate_email(FROM_EMAIL, check_deliverability=True)
            from_norm = v.normalized
        except EmailNotValidError as e:
            errors.append(f"Invalid FROM_EMAIL '{FROM_EMAIL}': {e}")
        except Exception as e:
            errors.append(f"FROM_EMAIL validation error for '{FROM_EMAIL}': {e}")
    else:
        errors.append("Invalid FROM_EMAIL ''")

    reply_to_norm = None
    if REPLY_TO:
        try:
            v = validate_email(REPLY_TO, check_deliverability=True)
            reply_to_norm = v.normalized
        except EmailNotValidError as e:
            errors.append(f"Invalid REPLY_TO '{REPLY_TO}': {e}")
        except Exception as e:
            errors.append(f"REPLY_TO validation error for '{REPLY_TO}': {e}")

    # 5) DNS resolver on host
    if SMTP_HOST:
        try:
            socket.getaddrinfo(SMTP_HOST, None)
        except Exception as e:
            errors.append(f"Cannot resolve SMTP_HOST '{SMTP_HOST}': {e}")

    # 6) Quick connectivity/TLS check (no login)
    try:
        if secure == "ssl":
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(SMTP_HOST, port, context=ctx, timeout=15) as s:
                s.ehlo()
        elif secure == "starttls":
            with smtplib.SMTP(SMTP_HOST, port, timeout=15) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.ehlo()
        else:  # none
            with socket.create_connection((SMTP_HOST, port), timeout=10):
                pass
    except Exception as e:
        errors.append(
            f"Cannot establish SMTP connection ({secure}) to {SMTP_HOST}:{port}: {e}"
        )

    # 7) Optional: deep login verification (off by default)
    deep = os.getenv("SMTP_DEEP_CHECK", "false").lower() == "true"
    if deep and not errors:
        try:
            with smtp_client() as s:
                pass  # succesfull connect and login
        except Exception as e:
            errors.append(f"SMTP login failed for user '{SMTP_USER}': {e}")

    # 8) Unpleasant but not fatal discrepancies → warnings only
    if secure == "ssl" and port == 587:
        logging.warning("Using SSL on port 587 is unusual; typical is 465.")
    if secure == "starttls" and port == 465:
        logging.warning("Using STARTTLS on port 465 is unusual; typical is 587.")
    if secure == "none":
        logging.warning("SMTP_SECURE=none → connection is plaintext (not recommended).")

    # 9) Финално решение
    if errors:
        for msg in errors:
            logging.warning(msg)
        logging.warning("SMTP preflight failed. Aborting.")
        return False
    return True


def build_message(
    row: dict, subject_tmpl: Template, html_tmpl: Template, text_tmpl: Template
):
    substitutions = {k: v for k, v in row.items()}  # email,name,company,...

    msg = EmailMessage()
    msg["Subject"] = subject_tmpl.safe_substitute(substitutions)
    msg["From"] = formataddr((FROM_NAME, FROM_EMAIL))
    msg["Reply-To"] = REPLY_TO
    msg["Message-ID"] = make_msgid()
    msg["Date"] = formatdate(localtime=True)
    msg["To"] = formataddr((row.get("name", ""), row["email"]))

    lh = []
    if UNSUBSCRIBE_MAILTO:
        lh.append(f"<mailto:{UNSUBSCRIBE_MAILTO}>")
    if UNSUBSCRIBE_URL:
        lh.append(f"<{Template(UNSUBSCRIBE_URL).safe_substitute(substitutions)}>")
    if lh:
        msg["List-Unsubscribe"] = ", ".join(lh)
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    text_body = text_tmpl.safe_substitute(substitutions)
    html_body = html_tmpl.safe_substitute(substitutions)

    # alternative parts: text + html
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if ATTACHMENT_PATH:
        try:
            attach_file(msg, ATTACHMENT_PATH)
        except Exception as e:
            logging.error(f"Attachment error for {row['email']}: {e}")

    return msg


def smtp_client():
    ctx = ssl.create_default_context()
    if SMTP_SECURE == "ssl":
        server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30)
        server.ehlo()
    else:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30)
        server.ehlo()
        if SMTP_SECURE == "starttls":
            server.starttls(context=ctx)
            server.ehlo()
        elif SMTP_SECURE == "none":
            pass
        else:
            raise RuntimeError(
                f"Unsupported SMTP_SECURE='{SMTP_SECURE}' (use starttls|ssl|none)"
            )
    server.login(SMTP_USER, SMTP_PASS)
    return server


def require_file(path: str, label: str) -> bool:
    if not os.path.exists(path):
        logging.error(f"{label} '{path}' not found. Aborting.")
        return False
    return True


# ---------- Main ----------
def main():
    if not all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, FROM_EMAIL]):
        logging.error("Missing SMTP config. Check config.env")
        return

    if not (
        require_file(HTML_TMPL_PATH, "HTML template")
        and require_file(TEXT_TMPL_PATH, "Text template")
    ):
        return

    subject_tmpl = Template(SUBJECT_TMPL)
    html_tmpl = read_template(HTML_TMPL_PATH)
    text_tmpl = read_template(TEXT_TMPL_PATH)

    used_keys = extract_placeholders(subject_tmpl, html_tmpl, text_tmpl)

    suppressions = set()
    if os.path.exists(SUPPRESSIONS_FILE):
        with open(SUPPRESSIONS_FILE, encoding="utf-8") as f:
            suppressions = {l.strip().lower() for l in f if l.strip()}

    # rate limit: pause between emails
    pause_seconds = 60.0 / RATE_LIMIT_PER_MIN
    required_cols = {"email"} | (used_keys & {"name", "company"})
    if not csv_preflight(RECIPIENTS_CSV, required=tuple(sorted(required_cols))):
        return

    rows = list(load_recipients(RECIPIENTS_CSV))
    total = len(rows)
    logging.info(f"Loaded {total} recipients.")
    if total == 0:
        logging.warning("No recipients found. Aborting.")
        return

    if not smtp_config_preflight():
        return

    sent = 0
    failed = 0
    invalid_total = 0
    skipped = 0

    with smtp_client() as smtp:
        for i, row in enumerate(rows, start=1):
            email = (row.get("email") or "").strip()
            normalized = normalize_email(email)
            if not normalized:
                logging.warning(f"[{i}/{total}] Invalid email: {email}. Skipping.")
                failed += 1
                invalid_total += 1
                continue

            row["email"] = normalized

            if normalized.lower() in suppressions:
                logging.info(f"[{i}/{total}] Suppressed: {email}. Skipping.")
                skipped += 1
                continue

            ok, reason = ensure_required_fields(row, used_keys, i, total)
            if not ok:
                logging.warning(reason)
                failed += 1
                continue

            msg = build_message(row, subject_tmpl, html_tmpl, text_tmpl)

            # retry with backoff
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    if DRY_RUN:
                        logging.info(
                            f"[{i}/{total}] DRY-RUN to {email} (skipped sending)"
                        )
                        skipped += 1
                        break

                    smtp.send_message(msg)
                    sent += 1
                    logging.info(f"[{i}/{total}] Sent to {email}")
                    break

                except smtplib.SMTPServerDisconnected as e:
                    logging.warning(
                        f"[{i}/{total}] SMTP disconnected for {email}: {e}. Reconnecting…"
                    )
                    try:
                        smtp = smtp_client()
                        continue
                    except Exception as e2:
                        logging.error(f"Reconnect failed: {e2}")
                        is_temp = True

                except smtplib.SMTPResponseException as e:
                    code = e.smtp_code
                    err = (
                        e.smtp_error.decode()
                        if isinstance(e.smtp_error, bytes)
                        else str(e.smtp_error)
                    )
                    logging.error(f"[{i}/{total}] SMTP {code} to {email}: {err}")
                    is_temp = 400 <= code < 500

                except Exception as e:
                    logging.error(f"[{i}/{total}] Error to {email}: {e}")
                    is_temp = True

                if attempt < MAX_RETRIES and is_temp:
                    sleep_time = min(
                        60, (2 ** (attempt - 1)) + random.uniform(0, JITTER)
                    )
                    time.sleep(sleep_time)
                else:
                    failed += 1
                    break

            # rate-limit between receivers
            if i < total:
                time.sleep(pause_seconds + random.uniform(0, JITTER))

    logging.info(
        f"Done. Sent: {sent} | Failed: {failed} | Skipped(dry-run): {skipped} | Log: {log_name}"
    )


if __name__ == "__main__":
    main()
