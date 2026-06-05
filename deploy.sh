#!/usr/bin/env bash
# ==============================================
# Deploy Avito Bot на Timeweb Cloud VDS
# Ubuntu 22.04 / 3 аккаунта
# ==============================================
set -euo pipefail

# ── Цвета ────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
err()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }
step()  { echo; echo -e "${YELLOW}═══ $1 ═══${NC}"; }

# ── Настройки ────────────────────────────────
REPO_URL="https://github.com/rusya13k/avito-bot.git"
BRANCH="main"
BOT_DIR="/opt/avito-bot"
BOT_USER="avito"
PYTHON_VERSION="3"  # 3 = latest (3.12 на 24.04, 3.11 на 22.04)

# ── Проверка root ────────────────────────────
[[ $EUID -eq 0 ]] || err "Запусти от root: sudo bash deploy.sh"

step "1/8 — Системные пакеты"
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    git curl wget unzip \
    software-properties-common \
    xvfb fluxbox \
    libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 \
    libasound2 libxshmfence1 libglib2.0-0 libgtk-3-0 \
    libxdamage1 libxrandr2 libxfixes3 libxi6 libxtst6 libcups2 \
    ufw

step "2/8 — Пользователь avito + python3"
if ! id -u "$BOT_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$BOT_USER"
    usermod -aG sudo "$BOT_USER"
    info "Пользователь $BOT_USER создан"
fi
PYTHON_BIN=$(command -v python3)
[[ -n "$PYTHON_BIN" ]] || err "python3 не установлен"
info "Python: $($PYTHON_BIN --version)"

step "3/8 — Клонирование репозитория"
rm -rf "$BOT_DIR"
mkdir -p "$(dirname "$BOT_DIR")"
git clone --branch "$BRANCH" "$REPO_URL" "$BOT_DIR"
chown -R "$BOT_USER:$BOT_USER" "$BOT_DIR"
cd "$BOT_DIR"
info "Репозиторий склонирован в $BOT_DIR"

step "4/8 — Python venv + зависимости"
sudo -u "$BOT_USER" python3 -m venv "$BOT_DIR/venv"
source "$BOT_DIR/venv/bin/activate"
pip install --upgrade pip setuptools wheel -q
pip install -r "$BOT_DIR/requirements.txt" -q
info "venv готов, зависимости установлены"

step "5/8 — .env (секреты)"
if [[ ! -f "$BOT_DIR/.env" ]]; then
    cp "$BOT_DIR/.env.example" "$BOT_DIR/.env"
    chown "$BOT_USER:$BOT_USER" "$BOT_DIR/.env"
    chmod 600 "$BOT_DIR/.env"
    warn "===== Заполни секреты: nano $BOT_DIR/.env ====="
    echo ""
    echo "  Обязательно:"
    echo "    DEEPSEEK_API_KEY=sk-..."
    echo "    TELEGRAM_BOT_TOKEN=<токен>"
    echo "    ADSPOWER_API_KEY=<ключ>"
    echo ""
else
    info ".env уже существует"
fi

step "6/8 — Установка AdsPower"
if ! command -v adspower &>/dev/null && [[ ! -d "/opt/AdsPower" ]]; then
    cd /tmp
    ADS_DEB="adsower_linux.tar.gz"
    # Последняя версия AdsPower — берём с оф. сайта
    wget -q "https://api.adspower.net/api/v3/download?platform=linux&type=tar.gz" -O "$ADS_DEB"
    tar -xzf "$ADS_DEB" -C /opt/
    chown -R "$BOT_USER:$BOT_USER" /opt/AdsPower
    # Симлинк для удобства
    ln -sf /opt/AdsPower/AdsPower /usr/local/bin/adspower
    info "AdsPower установлен в /opt/AdsPower"
else
    info "AdsPower уже установлен"
fi

step "7/8 — systemd сервисы"

# Xvfb — виртуальный дисплей для AdsPower
cat > /etc/systemd/system/xvfb.service << 'SVC'
[Unit]
Description=X Virtual Frame Buffer
After=network.target

[Service]
Type=simple
User=avito
ExecStart=/usr/bin/Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC

# AdsPower
cat > /etc/systemd/system/adsower.service << 'SVC'
[Unit]
Description=AdsPower Browser
After=xvfb.service network.target
Requires=xvfb.service

[Service]
Type=simple
User=avito
Environment=DISPLAY=:99
ExecStart=/opt/AdsPower/AdsPower
Restart=on-failure
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
SVC

# Avito Bot
cat > /etc/systemd/system/avito-bot.service << 'SVC'
[Unit]
Description=Avito Commercial Real Estate Bot
After=network.target adsower.service
Requires=adsower.service

[Service]
Type=simple
User=avito
WorkingDirectory=/opt/avito-bot
ExecStart=/opt/avito-bot/venv/bin/python bot.py
Restart=on-failure
RestartSec=15
StandardOutput=append:/var/log/avito-bot.log
StandardError=append:/var/log/avito-bot.err

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable xvfb.service adsower.service avito-bot.service

step "8/8 — Firewall + итог"
ufw allow 22/tcp         # SSH
ufw --force enable &>/dev/null || true

echo
echo "=========================================="
echo -e "${GREEN}Деплой завершён!${NC}"
echo "=========================================="
echo ""
echo "Команды:"
echo "  sudo systemctl start adsower.service    — запустить AdsPower"
echo "  sudo systemctl start avito-bot.service  — запустить бота"
echo "  sudo journalctl -u avito-bot -f         — логи бота"
echo ""
echo "Перед запуском:"
echo "  1. Редактировать .env:  sudo -u avito nano $BOT_DIR/.env"
echo "     — DEEPSEEK_API_KEY = твой ключ"
echo "  2. Создать/проверить accounts.json:  $BOT_DIR/accounts.json"
echo "  3. Запустить:  sudo systemctl start adsower"
echo "  4. Через 30с:  sudo systemctl start avito-bot"
echo ""
echo "После включения: TG бот будет доступен у 2 админов."
echo "=========================================="
