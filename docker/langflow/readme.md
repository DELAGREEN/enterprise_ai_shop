### Создать тестового пользователя в LDAP

cat <<EOF | docker exec -i ldap ldapadd -x -D "cn=admin,dc=example,dc=com" -w adminpass
dn: ou=users,dc=example,dc=com
objectClass: organizationalUnit
ou: users

dn: uid=testuser,ou=users,dc=example,dc=com
objectClass: inetOrgPerson
objectClass: posixAccount
objectClass: shadowAccount
uid: testuser
cn: Test User
sn: User
givenName: Test
mail: testuser@example.com
userPassword: {SHA}W6ph5Mm5Pz8GgiULbPgzG37mj9g=
uidNumber: 1000
gidNumber: 1000
homeDirectory: /home/testuser
loginShell: /bin/bash
EOF

---

## Офлайн-режим: кэш моделей

Модели Docling кэшируются в `langflow_data/.cache/huggingface/hub`.

Отдельно от них работает **fastembed** (sparse-энкодер BM25 в компоненте
`Qdrant Hybrid Ingest`): у него свой кэш, на huggingface-кэш он не смотрит.
Модель `Qdrant/bm25` кладётся в `docker/langflow/fastembed_cache` и
подключается в контейнер через `FASTEMBED_CACHE_PATH=/app/fastembed_cache`.
Если модель уже есть в этом каталоге, fastembed берёт её оттуда и в сеть
не обращается.

Каталог кэша **не хранится в git** (он в `.gitignore`): его нужно один раз
положить на машину — скопировать с физического носителя или создать
командой ниже. Без этого каталога Langflow упадёт с
`Could not load model Qdrant/bm25 from any source`.

Если кэш нужно пересоздать (например, после смены версии fastembed),
выполните на машине **с интернетом**:

```bash
cd docker
docker run --rm --user "$(id -u):$(id -g)" \
  --entrypoint /app/.venv/bin/python \
  -v "$PWD/langflow/fastembed_cache:/cache" \
  -e FASTEMBED_CACHE_PATH=/cache \
  langflow-custom:latest \
  -c "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25')"
```

Затем при необходимости: `chmod -R a+rwX langflow/fastembed_cache`.

### Перенос кэша на другую машину

Внутри `snapshots/` лежат относительные симлинки на `blobs/`. Если носитель
или файловая система не поддерживает симлинки (FAT32, часть настроек exFAT,
Windows), копируйте с разыменованием — кэш продолжит работать:

```bash
cp -rL langflow/fastembed_cache /media/usb/fastembed_cache
```

Либо переносите архивом, который сохраняет симлинки:

```bash
tar czf fastembed_cache.tar.gz -C langflow fastembed_cache
```

Проверка, что модель подхватывается из кэша:

```bash
docker exec langflow /app/.venv/bin/python -c \
  "from fastembed import SparseTextEmbedding; SparseTextEmbedding(model_name='Qdrant/bm25'); print('OK')"
```
