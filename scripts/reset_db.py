import os
import sys

# Add parent directory to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from webapp.app.models import init_db

if __name__ == '__main__':
    print("Reinitializing database...")
    init_db()
    print("Database has been reinitialized with empty tables.")