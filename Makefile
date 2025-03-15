.PHONY: build up down logs clean install deploy update update-audio-extractor update-transcription update-caption update-stream-mirroring update-nginx restart-service force-update force-build

# Docker Compose commands
build:
	docker compose build

# Force build with no cache
force-build:
	docker compose build --no-cache

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

clean:
	docker compose down -v

# Service update commands
update: down build up
	@echo "All services rebuilt and restarted"
	@echo "Run 'make logs' to view logs"

# Force update with no cache to ensure all changes take effect
force-update: down force-build up
	@echo "All services forcefully rebuilt with no cache and restarted"
	@echo "Run 'make logs' to view logs"

update-audio-extractor:
	docker compose build audio-extractor
	$(MAKE) restart-service SERVICE=audio-extractor

update-transcription:
	docker compose build transcription-service
	$(MAKE) restart-service SERVICE=transcription-service

update-caption:
	docker compose build caption-generator
	$(MAKE) restart-service SERVICE=caption-generator

update-stream-mirroring:
	docker compose build stream-mirroring
	$(MAKE) restart-service SERVICE=stream-mirroring

update-nginx:
	docker compose build nginx
	$(MAKE) restart-service SERVICE=nginx

# Force update a single service with no cache
force-update-service:
	@if [ -z "$(SERVICE)" ]; then \
		echo "Error: SERVICE not specified"; \
		echo "Usage: make force-update-service SERVICE=service-name"; \
		exit 1; \
	fi
	docker compose build --no-cache $(SERVICE)
	$(MAKE) restart-service SERVICE=$(SERVICE)

restart-service:
	@if [ -z "$(SERVICE)" ]; then \
		echo "Error: SERVICE not specified"; \
		echo "Usage: make restart-service SERVICE=service-name"; \
		exit 1; \
	fi
	docker compose stop $(SERVICE)
	docker compose rm -f $(SERVICE)
	docker compose up -d $(SERVICE)
	@echo "Service $(SERVICE) restarted successfully"
	docker compose logs -f $(SERVICE)

# Kubernetes/Helm commands
install-deps:
	poetry install

build-images:
	docker build -t rainscribe/shared-volume-init:latest -f shared-volume-init/Dockerfile .
	docker build -t rainscribe/audio-extractor:latest -f audio-extractor/Dockerfile .
	docker build -t rainscribe/transcription-service:latest -f transcription-service/Dockerfile .
	docker build -t rainscribe/caption-generator:latest -f caption-generator/Dockerfile .
	docker build -t rainscribe/stream-mirroring:latest -f stream-mirroring/Dockerfile .
	docker build -t rainscribe/nginx:latest -f nginx/Dockerfile .

push-images:
	docker push rainscribe/shared-volume-init:latest
	docker push rainscribe/audio-extractor:latest
	docker push rainscribe/transcription-service:latest
	docker push rainscribe/caption-generator:latest
	docker push rainscribe/stream-mirroring:latest
	docker push rainscribe/nginx:latest

helm-lint:
	helm lint helm-chart/

helm-install:
	helm upgrade --install rainscribe ./helm-chart --namespace rainscribe --create-namespace

helm-uninstall:
	helm uninstall rainscribe --namespace rainscribe

help:
	@echo "Available commands:"
	@echo "  build                - Build all Docker images"
	@echo "  force-build          - Build all Docker images with no cache"
	@echo "  up                   - Start all services with Docker Compose"
	@echo "  down                 - Stop all services"
	@echo "  logs                 - View logs from all services"
	@echo "  clean                - Stop all services and remove volumes"
	@echo "  update               - Full rebuild and restart of all services"
	@echo "  force-update         - Full rebuild with no cache and restart of all services"
	@echo "  update-audio-extractor - Rebuild and restart just the audio-extractor"
	@echo "  update-transcription - Rebuild and restart just the transcription-service"
	@echo "  update-caption       - Rebuild and restart just the caption-generator"
	@echo "  update-stream-mirroring - Rebuild and restart just the stream-mirroring"
	@echo "  update-nginx         - Rebuild and restart just the nginx service"
	@echo "  force-update-service - Force rebuild a service with no cache (usage: make force-update-service SERVICE=name)"
	@echo "  restart-service      - Restart a specific service (usage: make restart-service SERVICE=name)"
	@echo "  install-deps         - Install Poetry dependencies"
	@echo "  build-images         - Build Docker images for Kubernetes"
	@echo "  push-images          - Push Docker images to a registry"
	@echo "  helm-lint            - Lint the Helm chart"
	@echo "  helm-install         - Install or upgrade the Helm chart"
	@echo "  helm-uninstall       - Uninstall the Helm chart" 