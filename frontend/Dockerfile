# syntax=docker/dockerfile:1

# ---- build stage ----
FROM node:20-bookworm-slim AS build
WORKDIR /app

# Install deps first for layer caching.
COPY package*.json ./
RUN npm ci

# Build the SSR app. PUBLIC_* values come from frontend/.env (copied in below);
# they are public by design (inlined into the browser bundle).
COPY . .
RUN npm run build

# ---- runtime stage ----
FROM node:20-bookworm-slim AS runtime
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=4321

# Standalone Node server output + its runtime dependencies.
COPY --from=build --chown=appuser:appuser /app/dist ./dist
COPY --from=build --chown=appuser:appuser /app/node_modules ./node_modules
COPY --from=build --chown=appuser:appuser /app/package.json ./package.json

USER appuser
EXPOSE 4321
CMD ["node", "./dist/server/entry.mjs"]
