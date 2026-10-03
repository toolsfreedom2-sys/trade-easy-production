# Trade Easy V11 Production

Production-ready Streamlit package for the Trade Easy dashboard.

## User experience
- Normal users do not see FYERS App ID, Secret ID, Connect/Reconnect controls, login URLs, or WebSocket connection diagnostics.
- The trading dashboard shell remains visible even when live data is temporarily unavailable.
- Subscription/plan cards remain visible.
- 5/8 EMA and strategy cards are retained.

## Admin experience
- Admin Console: users, plans, subscriptions, payments, access control.
- Admin-only FYERS Connection tab.
- Admin Trading Dashboard after FYERS authentication.

## Production security changes
- Supabase URL/key are read from Streamlit Secrets/environment instead of being hard-coded.
- FYERS App ID/Secret are read from Streamlit Secrets/environment and are not committed to Git.
- Windows DPAPI/local token files are not used.
- The browser-side admin connects through an HTTPS OAuth link suitable for Community Cloud.
- Streamlit developer toolbar is set to viewer mode.

## Streamlit Community Cloud deployment
1. Put `Trade_Easy_V11_PRODUCTION.py`, `requirements.txt`, and `.streamlit/config.toml` in a GitHub repository.
2. Deploy the app from Streamlit Community Cloud.
3. Choose Python 3.12.
4. In App Settings -> Secrets, paste the values from `secrets.toml.example` with real values.
5. After the app receives its final URL, set `TRADE_EASY_PUBLIC_URL`, `SUPABASE_REDIRECT_URL`, and `FYERS_REDIRECT_URI` to that exact HTTPS URL.
6. Add the exact FYERS redirect URI to the FYERS app configuration.
7. Ensure the same Google/Supabase redirect URL is configured in Supabase Auth.

## Local test
Create `.streamlit/secrets.toml` from `secrets.toml.example` with local values, then run:

```bash
py -3.12 -m streamlit run Trade_Easy_V11_PRODUCTION.py
```

For local OAuth testing, the FYERS redirect URI must be an HTTPS URL registered by FYERS; this production package intentionally does not hard-code `localhost`.

## Important FYERS note
This build uses FYERS authentication for market-data/analysis access. It does not enable automatic live order placement. If live order execution is added later, review current FYERS/SEBI requirements before enabling it.
