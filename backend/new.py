import bcrypt
from sqlalchemy import create_engine, text

# Hash password
pwd = b'#Hari012345'
hashed = bcrypt.hashpw(pwd, bcrypt.gensalt()).decode()

# Insert admin
engine = create_engine('postgresql://postgres:%23Hari012345@localhost:5432/carbon_mrv')
with engine.connect() as conn:
    conn.execute(text("""
        INSERT INTO users (name, email, password_hash, wallet_address, role, is_approved, is_active, is_verified, created_at)
        VALUES (:name, :email, :hash, :wallet, :role, true, true, true, NOW())
        ON CONFLICT (email) DO UPDATE SET role='admin', is_approved=true
    """), {
        'name': 'Admin Hari',
        'email': 'hariprakashyd@gmail.com',
        'hash': hashed,
        'wallet': '0xD8dA6BF26964aF9D7eEd9e03E53415D37aA96045',
        'role': 'admin'
    })
    conn.execute(text("UPDATE users SET is_approved=true WHERE email='neha.verma@auditor.gov.in'"))
    conn.commit()
    print("Done! Admin created + Auditor approved!")