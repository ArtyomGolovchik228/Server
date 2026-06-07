import os
import secrets
import string
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from ipaddress import ip_address
from functools import wraps
import time
import logging
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException, Depends, Request, status, Form, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, desc, func, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
from passlib.context import CryptContext
from jose import JWTError, jwt
from pydantic import BaseModel, EmailStr, validator
import redis
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('logs/api.log', maxBytes=10485760, backupCount=5),  # 10MB файлы
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация
SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_urlsafe(32))
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
API_TOKEN_EXPIRE_DAYS = 30
LOGIN_ATTEMPT_LIMIT = 5  # Максимум попыток входа за период
LOGIN_ATTEMPT_PERIOD = 300  # 5 минут в секундах
REQUEST_LIMIT = 100  # Максимум запросов в минуту

# Создаем папку для логов если её нет
os.makedirs('logs', exist_ok=True)
os.makedirs('static', exist_ok=True)

# База данных
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./game_auth.db")
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Redis для rate limiting
redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "localhost"),
    port=int(os.getenv("REDIS_PORT", 6379)),
    db=0,
    decode_responses=True
)

# Контекст для хеширования паролей
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Модели базы данных
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    user_code = Column(String(6), unique=True, index=True, nullable=False)
    username = Column(String(100), nullable=False)
    phone = Column(String(20))
    email = Column(String(100), nullable=True)  # Optional for players
    hashed_password = Column(String(255))
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    is_franchise_participant = Column(Boolean, default=False)
    is_approved = Column(Boolean, default=False)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    passedlevels = Column(Integer, default=0)
    purchasedlevels = Column(Integer, default=0)
    available_points = Column(Integer, default=0)
    total_points = Column(Integer, default=0)
    # Game statistics
    current_level = Column(Integer, default=0)
    coins = Column(Integer, default=0)
    experience = Column(Integer, default=0)
    total_play_time = Column(Integer, default=0)  # in seconds
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login = Column(DateTime)
    
    # Связи
    login_logs = relationship("LoginLog", back_populates="user", cascade="all, delete-orphan")
    api_logs = relationship("ApiLog", back_populates="user", cascade="all, delete-orphan")
    approver = relationship("User", remote_side=[id], foreign_keys=[approved_by])

class LoginLog(Base):
    __tablename__ = "login_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    ip_address = Column(String(45))  # Поддержка IPv6
    user_agent = Column(Text)
    success = Column(Boolean, default=False)
    attempt_time = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="login_logs")

class ApiLog(Base):
    __tablename__ = "api_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    endpoint = Column(String(255))
    method = Column(String(10))
    ip_address = Column(String(45))
    user_agent = Column(Text)
    request_data = Column(Text, nullable=True)
    response_status = Column(Integer)
    response_time = Column(Float)  # Время выполнения в секундах
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User", back_populates="api_logs")

class ApiToken(Base):
    __tablename__ = "api_tokens"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    token = Column(String(255), unique=True, index=True)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used = Column(DateTime)
    is_active = Column(Boolean, default=True)
    
    user = relationship("User")

# Создание таблиц
Base.metadata.create_all(bind=engine)

# Миграция: добавление недостающих колонок в существующую базу данных
def migrate_database():
    """Добавляет недостающие колонки в существующую базу данных"""
    from sqlalchemy import inspect, text
    
    try:
        inspector = inspect(engine)
        
        # Проверяем существование таблицы users
        if 'users' not in inspector.get_table_names():
            logger.info("Users table does not exist, will be created by create_all")
            return
        
        existing_columns = [col['name'] for col in inspector.get_columns('users')]
        
        # Для SQLite BOOLEAN хранится как INTEGER
        new_columns = {
            'is_franchise_participant': 'INTEGER DEFAULT 0',
            'is_approved': 'INTEGER DEFAULT 0',
            'approved_by': 'INTEGER',
            'approved_at': 'DATETIME',
            'current_level': 'INTEGER DEFAULT 0',
            'coins': 'INTEGER DEFAULT 0',
            'experience': 'INTEGER DEFAULT 0',
            'total_play_time': 'INTEGER DEFAULT 0'
        }
        
        with engine.begin() as conn:
            for column_name, column_type in new_columns.items():
                if column_name not in existing_columns:
                    try:
                        sql = f"ALTER TABLE users ADD COLUMN {column_name} {column_type}"
                        conn.execute(text(sql))
                        logger.info(f"Added column {column_name} to users table")
                    except Exception as e:
                        logger.warning(f"Could not add column {column_name}: {e}")
            # Заполняем NULL → дефолтами у уже существующих юзеров.
            null_defaults = {
                'passedlevels': 0,
                'purchasedlevels': 0,
                'available_points': 0,
                'total_points': 0,
                'current_level': 0,
                'coins': 0,
                'experience': 0,
                'total_play_time': 0,
                'is_active': 1,
                'is_admin': 0,
                'is_franchise_participant': 0,
                'is_approved': 0,
            }
            for col, default in null_defaults.items():
                try:
                    conn.execute(text(
                        f"UPDATE users SET {col} = :d WHERE {col} IS NULL"
                    ), {"d": default})
                except Exception as e:
                    logger.warning(f"Could not backfill NULLs in {col}: {e}")

        logger.info("Database migration completed")
    except Exception as e:
        logger.error(f"Database migration error: {e}")

# Выполняем миграцию при запуске
migrate_database()

# Обновляем существующих админов (на случай если is_admin был NULL)
def fix_existing_admins():
    """Устанавливает is_admin=True для пользователей с кодом ADMIN"""
    try:
        db = SessionLocal()
        admin_users = db.query(User).filter(User.user_code == "ADMIN").all()
        for admin in admin_users:
            if admin.is_admin is None or admin.is_admin == False:
                admin.is_admin = True
                logger.info(f"Fixed is_admin for user {admin.user_code}")
        db.commit()
        db.close()
    except Exception as e:
        logger.error(f"Error fixing existing admins: {e}")

fix_existing_admins()

# Pydantic модели
class UserCreate(BaseModel):
    username: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    password: Optional[str] = None

class UserLogin(BaseModel):
    user_code: str
    password: str

class UserResponse(BaseModel):
    id: int
    user_code: str
    username: str
    phone: Optional[str] = None
    email: Optional[str] = None
    # Optional с дефолтами на случай NULL в БД
    passedlevels: Optional[int] = 0
    purchasedlevels: Optional[int] = 0
    available_points: Optional[int] = 0
    total_points: Optional[int] = 0
    is_active: Optional[bool] = True
    is_admin: Optional[bool] = False
    is_franchise_participant: Optional[bool] = False
    is_approved: Optional[bool] = False
    approved_by: Optional[int] = None
    approved_at: Optional[datetime] = None
    current_level: Optional[int] = 0
    coins: Optional[int] = 0
    experience: Optional[int] = 0
    total_play_time: Optional[int] = 0
    created_at: Optional[datetime] = None
    last_login: Optional[datetime] = None

    class Config:
        orm_mode = True
        from_attributes = True

class LoginRequest(BaseModel):
    user_code: str
    
    class Config:
        # Позволяет принимать данные с любыми дополнительными полями
        extra = "ignore"

class LoginResponse(BaseModel):
    id: int
    user_code: str
    username: str
    phone: Optional[str] = None
    passedlevels: Optional[int] = 0
    purchasedlevels: Optional[int] = 0
    available_points: Optional[int] = 0
    total_points: Optional[int] = 0
    current_level: Optional[int] = 0
    coins: Optional[int] = 0
    experience: Optional[int] = 0
    total_play_time: Optional[int] = 0

class TokenData(BaseModel):
    user_id: Optional[int] = None

class LoginLogResponse(BaseModel):
    id: int
    user_code: str
    username: str
    ip_address: str
    user_agent: Optional[str]
    success: bool
    attempt_time: datetime

class ApiLogResponse(BaseModel):
    id: int
    user_code: Optional[str]
    username: Optional[str]
    endpoint: str
    method: str
    ip_address: str
    response_status: int
    response_time: float
    created_at: datetime

class StatsResponse(BaseModel):
    total_users: int
    active_users: int
    total_logins: int
    successful_logins: int
    failed_logins: int
    total_api_calls: int
    avg_response_time: float

class FranchiseParticipantCreate(BaseModel):
    username: str
    email: str
    phone: str
    password: str

class PlayerCreate(BaseModel):
    username: str
    phone: str
    email: Optional[str] = None
    current_level: Optional[int] = 0
    coins: Optional[int] = 0
    experience: Optional[int] = 0
    total_play_time: Optional[int] = 0

class PlayerUpdate(BaseModel):
    current_level: Optional[int] = None
    coins: Optional[int] = None
    experience: Optional[int] = None
    total_play_time: Optional[int] = None
    passedlevels: Optional[int] = None
    purchasedlevels: Optional[int] = None

class PlayerResponse(BaseModel):
    id: int
    user_code: str
    username: str
    phone: Optional[str] = None
    current_level: Optional[int] = 0
    coins: Optional[int] = 0
    experience: Optional[int] = 0
    total_play_time: Optional[int] = 0
    passedlevels: Optional[int] = 0
    purchasedlevels: Optional[int] = 0
    available_points: Optional[int] = 0
    total_points: Optional[int] = 0
    is_active: Optional[bool] = True
    created_at: Optional[datetime] = None

    class Config:
        orm_mode = True
        from_attributes = True


# ---- Модели для админских CRUD над юзерами ----
class AdminUserCreate(BaseModel):
    username: str
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    current_level: Optional[int] = 0
    coins: Optional[int] = 0
    experience: Optional[int] = 0
    total_play_time: Optional[int] = 0
    passedlevels: Optional[int] = 0
    purchasedlevels: Optional[int] = 0
    available_points: Optional[int] = 0
    total_points: Optional[int] = 0
    is_admin: Optional[bool] = False
    is_active: Optional[bool] = True

    class Config:
        extra = "ignore"


class AdminUserUpdate(BaseModel):
    username: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    password: Optional[str] = None
    current_level: Optional[int] = None
    coins: Optional[int] = None
    experience: Optional[int] = None
    total_play_time: Optional[int] = None
    passedlevels: Optional[int] = None
    purchasedlevels: Optional[int] = None
    available_points: Optional[int] = None
    total_points: Optional[int] = None
    is_admin: Optional[bool] = None
    is_active: Optional[bool] = None

    class Config:
        extra = "ignore"

# Инициализация FastAPI
app = FastAPI(title="Game Authentication API", version="1.0.0")

# Настройка CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Статические файлы и шаблоны
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Схема аутентификации (опциональная для поддержки cookie)
security = HTTPBearer(auto_error=False)

# Утилиты
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=15)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def verify_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: int = payload.get("sub")
        if user_id is None:
            return None
        return TokenData(user_id=user_id)
    except JWTError:
        return None

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Rate limiting
def rate_limit(request: Request, limit: int = REQUEST_LIMIT, period: int = 60):
    ip = request.client.host
    key = f"rate_limit:{ip}"
    
    current = redis_client.get(key)
    if current is None:
        redis_client.setex(key, period, 1)
        return True
    
    if int(current) >= limit:
        return False
    
    redis_client.incr(key)
    return True

def check_login_attempts(ip: str):
    key = f"login_attempts:{ip}"
    attempts = redis_client.get(key)
    
    if attempts is None:
        redis_client.setex(key, LOGIN_ATTEMPT_PERIOD, 1)
        return True
    
    if int(attempts) >= LOGIN_ATTEMPT_LIMIT:
        return False
    
    redis_client.incr(key)
    return True

# Декоратор для логирования запросов
def log_request(endpoint: str = None):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            request = kwargs.get('request') or args[0] if args else None
            start_time = time.time()
            
            try:
                response = await func(*args, **kwargs)
                response_time = time.time() - start_time
                
                if request and hasattr(request, 'client'):
                    log_api_call(
                        db=kwargs.get('db'),
                        request=request,
                        endpoint=endpoint or request.url.path,
                        response_status=response.status_code if hasattr(response, 'status_code') else 200,
                        response_time=response_time,
                        user_id=kwargs.get('current_user_id')
                    )
                
                return response
            except Exception as e:
                response_time = time.time() - start_time
                if request and hasattr(request, 'client'):
                    log_api_call(
                        db=kwargs.get('db'),
                        request=request,
                        endpoint=endpoint or request.url.path,
                        response_status=getattr(e, 'status_code', 500),
                        response_time=response_time,
                        user_id=kwargs.get('current_user_id')
                    )
                raise
        
        return wrapper
    return decorator

def log_api_call(db: Session, request: Request, endpoint: str, response_status: int, 
                 response_time: float, user_id: Optional[int] = None):
    try:
        api_log = ApiLog(
            user_id=user_id,
            endpoint=endpoint,
            method=request.method,
            ip_address=request.client.host,
            user_agent=request.headers.get("user-agent"),
            response_status=response_status,
            response_time=response_time,
            created_at=datetime.utcnow()
        )
        
        # Логируем тело запроса для не-чувствительных endpoints
        if endpoint in ["/login", "/register"] and request.method in ["POST", "PUT"]:
            try:
                body = request.json()
                api_log.request_data = json.dumps(body)
            except:
                pass
        
        db.add(api_log)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to log API call: {e}")

def log_login_attempt(db: Session, user_id: Optional[int], ip: str, user_agent: str, success: bool):
    try:
        login_log = LoginLog(
            user_id=user_id,
            ip_address=ip,
            user_agent=user_agent,
            success=success,
            attempt_time=datetime.utcnow()
        )
        db.add(login_log)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to log login attempt: {e}")

# Dependency для проверки авторизации (поддерживает и Bearer токен, и cookie)
def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
):
    token = None
    
    # Сначала пробуем получить токен из Bearer заголовка
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    else:
        # Если нет Bearer токена, пробуем получить из cookie
        token = request.cookies.get("admin_token")
    
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    token_data = verify_token(token)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = db.query(User).filter(User.id == token_data.user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    
    return user

def get_admin_user(current_user: User = Depends(get_current_user)):
    is_admin = current_user.is_admin if current_user.is_admin is not None else False
    if not is_admin:
        raise HTTPException(status_code=403, detail="Not enough permissions")
    return current_user

# Dependency для проверки участника франшизы (админ или одобренный участник)
def get_franchise_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db)
):
    token = None
    
    # Сначала пробуем получить токен из Bearer заголовка
    if credentials and credentials.scheme.lower() == "bearer":
        token = credentials.credentials
    else:
        # Если нет Bearer токена, пробуем получить из cookie
        token = request.cookies.get("admin_token")
    
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    token_data = verify_token(token)
    if token_data is None:
        raise HTTPException(status_code=401, detail="Invalid token")
    
    user = db.query(User).filter(User.id == token_data.user_id).first()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    
    # Проверяем что пользователь либо админ, либо одобренный участник франшизы
    is_admin = user.is_admin if user.is_admin is not None else False
    is_approved = user.is_approved if user.is_approved is not None else False
    is_franchise = user.is_franchise_participant if user.is_franchise_participant is not None else False
    
    if not is_admin and not (is_franchise and is_approved):
        raise HTTPException(status_code=403, detail="Not enough permissions")
    
    return user

# Dependency для проверки аутентификации через cookie (для веб-интерфейса)
def get_current_user_from_cookie(request: Request, db: Session = Depends(get_db)):
    token = request.cookies.get("admin_token")
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or inactive")
        
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid token")

# Генерация кода пользователя
def generate_user_code():
    letters = ''.join(secrets.choice(string.ascii_uppercase) for _ in range(2))
    numbers = ''.join(secrets.choice(string.digits) for _ in range(4))
    return letters + numbers

# API Endpoints
@app.post("/register", response_model=UserResponse)
@log_request("/register")
async def register_user(
    user_data: UserCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    # Rate limiting
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    # Генерируем уникальный код
    user_code = generate_user_code()
    while db.query(User).filter(User.user_code == user_code).first():
        user_code = generate_user_code()
    
    # Хешируем пароль если есть
    hashed_password = None
    if user_data.password:
        hashed_password = get_password_hash(user_data.password)
    
    # Создаем пользователя
    user = User(
        user_code=user_code,
        username=user_data.username,
        email=user_data.email,
        phone=user_data.phone,
        hashed_password=hashed_password,
        created_at=datetime.utcnow()
    )
    
    db.add(user)
    db.commit()
    db.refresh(user)
    
    logger.info(f"New user registered: {user_code} - {user.username}")
    
    return user

@app.post("/register/franchise", response_model=UserResponse)
@log_request("/register/franchise")
async def register_franchise_participant(
    participant_data: FranchiseParticipantCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    # Rate limiting
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    # Проверяем что email не занят
    existing_user = db.query(User).filter(
        (User.email == participant_data.email) | (User.username == participant_data.username)
    ).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="Email or username already registered")
    
    # Генерируем уникальный код
    user_code = generate_user_code()
    while db.query(User).filter(User.user_code == user_code).first():
        user_code = generate_user_code()
    
    # Хешируем пароль
    hashed_password = get_password_hash(participant_data.password)
    
    # Создаем участника франшизы (не одобренного)
    user = User(
        user_code=user_code,
        username=participant_data.username,
        email=participant_data.email,
        phone=participant_data.phone,
        hashed_password=hashed_password,
        is_franchise_participant=True,
        is_approved=False,
        is_active=True,
        created_at=datetime.utcnow()
    )
    
    db.add(user)
    db.commit()
    db.refresh(user)
    
    logger.info(f"New franchise participant registered: {user_code} - {user.username} (pending approval)")
    
    return user

@app.post("/login", response_model=LoginResponse)
@log_request("/login")
async def login_user(
    login_data: LoginRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    try:
        # Проверка rate limiting для IP
        ip = request.client.host
        received_code = login_data.user_code
        normalized_code = received_code.upper().strip()
        
        logger.info(f"Login attempt - Received code: '{received_code}', Normalized: '{normalized_code}', IP: {ip}")
        
        if not check_login_attempts(ip):
            log_login_attempt(db, None, ip, request.headers.get("user-agent"), False)
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Please try again later."
            )
        
        # Поиск пользователя по коду (приводим к верхнему регистру)
        user = db.query(User).filter(User.user_code == normalized_code).first()
        
        # Если не найден, попробуем найти все коды для отладки
        if user is None:
            # Логируем все существующие коды для отладки
            all_users = db.query(User.user_code, User.username, User.is_active).limit(10).all()
            codes_info = [f"{c[0]} ({c[1]}, active={c[2]})" for c in all_users]
            logger.warning(f"Login attempt with non-existent code: '{normalized_code}' (original: '{received_code}')")
            logger.warning(f"Available codes in DB (first 10): {codes_info}")
            
            # Также попробуем найти без учета регистра (на случай если в БД код в нижнем регистре)
            user_lower = db.query(User).filter(func.lower(User.user_code) == normalized_code.lower()).first()
            if user_lower:
                logger.warning(f"Found user with case-insensitive match: {user_lower.user_code} (stored as: '{user_lower.user_code}')")
                user = user_lower
            else:
                log_login_attempt(db, None, ip, request.headers.get("user-agent"), False)
                raise HTTPException(status_code=404, detail="User not found")
        
        if not user.is_active:
            log_login_attempt(db, user.id, ip, request.headers.get("user-agent"), False)
            logger.warning(f"Login attempt for inactive user: {user.user_code}")
            raise HTTPException(status_code=403, detail="User account is disabled")
        
        # Обновляем время последнего входа
        user.last_login = datetime.utcnow()
        db.commit()
        
        # Логируем успешный вход
        log_login_attempt(db, user.id, ip, request.headers.get("user-agent"), True)
        
        logger.info(f"User logged in: {user.user_code} from {ip}")
        
        # Возвращаем данные (включая новые поля для совместимости)
        response_data = {
            "id": user.id,
            "user_code": user.user_code,
            "username": user.username,
            "phone": user.phone if user.phone else None,
            "passedlevels": user.passedlevels or 0,
            "purchasedlevels": user.purchasedlevels or 0,
            "available_points": user.available_points or 0,
            "total_points": user.total_points or 0,
            "current_level": user.current_level or 0,
            "coins": user.coins or 0,
            "experience": user.experience or 0,
            "total_play_time": user.total_play_time or 0
        }
        
        return response_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in login endpoint: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/complete_level")
@log_request("/complete_level")
async def complete_level(
    request_data: LoginRequest,
    request: Request,
    db: Session = Depends(get_db)
):
    # Rate limiting
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    user = db.query(User).filter(User.user_code == request_data.user_code.upper()).first()
    
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")
    
    # Обновляем прогресс
    user.passedlevels += 1
    user.available_points += 1
    user.total_points += 1
    
    db.commit()
    
    logger.info(f"Level completed for user: {user.user_code}")
    
    return {
        "message": "Level completed successfully",
        "passedlevels": user.passedlevels,
        "available_points": user.available_points
    }

# Franchise participant endpoints (for managing players)
@app.get("/franchise/users", response_model=List[PlayerResponse])
@log_request("/franchise/users")
async def get_franchise_players(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_franchise_user)
):
    """Get list of players (non-franchise, non-admin users)"""
    # Участники франшизы видят только игроков (не админов и не других участников франшизы)
    players = db.query(User).filter(
        User.is_admin == False,
        User.is_franchise_participant == False
    ).order_by(User.created_at.desc()).all()
    return players

@app.post("/franchise/users/add", response_model=PlayerResponse)
@log_request("/franchise/users/add")
async def add_player(
    player_data: PlayerCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_franchise_user)
):
    """Add a new player (only for franchise participants and admins)"""
    # Генерируем уникальный код
    user_code = generate_user_code()
    while db.query(User).filter(User.user_code == user_code).first():
        user_code = generate_user_code()
    
    # Создаем игрока
    player = User(
        user_code=user_code,
        username=player_data.username,
        phone=player_data.phone,
        email=player_data.email,
        is_active=True,
        is_admin=False,
        is_franchise_participant=False,
        current_level=player_data.current_level or 0,
        coins=player_data.coins or 0,
        experience=player_data.experience or 0,
        total_play_time=player_data.total_play_time or 0,
        created_at=datetime.utcnow()
    )
    
    db.add(player)
    db.commit()
    db.refresh(player)
    
    logger.info(f"Player {player.user_code} added by franchise participant {current_user.user_code}")
    
    return player

@app.get("/franchise/users/{user_id}", response_model=PlayerResponse)
@log_request("/franchise/users/{user_id}")
async def get_player(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_franchise_user)
):
    """Get player details"""
    player = db.query(User).filter(
        User.id == user_id,
        User.is_admin == False,
        User.is_franchise_participant == False
    ).first()
    
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    
    return player

@app.put("/franchise/users/{user_id}", response_model=PlayerResponse)
@log_request("/franchise/users/{user_id}")
async def update_player(
    user_id: int,
    player_data: PlayerUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_franchise_user)
):
    """Update player information"""
    player = db.query(User).filter(
        User.id == user_id,
        User.is_admin == False,
        User.is_franchise_participant == False
    ).first()
    
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    
    # Обновляем только переданные поля
    if player_data.current_level is not None:
        player.current_level = player_data.current_level
    if player_data.coins is not None:
        player.coins = player_data.coins
    if player_data.experience is not None:
        player.experience = player_data.experience
    if player_data.total_play_time is not None:
        player.total_play_time = player_data.total_play_time
    if player_data.passedlevels is not None:
        player.passedlevels = player_data.passedlevels
    if player_data.purchasedlevels is not None:
        player.purchasedlevels = player_data.purchasedlevels
    
    db.commit()
    db.refresh(player)
    
    logger.info(f"Player {player.user_code} updated by franchise participant {current_user.user_code}")
    
    return player

@app.delete("/franchise/users/{user_id}")
@log_request("/franchise/users/{user_id}")
async def delete_player(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_franchise_user)
):
    """Delete a player (deactivate)"""
    player = db.query(User).filter(
        User.id == user_id,
        User.is_admin == False,
        User.is_franchise_participant == False
    ).first()
    
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    
    # Деактивируем вместо удаления
    player.is_active = False
    db.commit()
    
    logger.info(f"Player {player.user_code} deactivated by franchise participant {current_user.user_code}")
    
    return {"message": "Player deactivated successfully"}

# Game API endpoints for game integration
@app.get("/api/player/{user_code}", response_model=PlayerResponse)
@log_request("/api/player/{user_code}")
async def get_player_by_code(
    user_code: str,
    db: Session = Depends(get_db)
):
    """Get player data by user code (for game integration)"""
    player = db.query(User).filter(
        User.user_code == user_code.upper(),
        User.is_admin == False,
        User.is_franchise_participant == False
    ).first()
    
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    
    if not player.is_active:
        raise HTTPException(status_code=403, detail="Player account is disabled")
    
    return player

@app.post("/api/player/{user_code}/update")
@log_request("/api/player/{user_code}/update")
async def update_player_game_data(
    user_code: str,
    player_data: PlayerUpdate,
    request: Request,
    db: Session = Depends(get_db)
):
    """Update player game data (level, coins, experience, playtime)"""
    # Rate limiting
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    player = db.query(User).filter(
        User.user_code == user_code.upper(),
        User.is_admin == False,
        User.is_franchise_participant == False
    ).first()
    
    if not player:
        raise HTTPException(status_code=404, detail="Player not found")
    
    if not player.is_active:
        raise HTTPException(status_code=403, detail="Player account is disabled")
    
    # Обновляем игровые данные
    if player_data.current_level is not None:
        player.current_level = player_data.current_level
    if player_data.coins is not None:
        player.coins = player_data.coins
    if player_data.experience is not None:
        player.experience = player_data.experience
    if player_data.total_play_time is not None:
        player.total_play_time = player_data.total_play_time
    if player_data.passedlevels is not None:
        player.passedlevels = player_data.passedlevels
    if player_data.purchasedlevels is not None:
        player.purchasedlevels = player_data.purchasedlevels
    
    db.commit()
    db.refresh(player)
    
    logger.info(f"Player {player.user_code} game data updated")
    
    return {
        "message": "Player data updated successfully",
        "user_code": player.user_code,
        "current_level": player.current_level,
        "coins": player.coins,
        "experience": player.experience,
        "total_play_time": player.total_play_time
    }

@app.post("/api/player/{user_code}/level")
@log_request("/api/player/{user_code}/level")
async def update_player_level(
    user_code: str,
    level: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db)
):
    """Update player level"""
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    player = db.query(User).filter(User.user_code == user_code.upper()).first()
    if not player or not player.is_active:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player.current_level = level
    db.commit()
    
    return {"message": "Level updated", "current_level": player.current_level}

@app.post("/api/player/{user_code}/coins")
@log_request("/api/player/{user_code}/coins")
async def update_player_coins(
    user_code: str,
    coins: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db)
):
    """Update player coins"""
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    player = db.query(User).filter(User.user_code == user_code.upper()).first()
    if not player or not player.is_active:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player.coins = coins
    db.commit()
    
    return {"message": "Coins updated", "coins": player.coins}

@app.post("/api/player/{user_code}/experience")
@log_request("/api/player/{user_code}/experience")
async def update_player_experience(
    user_code: str,
    experience: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db)
):
    """Update player experience"""
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    player = db.query(User).filter(User.user_code == user_code.upper()).first()
    if not player or not player.is_active:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player.experience = experience
    db.commit()
    
    return {"message": "Experience updated", "experience": player.experience}

@app.post("/api/player/{user_code}/playtime")
@log_request("/api/player/{user_code}/playtime")
async def update_player_playtime(
    user_code: str,
    playtime: int = Form(...),
    request: Request = None,
    db: Session = Depends(get_db)
):
    """Update player total play time (in seconds)"""
    if not rate_limit(request):
        raise HTTPException(status_code=429, detail="Too many requests")
    
    player = db.query(User).filter(User.user_code == user_code.upper()).first()
    if not player or not player.is_active:
        raise HTTPException(status_code=404, detail="Player not found")
    
    player.total_play_time = playtime
    db.commit()
    
    return {"message": "Playtime updated", "total_play_time": player.total_play_time}

# Admin endpoints

def _safe_user_dict(u):
    """Сериализация юзера руками — чтобы NULL/мусор в БД не валил эндпоинт."""
    def _i(v, default=0):
        try:
            return int(v) if v is not None else default
        except Exception:
            return default

    def _b(v, default=False):
        if v is None:
            return default
        return bool(v)

    def _dt(v):
        try:
            return v.isoformat() if v is not None else None
        except Exception:
            return None

    return {
        "id": _i(getattr(u, "id", None)),
        "user_code": getattr(u, "user_code", None) or "",
        "username": getattr(u, "username", None) or "",
        "phone": getattr(u, "phone", None),
        "email": getattr(u, "email", None),
        "passedlevels": _i(getattr(u, "passedlevels", 0)),
        "purchasedlevels": _i(getattr(u, "purchasedlevels", 0)),
        "available_points": _i(getattr(u, "available_points", 0)),
        "total_points": _i(getattr(u, "total_points", 0)),
        "is_active": _b(getattr(u, "is_active", True), default=True),
        "is_admin": _b(getattr(u, "is_admin", False)),
        "is_franchise_participant": _b(getattr(u, "is_franchise_participant", False)),
        "is_approved": _b(getattr(u, "is_approved", False)),
        "approved_by": getattr(u, "approved_by", None),
        "approved_at": _dt(getattr(u, "approved_at", None)),
        "current_level": _i(getattr(u, "current_level", 0)),
        "coins": _i(getattr(u, "coins", 0)),
        "experience": _i(getattr(u, "experience", 0)),
        "total_play_time": _i(getattr(u, "total_play_time", 0)),
        "created_at": _dt(getattr(u, "created_at", None)),
        "last_login": _dt(getattr(u, "last_login", None)),
    }


@app.get("/admin/users")
@log_request("/admin/users")
async def get_all_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    """Список всех юзеров. Ручная сериализация — никакой pydantic-валидации,
    чтобы NULL/неожиданные данные в БД не выкидывали 500."""
    users = db.query(User).order_by(User.created_at.desc()).all()

    result = []
    for u in users:
        try:
            result.append(_safe_user_dict(u))
        except Exception as e:
            logger.error(f"Failed to serialize user id={getattr(u,'id','?')}: {e}", exc_info=True)
            result.append({
                "id": getattr(u, "id", 0),
                "user_code": getattr(u, "user_code", "?"),
                "username": getattr(u, "username", "<broken>"),
                "_error": str(e),
            })

    logger.info(f"/admin/users -> returning {len(result)} users")
    return result

@app.get("/admin/logs/login", response_model=List[LoginLogResponse])
@log_request("/admin/logs/login")
async def get_login_logs(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    logs = (db.query(LoginLog, User)
            .join(User, LoginLog.user_id == User.id)
            .order_by(desc(LoginLog.attempt_time))
            .offset(skip)
            .limit(limit)
            .all())
    
    return [{
        "id": log.id,
        "user_code": user.user_code,
        "username": user.username,
        "ip_address": log.ip_address,
        "user_agent": log.user_agent,
        "success": log.success,
        "attempt_time": log.attempt_time
    } for log, user in logs]

@app.get("/admin/logs/api", response_model=List[ApiLogResponse])
@log_request("/admin/logs/api")
async def get_api_logs(
    skip: int = 0,
    limit: int = 100,
    user_code: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    query = (db.query(ApiLog, User)
            .outerjoin(User, ApiLog.user_id == User.id))
    
    if user_code:
        query = query.filter(User.user_code == user_code.upper())
    
    logs = (query.order_by(desc(ApiLog.created_at))
            .offset(skip)
            .limit(limit)
            .all())
    
    return [{
        "id": log.id,
        "user_code": user.user_code if user else None,
        "username": user.username if user else None,
        "endpoint": log.endpoint,
        "method": log.method,
        "ip_address": log.ip_address,
        "response_status": log.response_status,
        "response_time": log.response_time,
        "created_at": log.created_at
    } for log, user in logs]

@app.get("/admin/stats", response_model=StatsResponse)
@log_request("/admin/stats")
async def get_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    total_users = db.query(func.count(User.id)).scalar()
    active_users = db.query(func.count(User.id)).filter(User.is_active == True).scalar()
    
    total_logins = db.query(func.count(LoginLog.id)).scalar()
    successful_logins = db.query(func.count(LoginLog.id)).filter(LoginLog.success == True).scalar()
    failed_logins = db.query(func.count(LoginLog.id)).filter(LoginLog.success == False).scalar()
    
    total_api_calls = db.query(func.count(ApiLog.id)).scalar()
    avg_response_time = db.query(func.avg(ApiLog.response_time)).scalar() or 0
    
    return {
        "total_users": total_users,
        "active_users": active_users,
        "total_logins": total_logins,
        "successful_logins": successful_logins,
        "failed_logins": failed_logins,
        "total_api_calls": total_api_calls,
        "avg_response_time": round(avg_response_time, 3)
    }

# ---------------------------------------------------------------------------
# Admin CRUD over users
# ---------------------------------------------------------------------------

@app.post("/admin/users/add")
@log_request("/admin/users/add")
async def admin_create_user(
    user_data: AdminUserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    user_code = generate_user_code()
    while db.query(User).filter(User.user_code == user_code).first():
        user_code = generate_user_code()

    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(status_code=400, detail="Username already taken")

    if user_data.email and db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed_password = get_password_hash(user_data.password) if user_data.password else None

    new_user = User(
        user_code=user_code,
        username=user_data.username,
        phone=user_data.phone,
        email=user_data.email,
        hashed_password=hashed_password,
        is_admin=bool(user_data.is_admin),
        is_active=bool(user_data.is_active) if user_data.is_active is not None else True,
        is_franchise_participant=False,
        is_approved=False,
        current_level=user_data.current_level or 0,
        coins=user_data.coins or 0,
        experience=user_data.experience or 0,
        total_play_time=user_data.total_play_time or 0,
        passedlevels=user_data.passedlevels or 0,
        purchasedlevels=user_data.purchasedlevels or 0,
        available_points=user_data.available_points or 0,
        total_points=user_data.total_points or 0,
        created_at=datetime.utcnow()
    )

    db.add(new_user); db.commit(); db.refresh(new_user)
    logger.info(f"Admin {current_user.user_code} created user {new_user.user_code}")
    return _safe_user_dict(new_user)


@app.put("/admin/users/{user_id}")
@log_request("/admin/users/{user_id}")
async def admin_update_user(
    user_id: int,
    user_data: AdminUserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if target.id == current_user.id and user_data.is_admin is False:
        other_admins = db.query(User).filter(User.is_admin == True, User.id != current_user.id).count()
        if other_admins == 0:
            raise HTTPException(status_code=400, detail="Нельзя снять admin-права с последнего администратора")

    if user_data.username is not None: target.username = user_data.username
    if user_data.phone is not None: target.phone = user_data.phone
    if user_data.email is not None: target.email = user_data.email
    if user_data.password: target.hashed_password = get_password_hash(user_data.password)
    if user_data.current_level is not None: target.current_level = user_data.current_level
    if user_data.coins is not None: target.coins = user_data.coins
    if user_data.experience is not None: target.experience = user_data.experience
    if user_data.total_play_time is not None: target.total_play_time = user_data.total_play_time
    if user_data.passedlevels is not None: target.passedlevels = user_data.passedlevels
    if user_data.purchasedlevels is not None: target.purchasedlevels = user_data.purchasedlevels
    if user_data.available_points is not None: target.available_points = user_data.available_points
    if user_data.total_points is not None: target.total_points = user_data.total_points
    if user_data.is_admin is not None: target.is_admin = bool(user_data.is_admin)
    if user_data.is_active is not None: target.is_active = bool(user_data.is_active)

    db.commit(); db.refresh(target)
    logger.info(f"Admin {current_user.user_code} updated user {target.user_code}")
    return _safe_user_dict(target)


@app.delete("/admin/users/{user_id}")
@log_request("/admin/users/{user_id}")
async def admin_delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == current_user.id:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")
    if target.is_admin:
        other_admins = db.query(User).filter(User.is_admin == True, User.id != target.id).count()
        if other_admins == 0:
            raise HTTPException(status_code=400, detail="Нельзя удалить последнего администратора")
    db.delete(target); db.commit()
    logger.info(f"Admin {current_user.user_code} deleted user {target.user_code} (id={user_id})")
    return {"message": "User deleted successfully"}


@app.post("/admin/users/{user_id}/reset")
@log_request("/admin/users/{user_id}/reset")
async def admin_reset_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    new_code = generate_user_code()
    while db.query(User).filter(User.user_code == new_code).first():
        new_code = generate_user_code()
    old_code = target.user_code
    target.user_code = new_code
    target.hashed_password = None
    db.commit(); db.refresh(target)
    logger.info(f"Admin {current_user.user_code} reset user {old_code} -> {new_code}")
    return {"message": "Password reset", "new_code": new_code, "user_id": target.id}


# Franchise approval endpoints
@app.get("/admin/franchise/pending", response_model=List[UserResponse])
@log_request("/admin/franchise/pending")
async def get_pending_franchise_participants(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    """Get list of franchise participants waiting for approval"""
    pending = db.query(User).filter(
        User.is_franchise_participant == True,
        User.is_approved == False
    ).order_by(User.created_at.desc()).all()
    return pending

@app.post("/admin/franchise/approve/{user_id}")
@log_request("/admin/franchise/approve")
async def approve_franchise_participant(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    """Approve a franchise participant registration"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if not user.is_franchise_participant:
        raise HTTPException(status_code=400, detail="User is not a franchise participant")
    
    if user.is_approved:
        raise HTTPException(status_code=400, detail="User is already approved")
    
    user.is_approved = True
    user.approved_by = current_user.id
    user.approved_at = datetime.utcnow()
    
    db.commit()
    db.refresh(user)
    
    logger.info(f"Franchise participant {user.user_code} approved by admin {current_user.user_code}")
    
    return {"message": "Franchise participant approved successfully", "user": user}

@app.post("/admin/franchise/reject/{user_id}")
@log_request("/admin/franchise/reject")
async def reject_franchise_participant(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_admin_user)
):
    """Reject a franchise participant registration"""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if not user.is_franchise_participant:
        raise HTTPException(status_code=400, detail="User is not a franchise participant")
    
    # Деактивируем пользователя вместо удаления
    user.is_active = False
    
    db.commit()
    
    logger.info(f"Franchise participant {user.user_code} rejected by admin {current_user.user_code}")
    
    return {"message": "Franchise participant registration rejected"}

# Web интерфейс
@app.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    return RedirectResponse("/admin/login")

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request})

@app.post("/admin/login")
async def admin_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    try:
        user = db.query(User).filter(
            (User.username == username) | (User.email == username)
        ).first()
        
        if not user:
            logger.warning(f"Login attempt with non-existent username: {username}")
            log_login_attempt(db, None, 
                             request.client.host, request.headers.get("user-agent"), False)
            return templates.TemplateResponse("admin_login.html", {
                "request": request,
                "error": "Invalid credentials"
            })
        
        if not user.is_active:
            logger.warning(f"Login attempt for inactive user: {user.user_code}")
            log_login_attempt(db, user.id, 
                             request.client.host, request.headers.get("user-agent"), False)
            return templates.TemplateResponse("admin_login.html", {
                "request": request,
                "error": "Account is disabled"
            })
        
        # Проверяем пароль
        if not user.hashed_password or not verify_password(password, user.hashed_password):
            logger.warning(f"Invalid password for user: {user.user_code}")
            log_login_attempt(db, user.id, request.client.host, 
                             request.headers.get("user-agent"), False)
            return templates.TemplateResponse("admin_login.html", {
                "request": request,
                "error": "Invalid credentials"
            })
        
        # Проверяем права доступа - админ или одобренный участник франшизы
        is_admin = user.is_admin if user.is_admin is not None else False
        is_approved = user.is_approved if user.is_approved is not None else False
        is_franchise_approved = (user.is_franchise_participant and is_approved)
        
        if not is_admin and not is_franchise_approved:
            logger.warning(f"User {user.user_code} is not admin and not approved franchise participant")
            log_login_attempt(db, user.id, request.client.host, 
                             request.headers.get("user-agent"), False)
            return templates.TemplateResponse("admin_login.html", {
                "request": request,
                "error": "Access denied. You need admin rights or approved franchise participant status."
            })
        
        # Создаем токен для сессии
        access_token = create_access_token(
            data={"sub": str(user.id)},
            expires_delta=timedelta(hours=8)
        )
        
        # Определяем куда перенаправлять
        if is_admin:
            redirect_url = "/admin/dashboard"
        else:
            redirect_url = "/franchise/dashboard"
        
        response = RedirectResponse(redirect_url, status_code=303)
        response.set_cookie(
            key="admin_token",
            value=access_token,
            httponly=True,  # Безопаснее - JavaScript не может прочитать, но браузер отправит автоматически
            max_age=60*60*8,
            secure=False,  # True в production с HTTPS
            samesite="lax",
            path="/"
        )
        logger.info(f"Cookie set: admin_token (length: {len(access_token)})")
        
        log_login_attempt(db, user.id, request.client.host, 
                         request.headers.get("user-agent"), True)
        
        logger.info(f"User {user.user_code} logged in successfully, redirecting to {redirect_url}")
        
        return response
    except Exception as e:
        logger.error(f"Error in admin_login: {e}", exc_info=True)
        return templates.TemplateResponse("admin_login.html", {
            "request": request,
            "error": "An error occurred. Please try again."
        })

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard_page(
    request: Request,
    db: Session = Depends(get_db)
):
    token = request.cookies.get("admin_token")
    if not token:
        logger.warning("No admin_token cookie found")
        return RedirectResponse("/admin/login")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            logger.warning(f"User with id {user_id} not found")
            return RedirectResponse("/admin/login")
        
        if not user.is_active:
            logger.warning(f"User {user_id} is not active")
            return RedirectResponse("/admin/login")
        
        if not user.is_admin:
            logger.warning(f"User {user_id} is not admin, redirecting to franchise dashboard")
            # Если пользователь - участник франшизы, перенаправляем на его панель
            if user.is_franchise_participant and user.is_approved:
                return RedirectResponse("/franchise/dashboard")
            return RedirectResponse("/admin/login")
        
        return templates.TemplateResponse("dashboard.html", {
            "request": request,
            "user": user
        })
    except JWTError as e:
        logger.error(f"JWT decode error: {e}")
        return RedirectResponse("/admin/login")
    except Exception as e:
        logger.error(f"Error in admin_dashboard_page: {e}")
        return RedirectResponse("/admin/login")

@app.get("/admin/users_management", response_class=HTMLResponse)
async def users_management_page(
    request: Request,
    db: Session = Depends(get_db)
):
    token = request.cookies.get("admin_token")
    if not token:
        return RedirectResponse("/admin/login")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user or not user.is_active:
            return RedirectResponse("/admin/login")
        
        is_admin = user.is_admin if user.is_admin is not None else False
        if not is_admin:
            return RedirectResponse("/admin/login")
        
        return templates.TemplateResponse("users.html", {
            "request": request,
            "user": user
        })
    except (JWTError, ValueError, Exception) as e:
        logger.error(f"Error in users_management_page: {e}")
        return RedirectResponse("/admin/login")

@app.get("/admin/logs", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    db: Session = Depends(get_db)
):
    token = request.cookies.get("admin_token")
    if not token:
        return RedirectResponse("/admin/login")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user or not user.is_active:
            return RedirectResponse("/admin/login")
        
        is_admin = user.is_admin if user.is_admin is not None else False
        if not is_admin:
            return RedirectResponse("/admin/login")
        
        return templates.TemplateResponse("logs.html", {
            "request": request,
            "user": user
        })
    except (JWTError, ValueError, Exception) as e:
        logger.error(f"Error in logs_page: {e}")
        return RedirectResponse("/admin/login")

@app.get("/franchise/dashboard", response_class=HTMLResponse)
async def franchise_dashboard_page(
    request: Request,
    db: Session = Depends(get_db)
):
    token = request.cookies.get("admin_token")
    if not token:
        return RedirectResponse("/admin/login")
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user or not user.is_active:
            logger.warning(f"User {user_id} not found or inactive")
            return RedirectResponse("/admin/login")
        
        # Проверяем что пользователь либо админ, либо одобренный участник франшизы
        if not user.is_admin:
            # Проверяем участника франшизы
            if not user.is_franchise_participant:
                logger.warning(f"User {user_id} is not a franchise participant")
                return RedirectResponse("/admin/login")
            
            # Проверяем что is_approved не None и True
            if not user.is_approved:
                logger.warning(f"Franchise participant {user_id} is not approved yet")
                return RedirectResponse("/admin/login")
        
        return templates.TemplateResponse("franchise_dashboard.html", {
            "request": request,
            "user": user
        })
    except (JWTError, ValueError, Exception) as e:
        logger.error(f"Error in franchise_dashboard_page: {e}")
        return RedirectResponse("/admin/login")

# Logout endpoint
@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin/login")
    response.delete_cookie("admin_token")
    return response

# Health check endpoint
@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

# Создание админ пользователя при первом запуске
def create_admin_user():
    db = SessionLocal()
    try:
        admin_exists = db.query(User).filter(User.is_admin == True).first()
        if not admin_exists:
            admin_code = "ADMIN"
            admin_password = secrets.token_urlsafe(12)
            
            admin_user = User(
                user_code=admin_code,
                username="Administrator",
                email="admin@example.com",
                hashed_password=get_password_hash(admin_password),
                is_admin=True,
                is_active=True
            )
            
            db.add(admin_user)
            db.commit()
            
            print("=" * 50)
            print("ADMIN USER CREATED")
            print(f"Code: {admin_code}")
            print(f"Password: {admin_password}")
            print("=" * 50)
            
            # Сохраняем в файл на случай если потеряем
            with open("admin_credentials.txt", "w") as f:
                f.write(f"Admin Code: {admin_code}\n")
                f.write(f"Admin Password: {admin_password}\n")
                f.write(f"Created: {datetime.utcnow()}\n")
    except Exception as e:
        logger.error(f"Failed to create admin user: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    import uvicorn
    
    # Создаем админа при первом запуске
    create_admin_user()
    
    logger.info("Starting Game Authentication API Server...")
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_config=None
    )