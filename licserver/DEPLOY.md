# Servidor de licencias de ShinyBot — despliegue

Microservicio **FastAPI + SQLite** que valida las licencias (planes 7/15/30 días,
máximo de dispositivos, alta/renovación por webhook de pago).

> Guía **autocontenida** (con todo el código) para montarlo desde cero:
> `licserver/SETUP.md`.

## 1. Requisitos
- Un VPS pequeño (1 vCPU / 512 MB basta) con un dominio apuntando a su IP.
- Python 3.10+.
- `pip install -r requirements.txt` (FastAPI + Uvicorn).

## 2. Configuración
```bash
cp config.example.env .env
# edita .env: LICENSE_SECRET, ADMIN_TOKEN, WEBHOOK_SECRET, DB_PATH, PLANS, VARIANT_PLANS
```
- **`LICENSE_SECRET`**: el mismo que pondrás en `pxg_bot/license.py`
  (`_BUILTIN_SECRET`). Genera uno largo: `python -c "import secrets;print(secrets.token_hex(32))"`.
- **`VARIANT_PLANS`**: mapea el `variant_id` de cada producto de la pasarela a
  `7d` / `15d` / `30d`.

## 3. Arranque manual
```bash
set -a; . ./.env; set +a
uvicorn licserver.main:app --host 127.0.0.1 --port 8080
```

## 4. systemd (servicio)
`/etc/systemd/system/shinybot-licenses.service`:
```ini
[Unit]
Description=ShinyBot licenses
After=network.target

[Service]
WorkingDirectory=/opt/shinybot-licenses
EnvironmentFile=/opt/shinybot-licenses/.env
ExecStart=/usr/bin/uvicorn licserver.main:app --host 127.0.0.1 --port 8080
Restart=always
User=www-data

[Install]
WantedBy=multi-user.target
```
```bash
systemctl enable --now shinybot-licenses
```

## 5. HTTPS con nginx (+ certbot)
```bash
sudo apt install nginx certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/shinybot` (web de venta Next en `:3002` + licencias en `:8080`):
```nginx
# web de venta (Next.js en :3002)
server {
    listen 80;
    server_name tudominio.com www.tudominio.com;
    location / {
        proxy_pass http://127.0.0.1:3002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}

# API de licencias (FastAPI en :8080)
server {
    listen 80;
    server_name lic.tudominio.com;
    client_max_body_size 64k;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```
```bash
sudo ln -s /etc/nginx/sites-available/shinybot /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
# TLS + redirect 80->443 + renovacion automatica (certbot.timer)
sudo certbot --nginx -d tudominio.com -d www.tudominio.com -d lic.tudominio.com
sudo certbot renew --dry-run
```
El bot apunta a `https://lic.tudominio.com`. El servidor de licencias lee
`X-Forwarded-For`, así que registra bien la IP del cliente. La **web Next** corre
en `127.0.0.1:3002` (p. ej. `next start -p 3002`, bajo systemd).

## 6. Webhook de la pasarela de pago (LemonSqueezy)
1. En LemonSqueezy → **Settings → Webhooks** crea un webhook a
   `https://lic.tudominio.com/api/webhook/lemonsqueezy`, eventos
   `order_created` y `subscription_payment_success`.
2. Copia el **signing secret** a `WEBHOOK_SECRET`.
3. Mapea cada `variant_id` en `VARIANT_PLANS`. Al pagar, el server **crea la
   clave** y la **envía por email** (si configuras SMTP) o la deja en el log/BD.

## 7. Administración (CLI)
```bash
export DB_PATH=/opt/shinybot-licenses/licenses.db
python -m licserver.admin create --plan 30d --max-devices 3 --email cliente@x.com
python -m licserver.admin list
python -m licserver.admin extend DK-XXXX-XXXX-XXXX --days 30
python -m licserver.admin revoke DK-XXXX-XXXX-XXXX
python -m licserver.admin devices DK-XXXX-XXXX-XXXX
python -m licserver.admin delete DK-XXXX-XXXX-XXXX
```

## 8. Backups
El estado está en un único SQLite (`DB_PATH`). Copia periódica:
```bash
sqlite3 licenses.db ".backup '/backup/licenses-$(date +%F).db'"
```

## 9. Notas
- El `LICENSE_SECRET` y la URL van **dentro del binario** ofuscado (PyArmor); el
  `config.json` no los necesita.
- **Sin gracia offline**: si el server no es accesible, la licencia falla y el
  bot **no arranca** (validación online obligatoria al arrancar y cada
  `recheck_hours`). La cache firmada solo guarda el último estado, no autoriza.
- La firma HMAC es un **deterrente**, no un candado: el bloqueo real es la
  validación online recurrente.
