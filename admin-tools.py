#!/usr/bin/env python3
import sys
import os
sys.path.append('/app')

from main import SessionLocal, User, get_password_hash
import secrets

def show_admin_credentials():
    """Показать существующие учетные данные админа"""
    db = SessionLocal()
    try:
        admin = db.query(User).filter(User.is_admin == True).first()
        if admin:
            print("=" * 50)
            print("EXISTING ADMIN USER")
            print("=" * 50)
            print(f"User Code: {admin.user_code}")
            print(f"Username: {admin.username}")
            print(f"Email: {admin.email}")
            print(f"Is Active: {admin.is_active}")
            print(f"Created: {admin.created_at}")
            print("=" * 50)
            print("Note: Password is hashed and cannot be retrieved")
            print("Use reset function to set new password")
            print("=" * 50)
        else:
            print("No admin user found in database")
    finally:
        db.close()

def reset_admin_password():
    """Сбросить пароль админа"""
    db = SessionLocal()
    try:
        admin_code = os.getenv("ADMIN_CODE", "ADMIN")
        admin = db.query(User).filter(User.user_code == admin_code).first()
        
        if not admin:
            print(f"Admin user with code {admin_code} not found")
            return
        
        new_password = secrets.token_urlsafe(12)
        if len(new_password) > 72:
            new_password = new_password[:72]
        
        admin.hashed_password = get_password_hash(new_password)
        db.commit()
        
        print("=" * 50)
        print("ADMIN PASSWORD RESET")
        print("=" * 50)
        print(f"Admin Code: {admin_code}")
        print(f"New Password: {new_password}")
        print("=" * 50)
        
        # Сохраняем в файл
        try:
            with open("/app/data/admin_password_reset.txt", "w") as f:
                f.write("=" * 50 + "\n")
                f.write("ADMIN PASSWORD RESET\n")
                f.write("=" * 50 + "\n")
                f.write(f"Admin Code: {admin_code}\n")
                f.write(f"New Password: {new_password}\n")
                f.write(f"Reset at: {datetime.now()}\n")
                f.write("=" * 50 + "\n")
        except Exception as e:
            print(f"Warning: Could not save to file: {e}")
            
    finally:
        db.close()

def create_new_admin():
    """Создать нового админа (если не существует)"""
    db = SessionLocal()
    try:
        admin_code = os.getenv("ADMIN_CODE", "ADMIN")
        admin = db.query(User).filter(User.user_code == admin_code).first()
        
        if admin:
            print(f"Admin user with code {admin_code} already exists")
            return
        
        admin_password = secrets.token_urlsafe(12)
        if len(admin_password) > 72:
            admin_password = admin_password[:72]
        
        admin_user = User(
            user_code=admin_code,
            username=os.getenv("ADMIN_USERNAME", "Administrator"),
            email=os.getenv("ADMIN_EMAIL", "admin@example.com"),
            hashed_password=get_password_hash(admin_password),
            is_admin=True,
            is_active=True,
            created_at=datetime.utcnow()
        )
        
        db.add(admin_user)
        db.commit()
        
        print("=" * 50)
        print("NEW ADMIN USER CREATED")
        print("=" * 50)
        print(f"Admin Code: {admin_code}")
        print(f"Password: {admin_password}")
        print("=" * 50)
        
    finally:
        db.close()

if __name__ == "__main__":
    import argparse
    from datetime import datetime
    
    parser = argparse.ArgumentParser(description="Admin Tools for Game Auth System")
    parser.add_argument("action", choices=["show", "reset", "create"], help="Action to perform")
    
    args = parser.parse_args()
    
    if args.action == "show":
        show_admin_credentials()
    elif args.action == "reset":
        reset_admin_password()
    elif args.action == "create":
        create_new_admin()