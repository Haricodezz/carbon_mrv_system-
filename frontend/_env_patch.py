import os

# patch secret key before the rest of app.py loads
_SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production")
