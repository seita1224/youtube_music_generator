# frontend イメージ (ADR-0029)。 Next.js standalone 出力で薄く保つ。
# 画面実装は Phase 2 (T064-T068) で追加される。
FROM node:24-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install

FROM node:24-bookworm-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# Basic 認証クレデンシャル(ADR-0013, LAN 単一ユーザー前提)。 NEXT_PUBLIC_* は
# ビルド時にバンドルへインライン化されるため build ARG で渡す。 compose が .env の
# ADMIN_USERNAME/ADMIN_PASSWORD から注入する。
ARG NEXT_PUBLIC_BASIC_AUTH_USER
ARG NEXT_PUBLIC_BASIC_AUTH_PASSWORD
ENV NEXT_PUBLIC_BASIC_AUTH_USER=${NEXT_PUBLIC_BASIC_AUTH_USER}
ENV NEXT_PUBLIC_BASIC_AUTH_PASSWORD=${NEXT_PUBLIC_BASIC_AUTH_PASSWORD}
# next.config の rewrites() はビルド時に評価され destination が manifest へ焼き込まれる。
# そのため backend のプロキシ先 URL も build ARG で渡す(runtime env だけでは fallback の
# 127.0.0.1:8000 が焼かれコンテナ内 localhost へ proxy して失敗する)。
ARG BACKEND_BASE_URL=http://backend:8000
ENV BACKEND_BASE_URL=${BACKEND_BASE_URL}
RUN npm run build

FROM node:24-bookworm-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
EXPOSE 3000
CMD ["node", "server.js"]
