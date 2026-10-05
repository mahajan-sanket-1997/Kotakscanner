# KotakScanner

Live Kotak Neo market-data web application for candlestick charts and pattern detection.

## Features
- Recent OHLCV candles from Kotak Neo Historical Data.
- Current Kotak Neo SFeed WebSocket for live prices.
- Live candle construction for 1/3/5/10/15/30/60 minute intervals.
- Common candlestick pattern detection.
- Mobile-friendly chart.
- No order placement, modification, or cancellation.

## Credentials
Copy .env.example to .env and fill in your Kotak Neo API credentials. Never commit .env.

Required variables:
- KOTAK_CONSUMER_KEY
- KOTAK_MOBILE
- KOTAK_UCC
- KOTAK_MPIN
- KOTAK_TOTP_SECRET

## Run in Termux
Python 3.10+ is required.

    pip install -r requirements.txt
    cp .env.example .env
    python app.py

Open http://127.0.0.1:8000 on the same device.

## Instrument token
The first version accepts the Kotak Neo exchange segment and instrument token directly. A searchable NSE/BSE instrument selector can be added next.

## Deployment
GitHub stores the source code. A persistent Python WebSocket backend needs a Python host such as Render, Railway, Fly.io, or a VM. Configure the environment variables as encrypted server-side secrets.

Do not use GitHub Pages for the live backend.

## Important
A pattern displayed while the current candle is forming is not a confirmed signal. This application is for market-data visualization/research and is not financial advice.
