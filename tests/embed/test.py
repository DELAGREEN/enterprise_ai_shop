"""Генерация и мгновенная проверка embed-URL."""
import hashlib
import hmac
import secrets
import sys
import time

# ─── ПОДСТАВЬТЕ СВОИ ЗНАЧЕНИЯ ──────────────────────────────────
INTEGRATION_ID = "2573dfd16b7e441f9ac7b78e80acd89c"
SECRET         = "4a06ba6ed218c7475c6a3f4796d57dae2b5492489e67d6a10611e4e072168625"
BASE_URL       = "http://localhost:5001"
FLOW_ID        = "db5ed516-b4a2-4ab4-ba6e-342816206b42"   # ← ваш flow_id
TTL            = 3600
# ──────────────────────────────────────────────────────────────


#Integration ID: 2573dfd16b7e441f9ac7b78e80acd89c

#Secret: 4a06ba6ed218c7475c6a3f4796d57dae2b5492489e67d6a10611e4e072168625

username   = sys.argv[1] if len(sys.argv) > 1 else "testuser"
expires_at = int(time.time()) + TTL
nonce      = secrets.token_hex(16)

# 1. Формируем сообщение так же, как это делает сервер
message = f"{username}|{expires_at}|{nonce}"

# 2. Считаем подпись
signature = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

# 3. Собираем URL
url = (
    f"{BASE_URL}/embed/chat/{FLOW_ID}"
    f"?i={INTEGRATION_ID}&u={username}&e={expires_at}&n={nonce}&s={signature}"
)

# 4. Проверяем локально (симуляция того, что сделает сервер)
expected = hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()

print("=== EMBED URL ===")
print(url)
print()
print("=== ЛОКАЛЬНАЯ ПРОВЕРКА ===")
print(f"integration_id: {INTEGRATION_ID}")
print(f"secret len:     {len(SECRET)}")
print(f"user:           {username!r}")
print(f"expires_at:     {expires_at}")
print(f"nonce:          {nonce!r}")
print(f"message:        {message!r}")
print(f"signature:      {signature}")
print(f"expected:       {expected}")
print(f"MATCH:          {signature == expected}")
print()

# 5. Резюме — что делать дальше
print("Открой этот URL в браузере:")
print(f"  {url}")
print()
print("Если увидишь 403 «Неверная подпись» — пришли эту строку:")
print(f"  message={message!r} sig={signature}")