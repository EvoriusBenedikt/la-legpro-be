import logging
import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from services.email_service import send_alert_email
from services import grant_policy, pg_service

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

def expire_access_grants():
    """Write one 'expired' history event per newly expired, un-revoked grant.

    Enforcement never depends on this job (expiry is a read-time predicate);
    it only feeds the admin history. Idempotent: expired_logged_at is set in
    the same statement that selects the rows, so a second run finds nothing.
    """
    try:
        with pg_service.get_conn() as conn:
            rows = conn.execute(
                "UPDATE access_grants SET expired_logged_at = NOW() "
                "WHERE revoked_at IS NULL AND expires_at IS NOT NULL "
                "AND expires_at < NOW() AND expired_logged_at IS NULL "
                "RETURNING id, doc_id, granted_to, expires_at").fetchall()
            for g in rows:
                names = conn.execute(
                    "SELECT (SELECT username FROM users WHERE id = %s) AS grantee_name, "
                    "(SELECT judul FROM regulations WHERE id::text = %s) AS judul",
                    (g["granted_to"], g["doc_id"])).fetchone()
                grant_policy.record_event(
                    conn, {**g, "grantee_name": names["grantee_name"], "judul": names["judul"]},
                    "expired", None, when=g["expires_at"])
        if rows:
            logger.info("expire_access_grants: logged %d expired grant(s)", len(rows))
    except Exception:
        logger.exception("expire_access_grants failed")


def start_scheduler():
    scheduler = BackgroundScheduler()
    # Run everyday at 08:00 AM
    scheduler.add_job(check_expiring_contracts, 'cron', hour=8, minute=0)
    scheduler.add_job(expire_access_grants, 'interval', minutes=15,
                      id='expire_access_grants', coalesce=True, max_instances=1,
                      replace_existing=True)
    scheduler.start()
    logger.info("Alert Scheduler started. Will check for expirations daily at 08:00 AM.")
