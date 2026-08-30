# Frontend image: two stages — build the Vite bundle with node, then serve the
# static output with nginx. The final image contains no node toolchain.
FROM node:22-alpine AS build

WORKDIR /app

# npm ci needs a committed package-lock.json; the glob keeps the COPY valid
# either way so the failure (if any) is npm's clear lockfile error.
COPY package*.json ./
RUN npm ci

COPY . .

# VITE_API_BASE is baked into the bundle at build time. The default points at
# the backend's published port on the docker host; override the build arg when
# the API lives elsewhere.
ARG VITE_API_BASE=http://localhost:8000
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
