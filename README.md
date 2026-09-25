# BankSim

- 1 quarter = 1 hour.
- Quarter boundaries are aligned to UTC/GMT half-hour marks (e.g. 12:30, 13:30, 14:30 UTC).
- New banks start with a 2M reserve.
- Bank rates and terms are shown before savings, FD, and loan transactions.
- INR currency market remains available with bid/ask order book.
- Includes `wsgi.py` for Gunicorn/Render deployment.

## Render start command
`gunicorn --bind 0.0.0.0:$PORT wsgi:app`
