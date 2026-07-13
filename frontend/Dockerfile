# frontend イメージ (ADR-0029)。 Next.js standalone 出力で薄く保つ。
# 資格情報はランタイム env のみ(ビルド時にバンドルへ埋め込まない、 ADR-0013)。
FROM node:24-bookworm-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install

FROM node:24-bookworm-slim AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .
# BFF のフォールバック用。 実値は runner の runtime env で上書きする。
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
