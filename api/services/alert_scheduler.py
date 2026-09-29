import logging
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from services.email_service import send_alert_email
from services import pg_service

logger = logging.getLogger(__name__)

def check_expiring_contracts():
    """
    Scans the database for contracts expiring in 30, 7, and 1 days.
    Sends email alerts accordingly.
    """
    logger.info("Running daily contract expiration check...")
    try:
        contracts = pg_service.query(
            "SELECT id, filename, company_name, expiration_date "
            "FROM compliance_history WHERE expiration_date IS NOT NULL"
        )

        today = datetime.date.today()

        for contract in contracts:
            filename = contract["filename"]
            company_name = contract["company_name"]
            # Migration M3: expiration_date is a PG DATE column -- psycopg
            # returns datetime.date, so the legacy YYYY-MM-DD string parsing
            # (and its ValueError branch) is gone.
            exp_date = contract["expiration_date"]
            if not exp_date:
                continue

            days_left = (exp_date - today).days

            # Alert thresholds
            if days_left in [30, 7, 1]:
                comp_display = company_name if company_name else "Unknown Company"
                subject = f"Alert: Contract Expiring in {days_left} Days - {comp_display}"

                html_body = f"""
                <h2>Contract Expiration Alert</h2>
                <p>The following contract is expiring in <strong>{days_left} days</strong>:</p>
                <ul>
                    <li><strong>File Name:</strong> {filename}</li>
                    <li><strong>Company Name:</strong> {comp_display}</li>
                    <li><strong>Expiration Date:</strong> {exp_date.isoformat()}</li>
                </ul>
                <p>Please review the contract on the Legal Analyzer Dashboard.</p>
                <br>
                <p><i>This is an automated alert from Legal Analyzer.</i></p>
                """
                send_alert_email(subject, html_body)

    except Exception as e:
        logger.error(f"Error checking expiring contracts: {e}")

def start_scheduler():
    scheduler = BackgroundScheduler()
    # Run everyday at 08:00 AM
    scheduler.add_job(check_expiring_contracts, 'cron', hour=8, minute=0)
    scheduler.start()
    logger.info("Alert Scheduler started. Will check for expirations daily at 08:00 AM.")
