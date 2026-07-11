"""Minimal hello-world HTTP API + saberistic landing site."""

from __future__ import annotations

import os
import logging
import datetime
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Depends
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SITE_DIR = Path(__file__).resolve().parent.parent / "site"
ASSETS_DIR = SITE_DIR / "assets"

# Database Setup
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./project_briefs.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# For SQLite, we need check_same_thread=False
connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class ProjectBrief(Base):
    __tablename__ = "project_briefs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, nullable=False)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    stripe_status = Column(String, default="pending")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# Create tables
Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app = FastAPI(title="agent-web", version="0.2.0")
app.mount("/assets", StaticFiles(directory=ASSETS_DIR), name="assets")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/hello")
def hello() -> dict[str, str]:
    return {"message": "hello world"}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(SITE_DIR / "index.html")


@app.get("/about")
def about() -> FileResponse:
    return FileResponse(SITE_DIR / "about.html")


@app.get("/brief")
def brief() -> FileResponse:
    return FileResponse(SITE_DIR / "brief.html")


@app.get("/brief/payment-mock")
def payment_mock() -> FileResponse:
    return FileResponse(SITE_DIR / "payment_mock.html")


@app.get("/brief/success")
def success(brief_id: int | None = None, db: Session = Depends(get_db)) -> FileResponse:
    if brief_id:
        brief = db.query(ProjectBrief).filter(ProjectBrief.id == brief_id).first()
        if brief:
            brief.stripe_status = "paid"
            db.commit()
            logger.info(f"Brief {brief_id} marked as paid.")
    return FileResponse(SITE_DIR / "success.html")


def send_email_notification(name: str, email: str, title: str, description: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    notification_email = os.getenv("NOTIFICATION_EMAIL", "")

    subject = f"New $200 Project Brief: {title}"
    body = f"""You have received a new project brief submission!

Name: {name}
Email: {email}
Title: {title}

Description:
{description}

Status: Pending Payment ($200)
"""

    if not smtp_host or not smtp_user or not smtp_password or not notification_email:
        logger.info("SMTP not fully configured. Logging email notification:")
        logger.info(f"To: {notification_email or 'Not Set'}")
        logger.info(f"Subject: {subject}")
        logger.info(f"Body:\n{body}")
        return

    try:
        msg = MIMEMultipart()
        msg["From"] = smtp_user
        msg["To"] = notification_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP(smtp_host, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
        server.quit()
        logger.info("Email notification sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send email notification: {e}")


@app.post("/brief")
def submit_brief(
    name: str = Form(...),
    email: str = Form(...),
    title: str = Form(...),
    description: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if not name.strip() or not email.strip() or not title.strip() or not description.strip():
        raise HTTPException(status_code=400, detail="All fields are required.")

    # Save to DB
    brief = ProjectBrief(
        name=name,
        email=email,
        title=title,
        description=description,
        stripe_status="pending",
    )
    db.add(brief)
    db.commit()
    db.refresh(brief)

    # Send email notification
    send_email_notification(name, email, title, description)

    # Redirect to Stripe payment page
    stripe_url = os.getenv("STRIPE_PAYMENT_URL", "/brief/payment-mock")
    if stripe_url == "/brief/payment-mock":
        stripe_url = f"/brief/payment-mock?brief_id={brief.id}"

    return RedirectResponse(url=stripe_url, status_code=303)
