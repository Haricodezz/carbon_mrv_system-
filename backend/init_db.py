# init_db.py
from database import engine, Base
import models

DEFAULT_SETTINGS = [
    ("auto_approve_land_owners",   "true",  "Auto approve land owner registrations"),
    ("auto_approve_organizations", "true",  "Auto approve organization registrations"),
    ("auto_approve_auditors",      "false", "Auto approve auditor registrations"),
    ("max_upload_size_mb",         "5",     "Maximum document upload size in MB"),
    ("buffer_rate",                "0.20",  "Buffer pool rate (20%)"),
]

def create_tables():
    print("Connecting to PostgreSQL...")
    try:
        Base.metadata.create_all(bind=engine)
        print("All tables created successfully!")
        seed_settings()
    except Exception as e:
        print("Error creating tables:", e)


def seed_settings():
    from database import SessionLocal
    db = SessionLocal()
    try:
        for key, value, description in DEFAULT_SETTINGS:
            exists = db.query(models.SystemSetting).filter_by(key=key).first()
            if not exists:
                db.add(models.SystemSetting(key=key, value=value, description=description))
        db.commit()
        print("Default system settings seeded!")
    except Exception as e:
        db.rollback()
        print("Error seeding settings:", e)
    finally:
        db.close()


if __name__ == "__main__":
    create_tables()
