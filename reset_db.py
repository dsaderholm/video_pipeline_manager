from app.models import init_db

if __name__ == '__main__':
    print("Reinitializing database...")
    init_db()
    print("Database has been reinitialized with empty tables.")