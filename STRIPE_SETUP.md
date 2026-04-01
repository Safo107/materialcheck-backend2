# Stripe Setup — MaterialCheck Backend

## Umgebungsvariablen (Render)

Unter **Render → Service → Environment** eintragen:

| Variable | Wert | Wo finden |
|---|---|---|
| `STRIPE_SECRET_KEY` | `sk_live_...` | Stripe Dashboard → Developers → API keys |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...` | Stripe Dashboard → Developers → Webhooks → Endpoint |
| `STRIPE_PRICE_ID` | `price_...` | Stripe Dashboard → Products → MaterialCheck+ → Price ID |

## Python-Abhängigkeit

```bash
pip install stripe
```

In `requirements.txt` eintragen:
```
stripe>=7.0.0
```

## Stripe Dashboard Setup

1. **Produkt anlegen:** Products → Add product
   - Name: `MaterialCheck+`
   - Preis: `X,XX € / Monat` (recurring) — Preis noch festlegen
   - → Price ID kopieren → `STRIPE_PRICE_ID`

2. **Webhook anlegen:** Developers → Webhooks → Add endpoint
   - URL: `https://<render-service>.onrender.com/api/stripe/webhook`
   - Events auswählen:
     - `checkout.session.completed`
     - `customer.subscription.deleted`
     - `customer.subscription.updated`
   - → Signing secret kopieren → `STRIPE_WEBHOOK_SECRET`

## API Endpunkte

### POST `/api/stripe/create-checkout-session`
```json
{ "email": "user@example.com", "deviceId": "abc123" }
```
Response:
```json
{ "url": "https://checkout.stripe.com/..." }
```

### POST `/api/stripe/webhook`
Stripe-Signatur wird über `STRIPE_WEBHOOK_SECRET` verifiziert.

## MongoDB — `profiles` Collection

Nach erfolgreicher Zahlung werden folgende Felder gesetzt:
```json
{
  "isPremium": true,
  "premiumSince": "2026-04-01T00:00:00Z",
  "stripeCustomerId": "cus_..."
}
```

## Zugriff im App-Code prüfen

```python
profile = await db.profiles.find_one({"email": email})
is_premium = profile.get("isPremium", False)
```
