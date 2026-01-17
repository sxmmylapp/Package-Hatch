"""
Delivery Hatch Analytics Bot
============================
A Flask server that receives webhooks from:
- QR Code Generator (scan events)
- Website (click tracking)
- Stripe (payment events)

Sends hourly reports to Telegram with conversion funnel metrics.
"""

import os
import json
import sqlite3
import hashlib
import hmac
from datetime import datetime, timedelta
from contextlib import contextmanager

from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import stripe
import requests
from dotenv import load_dotenv
import pytz

# Load environment variables
load_dotenv()

app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
# Support multiple chat IDs (comma separated)
chat_ids_str = os.getenv('TELEGRAM_CHAT_ID', '')
TELEGRAM_CHAT_IDS = [cid.strip() for cid in chat_ids_str.split(',') if cid.strip()]
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
QR_API_KEY = os.getenv('QR_API_KEY')
QR_CODE_ID = os.getenv('QR_CODE_ID', '88145711')  # Your QR code ID
TIMEZONE = pytz.timezone(os.getenv('TIMEZONE', 'America/New_York'))
DATABASE = 'analytics.db'

# Initialize Stripe
stripe.api_key = os.getenv('STRIPE_SECRET_KEY', '')


# ============================================================================
# Database Setup
# ============================================================================

def init_db():
    """Initialize the SQLite database with required tables."""
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                data TEXT,
                amount_cents INTEGER DEFAULT 0
            )
        ''')
        # Table to store QR scan count snapshots for calculating hourly changes
        conn.execute('''
            CREATE TABLE IF NOT EXISTS qr_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                total_scans INTEGER,
                unique_scans INTEGER
            )
        ''')
        # Table to store email signups for launch notification
        conn.execute('''
            CREATE TABLE IF NOT EXISTS email_signups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                source TEXT DEFAULT 'website'
            )
        ''')
        conn.commit()


@contextmanager
def get_db():
    """Context manager for database connections."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def log_event(event_type: str, data: dict = None, amount_cents: int = 0):
    """Log an event to the database."""
    with get_db() as conn:
        conn.execute(
            'INSERT INTO events (event_type, data, amount_cents) VALUES (?, ?, ?)',
            (event_type, json.dumps(data) if data else None, amount_cents)
        )
        conn.commit()
    print(f"[{datetime.now()}] Logged event: {event_type}")


# ============================================================================
# Webhook Endpoints
# ============================================================================

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint for Railway."""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})


@app.route('/webhook/qr', methods=['POST'])
def qr_webhook():
    """
    Receive QR code scan events from QR Code Generator.
    
    Expected payload includes:
    - timestamp
    - country
    - device type
    - short URL / QR code ID
    """
    try:
        data = request.get_json() or request.form.to_dict()
        
        # Log the scan event
        log_event('qr_scan', {
            'country': data.get('country'),
            'device': data.get('device_type'),
            'qr_id': data.get('short_url') or data.get('qr_code_id'),
            'raw': data
        })
        
        return jsonify({'success': True}), 200
    except Exception as e:
        print(f"QR webhook error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/track/click', methods=['POST', 'OPTIONS'])
def track_click():
    """
    Receive click tracking events from the website.
    Uses sendBeacon, so we need to handle both JSON and form data.
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 200
    
    try:
        # sendBeacon may send as text/plain
        if request.content_type and 'json' in request.content_type:
            data = request.get_json()
        else:
            try:
                data = json.loads(request.data.decode('utf-8'))
            except:
                data = {}
        
        log_event('click', {
            'button': data.get('button'),
            'timestamp': data.get('timestamp'),
            'page': data.get('page', 'unknown')
        })
        
        response = jsonify({'success': True})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 200
    except Exception as e:
        print(f"Click tracking error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/signup', methods=['POST', 'OPTIONS'])
def email_signup():
    """
    Receive email signups for launch notification.
    Stores in SQLite and optionally sends to Google Sheets.
    """
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response, 200
    
    try:
        # Parse request data
        if request.content_type and 'json' in request.content_type:
            data = request.get_json()
        else:
            try:
                data = json.loads(request.data.decode('utf-8'))
            except:
                data = {}
        
        email = data.get('email', '').strip().lower()
        
        # Basic email validation
        if not email or '@' not in email or '.' not in email:
            response = jsonify({'success': False, 'error': 'Invalid email address'})
            response.headers['Access-Control-Allow-Origin'] = '*'
            return response, 400
        
        # Store in database
        with get_db() as conn:
            try:
                conn.execute(
                    'INSERT INTO email_signups (email, source) VALUES (?, ?)',
                    (email, data.get('source', 'website'))
                )
                conn.commit()
                
                # Get total signup count
                count = conn.execute('SELECT COUNT(*) FROM email_signups').fetchone()[0]
                
            except sqlite3.IntegrityError:
                # Email already exists
                response = jsonify({'success': True, 'message': 'Already signed up!'})
                response.headers['Access-Control-Allow-Origin'] = '*'
                return response, 200
        
        # Send Telegram notification
        send_signup_notification(email, count)
        
        # Log as event too
        log_event('email_signup', {'email': email})
        
        response = jsonify({'success': True, 'message': 'Thanks! We\'ll notify you at launch.'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 200
        
    except Exception as e:
        print(f"Email signup error: {e}")
        response = jsonify({'success': False, 'error': 'Something went wrong'})
        response.headers['Access-Control-Allow-Origin'] = '*'
        return response, 500


@app.route('/webhook/stripe', methods=['POST'])
def stripe_webhook():
    """
    Receive payment events from Stripe.
    
    We're interested in:
    - checkout.session.completed (successful payment)
    """
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')
    
    try:
        # Verify webhook signature
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET
            )
        else:
            # Dev mode - no signature verification
            event = json.loads(payload)
        
        # Handle successful checkout
        if event['type'] == 'checkout.session.completed':
            session = event['data']['object']
            amount_cents = session.get('amount_total', 0)
            
            log_event('purchase', {
                'session_id': session.get('id'),
                'customer_email': session.get('customer_details', {}).get('email'),
                'amount': amount_cents / 100,
                'currency': session.get('currency', 'usd').upper()
            }, amount_cents=amount_cents)
            
            # Send immediate notification for purchases
            send_purchase_notification(session)
        
        # Handle expired checkout (user abandoned)
        elif event['type'] == 'checkout.session.expired':
            session = event['data']['object']
            log_event('expired', {
                'session_id': session.get('id'),
                'amount': session.get('amount_total', 0) / 100
            })
        
        return jsonify({'received': True}), 200
    except stripe.error.SignatureVerificationError as e:
        print(f"Stripe signature verification failed: {e}")
        return jsonify({'error': 'Invalid signature'}), 400
    except Exception as e:
        print(f"Stripe webhook error: {e}")
        return jsonify({'error': str(e)}), 500


# ============================================================================
# Telegram Notifications
# ============================================================================

def send_telegram_message(text: str):
    """Send a message to all configured Telegram chats."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_IDS:
        print(f"Telegram not configured. Message: {text}")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    success = True
    
    for chat_id in TELEGRAM_CHAT_IDS:
        payload = {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML'
        }
        
        try:
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
        except Exception as e:
            print(f"Telegram error for chat {chat_id}: {e}")
            success = False
            
    return success


def send_purchase_notification(session: dict):
    """Send an immediate notification when a purchase is made."""
    amount = session.get('amount_total', 0) / 100
    email = session.get('customer_details', {}).get('email', 'Unknown')
    
    message = (
        "🎉 <b>New Pre-Order!</b>\n\n"
        f"💰 Amount: ${amount:.0f}\n"
        f"📧 Customer: {email}\n"
        f"🕐 Time: {datetime.now(TIMEZONE).strftime('%I:%M %p')}"
    )
    
    send_telegram_message(message)


def send_signup_notification(email: str, total_count: int):
    """Send a notification when someone signs up for launch notification."""
    message = (
        "📧 <b>New Email Signup!</b>\n\n"
        f"✉️ Email: {email}\n"
        f"📊 Total signups: {total_count}\n"
        f"🕐 Time: {datetime.now(TIMEZONE).strftime('%I:%M %p')}"
    )
    send_telegram_message(message)


def get_stats(hours: int = 1) -> dict:
    """Get event statistics for the specified time period."""
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=hours)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    with get_db() as conn:
        # Last N hours
        hour_stats = conn.execute('''
            SELECT 
                event_type,
                COUNT(*) as count,
                SUM(amount_cents) as total_cents
            FROM events
            WHERE timestamp > ?
            GROUP BY event_type
        ''', (cutoff.isoformat(),)).fetchall()
        
        # Today totals
        today_stats = conn.execute('''
            SELECT 
                event_type,
                COUNT(*) as count,
                SUM(amount_cents) as total_cents
            FROM events
            WHERE timestamp > ?
            GROUP BY event_type
        ''', (today_start.isoformat(),)).fetchall()
    
    def dict_from_stats(stats):
        result = {'qr_scan': 0, 'click': 0, 'purchase': 0, 'expired': 0, 'revenue': 0}
        for row in stats:
            result[row['event_type']] = row['count']
            if row['event_type'] == 'purchase':
                result['revenue'] = (row['total_cents'] or 0) / 100
        return result
    
    return {
        'hour': dict_from_stats(hour_stats),
        'today': dict_from_stats(today_stats)
    }


def get_qr_scan_count() -> dict:
    """
    Fetch QR code scan counts from QR Code Generator API.
    Calculates hourly scans by comparing to the last stored snapshot.
    Returns total, unique, and hourly scan counts.
    """
    if not QR_API_KEY or not QR_CODE_ID:
        return {'total': 0, 'unique': 0, 'last_hour': 0, 'today': 0}
    
    url = f"https://api.qr-code-generator.com/v1/qr-codes/{QR_CODE_ID}/scans/total"
    headers = {
        'Authorization': f'Key {QR_API_KEY}'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        current_total = data.get('total', 0)
        current_unique = data.get('unique', 0)
        
        # Get the last snapshot to calculate hourly scans
        with get_db() as conn:
            # Get snapshot from ~1 hour ago
            hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat()
            hour_snapshot = conn.execute('''
                SELECT total_scans FROM qr_snapshots 
                WHERE timestamp <= ? 
                ORDER BY timestamp DESC LIMIT 1
            ''', (hour_ago,)).fetchone()
            
            # Get snapshot from start of today (UTC)
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            today_snapshot = conn.execute('''
                SELECT total_scans FROM qr_snapshots 
                WHERE timestamp <= ? 
                ORDER BY timestamp DESC LIMIT 1
            ''', (today_start,)).fetchone()
            
            # Store current snapshot
            conn.execute(
                'INSERT INTO qr_snapshots (total_scans, unique_scans) VALUES (?, ?)',
                (current_total, current_unique)
            )
            conn.commit()
        
        # Calculate differences
        last_hour_scans = current_total - (hour_snapshot['total_scans'] if hour_snapshot else current_total)
        today_scans = current_total - (today_snapshot['total_scans'] if today_snapshot else current_total)
        
        # Ensure non-negative (in case of data issues)
        last_hour_scans = max(0, last_hour_scans)
        today_scans = max(0, today_scans)
        
        return {
            'total': current_total,
            'unique': current_unique,
            'last_hour': last_hour_scans,
            'today': today_scans
        }
    except Exception as e:
        print(f"QR API error: {e}")
        return {'total': 0, 'unique': 0, 'last_hour': 0, 'today': 0}


def send_hourly_report():
    """Send the hourly analytics report to Telegram."""
    stats = get_stats(hours=1)
    qr_stats = get_qr_scan_count()  # Fetch from QR Code Generator API
    now = datetime.now(TIMEZONE)
    
    # Calculate conversion rates
    def calc_rate(numerator, denominator):
        if denominator == 0:
            return "—"
        return f"{(numerator / denominator * 100):.0f}%"
    
    hour = stats['hour']
    today = stats['today']
    
    # Use QR API scans for conversion calculation if available
    qr_today = qr_stats['today'] if qr_stats['today'] > 0 else today['qr_scan']
    
    scan_to_click = calc_rate(today['click'], qr_today)
    click_to_purchase = calc_rate(today['purchase'], today['click'])
    
    message = (
        f"📊 <b>Delivery Hatch — Hourly Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"🔲 <b>QR Code Scans</b>\n"
        f"   • Last hour: {qr_stats['last_hour']}\n"
        f"   • Today: {qr_stats['today']}\n"
        f"   • All-time: {qr_stats['total']} ({qr_stats['unique']} unique)\n\n"
        
        f"🖱️ <b>Pre-order Clicks</b>\n"
        f"   • Last hour: {hour['click']}\n"
        f"   • Today: {today['click']}\n\n"
        
        f"💰 <b>Completed Purchases</b>\n"
        f"   • Last hour: {hour['purchase']} (${hour['revenue']:.0f})\n"
        f"   • Today: {today['purchase']} (${today['revenue']:.0f})\n\n"
        
        f"❌ <b>Abandoned Checkouts</b>\n"
        f"   • Last hour: {hour['expired']}\n"
        f"   • Today: {today['expired']}\n\n"
        
        f"📈 <b>Conversion Rate (Today)</b>\n"
        f"   • Scan → Click: {scan_to_click}\n"
        f"   • Click → Purchase: {click_to_purchase}"
    )
    
    send_telegram_message(message)
    print(f"[{now}] Sent hourly report")


# ============================================================================
# Debug Endpoints (for testing)
# ============================================================================

@app.route('/debug/stats', methods=['GET'])
def debug_stats():
    """Get current stats (for debugging)."""
    return jsonify(get_stats(hours=24))


@app.route('/debug/send-report', methods=['POST'])
def debug_send_report():
    """Manually trigger the hourly report."""
    send_hourly_report()
    return jsonify({'status': 'sent'})


@app.route('/emails', methods=['GET'])
def list_emails():
    """List all email signups (for exporting)."""
    with get_db() as conn:
        rows = conn.execute(
            'SELECT email, timestamp, source FROM email_signups ORDER BY timestamp DESC'
        ).fetchall()
    
    emails = [{'email': r['email'], 'timestamp': r['timestamp'], 'source': r['source']} for r in rows]
    return jsonify({'count': len(emails), 'emails': emails})


# ============================================================================
# Scheduler Setup
# ============================================================================

scheduler = BackgroundScheduler(timezone=TIMEZONE)


def start_scheduler():
    """Start the APScheduler for hourly reports."""
    # DISABLED: Hourly reports turned off per user request
    # scheduler.add_job(
    #     send_hourly_report,
    #     CronTrigger(minute=0),  # Every hour at :00
    #     id='hourly_report',
    #     replace_existing=True
    # )
    # scheduler.start()
    print(f"Scheduler disabled - hourly reports turned off.")


# ============================================================================
# Application Startup
# ============================================================================

# Initialize database on import
init_db()

# Start scheduler when running directly or via gunicorn
if os.environ.get('WERKZEUG_RUN_MAIN') == 'true' or 'gunicorn' in os.environ.get('SERVER_SOFTWARE', ''):
    start_scheduler()
elif __name__ != '__main__':
    # Running under gunicorn
    start_scheduler()

if __name__ == '__main__':
    start_scheduler()
    app.run(debug=True, port=5000)
