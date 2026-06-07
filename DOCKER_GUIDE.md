# Запуск VREnglish-сервера в Docker

Папка `server/` — самодостаточный пакет: FastAPI-приложение (`main.py`) + админ-панель (`templates/`) + Redis (для rate-limiting) — всё поднимается одной командой через `docker compose`.

## Что внутри

```
server/
├── Dockerfile              # образ для FastAPI-приложения
├── docker-compose.yml      # связка api + redis с healthcheck'ами
├── requirements.txt        # python-зависимости
├── .env.example            # шаблон env-переменных (скопируй в .env)
├── .dockerignore           # что не тащить в образ
├── main.py                 # серверный код (FastAPI)
├── admin-tools.py          # утилита: показать/сбросить пароль админа
├── templates/              # HTML-шаблоны админ-панели
├── data/                   # SQLite-база (создастся автоматически)
└── logs/                   # логи uvicorn (создастся автоматически)
```

После первого запуска появятся ещё `data/game_auth.db` (SQLite) и `data/admin_credentials.txt` (логин/пароль автоматически созданного админа).

---

## 1. Установка Docker

### Windows / macOS

Скачай и поставь **Docker Desktop**: <https://www.docker.com/products/docker-desktop>

После установки запусти Docker Desktop и проверь:

```powershell
docker --version
docker compose version
```

### Linux

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # чтобы не писать sudo каждый раз
# перелогинься после этого
```

---

## 2. Подготовь `.env`

В папке `server/` есть `.env.example`. Скопируй и поменяй `SECRET_KEY` на свой длинный случайный.

### Windows (PowerShell)

```powershell
cd D:\Documents\projects\VREnglish-dev\server
copy .env.example .env

# Сгенерируй и подставь SECRET_KEY:
$key = -join ((1..32) | ForEach-Object { '{0:X2}' -f (Get-Random -Maximum 256) })
(Get-Content .env) -replace 'change-me-to-a-long-random-hex-string', $key | Set-Content .env
notepad .env   # проверь что SECRET_KEY теперь нормальный
```

### Linux / macOS

```bash
cd /path/to/VREnglish-dev/server
cp .env.example .env
sed -i.bak "s/change-me-to-a-long-random-hex-string/$(openssl rand -hex 32)/" .env && rm .env.bak
cat .env   # глянь что SECRET_KEY заполнен
```

> **Важно:** если оставить `SECRET_KEY=change-me-...`, docker compose всё равно стартанёт, но все JWT-токены при перезапуске будут невалидными — менять обязательно.

---

## 3. Сборка и запуск

Из той же папки `server/`:

```bash
docker compose up -d --build
```

* `--build` — пересобрать образ (нужно после правок `main.py`, `requirements.txt`, шаблонов)
* `-d` — в фоне

Проверь статус:

```bash
docker compose ps
```

Должно быть видно два сервиса: `vrenglish-api` (status `healthy`) и `vrenglish-redis` (status `healthy`). Хелсчек у api занимает ~20 секунд после старта, не паникуй.

Открой в браузере: <http://localhost:8000/admin/login>

---

## 4. Достать пароль админа

При первом запуске сервер сам создаёт пользователя `ADMIN` со случайным паролем. Где его взять:

### Из примонтированной папки (проще всего)

```bash
# Windows
type data\admin_credentials.txt

# Linux/Mac
cat data/admin_credentials.txt
```

### Из контейнера

```bash
docker compose exec api cat /app/admin_credentials.txt
```

Если файла нет (например, удалил случайно) — сбрось пароль:

```bash
docker compose exec api python admin-tools.py reset
```

---

## 5. Подключение Unity-клиента

В Unity-сцене `LoginScene` у компонента `AuthManager` поле `Server URL` поменяй на:

* запуск локально, та же машина → `http://127.0.0.1:8000`
* запуск с другого устройства в локальной сети → `http://<IP_твоего_компа>:8000` (например `http://192.168.1.42:8000`)
* запуск с VR-шлема → тоже `http://<IP_твоего_компа>:8000` (шлем должен быть в той же Wi-Fi сети)

В `LessonCompleteHandler` (новый скрипт для записи прогресса) поле `Server Base URL` либо такое же, либо оставь пустым — он по умолчанию берёт `http://127.0.0.1:8000`.

---

## 6. Полезные команды

```bash
# Посмотреть живые логи API
docker compose logs -f api

# Логи Redis
docker compose logs -f redis

# Зайти внутрь контейнера API (bash)
docker compose exec api bash

# Перезапустить только API (после правки main.py)
docker compose up -d --build api

# Остановить всё
docker compose down

# Остановить + удалить БД + удалить Redis-данные (полный сброс)
docker compose down -v
rm -rf data logs    # на Windows: rmdir /s /q data logs

# Список юзеров через admin-tools
docker compose exec api python admin-tools.py list

# Сбросить пароль админу
docker compose exec api python admin-tools.py reset
```

---

## 7. Что делать после обновления кода

После любых правок `main.py`, `templates/*`, `requirements.txt`:

```bash
docker compose up -d --build api
```

База в `./data/` и Redis-данные **переживут** пересборку — пересоздаётся только образ приложения.

---

## 8. Прод (опционально)

Если хочешь выкатить наружу:

1. Поставь reverse-proxy (caddy / nginx / traefik) перед api, повесь HTTPS.
2. В `docker-compose.yml` убери проброс порта `6379:6379` у redis — Redis должен быть доступен ТОЛЬКО внутри docker-сети.
3. В Dockerfile поменяй CMD на запуск через gunicorn:
   `CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:8000"]`
   (добавь `gunicorn==21.2.0` в `requirements.txt`).
4. Поменяй SQLite на PostgreSQL: в `.env` укажи `DATABASE_URL=postgresql://user:pass@db:5432/game_auth` и добавь сервис `db: image: postgres:16-alpine` в `docker-compose.yml`.

---

## 9. Траблшутинг

| Симптом | Что проверить |
|---|---|
| `Set SECRET_KEY in .env` при `docker compose up` | Нет файла `.env` рядом с `docker-compose.yml`. Скопируй `.env.example` → `.env`. |
| `bind: address already in use` на порту 8000/6379 | Уже что-то занимает порт. Освободи или поменяй маппинг в `docker-compose.yml` (например `8001:8000`). |
| Юнити не достучаться до сервера | Брандмауэр Windows блочит входящие на 8000. Открой порт: `New-NetFirewallRule -DisplayName "VREnglish API" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow` (PowerShell от админа). |
| База обнулилась после перезапуска | В `docker-compose.yml` должна быть строка `- ./data:/app/data`, а `DATABASE_URL` — `sqlite:///./data/game_auth.db`. Проверь оба места. |
| `redis.exceptions.ConnectionError` в логах | Redis не успел подняться. Хелсчек должен это уже решать, но если нет — `docker compose restart api`. |
