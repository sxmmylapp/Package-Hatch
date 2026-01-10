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

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
STRIPE_WEBHOOK_SECRET = os.getenv('STRIPE_WEBHOOK_SECRET')
QR_API_KEY = os.getenv('QR_API_KEY')
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
    """Send a message to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"Telegram not configured. Message: {text}")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Telegram error: {e}")
        return False


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
        result = {'qr_scan': 0, 'click': 0, 'purchase': 0, 'revenue': 0}
        for row in stats:
            result[row['event_type']] = row['count']
            if row['event_type'] == 'purchase':
                result['revenue'] = (row['total_cents'] or 0) / 100
        return result
    
    return {
        'hour': dict_from_stats(hour_stats),
        'today': dict_from_stats(today_stats)
    }


def send_hourly_report():
    """Send the hourly analytics report to Telegram."""
    stats = get_stats(hours=1)
    now = datetime.now(TIMEZONE)
    
    # Calculate conversion rates
    def calc_rate(numerator, denominator):
        if denominator == 0:
            return "—"
        return f"{(numerator / denominator * 100):.0f}%"
    
    hour = stats['hour']
    today = stats['today']
    
    scan_to_click = calc_rate(today['click'], today['qr_scan'])
    click_to_purchase = calc_rate(today['purchase'], today['click'])
    
    message = (
        f"📊 <b>Delivery Hatch — Hourly Report</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        f"🔲 <b>QR Code Scans</b>\n"
        f"   • Last hour: {hour['qr_scan']}\n"
        f"   • Today: {today['qr_scan']}\n\n"
        
        f"🖱️ <b>Pre-order Clicks</b>\n"
        f"   • Last hour: {hour['click']}\n"
        f"   • Today: {today['click']}\n\n"
        
        f"💰 <b>Completed Purchases</b>\n"
        f"   • Last hour: {hour['purchase']} (${hour['revenue']:.0f})\n"
        f"   • Today: {today['purchase']} (${today['revenue']:.0f})\n\n"
        
        f"📈 <b>Conversion Rate (Today)</b>\n"
        f"   • Scan → Click: {scan_to_click}\n"
        f"   • Click → Purchase: {click_to_purchase}\n\n"
        
        f"🕐 {now.strftime('%I:%M %p')} ET"
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


# ============================================================================
# Scheduler Setup
# ============================================================================

scheduler = BackgroundScheduler(timezone=TIMEZONE)


def start_scheduler():
    """Start the APScheduler for hourly reports."""
    # Run at the top of every hour
    scheduler.add_job(
        send_hourly_report,
        CronTrigger(minute=0),  # Every hour at :00
        id='hourly_report',
        replace_existing=True
    )
    scheduler.start()
    print(f"Scheduler started. Next report at the top of the next hour.")


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
