#!/usr/bin/env bash
set -euo pipefail

cd "$HOME/anki-papers"
app_dir="$HOME/anki-papers"
data_dir="$app_dir/data"
current_user="$(id -un)"
run_root() {
  if test "$(id -u)" -eq 0; then
    "$@"
  else
    printf '%s' "$DEPLOY_PASSWORD_B64" | base64 -d | sudo -S -p '' "$@"
  fi
}

if ! python3 -m venv .venv; then
  venv_package="python$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')-venv"
  run_root apt-get update
  run_root apt-get install -y "$venv_package"
  python3 -m venv --clear .venv
fi
run_root apt-get update
run_root apt-get install -y caddy
if command -v ufw >/dev/null 2>&1; then
  run_root ufw allow 80/tcp
  run_root ufw allow 443/tcp
  run_root ufw --force delete allow 8000/tcp || true
fi
mkdir -p "$data_dir"
chmod 700 "$data_dir"
backup_dir="$data_dir/backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"

run_root systemctl stop anki-papers.service 2>/dev/null || true
run_root systemctl stop anki-papers-web.service 2>/dev/null || true
run_root systemctl stop anki-papers-sync-worker.service 2>/dev/null || true
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install . ./sync-worker
.venv/bin/python -m pip check
BACKUP_DIR="$backup_dir" DATA_DIR="$data_dir" PYTHONPATH=src .venv/bin/python - <<'PY'
import os
import shutil
import tempfile
from pathlib import Path
from articles_to_anki.backups.snapshot import build_snapshot

data = Path(os.environ["DATA_DIR"])
if (data / "app.sqlite3").exists():
    with tempfile.TemporaryDirectory(prefix="anki-predeploy-") as temporary:
        archive = build_snapshot(data, Path(temporary), config_file=Path(".env"))
        shutil.copy2(archive, Path(os.environ["BACKUP_DIR"]) / "backup.tar.gz")
PY
.venv/bin/python scripts/install_quick_dictionary.py --data-dir "$data_dir"
.venv/bin/python -c 'from articles_to_anki.webapp import create_app; create_app()'

for unit in anki-papers-web anki-papers-sync-worker; do
  rendered="$app_dir/.$unit.service"
  sed \
    -e "s|__APP_DIR__|$app_dir|g" \
    -e "s|__DATA_DIR__|$data_dir|g" \
    -e "/^\[Service\]/a User=$current_user" \
    "deploy/$unit.service" > "$rendered"
  run_root install -m 0644 "$rendered" "/etc/systemd/system/$unit.service"
done
sed -e "s|__APP_DIR__|$app_dir|g" deploy/anki-papers-backup.service > "$app_dir/.anki-papers-backup.service"
run_root install -m 0644 "$app_dir/.anki-papers-backup.service" /etc/systemd/system/anki-papers-backup.service
run_root install -d -o caddy -g caddy -m 0750 /var/log/caddy
run_root install -m 0644 deploy/Caddyfile /etc/caddy/Caddyfile
run_root systemctl disable --now anki-papers.service 2>/dev/null || true
run_root install -m 0644 deploy/anki-papers-backup.timer /etc/systemd/system/anki-papers-backup.timer
run_root systemctl daemon-reload
run_root systemctl enable anki-papers-web.service anki-papers-sync-worker.service caddy.service
run_root systemctl enable --now anki-papers-backup.timer
run_root systemctl restart anki-papers-web.service anki-papers-sync-worker.service caddy.service
run_root systemctl is-active --quiet anki-papers-web.service
run_root systemctl is-active --quiet anki-papers-sync-worker.service
run_root systemctl is-active --quiet caddy.service
run_root systemctl is-active --quiet anki-papers-backup.timer
unset DEPLOY_PASSWORD_B64

.venv/bin/python - <<'PY'
import time
from urllib.error import URLError
from urllib.request import urlopen

last_error = None
for _ in range(15):
    try:
        with urlopen("http://127.0.0.1:8000/health", timeout=5) as response:
            if response.status == 200:
                break
            last_error = RuntimeError(f"health check failed: {response.status}")
    except URLError as exc:
        last_error = exc
    time.sleep(2)
else:
    raise SystemExit(f"health check failed after retries: {last_error}")
PY
