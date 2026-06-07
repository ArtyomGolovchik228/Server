"""
Seed-скрипт для VREnglish БД.

Создаёт (или обновляет, если уже есть):
  1. Админа        — admin@example.com / Admin123!
  2. Тестового игрока — user_code TEST01 (логин в Unity по этому коду)

Запуск ВНУТРИ docker-контейнера:
    docker compose exec api python seed.py

Скрипт идемпотентный — можно гонять повторно, просто перезапишет данные.
"""
from datetime import datetime

# Импортируем из основного приложения
from main import SessionLocal, User, get_password_hash


# ---------- Данные ----------
ADMIN_EMAIL = "admin@example.com"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "Admin123!"
ADMIN_USER_CODE = "ADMN01"   # 6 символов (2 буквы + 4 цифры/буквы)

PLAYER_USER_CODE = "TEST01"
PLAYER_USERNAME = "TestPlayer"
PLAYER_EMAIL = "testplayer@example.com"
PLAYER_PHONE = "+79991234567"
PLAYER_COINS = 100
PLAYER_XP = 50
PLAYER_LEVEL = 0


def upsert_admin(db):
    """Создать или обновить админа по email."""
    user = (
        db.query(User)
        .filter((User.email == ADMIN_EMAIL) | (User.user_code == ADMIN_USER_CODE))
        .first()
    )
    is_new = user is None
    if is_new:
        user = User(
            user_code=ADMIN_USER_CODE,
            username=ADMIN_USERNAME,
            email=ADMIN_EMAIL,
            phone=None,
            created_at=datetime.utcnow(),
        )
        db.add(user)

    # Перезаписываем поля (даже если юзер уже был — обновляем пароль/флаги)
    user.user_code = ADMIN_USER_CODE
    user.username = ADMIN_USERNAME
    user.email = ADMIN_EMAIL
    user.hashed_password = get_password_hash(ADMIN_PASSWORD)
    user.is_admin = True
    user.is_active = True
    user.is_franchise_participant = False
    user.is_approved = True

    db.commit()
    db.refresh(user)
    return user, is_new


def upsert_player(db):
    """Создать или обновить тестового игрока по user_code."""
    player = db.query(User).filter(User.user_code == PLAYER_USER_CODE).first()
    is_new = player is None
    if is_new:
        player = User(
            user_code=PLAYER_USER_CODE,
            username=PLAYER_USERNAME,
            email=PLAYER_EMAIL,
            phone=PLAYER_PHONE,
            hashed_password=None,  # игроки логинятся только по коду
            created_at=datetime.utcnow(),
        )
        db.add(player)

    player.username = PLAYER_USERNAME
    player.email = PLAYER_EMAIL
    player.phone = PLAYER_PHONE
    player.is_admin = False
    player.is_active = True
    player.is_franchise_participant = False
    player.is_approved = False

    # Стартовый прогресс
    player.current_level = PLAYER_LEVEL
    player.coins = PLAYER_COINS
    player.experience = PLAYER_XP
    player.total_play_time = 0
    player.passedlevels = 0
    player.purchasedlevels = 0
    player.available_points = 0
    player.total_points = 0

    db.commit()
    db.refresh(player)
    return player, is_new


def main():
    db = SessionLocal()
    try:
        admin, admin_new = upsert_admin(db)
        player, player_new = upsert_player(db)

        bar = "=" * 60
        print(bar)
        print("SEED DONE")
        print(bar)

        print(f"\n[ADMIN]  {'CREATED' if admin_new else 'UPDATED'}")
        print(f"  user_code : {admin.user_code}")
        print(f"  username  : {admin.username}")
        print(f"  email     : {admin.email}")
        print(f"  password  : {ADMIN_PASSWORD}   (для входа в админку)")
        print(f"  is_admin  : {admin.is_admin}")
        print(f"  is_active : {admin.is_active}")
        print(f"  Логин в админ-панель: http://localhost:8000/admin/login")
        print(f"  Поле 'Username' → {admin.username}  (или {admin.email})")
        print(f"  Поле 'Password' → {ADMIN_PASSWORD}")

        print(f"\n[PLAYER] {'CREATED' if player_new else 'UPDATED'}")
        print(f"  user_code : {player.user_code}   ← вводить в Unity LoginScene")
        print(f"  username  : {player.username}")
        print(f"  email     : {player.email}")
        print(f"  phone     : {player.phone}")
        print(f"  coins     : {player.coins}")
        print(f"  exp       : {player.experience}")
        print(f"  level     : {player.current_level}")

        print(f"\n{bar}")
        print("В Unity открой LoginScene → введи код TEST01 → должен пустить.")
        print(bar)
    finally:
        db.close()


if __name__ == "__main__":
    main()
