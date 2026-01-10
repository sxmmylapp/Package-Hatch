# Delivery Hatch Analytics Bot

A Telegram bot that sends hourly reports with:
- 🔲 QR code scans
- 🖱️ Pre-order button clicks
- 💰 Completed purchases

## Quick Start

### 1. Create a Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`
3. Name it something like "Delivery Hatch Analytics"
4. Copy the **bot token** (looks like `123456:ABC-DEF...`)

### 2. Get Your Chat ID

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram
2. It will reply with your **chat ID** (a number like `123456789`)

### 3. Configure Environment Variables

In Railway (or your `.env` file for local testing):

```
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
STRIPE_WEBHOOK_SECRET=whsec_...
TIMEZONE=America/New_York
```

### 4. Deploy to Railway

```bash
cd analytics-bot
railway login
railway init
railway up
```

After deployment, Railway will give you a URL like:
`https://analytics-bot-production-xxxx.up.railway.app`

### 5. Configure Webhooks

#### QR Code Generator
1. Go to your QR Code Generator dashboard
2. Find Webhooks/Integrations settings
3. Add webhook URL: `https://YOUR-RAILWAY-URL/webhook/qr`

#### Stripe
1. Go to [Stripe Dashboard → Webhooks](https://dashboard.stripe.com/webhooks)
2. Add endpoint: `https://YOUR-RAILWAY-URL/webhook/stripe`
3. Select events: `checkout.session.completed`
4. Copy the signing secret to `STRIPE_WEBHOOK_SECRET`

### 6. Update Your Website

Add the tracking script to your landing page (see the updated `index.html` in the parent directory).

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/webhook/qr` | POST | QR code scan events |
| `/webhook/stripe` | POST | Stripe payment events |
| `/track/click` | POST | Website click tracking |
| `/debug/stats` | GET | View current stats |
| `/debug/send-report` | POST | Manually trigger a report |

## Testing Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Copy env template
cp .env.example .env
# Edit .env with your values

# Run the server
python server.py

# Test QR webhook
curl -X POST http://localhost:5000/webhook/qr \
  -H "Content-Type: application/json" \
  -d '{"country": "US", "device_type": "mobile"}'

# Test click tracking
curl -X POST http://localhost:5000/track/click \
  -H "Content-Type: application/json" \
  -d '{"button": "Pre-Order Now", "timestamp": 1234567890}'

# View stats
curl http://localhost:5000/debug/stats

# Manually trigger report
curl -X POST http://localhost:5000/debug/send-report
```
