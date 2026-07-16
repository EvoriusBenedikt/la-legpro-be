import logging
import sqlite3
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from auth import get_db_connection
from services.email_service import send_alert_email

logger = logging.getLogger(__name__)

def check_expiring_contracts():
    """
    Scans the database for contracts expiring in 30, 7, and 1 days.
    Sends email alerts accordingly.
    """
    logger.info("Running daily contract expiration check...")
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT id, filename, company_name, expiration_date FROM compliance_history WHERE expiration_date IS NOT NULL")
        contracts = c.fetchall()
        conn.close()

        today = datetime.date.today()

        for contract in contracts:
            doc_id, filename, company_name, exp_date_str = contract
            if not exp_date_str:
                continue

            try:
                # Assuming YYYY-MM-DD
                exp_date = datetime.datetime.strptime(exp_date_str, "%Y-%m-%d").date()
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
                        <li><strong>Expiration Date:</strong> {exp_date_str}</li>
                    </ul>
                    <p>Please review the contract on the Legal Analyzer Dashboard.</p>
                    <br>
                    <p><i>This is an automated alert from Legal Analyzer.</i></p>
                    """
                    send_alert_email(subject, html_body)
                    
            except ValueError:
                # Failed to parse date string
                logger.warning(f"Could not parse expiration date '{exp_date_str}' for doc {doc_id}")
                
    except Exception as e:
        logger.error(f"Error checking expiring contracts: {e}")

def start_scheduler():
    scheduler = BackgroundScheduler()
    # Run everyday at 08:00 AM
    scheduler.add_job(check_expiring_contracts, 'cron', hour=8, minute=0)
    scheduler.start()
    logger.info("Alert Scheduler started. Will check for expirations daily at 08:00 AM.")
