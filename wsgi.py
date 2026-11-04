# Load .env (if present) before the app reads its settings; real environment variables always win.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional in production, where the host sets the variables
    pass

from app import create_app

app = create_app()
