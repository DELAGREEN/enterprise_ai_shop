# ============================================================
#  Nginx с модулем SPNEGO (Kerberos/SSO) на базе Astra Linux UBI 1.7
# ============================================================
FROM registry.astralinux.ru/library/astra/ubi17-nginx1283:latest

# ---------- Аргументы ----------
ARG NGINX_VERSION=1.28.3
ARG SPNEGO_MODULE_REPO=https://github.com/stnoonan/spnego-http-auth-nginx-module.git

# ---------- Установка зависимостей для сборки ----------
# Astra Linux UBI 1.7 основан на Debian, поэтому используем apt.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        libkrb5-dev \
        libpcre3-dev \
        zlib1g-dev \
        libssl-dev \
        wget \
        git \
        ca-certificates \
        krb5-user \
        libgssapi-krb5-2 \
        libnginx-mod-http-subs-filter \
        && rm -rf /var/lib/apt/lists/*

# ---------- Сборка модуля SPNEGO ----------
# 1. Скачиваем исходники Nginx той же версии, что установлена в образе
# 2. Клонируем репозиторий модуля
# 3. Собираем модуль как динамический (--add-dynamic-module)
# 4. Копируем .so в директорию модулей Nginx и удаляем временные файлы
RUN mkdir -p /tmp/nginx-build && \
    cd /tmp/nginx-build && \
    wget -q http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz && \
    tar -xzf nginx-${NGINX_VERSION}.tar.gz && \
    git clone --depth 1 ${SPNEGO_MODULE_REPO} spnego-module && \
    cd nginx-${NGINX_VERSION} && \
    ./configure \
        --with-compat \
        --add-dynamic-module=../spnego-module && \
    make modules && \
    mkdir -p /usr/lib/nginx/modules && \
    cp objs/ngx_http_auth_spnego_module.so /usr/lib/nginx/modules/ && \
    cd / && rm -rf /tmp/nginx-build

# ---------- Подключение модуля ----------
# Директива load_module должна быть в конфигурации Nginx.
# Добавляем её в основной конфиг, если её там ещё нет.
RUN echo 'load_module modules/ngx_http_auth_spnego_module.so;' > /etc/nginx/modules-enabled/50-mod-http-auth-spnego.conf

# ---------- Очистка и финальные настройки ----------
# Удаляем дефолтный конфиг, чтобы наш монтировался поверх
RUN rm -f /etc/nginx/conf.d/default.conf && \
    rm -rf /etc/nginx/sites-enabled && \
    mkdir -p /etc/nginx/sites-enabled

# Проверка синтаксиса
RUN nginx -t

EXPOSE 80 443

# Точка входа и команда по умолчанию (переопределяются в docker-compose)
CMD ["nginx", "-g", "daemon off;"]