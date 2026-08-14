FROM node:22.17-alpine AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .
ARG VITE_API_MODE=gateway
ARG VITE_UI_MODE=harness
ENV VITE_API_MODE=${VITE_API_MODE} \
    VITE_UI_MODE=${VITE_UI_MODE} \
    VITE_BUILD_SOURCEMAP=false
RUN npm run build

FROM nginx:1.28-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=6 \
  CMD wget -q -O /dev/null http://127.0.0.1:8080/ || exit 1
